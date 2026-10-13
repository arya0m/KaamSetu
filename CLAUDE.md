# CLAUDE.md

Guidance for Claude Code sessions on KaamSetu. **PRD.md is the source of truth**;
work one build-plan phase per session and follow the session rules in PRD.md
("Instructions for Claude Code").

## Status

| Phase | State |
| --- | --- |
| 1. Foundation | Done locally (2026-10-13): `docker compose up` brings all 4 services up healthy and `/readyz` is green. All 37 tests pass on Python 3.12 in the image, including the `db` tests (99% coverage), and the Celery ping round-trips. **CI has not run yet** (no GitHub remote) |
| 2–7 | Not started |

## What Phase 1 built

- **Monorepo** laid out per PRD. This folder is the repo root; there is no nested `kaamsetu/`.
- **`docker-compose.yml`:** `postgres` (`pgvector/pgvector:pg16`), `redis` (7), `api` and `worker`. The last two share one image built from `apps/api/Dockerfile` (Python 3.12). Before uvicorn starts, `api` runs `alembic upgrade head`. Host ports bind to 127.0.0.1 only.
- **`app/config.py`:** pydantic-settings, environment only. `DATABASE_URL` and `REDIS_URL` have no defaults. Holds the match weights, threshold and max distance (validated to sum to 1) and `EMBEDDING_DIM`.
- **`app/main.py`:** `create_app()` factory. OpenAPI at `/api/v1/openapi.json`, docs at `/api/v1/docs`. CORS allows only `DASHBOARD_ORIGIN`.
- **`app/errors.py`:** every error uses `{"error": {"code", "message"}}`. Raise `AppError(code, message, status)` from app code. Unhandled exceptions return a generic 500.
- **`app/readiness.py` and `routers/health.py`:**
  - `/healthz` is pure liveness.
  - `/readyz` runs registered checks concurrently under one shared deadline (`READINESS_TIMEOUT_S`). It returns 200, or 503 with `checks` listed. Failure details go to logs only.
  - Phase 1 registers `database`, `pgvector` and `redis`. **Later phases must register their providers** (STT, LLM, embeddings), e.g. `app.state.readiness.register("llm", check_fn)`.
- **`app/models/`:** all 10 PRD tables. Every table gets UUID `id`, `created_at` and `updated_at` from `TimestampedBase`. Native PG enums for trade, job status, application status and message direction. The `vector(EMBEDDING_DIM)` columns are on `worker_profiles` and `jobs`.
- **`migrations/versions/0001_initial.py`:** handwritten, mirroring the models. It creates the `vector` extension and reads `EMBEDDING_DIM` from settings. `test_p1_migration_matches_models` fails on any drift, so write a new migration whenever you change a model.
- **`app/workers/`:** Celery app on Redis (JSON only, acks late, prefetch 1) and a `kaamsetu.ping` task.
- **`.github/workflows/ci.yml`:**
  - `lint`: ruff check, ruff format check, `mypy app tests`.
  - `test`: pytest against Postgres+pgvector and Redis service containers, gated at 80% coverage or more.
  - `docker`: image build.
- **`.pre-commit-config.yaml`:** ruff, ruff-format, mypy, private-key detection.
- **Empty packages:** `schemas/`, `services/`, `ai/`, `i18n/`, `apps/dashboard/` and `seed/` are placeholders for later phases.

### Data-model decisions made in Phase 1 (confirm or change)

- `worker_profiles.worker_id` is UNIQUE, which is how "one active profile per worker" is enforced.
- `applications` is UNIQUE on (worker_id, job_id), like `matches`.
- `messages.worker_id` and `messages.wa_message_id` are nullable. The first allows pre-consent and unknown senders; the second is NULL on outbound messages until WhatsApp returns an ID. Postgres UNIQUE still allows multiple NULLs.
- `audit_log` has no foreign keys, so entries survive a worker's deletion (FR-11).
- Worker child rows use `ON DELETE CASCADE`, so a hard delete of `workers` removes profile, voice notes, matches, applications and messages.
- `workers.state` is a free `String(32)` with no default. Phase 2 defines the states.
- `jobs` CHECK constraints: `pay_min <= pay_max`, `openings >= 1`.

### Gaps for later phases (PRD is silent, so ask before adding)

- **FR-14:** the job form has **shifts**, but `jobs` has no shifts column.
- **Match score (Phase 5):** experience fit needs the job's **minimum years**, but `jobs` has no such column.
- **FR-17:** "median time to first interest" may need an interest timestamp. `applications.created_at` might be enough.

## How to run

### Full stack (needs Docker)

```bash
cp .env.example .env    # set POSTGRES_PASSWORD; DATABASE_URL must use the same credentials
docker compose up --build
curl localhost:8000/healthz
curl localhost:8000/readyz   # 200 with database/pgvector/redis = ok
```

### Backend checks without Docker

```bash
cd apps/api
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy app tests
.venv/bin/pytest --cov --cov-fail-under=80
```

Tests marked `db` (migration up and down, model and migration drift, constraints, cascades, `/readyz` against real services) need a real Postgres with pgvector and Redis. Without them those tests **skip**. To run them, point at real services:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/kaamsetu_test \
TEST_REDIS_URL=redis://localhost:6379/0 \
REQUIRE_DB_TESTS=1 .venv/bin/pytest
```

`REQUIRE_DB_TESTS=1` (set in CI) turns those skips into failures. **The `db` tests drop and recreate the schema**, so never point them at a real database.

### Migrations

```bash
cd apps/api
alembic upgrade head            # uses DATABASE_URL
alembic upgrade head --sql      # print SQL without a database
```

`EMBEDDING_DIM` is baked in by `0001_initial`. Changing it after the first migration needs a new migration.

### Running the full suite inside the image (as done for Phase 1 sign-off)

```bash
set -a; . ./.env; set +a
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE kaamsetu_test"  # once
docker run --rm --network kaamsetu_default --user root -v "$PWD/apps/api:/src:ro" \
  -e TEST_DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/kaamsetu_test" \
  -e TEST_REDIS_URL=redis://redis:6379/1 -e REQUIRE_DB_TESTS=1 \
  kaamsetu-api:local sh -c 'cp -r /src /tmp/api && cd /tmp/api && pip install -q ".[dev]" && pytest --cov --cov-fail-under=80'
```

## Conventions

- Name tests and commits after their requirement ID (`FR-x`). Foundation work with no FR uses `P1`.
- Put every external service behind an interface with a mock for tests. Mark real-service smoke tests separately.
- Keep worker-facing text in `app/i18n` and never inline it.
- Secrets live only in the environment. Add every new setting to `.env.example`.
- Starlette warns that `httpx` in TestClient is deprecated (it prefers `httpx2`). The PRD mandates httpx, so the warning is filtered in `pyproject.toml`.

# KaamSetu

WhatsApp job agent: a worker sends a voice note in their own language and gets a
skill profile, a resume and nearby job matches back. Built for the IBM SkillsBuild
final project. The full spec is in [PRD.md](PRD.md).

**Status:** Phase 1 (Foundation) of 7. See [CLAUDE.md](CLAUDE.md) for what exists today.

## Quick start

```bash
cp .env.example .env          # then set POSTGRES_PASSWORD and DATABASE_URL to match
docker compose up --build
curl localhost:8000/readyz    # {"status":"ok","checks":{"database":"ok","pgvector":"ok","redis":"ok"}}
```

API docs: <http://localhost:8000/api/v1/docs>

## Layout

```
apps/api/        FastAPI app, Celery worker, Alembic migrations, tests
apps/dashboard/  Next.js employer/admin UI (Phase 6)
seed/            sample jobs, courses, test voice notes (Phase 5)
```

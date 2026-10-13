# KaamSetu PRD

Oct 13, 2026 · @Uday

KaamSetu is a WhatsApp job agent: a worker sends a voice note in their own language and gets a skill profile, a resume and nearby job matches back. This PRD is the build spec for Claude Code to ship a production-grade MVP for the IBM SkillsBuild final project.

## Goals and success criteria

The MVP wins if a judge can send a Hindi voice note on WhatsApp and get a resume PDF plus three relevant job matches back in under 60 seconds, live, with no crash.

**Product goals**

- Let a worker with no English and no app create a job profile using only WhatsApp voice notes.
- Match workers to nearby jobs by skill meaning, not keyword overlap.
- Give employers a dashboard to post jobs and contact matched workers.
- Close skill gaps by linking to free IBM SkillsBuild courses.

**Demo success criteria (must all pass before submission)**

| Criterion | Target |
| --- | --- |
| Voice note to profile summary reply | Under 20 seconds |
| Profile to resume PDF delivered | Under 30 seconds |
| Top-3 job matches judged relevant by the team | 3 of 3 in 9 of 10 test cases |
| Languages supported in demo | Hindi, English, plus one regional language |
| Uptime during demo week | No unplanned downtime |
| Automated test coverage on backend | 80% or higher |

## Users and user stories

There are three users: the worker (primary), the employer (paying customer) and the admin (the team running the platform).

| ID | As a… | I want to… | So that… |
| --- | --- | --- | --- |
| US-1 | Worker | Describe my work in a voice note in my language | I don't need to type or know English |
| US-2 | Worker | Hear or read my profile back and correct it | My profile is accurate |
| US-3 | Worker | Get a resume PDF on WhatsApp | I can share it with any employer |
| US-4 | Worker | Get nearby job matches with pay and contact | I can apply without a middleman |
| US-5 | Worker | Reply "interested" to a job | The employer is notified |
| US-6 | Worker | Get a free course link when I'm missing a skill | I qualify for better jobs |
| US-7 | Worker | Delete all my data with one message | I stay in control of my information |
| US-8 | Employer | Post a job in a web form | Workers get matched to it |
| US-9 | Employer | See ranked matched workers with profiles | I hire faster |
| US-10 | Employer | Mark a worker as hired | Placements are tracked |
| US-11 | Admin | See metrics and moderate jobs | The platform stays safe and measurable |

## Scope

The MVP ships one complete loop: voice note, profile, resume, match, interest, hire. Everything else waits.

**In scope (MVP)**

- WhatsApp bot for workers: voice and text input, Hindi, English and one regional language
- Speech-to-text, AI skill extraction, profile confirmation loop
- Resume PDF generation in English plus the worker's language
- Semantic job matching by skill and distance
- Employer web dashboard: sign-up, post job, view matches, mark hired
- Admin page: metrics and job moderation
- Consent capture, data deletion command, audit log
- Seed data: 200 sample jobs across 5 trades and 3 cities

**Out of scope (later)**

- Payments and employer billing (show pricing in the deck only)
- Identity or document verification (Aadhaar, certificates)
- Native mobile apps
- Video interviews and in-app chat between employer and worker
- Automated scraping of external job boards

## System architecture

Text version of the architecture diagram:

```
Worker (WhatsApp) --> WhatsApp Cloud API <--webhooks in, replies out--> FastAPI backend
Employer (browser) --> Next.js dashboard --REST /api/v1--> FastAPI backend
FastAPI backend --> Redis queue --> Background worker
FastAPI backend --> PostgreSQL + pgvector
Background worker --> PostgreSQL + pgvector
Background worker --> AI services (each behind a swappable interface):
                      Speech-to-text (Whisper or Watson)
                      LLM extraction (IBM Granite)
                      Embeddings (multilingual model)
                      Resume PDF (WeasyPrint)
```

The webhook returns at once and queues the work; the background worker runs speech-to-text, extraction, embedding and PDF generation, then writes results to PostgreSQL.

## Functional requirements

Each requirement has an ID so Claude Code can reference it in commits and tests.

**Worker WhatsApp bot**

- **FR-1 Onboarding.** On first message, send a short welcome, ask for language (buttons: Hindi, English, regional), and ask for consent to store data. No data is stored until the worker taps "I agree".
- **FR-2 Voice intake.** Accept voice notes up to 2 minutes. Download media from WhatsApp, transcribe, and store the transcript. Also accept typed text.
- **FR-3 Skill extraction.** Run the transcript through the LLM extractor (see AI pipeline) and produce a structured profile.
- **FR-4 Missing fields.** If location, trade or years of experience is missing, ask one follow-up question at a time, in the worker's language. Maximum 3 follow-ups.
- **FR-5 Confirmation.** Send a short profile summary with buttons: "Correct" / "Change". "Change" accepts a new voice note and re-runs extraction on the combined transcript.
- **FR-6 Resume.** On confirmation, generate a one-page resume PDF and send it as a WhatsApp document.
- **FR-7 Matches.** Send the top 3 jobs: title, employer, pay, distance, with buttons "Interested" / "Not for me". "More jobs" returns the next 3.
- **FR-8 Interest.** "Interested" creates an application and notifies the employer on the dashboard (and by email if configured).
- **FR-9 Skill gap.** When a job needs a skill the worker lacks, include one relevant free course link.
- **FR-10 Commands.** Support `PROFILE`, `JOBS`, `RESUME`, `LANGUAGE`, `DELETE` and `HELP`, plus their Hindi equivalents.
- **FR-11 Deletion.** `DELETE` asks to confirm, then hard-deletes the worker's profile, audio, transcripts and applications within 1 minute, and confirms.
- **FR-12 Job alerts.** When a new job matches an existing worker above the threshold, send an alert using an approved WhatsApp template message.

**Employer dashboard**

- **FR-13 Auth.** Email and password sign-up with email verification. Session via secure HTTP-only cookies.
- **FR-14 Post job.** Form: title, trade, required skills, pay range, location (pin code or map), shifts, openings. Job is embedded on save.
- **FR-15 Matches view.** Ranked list of matched workers with match score, skills, distance and applied status. Phone number shown only after the worker taps "Interested".
- **FR-16 Hiring.** Mark an applicant as shortlisted, hired or rejected. The worker gets a WhatsApp update.

**Admin**

- **FR-17 Metrics.** Profiles created, active jobs, matches sent, interest rate, hires, median time to first interest.
- **FR-18 Moderation.** Approve, hide or delete jobs. Flag jobs with pay below a set minimum or banned keywords.

## AI pipeline

The pipeline has four steps: transcribe, extract, embed, match. Every model sits behind an interface so providers can be swapped by config.

1. **Transcribe.** `SpeechToText` interface with two providers: Whisper (local or API) and IBM Watson STT. Convert WhatsApp OGG/Opus audio to 16 kHz WAV with ffmpeg first. Store the detected language.
2. **Extract.** `LLMClient` interface with providers for IBM Granite on watsonx.ai (default) and one fallback. The prompt asks for JSON only, validated with Pydantic. On invalid JSON, retry once with the validation error; then fall back to asking the worker a follow-up.
3. **Embed.** Build a profile text (trade + skills + experience) and embed it with a multilingual embedding model. Jobs are embedded the same way on save. Store vectors in pgvector.
4. **Match.** Filter jobs by distance and active status in SQL, then rank by the score below. Return matches above 0.55 only.

**Extraction output schema**

```json
{
  "name": "string | null",
  "primary_trade": "electrician | plumber | tailor | driver | beautician | carpenter | delivery | other",
  "skills": [{"name": "string", "level": "basic | skilled | expert"}],
  "years_experience": "number | null",
  "location": {"city": "string | null", "area": "string | null", "pincode": "string | null"},
  "languages_spoken": ["string"],
  "availability": "full_time | part_time | gig | null",
  "expected_daily_pay_inr": "number | null",
  "missing_fields": ["string"],
  "confidence": "number 0-1"
}
```

**Match score**

```latex
\text{score} = 0.6 \cdot \text{skill similarity} + 0.25 \cdot \text{distance score} + 0.15 \cdot \text{experience fit}
```

Distance score falls linearly from 1 at 0 km to 0 at 25 km. Experience fit is 1 when the worker meets the job's minimum years, else the ratio. Weights live in config so they can be tuned against the test set.

**Guardrails**

- Never invent skills not in the transcript; the prompt says so and a test checks it.
- Strip phone numbers and ID numbers from transcripts before sending them to the LLM.
- Log prompt, response, latency and token count for every call, without personal data.

## Data model

PostgreSQL 16 with the pgvector extension. All tables have `id` (UUID), `created_at` and `updated_at`. Migrations via Alembic.

| Table | Key columns | Notes |
| --- | --- | --- |
| `workers` | `wa_phone_hash`, `wa_phone_encrypted`, `language`, `consent_at`, `state` | Phone hashed for lookup, encrypted for sending; `state` drives the conversation |
| `worker_profiles` | `worker_id`, `profile_json`, `confirmed`, `embedding vector(768)`, `lat`, `lng` | One active profile per worker |
| `voice_notes` | `worker_id`, `media_url`, `transcript`, `language`, `duration_s` | Audio deleted after 30 days |
| `employers` | `email`, `password_hash`, `company_name`, `verified` | Argon2 password hashes |
| `jobs` | `employer_id`, `title`, `trade`, `skills[]`, `pay_min`, `pay_max`, `lat`, `lng`, `openings`, `status`, `embedding vector(768)` | Status: draft, active, hidden, closed |
| `matches` | `worker_id`, `job_id`, `score`, `sent_at`, `worker_response` | Unique on worker + job |
| `applications` | `worker_id`, `job_id`, `status` | Status: interested, shortlisted, hired, rejected |
| `courses` | `skill`, `title`, `url`, `language` | Curated SkillsBuild links |
| `messages` | `worker_id`, `direction`, `type`, `wa_message_id`, `status` | Idempotency on `wa_message_id` |
| `audit_log` | `actor`, `action`, `entity`, `entity_id` | Consent, deletion and hiring events |

Embedding size must match the chosen model; set it from config in the migration.

## API endpoints

FastAPI, versioned under `/api/v1`, with OpenAPI docs auto-generated. All responses are JSON with a consistent error shape: `{"error": {"code", "message"}}`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/webhooks/whatsapp` | Verify token | Meta webhook verification |
| POST | `/webhooks/whatsapp` | Signature check | Receive messages; return 200 fast, queue work |
| POST | `/api/v1/auth/signup` | None | Employer sign-up |
| POST | `/api/v1/auth/login` | None | Employer login, sets cookie |
| POST | `/api/v1/auth/logout` | Employer | End session |
| GET | `/api/v1/jobs` | Employer | List own jobs |
| POST | `/api/v1/jobs` | Employer | Create job, triggers embedding and matching |
| PATCH | `/api/v1/jobs/{id}` | Employer | Edit or close job |
| GET | `/api/v1/jobs/{id}/matches` | Employer | Ranked matched workers |
| PATCH | `/api/v1/applications/{id}` | Employer | Shortlist, hire or reject |
| GET | `/api/v1/admin/metrics` | Admin | Platform metrics |
| PATCH | `/api/v1/admin/jobs/{id}` | Admin | Moderate a job |
| GET | `/healthz` | None | Liveness |
| GET | `/readyz` | None | Checks database, Redis and model providers |

The webhook must verify the `X-Hub-Signature-256` header with the app secret and reject anything that fails.

## Non-functional requirements

These are what make it "production level" in the judges' eyes: fast, safe with personal data, and observable.

**Performance and reliability**

- Webhook returns 200 within 2 seconds; all AI work runs in a background worker queue.
- Retries with exponential backoff on STT, LLM and WhatsApp send failures; max 3 attempts.
- Duplicate WhatsApp deliveries are ignored using the message ID.
- Matching query returns in under 300 ms for 10,000 jobs.

**Security**

- All secrets from environment variables; `.env.example` committed, `.env` never committed.
- Phone numbers encrypted at rest; lookups use a salted hash.
- Rate limit: 20 messages per worker per minute; login attempts limited per IP.
- Input validation on every endpoint with Pydantic; parameterised SQL only.
- CORS limited to the dashboard origin; HTTPS only in production.

**Privacy (India's DPDP Act 2023 principles)**

- Explicit consent before storing data; consent time logged.
- Collect only what matching needs; no ID documents in MVP.
- Deletion on request via `DELETE` command.
- A plain-language privacy notice linked in the welcome message.

**Observability**

- Structured JSON logs with a request ID on every line.
- Error tracking with Sentry.
- Metrics for message volume, pipeline latency per step and error rate.

**Accessibility**

- Worker messages under 300 characters, simple words, buttons over typed commands.
- Dashboard meets WCAG 2.1 AA for contrast and keyboard use.

## Tech stack and repo structure

One monorepo, everything runs locally with `docker compose up`.

| Layer | Choice |
| --- | --- |
| Backend API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic |
| Background jobs | Celery or RQ with Redis |
| Database | PostgreSQL 16 + pgvector |
| Speech-to-text | Whisper (default), IBM Watson STT (option) |
| LLM | IBM Granite on watsonx.ai (default), one fallback provider |
| PDF | WeasyPrint with Noto fonts for Indic scripts |
| Messaging | WhatsApp Business Cloud API |
| Dashboard | Next.js (TypeScript), Tailwind, shadcn/ui |
| Tests | pytest, pytest-asyncio, httpx; Playwright for the dashboard |
| Quality | Ruff, mypy, ESLint, pre-commit |
| CI | GitHub Actions: lint, type check, tests, build images |
| Hosting | IBM Cloud Code Engine, or Render/Railway as fallback |

```
kaamsetu/
  apps/
    api/              # FastAPI app
      app/
        routers/      # webhooks, auth, jobs, admin
        services/     # conversation, matching, resume
        ai/           # stt.py, llm.py, embeddings.py, prompts/
        models/       # SQLAlchemy models
        schemas/      # Pydantic schemas
        workers/      # background tasks
        i18n/         # message strings per language
      migrations/
      tests/
    dashboard/        # Next.js employer and admin UI
  seed/               # sample jobs, courses, test voice notes
  docker-compose.yml
  .env.example
  CLAUDE.md
  README.md
```

## Testing and quality gates

No phase is done until its tests pass in CI. External services are mocked in unit tests and called for real only in a tagged smoke suite.

- **Unit tests:** conversation state machine, extraction parsing and validation, match scoring, resume rendering, deletion.
- **Integration tests:** webhook to reply end to end with mocked WhatsApp, STT and LLM; database via a test Postgres container.
- **AI evaluation set:** 30 recorded or synthetic transcripts (10 Hindi, 10 English, 10 regional) with expected profiles. Report field accuracy; target 85% or higher on trade, skills and location.
- **Matching evaluation:** 10 worker profiles with hand-labelled correct jobs; target 3 of 3 relevant in 9 of 10 cases.
- **Dashboard E2E:** Playwright test for sign-up, post job, view matches, mark hired.
- **Load test:** 50 concurrent webhook messages with Locust; no dropped messages.
- **Quality gates in CI:** Ruff and mypy clean, ESLint clean, backend coverage 80% or higher, Docker images build.

## Build plan for Claude Code

Build in seven phases, in order. Give Claude Code one phase per session and check the "done when" line before moving on.

1. **Foundation.** Monorepo, Docker Compose (api, worker, postgres+pgvector, redis), FastAPI skeleton, health checks, Alembic, all tables, CI pipeline, `.env.example`, `CLAUDE.md`.
   - Done when: `docker compose up` works, `/readyz` is green, CI passes.
2. **WhatsApp loop.** Webhook verification, signature check, message idempotency, conversation state machine, onboarding and consent (FR-1), text echo through the queue, i18n strings.
   - Done when: a real WhatsApp test number completes onboarding in Hindi and English.
3. **Voice to profile.** Media download, ffmpeg conversion, STT interface, LLM extraction with schema validation, follow-up questions, confirmation (FR-2 to FR-5).
   - Done when: AI evaluation set scores 85% or higher.
4. **Resume.** Bilingual one-page PDF template with Indic fonts, sent as a WhatsApp document (FR-6).
   - Done when: PDFs render correctly in all three demo languages.
5. **Matching.** Embeddings, seed 200 jobs, scoring, top-3 with buttons, "More jobs", interest, skill-gap courses, job alerts (FR-7 to FR-12).
   - Done when: matching evaluation passes.
6. **Dashboard.** Employer auth, post job, matches view, hiring actions, admin metrics and moderation (FR-13 to FR-18).
   - Done when: Playwright E2E passes.
7. **Hardening and deploy.** Rate limits, encryption, deletion, Sentry, structured logs, load test, deploy to cloud, demo script and backup demo video.
   - Done when: every demo success criterion in Goals passes on the deployed system.

## Instructions for Claude Code

This file lives as `PRD.md` in the repo root. Start each session with the prompt below.

```
Read PRD.md. We are building KaamSetu. Work on Phase <N> only.
Before writing code, list the files you will create or change and how you will test them.
Rules:
- Follow the repo structure, stack and data model in PRD.md exactly.
- Reference requirement IDs (FR-x) in commit messages and test names.
- Write tests alongside code; run them and fix failures before finishing.
- Put every external service behind an interface with a mock for tests.
- Never hardcode secrets; add new settings to .env.example.
- Keep all worker-facing text in app/i18n, never inline.
- When something in PRD.md is unclear, stop and ask instead of guessing.
Finish by updating CLAUDE.md with what was built and how to run it.
```

After each phase, review the diff yourself, run the app, and try the flow on your phone. Commit before starting the next phase.

## Open questions

- [ ] Which regional language is third: Marathi, Tamil, Bengali or another?
- [ ] Which 3 cities does the seed data cover?
- [ ] Do we have watsonx.ai credits through SkillsBuild, or use the fallback LLM?
- [ ] Meta WhatsApp test number or Twilio sandbox for the demo?
- [ ] Team size and who owns backend, dashboard and the deck?
- [ ] Submission deadline, so the seven phases can be dated.

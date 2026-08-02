# Herald

**You ship the projects. Herald writes and publishes the posts.**

Herald is marketing automation for developers who ship more than they write
about. It watches your project repos, drafts blog posts and social copy when
something meaningful lands, runs them past you, publishes them across seven
destinations on a schedule, and tracks which pieces actually got read.

---

## What it does

| | |
|---|---|
| **Project registry** | Register what you ship — description, stack, audience, tone, repo. Herald reads the repo for what's changed since it last looked. |
| **Content engine** | Five content types (tutorial, announcement, feature spotlight, comparison, how-to) × three tones, generated from the project record plus real commit and release data. |
| **SEO** | Meta description, keywords, tags and a heading-outline audit, applied deterministically rather than spent as a second model call. |
| **Publishing** | Adapters per platform. Seven publish today — including a commit to your own blog repo — and two need an auth flow nobody can complete on a free tier (see [Platform support](#platform-support)). |
| **Calendar** | Month grid with drag-to-reschedule, plus per-platform cadence guidance — learned from your own first-day views once there are enough of them, and the published guidance until then. |
| **Analytics** | Views and engagement per post, per platform, per content type, per project — plus reader-minutes over time and whether the long pieces earn their length. |
| **Velocity** | How fast each piece found its audience, read from the whole snapshot series rather than the latest number, and which posts have stopped growing. |
| **Alerts** | Posts running far under *your own* median for that platform, flagged while a headline swap can still change the outcome. |
| **Autopilot** | Watch repos, write when something ships, publish without review only when the model is confident and you've said it may. |

## Stack

- **Backend** — Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL
- **Queue** — Celery + Redis (worker and beat as separate processes)
- **AI** — OpenRouter free models (`openai/gpt-oss-20b:free`, `openai/gpt-oss-120b:free`),
  with Gemini / Groq / Cerebras as fallbacks
- **Frontend** — React 18, Vite, Tailwind CSS, React Router

---

## Quick start

### With Docker (everything)

```bash
cp .env.example .env          # fill in OPENROUTER_API_KEY at minimum
docker compose up --build
```

The API migrates on boot and serves at <http://localhost:8000>
(<http://localhost:8000/docs> for the OpenAPI browser).

Seed an account and the project records — this, not `/auth/register`, is
how the account is created: registration is closed unless `REGISTRATION_ENABLED`
says otherwise (see Configuration). The projects registered live in
`SEED_PROJECTS` in `backend/app/seed.py`; re-running picks up any added there
since the last run and leaves the existing ones alone.

```bash
docker compose exec api python -m app.seed
```

Then the frontend:

```bash
cd frontend && npm install && npm run dev     # http://localhost:5173
```

### Backend on its own

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e . --no-deps

# Postgres via compose, or point DATABASE_URL at any Postgres you have
docker compose up -d db redis

.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --reload
```

Workers, if you want scheduling and the autopilot to actually run:

```bash
.venv/bin/celery -A app.tasks.celery_app.celery_app worker --loglevel=info
.venv/bin/celery -A app.tasks.celery_app.celery_app beat   --loglevel=info
```

Without them, publishing still works — the API falls back to running it inline
(see `CELERY_ENABLED`).

### Tests

```bash
cd backend  && .venv/bin/python -m pytest && .venv/bin/ruff check app tests
cd frontend && npm test && npm run build
```

78 backend tests and 16 frontend tests, no external services required.

---

## How it hangs together

```
GitHub ──▶ autopilot_tasks ──▶ content_generator ──▶ Content (draft/review)
                │                     │
                │                     └── llm_router: OpenRouter → Gemini → Groq
                │                         → Cerebras → deterministic template
                │
                └── ContentIdea (banked for the calendar)

Content ──▶ publishing_service.queue ──▶ Publication (one per platform)
                                              │
                        publish_tasks ────────┘
                                              │
                                    publishers/{devto,medium,…}
                                              │
                                    metrics_tasks ──▶ ContentMetric
```

Three design decisions worth knowing before you read the code:

**The AI chain never hard-fails.** Four providers are tried in order, each
skipped for five minutes after three consecutive failures. When all of them are
down, generation returns a deterministic template built from the project record,
marked `confidence=0.0` so the autopilot can never publish it unreviewed. A
background job that 500s loses the trigger entirely; a mediocre draft a human
can fix does not.

**A publication is per-platform.** "Published to Dev.to, rejected by LinkedIn"
is the normal case, not an error. One success marks the piece published; only
every platform failing marks it failed.

**Metrics are append-only.** Each poll writes a new row rather than updating
one, so "400 views in two days and nothing since" is answerable. A metric a
platform doesn't report stays `NULL` rather than becoming a zero that drags
averages down.

---

## Platform support

| Platform | Status | Notes |
|---|---|---|
| **Git repo** | ✅ Publishes | Commits Markdown + front matter to the repo your blog is built from. No OAuth, no rate tier, nobody who can revoke it. Normally the right canonical platform. |
| **Dev.to** | ✅ Publishes, reports metrics | One API key. The reference adapter — read this one first. |
| **Mastodon** | ✅ Publishes, reports metrics | Bearer token from any instance. Four clicks to get one. No draft state, so `as_draft` is refused rather than posted live. |
| **Bluesky** | ✅ Publishes, reports metrics | App password, not the account password. Links need byte-offset facets — see the adapter. No draft state. |
| **Hashnode** | ✅ Publishes | GraphQL. Needs the publication id of the blog to post to; connecting the account lists the ones your token can see. |
| **WordPress** | ✅ Publishes | Self-hosted, REST API at `/wp-json`, application password. WordPress.com is a different API. |
| **Medium** | ✅ Publishes | ⚠️ Medium stopped issuing new integration tokens in 2023. Works with a token created before then; a new account cannot get one. No stats API. |
| LinkedIn | 🔨 Scaffolded | Needs a registered app and 3-legged OAuth (`w_member_social`) — an auth flow, not an adapter. |
| Twitter/X | 🔨 Scaffolded | Thread splitting is done and tested. Needs OAuth 1.0a signing and a paid tier for writes. |

Scaffolded adapters have their formatting layer implemented and under test;
`publish()` raises a clear `NotImplementedAdapter` rather than pretending. The
API refuses to queue content for them, so nothing silently disappears.

Links published to any of these carry UTM parameters pointing back at the
platform they went out on, so the project's own analytics can answer "which
destination actually sent traffic" — which matters when only Dev.to has a stats
API. The `rel=canonical` URL is never tagged.

Adding a platform: write the adapter against
`app/services/publishers/base.py:Adapter`, register it in
`app/services/publishers/__init__.py`, add the enum member. Nothing else needs
to know it exists.

---

## Configuration

Every setting is in `.env.example` with a comment explaining what it does and
what happens if you leave it blank. The ones that matter:

| Variable | Why |
|---|---|
| `OPENROUTER_API_KEY` | Without it, all generated content is the static template. Free tier is 50 requests/**day**, and the quota belongs to the key — a key shared with another project can already be spent. |
| `GEMINI_API_KEY` / `GROQ_API_KEY` | Fallbacks, tried in that order when OpenRouter fails or is out of quota. Same dialect, own free tiers: [aistudio.google.com/apikey](https://aistudio.google.com/apikey), [console.groq.com/keys](https://console.groq.com/keys). Configure at least one, or a spent OpenRouter quota means template output until it resets. |
| `GITHUB_TOKEN` | Without it, repo scans run at 60 req/hour on public repos only. |
| `TOKEN_ENCRYPTION_KEY` | Encrypts platform tokens at rest. **Production refuses to store a credential without it.** |
| `JWT_SECRET` | Change it. |
| `REGISTRATION_ENABLED` | Off by default. `POST /auth/register` answers 403 — Herald is single-user and the account comes from `python -m app.seed`. |
| `REGISTRATION_INVITE_TOKEN` | Required alongside the flag in production. Enabled-but-tokenless registration is refused there rather than served open. |
| `RATE_LIMIT_STORAGE_URI` | Blank counts per uvicorn worker. Point it at Redis for one shared budget. |
| `SMTP_HOST` | Blank means the password reset link is written to the log instead of emailed. Fine for a single-user install; it does put the link in the logs. |
| `AUTOPILOT_AUTO_PUBLISH_CONFIDENCE` | The bar for publishing without review. Default 0.8. |

`GET /api/v1/health` reports which of these are set, and the Settings page shows
the same thing in English. It also probes Postgres and Redis and answers **503**
when a required one is down — the reverse proxy uses it as a health check, so
the status code is not decorative (`deploy/DEPLOYMENT.md` has the table).

## Deployment

Built for a single Hetzner box, like the sibling projects: `docker compose up -d`
behind nginx with TLS, `frontend/` built to static files and served by nginx,
`/api` proxied to the API container. The compose file already separates worker
from beat, so scaling workers doesn't duplicate the schedule.

Before going live:

1. `ENVIRONMENT=production` and `DEBUG=false` — this is also what hides `/docs`,
   `/redoc` and `/openapi.json` (404, not 401)
2. Set `TOKEN_ENCRYPTION_KEY` (production won't store credentials without it)
3. A real `JWT_SECRET`
4. `BACKEND_CORS_ORIGINS` set to your actual frontend origin

## Layout

```
backend/
  app/
    config.py            settings (pydantic-settings)
    database.py          engine, session, Base
    security.py deps.py  JWT, password hashing, current-user
    models/              user, project, content, publication, connection, metrics
    ratelimit.py         slowapi limiter for /auth/*
    routers/             auth, projects, content, calendar, analytics, settings
    schemas/             pydantic request/response models
    services/
      llm_router.py      provider chain + circuit breaker
      password_reset.py  single-use reset tokens (hashed at rest)
      mailer.py          SMTP; logs the link when unconfigured
      ai.py              the single entry point every AI feature calls
      content_generator.py
      github_client.py   repo activity since a watermark
      seo.py             deterministic SEO hygiene + audit
      cadence.py         per-platform posting rhythm (the generic table)
      learned_cadence.py the same, derived from your own results
      publishing_service.py
      analytics_service.py
      velocity.py        growth curves over the snapshot series
      alerts.py          posts under your own median for that platform
      publishers/        base, registry, formatting, one module per platform
    tasks/               celery app, publish, autopilot, metrics
    seed.py
  alembic/               migrations
  tests/                 78 tests, no external services
frontend/
  src/
    pages/               Dashboard, Projects, ContentList, ContentEditor,
                         Calendar, Publish, Settings, Login
    components/          Shell + ui bits
    hooks/               useAuth, useApi
    lib/                 api client, formatters, markdown renderer
```

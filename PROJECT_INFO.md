# Herald — PROJECT_INFO

**Marketing automation for developers: watches your project repos, drafts blog and social copy when something ships, publishes it on a schedule, and tracks what got read.**

- **Repo:** https://github.com/r2st/Herald · branch `main`
- **Local path:** `Products/Herald`

## Tech stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (async), Python |
| Frontend | React SPA (built to `frontend/dist`) |
| Database | PostgreSQL |
| Queue | Celery + Redis (worker + beat) |
| LLM | OpenRouter primary → Gemini → Groq → Cerebras → static template |
| Mail | SendGrid SMTP (password-reset mail only) |
| Publishing | Per-platform adapters (Dev.to, Hashnode, Bluesky, blog-repo commit, …) |

## Deploy location

| | |
|---|---|
| Host | Hetzner `89.167.8.178` (Ubuntu 24.04, 4 GB) — shared with GoSumo, Documedic, HomeNex, Knol |
| Code | `/opt/Herald` — plain rsync copy, **no `.git` on the server** |
| Runs as | system user `herald` |
| Public URL | https://herald.aiknol.com |
| Process model | **systemd, not Docker.** The repo's `docker-compose.yml` is local-dev only. |
| Ports | `3006` `herald-api` (uvicorn, 2 workers) · `3007` `herald-web` (static SPA) |
| Bind address | `172.18.0.1` (the `knol_knol` Docker bridge gateway) so only the shared Caddy container can reach it — deliberately not `0.0.0.0` |
| Units | `herald-api`, `herald-web`, `herald-worker`, `herald-beat` |

Full detail: [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md), [`deploy/Caddyfile.herald`](deploy/Caddyfile.herald).

## SSH key

`../GoSumo/keys/hetzner_deploy_ed25519` — Herald has no key of its own; it reuses the
shared Hetzner deploy key kept in the GoSumo project.

```bash
ssh -i ../GoSumo/keys/hetzner_deploy_ed25519 root@89.167.8.178
```

> This one ed25519 key is copied into five projects and opens every Hetzner deploy
> target. See `~/projects/keys/KEYS_INDEX.md` §4.

## Environment variables

| Where | What |
|---|---|
| `.env` (gitignored) | Local dev config — full var list documented in `.env.example` |
| `keys/` (gitignored) | Individual secrets, mode 600: `openrouter_api_key`, `gemini_api_key`, `groq_api_key`, `devto_api_key`, `hashnode_api_key`, `bluesky_app_password`, `bluesky_handle`, `sendgrid.txt` |
| Server | `/opt/Herald/.env` |

`keys/groq_api_key` holds the **canonical Groq key for the whole estate** — the same
value is mirrored in `../TalentPing/keys/groq`, `../USTradingBot/keys/groq_api_key`,
and Landline production.

Known gaps as of 2026-07-31: `JWT_SECRET` and `TOKEN_ENCRYPTION_KEY` are still template
placeholders, `CEREBRAS_API_KEY` is blank, and `GEMINI_API_KEY` authenticates but is
quota-exhausted.

## Key commands

```bash
# Local dev
docker compose up -d db redis
uvicorn app.main:app --reload            # API on :8000
cd frontend && npm run dev
python -m app.seed                       # single-user account creation

# Deploy
./deploy/deploy.sh

# On the server
systemctl status  herald-api herald-web herald-worker herald-beat
systemctl restart herald-api herald-web herald-worker herald-beat
journalctl -u herald-api -f

# Health
curl https://herald.aiknol.com/api/v1/health   # also lists configured LLM providers
```

## Related projects

- [`../TalentPing`](../TalentPing) — same Hetzner box, same LLM fallback pattern, shares the Groq key
- [`../GoSumo`](../GoSumo) — same box; **owns the SSH deploy key Herald uses**
- [`../Documedic`](../Documedic), [`../HomeNex`](../HomeNex) — same box
- `knol/memorylayer` — backs the `*.aiknol.com` estate and the shared Caddy container
- `~/projects/PROJECT-INDEX.md`, `~/projects/keys/KEYS_INDEX.md` — estate-wide index

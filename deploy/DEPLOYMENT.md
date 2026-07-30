# Herald — production deployment

Herald runs on the shared Hetzner box `89.167.8.178` (Ubuntu 24.04, 4 GB RAM)
alongside GoSumo, Documedic, HomeNex, Authmatic and Knol. It is **not** Docker:
`docker-compose.yml` in the repo root is local-dev only. Production is four
systemd units in front of the host's PostgreSQL and Redis, published by the
shared Caddy container.

| | |
|---|---|
| Host | `89.167.8.178` |
| SSH | `ssh -i /Users/dev/projects/Products/GoSumo/keys/hetzner_deploy_ed25519 root@89.167.8.178` |
| Code | `/opt/Herald` — a plain rsync copy, **no `.git`** |
| Runs as | system user `herald` (not root) |
| Public URL | `https://herald.aiknol.com` |
| Deploy | `./deploy/deploy.sh` |

## Ports

Chosen to sit after the apps already on the box (GoSumo 3001/3002,
Documedic 3003/3004, HomeNex 3005, Authmatic 8000):

| Port | Service |
|---|---|
| 3006 | `herald-api` — uvicorn, 2 workers |
| 3007 | `herald-web` — static SPA server |

Both bind **`172.18.0.1`** (the `knol_knol` Docker bridge gateway), not
`0.0.0.0`. That address is the host as seen from inside the Caddy container, so
Caddy can reach Herald while the public internet cannot — the box has no host
firewall, and the sibling apps' `0.0.0.0` binds are in fact reachable on the
open internet. The cost is that the units depend on Docker being up; they have
`After=docker.service` and `Restart=always`, so a Docker restart resolves
itself within `RestartSec`.

## Services

| Unit | What it does |
|---|---|
| `herald-api` | FastAPI on 3006 |
| `herald-web` | `deploy/static-server.mjs` serving `frontend/dist` on 3007 |
| `herald-worker` | Celery worker — generation, publishing, metrics |
| `herald-beat` | Celery beat — publish sweep (5 min), repo scan (1 h), metrics (6 h) |

```bash
systemctl status  herald-api herald-web herald-worker herald-beat
systemctl restart herald-api herald-web herald-worker herald-beat
journalctl -u herald-api -f
```

All four are hardened (`ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges`, `ReadWritePaths=/opt/Herald`). Beat's schedule file is
pinned to `/opt/Herald/backend/celerybeat-schedule` because its default
location — the working directory — is read-only under `ProtectSystem=strict`.

## Data stores

- **PostgreSQL 16**, the host instance on `127.0.0.1:5432`. Database `herald`,
  owner role `herald`. (GoSumo's Postgres on 5433 is a separate container and
  is not used here.)
- **Redis**, installed from apt for Herald and listening on `127.0.0.1:6379`,
  DB 0 (app) / 1 (Celery broker) / 2 (results). GoSumo's containerised Redis on
  6380 is deliberately left alone — sharing it would couple Herald's queue to
  GoSumo's container lifecycle and its `noeviction` budget.

## Frontend: built locally, never on the server

`npm run build` runs on the developer machine and `frontend/dist/` is rsynced.
The box has 4 GB shared between six applications; a Vite build there competes
with live traffic. `frontend/dist` is gitignored, so `deploy.sh` syncs it in a
second explicit pass.

The SPA calls the API at the **relative** path `/api/v1`
(`frontend/src/lib/api.js`), which is why there is a single origin with Caddy
splitting `/api/*` to the API rather than a separate `api.herald.aiknol.com`.
A split-origin setup would need CORS plus an absolute URL baked into the build.

## Caddy

Herald's vhost lives in the shared config at `/opt/knol/Caddyfile`
(container `knol-caddy`); the canonical copy of the block is
`deploy/Caddyfile.herald`.

```bash
docker exec knol-caddy caddy validate --config /etc/caddy/Caddyfile
docker exec knol-caddy caddy reload   --config /etc/caddy/Caddyfile
```

> **`/opt/knol/Caddyfile` is a *file* bind-mount, so never edit it with
> `sed -i`, `vim`, or anything else that writes-then-renames.** Those replace
> the inode; the container stays pinned to the old one and silently keeps
> serving the previous config — `caddy reload` will cheerfully report success
> while changing nothing. Append with `>>` or truncate in place with `cat new >
> file`, both of which preserve the inode. If the inode has already been
> replaced, `docker compose -f /opt/knol/docker-compose.prod.yml up -d caddy`
> re-links it (brief blip for every site on the box).
>
> As of the initial Herald deploy the host file and the running config are
> **byte-for-byte identical in content but on different inodes** — a Caddy
> container recreate is needed before any further Caddyfile edit will take
> effect.

To verify a vhost's routing without touching the live proxy, run a throwaway
Caddy on the same network with just that block bound to a spare port:

```bash
{ echo ":8099 {"; sed -n '/^herald.aiknol.com {/,/^}/p' /opt/knol/Caddyfile | tail -n +2; } > /tmp/ht/Caddyfile
docker run --rm -d --name herald-caddy-test --network knol_knol \
  -p 127.0.0.1:8099:8099 -v /tmp/ht/Caddyfile:/etc/caddy/Caddyfile:ro caddy:2-alpine
curl -s http://127.0.0.1:8099/api/v1/health
docker rm -f herald-caddy-test
```

## DNS

`herald.aiknol.com` needs an **A record → 89.167.8.178, proxy disabled (grey
cloud)** in the Cloudflare zone `aiknol.com`, matching how
`documedic.aiknol.com` is set up. Caddy solves the ACME HTTP-01 challenge
itself; an orange-cloud record would break issuance. Until the record exists,
Caddy retries every 60 s and logs `NXDOMAIN looking up A for
herald.aiknol.com` — it picks up the certificate on its own once DNS resolves,
with no restart needed.

## Environment

`/opt/Herald/.env`, mode 600, owned by `herald`; systemd reads it via
`EnvironmentFile`. Generated at deploy time and **never** in git. `JWT_SECRET`
and `TOKEN_ENCRYPTION_KEY` were generated on the box —
rotating `TOKEN_ENCRYPTION_KEY` invalidates every stored platform credential.

LLM calls go to **OpenRouter free models** (`openai/gpt-oss-20b:free`,
`openai/gpt-oss-120b:free`), using the same key Documedic uses. That key is on
OpenRouter's free tier: **50 free-model requests per day, shared across every
app using it**, after which calls return HTTP 429 and Herald falls through its
provider chain to a static template. Fixes, in order of preference: give Herald
its own key, add $10 of credit to unlock 1000/day, or configure one of the
already-supported fallbacks (`GROQ_API_KEY`, `GEMINI_API_KEY`,
`CEREBRAS_API_KEY`) — all have free tiers and all speak the same dialect.

## Health check

`GET /api/v1/health` probes its dependencies rather than returning a bare 200:
`SELECT 1` against Postgres and a `PING` against Redis, each with a 2 s budget
(`HEALTH_CHECK_TIMEOUT_SECONDS`). The body reports both, plus the LLM chain and
breaker state.

**The status code is load-bearing** — it is Caddy's `health_uri`, so a non-2xx
takes this uvicorn out of the upstream pool, and with one upstream that means
`/api/*` starts answering 502. So it fails only on a dependency Herald cannot
work without:

| Dependency | Required | Down ⇒ |
|---|---|---|
| Postgres | always | 503 |
| Redis | when `CELERY_ENABLED=true` (production) | 503 |
| Redis | when `CELERY_ENABLED=false` | reported, still 200 |

Redis counting as fatal in production is deliberate: with the worker and beat
running, a dead broker means every generate and publish is queued into nothing
and silently lost. A loud 502 is better than accepting work that will never
happen. The tradeoff is that a Redis blip takes the whole API down with it —
`systemctl status redis` is the first thing to check on an unexplained 502.

`deploy.sh` ends with `curl -fsS …/health`, so a deploy that leaves a dependency
broken fails at the last step instead of looking fine.

## Auth surface

`/auth/*` is the only part of the API reachable without a bearer token, so it is
the only part that is rate limited (`app/ratelimit.py`, slowapi).

- **Registration is closed.** `REGISTRATION_ENABLED=false` — `POST
  /auth/register` answers 403. The account is the seeded one. Opening it in
  production additionally requires `REGISTRATION_INVITE_TOKEN`; enabled without
  a token is refused rather than served open.
- **Limits** default to 10/minute login, 5/hour register, 5/hour password reset,
  60/minute `/auth/me`. Counters live in process memory, so with two uvicorn
  workers the real budget is double the number. Set
  `RATE_LIMIT_STORAGE_URI=redis://localhost:6379/3` to make it exact.
- **The client address comes from Caddy**, which appends the peer it saw to
  `X-Forwarded-For`. Herald reads the **rightmost** entry, so a caller cannot
  prepend a value and reset its own budget. `RATE_LIMIT_TRUST_FORWARDED_FOR=false`
  falls back to `request.client.host`, which behind this proxy is one bucket for
  the entire internet — only correct if the app is ever exposed directly.

## Migrations

```bash
cd /opt/Herald/backend
sudo -u herald env $(grep -E '^DATABASE_URL=' /opt/Herald/.env | xargs) \
  /opt/Herald/.venv/bin/alembic upgrade head
```

`deploy.sh` stops the worker and beat before migrating and starts them after.

## Seeding

```bash
cd /opt/Herald/backend
sudo -u herald env $(grep -vE '^#|^$' /opt/Herald/.env | xargs) \
  /opt/Herald/.venv/bin/python -m app.seed
```

Idempotent, keyed on `SEED_EMAIL`. The seed address must be a **routable**
domain: `EmailStr` rejects special-use TLDs, so the old `dev@herald.local`
default seeded fine and then 500'd `/api/v1/auth/me` on response validation.

## Rollback

The server holds no `.git`, so roll back from the developer machine:

```bash
git checkout <good-sha>
./deploy/deploy.sh
```

Take a code and database snapshot before anything risky:

```bash
ssh … 'tar czf /opt/backups/herald-code-$(date +%s).tar.gz -C /opt Herald'
ssh … 'sudo -u postgres pg_dump herald | gzip > /opt/backups/herald-db-$(date +%s).sql.gz'
```

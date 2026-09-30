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
| Public URL | `https://herald.doaide.com` |
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
splitting `/api/*` to the API rather than a separate `api.herald.doaide.com`.
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
{ echo ":8099 {"; sed -n '/^herald.doaide.com {/,/^}/p' /opt/knol/Caddyfile | tail -n +2; } > /tmp/ht/Caddyfile
docker run --rm -d --name herald-caddy-test --network knol_knol \
  -p 127.0.0.1:8099:8099 -v /tmp/ht/Caddyfile:/etc/caddy/Caddyfile:ro caddy:2-alpine
curl -s http://127.0.0.1:8099/api/v1/health
docker rm -f herald-caddy-test
```

## DNS

`herald.doaide.com` needs an **A record → 89.167.8.178, proxy disabled (grey
cloud)** in the Cloudflare zone `doaide.com`, matching how
`documedic.doaide.com` is set up. Caddy solves the ACME HTTP-01 challenge
itself; an orange-cloud record would break issuance. Until the record exists,
Caddy retries every 60 s and logs `NXDOMAIN looking up A for
herald.doaide.com` — it picks up the certificate on its own once DNS resolves,
with no restart needed.

## Environment

`/opt/Herald/.env`, mode 600, owned by `herald`; systemd reads it via
`EnvironmentFile`. Generated at deploy time and **never** in git. `JWT_SECRET`
and `TOKEN_ENCRYPTION_KEY` were generated on the box.

### Rotating `TOKEN_ENCRYPTION_KEY`

`TOKEN_ENCRYPTION_KEY` holds a **comma-separated list, newest first**: the head
key encrypts, every key in the list decrypts. Replacing the value outright is
still an outage — it makes every stored platform credential, webhook signing
secret and inbound trigger secret unreadable at once — so rotate in three steps
instead, with nothing going down in between:

1. **Prepend** the new key, keeping the old one behind it, and restart:

   ```sh
   NEW=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
   # TOKEN_ENCRYPTION_KEY=<new>,<old>
   systemctl restart herald-api herald-worker herald-beat
   ```

   Everything still reads; new writes go under the new key.

2. **Re-encrypt** what is already stored. The `rewrap-credentials` beat job does
   this nightly, but after a rotation run it now:

   ```sh
   cd /opt/Herald/backend && .venv/bin/celery -A app.tasks.celery_app \
       call app.tasks.maintenance_tasks.rewrap_credentials
   ```

   It logs how many rows moved, per table, and is safe to run repeatedly.

3. **Drop the old key** — `TOKEN_ENCRYPTION_KEY=<new>` — and restart again.

Do not do step 3 before step 2: the ciphertext is the only copy of those
secrets. If it happens anyway, the fix is to put the old key back on the end of
the list and run step 2 — the sweep counts rows it cannot read and leaves them
untouched precisely so that recovery stays possible.

LLM calls go to **OpenRouter free models** (`openai/gpt-oss-20b:free`,
`openai/gpt-oss-120b:free`), using the same key Documedic uses. That key is on
OpenRouter's free tier: **50 free-model requests per day, shared across every
app using it**, after which calls return HTTP 429 and Herald falls through its
provider chain to a static template.

### The fallback chain

`OpenRouter → Gemini → Groq → Cerebras → static template`
(`app/services/llm_router.py`). One dialect for all four, so a provider is just
a base URL, a key and a model name; a provider with a blank key is skipped
rather than attempted and failed. Whichever one serves a piece is recorded on it
(`generated_by_provider`), and `GET /api/v1/health` lists the configured chain
in the order it will be tried.

**No fallback key is set on the box yet** — `GEMINI_API_KEY` and `GROQ_API_KEY`
are present in `/opt/Herald/.env` but empty, so the live chain is
`["openrouter"]` and a day when the shared quota is already spent is a day of
template output. Fixing that needs a key, which has to be created by hand:

```bash
# /opt/Herald/.env, then: systemctl restart herald-api herald-worker herald-beat
GEMINI_API_KEY=…    # https://aistudio.google.com/apikey
GROQ_API_KEY=…      # https://console.groq.com/keys
```

Both are free tiers with limits an order of magnitude above 50/day, and neither
is shared with the other apps on this box. Giving Herald its own OpenRouter key
(or $10 of credit, which unlocks 1000/day) is still the better fix for the
primary provider; the fallbacks are what stop a bad afternoon from silently
degrading every generated post.

Verify what took effect:

```bash
curl -s https://herald.doaide.com/api/v1/health | python3 -m json.tool
# → "llm_providers": ["openrouter", "gemini", "groq"]
```

## API docs

`/docs`, `/redoc` and `/openapi.json` are **404 in production**. The gate is
`ENVIRONMENT != production or DEBUG == true` (`app.main.docs_enabled`), and the
routes are removed rather than protected — an auth prompt would confirm the
schema is there, and there is nothing in it worth authenticating for.

The schema is a full inventory of every route and field, served on the same
origin as the SPA, which is what makes it worth withholding from anonymous
visitors. To read it against the live box, `DEBUG=true` in `/opt/Herald/.env`
plus `systemctl restart herald-api` — no Caddy edit, the vhost still routes
those paths.

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
- **Limits** are 10/minute login, 5/hour register, 5/hour password reset,
  60/minute `/auth/me`. Counters are in **Redis DB 3** on this box
  (`RATE_LIMIT_STORAGE_URI=redis://localhost:6379/3`, set in `/opt/Herald/.env`),
  so the numbers are exact. Left blank they live in process memory, which with
  `--workers 2` means each worker keeps its own and the real budget is double —
  and, because a caller's consecutive requests land on either worker, the limit
  is not observable from outside. Redis being unreachable degrades to allowing
  requests (`swallow_errors`), not to blocking them; the health check catches the
  outage separately.
- **The client address comes from Caddy**, which appends the peer it saw to
  `X-Forwarded-For`. Herald reads the **rightmost** entry, so a caller cannot
  prepend a value and reset its own budget. `RATE_LIMIT_TRUST_FORWARDED_FOR=false`
  falls back to `request.client.host`, which behind this proxy is one bucket for
  the entire internet — only correct if the app is ever exposed directly.

### Password reset

`POST /auth/password-reset` (always 202, whatever the address) emails a
single-use link; `POST /auth/password-reset/confirm` spends it. Only a SHA-256
hash of the token is stored, the TTL is 60 minutes, and requesting a new link
invalidates the one before it.

**No SMTP is configured on the box**, so the link is written to the log instead
of sent — which is a workable way to reset your own password on a single-user
install:

```bash
journalctl -u herald-api --since '2 min ago' | grep reset-password
```

To send it properly, set `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` in
`/opt/Herald/.env` (Gmail wants an app password) and restart `herald-api`.

Two things this flow does **not** do:

- **It does not log anyone out.** Herald's JWTs are stateless with no revocation
  list, so tokens issued before a reset keep working until they expire —
  `ACCESS_TOKEN_EXPIRE_MINUTES`, 24 h by default. Resetting a password because
  a token leaked needs a `JWT_SECRET` rotation, which invalidates every session.
- **There is no frontend page for it yet.** The link points at
  `$FRONTEND_URL/reset-password?token=…` and the SPA has no such route, so the
  token has to be posted to the confirm endpoint by hand for now:

  ```bash
  curl -X POST https://herald.doaide.com/api/v1/auth/password-reset/confirm \
    -H 'Content-Type: application/json' \
    -d '{"token":"…","new_password":"…"}'
  ```

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

## Backups

`herald-backup.timer` runs `deploy/backup.sh` at 03:30 UTC. It writes a
verified `pg_dump --format=custom` into `/var/backups/herald` and prunes dumps
older than 14 days. The snapshot above is still worth taking before a risky
deploy — it is a point-in-time copy you chose; this is the one that exists when
nobody chose anything.

Deleting a piece is a real `DELETE` (`routers.content.delete_content` — archive
is the reversible option), so these dumps are the only way back from a mistaken
one.

```bash
# Did last night run?
ssh … 'systemctl status herald-backup; ls -la /var/backups/herald'
ssh … 'journalctl -u herald-backup --since "2 days ago"'

# Take one now.
ssh … 'systemctl start herald-backup'
```

Installing it on a fresh box — the units are rsynced to `/opt/Herald/deploy`
by `deploy.sh`, but systemd needs them in `/etc` and the directory has to
exist:

```bash
ssh … '
  install -d -o herald -g herald -m 0700 /var/backups/herald
  cp /opt/Herald/deploy/systemd/herald-backup.{service,timer} /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now herald-backup.timer'
```

**Restoring.** `--format=custom` is what makes a single table recoverable
without replaying the whole database, which is what an accidental delete
actually needs.

One thing to know before the incident rather than during it: `backup.sh` runs
`umask 077`, so the dumps are `0600` inside a `0700` directory owned by
`herald`. That is deliberate — a dump holds every password hash and every
encrypted platform credential in the database, on a box six products share. It
also means **`sudo -u postgres pg_restore` cannot read them.** Handing the path
straight to the `postgres` user fails with

```
pg_restore: error: could not open input file "...": Permission denied
```

which at 03:00 reads like a corrupt backup and is not one. Stage a copy the
`postgres` user owns, and keep it `0600` while it exists:

```bash
# What is in the dump. As root, which is not subject to the mode bits.
pg_restore --list /var/backups/herald/herald-<stamp>.dump

# Stage it where postgres can read it, without widening the mode.
install -d -o postgres -g postgres -m 0700 /var/backups/herald-restore
install -o postgres -g postgres -m 0600 \
  /var/backups/herald/herald-<stamp>.dump /var/backups/herald-restore/dump

# One table, into a scratch database first — never straight over prod.
sudo -u postgres createdb herald_restore
sudo -u postgres pg_restore -d herald_restore -t content \
  /var/backups/herald-restore/dump

# The staged copy is a second unencrypted copy of the database. Remove it.
rm -rf /var/backups/herald-restore
```

**Restore drill.** The above, whole rather than one table, is how you find out
the dumps are real before you need them. `--exit-on-error` is the point: without
it `pg_restore` reports a partial restore as success.

```bash
sudo -u postgres createdb herald_restore_drill
sudo -u postgres pg_restore --exit-on-error -d herald_restore_drill \
  /var/backups/herald-restore/dump
# Compare against live, then drop it.
sudo -u postgres psql -d herald_restore_drill -c 'select count(*) from content'
sudo -u postgres dropdb herald_restore_drill
```

Last drilled 2026-08-15 against `herald-20260815T033246Z.dump`: restored clean,
15/15 tables, 53 indexes and 14 foreign keys matching live, same Alembic head.

Two limits, stated so they are not discovered during an incident: the dumps sit
on the **same disk** as the database, so they cover operator error and
corruption but not losing the box; and copying them off-host is not set up yet.

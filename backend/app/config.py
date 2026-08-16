"""Application configuration, loaded from environment / .env.

All settings are typed and validated by pydantic-settings. Nothing sensitive is
hard-coded — secrets come from the environment or the gitignored ``keys/`` dir.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Shortest ``JWT_SECRET`` production will start with, in bytes. 256 bits, the
#: output size of SHA-256 — RFC 7518 §3.2 requires an HMAC key at least as long
#: as the hash it is used with, and PyJWT warns below this and signs anyway.
_MIN_JWT_SECRET_BYTES = 32

#: The range bcrypt itself accepts for a work factor; it raises outside it.
_BCRYPT_MIN_ROUNDS = 4
_BCRYPT_MAX_ROUNDS = 31
#: The lowest work factor production may run with, whatever the .env says.
_BCRYPT_PRODUCTION_MIN_ROUNDS = 12

#: Hostnames that never leave the machine, so a plaintext origin on one is not
#: the exposure a plaintext origin normally is.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_loopback(origin: str) -> bool:
    """Whether *origin*'s host is this machine.

    Used to spare ``http://localhost:5173`` from the production CORS filter —
    see :meth:`Settings.cors_origins`. Parsed rather than matched as a prefix,
    so that ``http://localhost.evil.test`` is not read as loopback because of
    how it starts.
    """
    return (urlsplit(origin).hostname or "").lower() in _LOOPBACK_HOSTS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App ----
    app_name: str = "Herald"
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    backend_cors_origins: str = "http://localhost:5173,http://localhost:3000"
    frontend_url: str = "http://localhost:5173"
    # Where this API is reachable from the outside, without the ``/api/v1``
    # prefix. Only needed for URLs Herald hands to somebody else's system — an
    # inbound trigger's webhook URL is pasted into GitHub or Zapier, so a
    # localhost default would be worse than useless. Blank means "same origin as
    # the frontend", which is true in production (Caddy proxies both) and false
    # in local dev, where the API is on :8000 and this wants setting.
    public_api_url: str = ""
    # Root log level, as a name (``DEBUG``/``INFO``/``WARNING``/…). INFO is the
    # level the codebase writes its operational lines at — what a sweep
    # dispatched, what was published where, which fallback fired — so anything
    # above it turns the box silent on everything except failures. See
    # ``app.logging_config``.
    log_level: str = "INFO"

    # ---- Security / JWT ----
    jwt_secret: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440
    # Fernet key protecting stored platform API tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Blank means tokens are stored in the clear — fine for local dev, refused
    # in production (see app.services.crypto).
    #
    # Accepts a comma-separated *list*, newest first: the head encrypts, all of
    # them decrypt. That is how the key is rotated with nothing going dark —
    # prepend the new key, let the nightly rewrap sweep move the stored rows
    # onto it, then drop the old one.
    token_encryption_key: str = ""
    # bcrypt's work factor: the cost of one hash is 2**rounds, so each step up
    # doubles it. 12 is the current sensible default for a password hash and is
    # the floor production is held to below.
    #
    # This is settable for exactly one reason: the test suite. A hash at 12
    # costs ~230ms, the fixtures make a user for nearly every test, and that
    # single line was over half the suite's runtime — enough that a full run
    # reads as a hang and gets killed rather than waited out. Tests set 4, the
    # library minimum, and run the same code path in ~1ms.
    #
    # Lowering this does not strand existing hashes: bcrypt encodes the cost in
    # the hash itself, so `verify_password` keeps checking old ones at whatever
    # they were made with, and only new hashes are made at the new cost.
    bcrypt_rounds: int = 12

    # ---- Registration ----
    # Herald is a single-user product: the account is created once by
    # ``python -m app.seed``. An open /auth/register on a public box lets anyone
    # sign up and spend the shared free-tier LLM quota, so it is closed unless
    # explicitly opened.
    registration_enabled: bool = False
    # When set, /auth/register additionally requires this exact token in the
    # request body. Mandatory in production: registration that is enabled but
    # tokenless is refused rather than served wide open.
    registration_invite_token: str = ""

    # ---- Password reset ----
    # Long enough to survive a slow mail hop and a coffee, short enough that a
    # link sitting in an inbox is not a standing key to the account.
    password_reset_token_ttl_minutes: int = 60

    # ---- Draft preview links ----
    # A shareable, unauthenticated, read-only link to one draft. Long enough
    # that a reviewer gets to it without a deadline; the max is a ceiling on
    # what the author can ask for, not a default.
    preview_link_default_ttl_hours: int = 168  # 7 days
    preview_link_max_ttl_hours: int = 720  # 30 days
    # How long a dead link (revoked, or lapsed) is kept before the maintenance
    # sweep prunes it. The listing endpoint returns every link ever issued for
    # a draft, so without a sweep that read grows for the life of the account.
    # Comfortably past preview_link_max_ttl_hours so a link is never pruned
    # while it could still open.
    preview_link_retention_days: int = 90

    # ---- SMTP (password reset mail — the only mail Herald sends) ----
    # With SMTP_HOST blank the reset link is written to the log instead of sent.
    # That is a deliberate fallback for a self-hosted single-user install, not a
    # stub: `journalctl -u herald-api` is a workable way to collect your own
    # link. It does put the link in the logs.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    #: Implicit TLS (port 465). Mutually exclusive with STARTTLS.
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 15.0

    # ---- Rate limiting ----
    rate_limit_enabled: bool = True
    # Blank means in-process memory, which counts per uvicorn worker (2 in
    # production, so the effective limit is doubled). Point it at
    # ``redis://localhost:6379/3`` to share one counter across workers.
    rate_limit_storage_uri: str = ""
    # Herald sits behind Caddy, so ``request.client.host`` is always the proxy.
    # With this on, the *rightmost* X-Forwarded-For entry is used instead — the
    # one Caddy appended, i.e. the peer it actually saw. A client-supplied
    # header lands to the left of it and so cannot be used to dodge a limit.
    rate_limit_trust_forwarded_for: bool = True
    # Per-endpoint budgets. Any format `limits` understands, including several
    # windows separated by ";" ("10/minute;100/hour").
    rate_limit_login: str = "10/minute;100/hour"
    rate_limit_register: str = "5/hour"
    rate_limit_password_reset: str = "5/hour"
    rate_limit_auth_read: str = "60/minute"
    # The public RSS feed. Two database queries and an XML render for anyone who
    # can guess a project id, so it is the most expensive thing an anonymous
    # caller can reach. A reader polls a feed every 15-60 minutes; 20/minute is
    # far above any real client and far below what makes the endpoint a lever.
    rate_limit_public_feed: str = "20/minute;300/hour"
    # The liveness probe. Caddy polls it every 30s (see deploy/Caddyfile.herald)
    # from the Docker bridge with no X-Forwarded-For, so its requests bucket
    # against the proxy address rather than any caller's — and a deploy adds at
    # most a handful of retries to that same bucket. Public traffic arrives
    # through Caddy and buckets per visitor. 60/minute leaves both an order of
    # magnitude of headroom while still capping a probe that opens a fresh Redis
    # connection and round-trips Postgres on every call.
    rate_limit_health: str = "60/minute"
    # The unauthenticated constant lists (webhook events, trigger kinds). Cheap
    # to serve, but there is no reason for one caller to need hundreds a minute.
    rate_limit_public_read: str = "60/minute"
    # The budgets below are per *account*, not per address, and exist because
    # the request spends something shared rather than because the caller is
    # anonymous — see the `app.ratelimit` module docstring. Every one of them is
    # set well above a person clicking a button and well below a retry loop.
    #
    # Writing a whole piece: one LLM call of a few thousand tokens, ~20s of
    # somebody watching a spinner. Six an hour is a busy editorial day; sixty is
    # not a person.
    rate_limit_ai_generate: str = "60/hour;300/day"
    # The small single-shot model calls — rewriting a passage, headline
    # variants, a repurposed blurb. Cheaper per call and used far more often
    # inside one editing session, so the budget is looser.
    rate_limit_ai_assist: str = "120/hour;600/day"
    # Repo scans spend the *install's* GitHub quota (5000/hour authenticated,
    # 60 unauthenticated), which is one budget shared by every account. A scan
    # is a handful of calls, so this caps one account at a small fraction of it.
    rate_limit_repo_scan: str = "60/hour;500/day"
    # The link checker fans one request out to `link_check_max_urls` outbound
    # requests from Herald's own address. Limited so a document full of links
    # cannot be replayed into an outbound-traffic amplifier.
    rate_limit_link_check: str = "60/hour;500/day"
    # Mailing the digest on demand spends the install's single SMTP account —
    # one budget, one sending reputation, shared by every account here. A person
    # checking what this week's mail looks like sends one or two; a loop sends
    # enough to get the domain filed as a sender nobody asked for.
    rate_limit_digest_send: str = "10/hour;30/day"
    # A webhook ping and a trigger check are both synchronous outbound requests
    # made from Herald's address on the caller's say-so, which is the link
    # checker's problem in a different shape. A trigger check is the sharper of
    # the two: on a GitHub trigger it spends the install's single GITHUB_TOKEN,
    # so leaving it unlimited was a way around `rate_limit_repo_scan`.
    rate_limit_outbound_probe: str = "60/hour;500/day"

    # ---- Database ----
    database_url: str = "postgresql+psycopg://herald:herald@localhost:5432/herald"
    # Connections are a per-process budget spent against one shared server, so
    # the number that matters is not this one but this one times the processes
    # running it. The deployed shape is two uvicorn workers, a Celery worker and
    # beat (deploy/systemd), so the ceiling is 4 x (pool + overflow) = 60
    # against PostgreSQL's default `max_connections` of 100 — leaving room for
    # psql, a migration, and the next worker somebody adds. Raise both together
    # with `max_connections` if that stops being true; the arithmetic is the
    # setting, not the number.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # How long a request waits for a connection before giving up. SQLAlchemy's
    # default is 30 seconds, which is not a wait — it is a hang: the caller and
    # every proxy between have long since timed out, and the request goes on
    # holding a worker thread for a connection nobody is waiting for any more.
    # Ten seconds is long enough to ride out a burst and short enough that a
    # saturated pool shows up as errors, which page someone, rather than as
    # latency, which does not.
    db_pool_timeout_seconds: float = 10.0
    # Rotate connections proactively so a PostgreSQL restart or a network blip
    # does not accumulate dead ones in the pool.
    db_pool_recycle_seconds: int = 1800
    # Server-side ceiling on any single statement, PostgreSQL only. A query with
    # no bound is a connection with no bound: it holds its slot until someone
    # notices, and the pool's own timeout cannot reclaim what was legitimately
    # checked out. Set to 0 to disable.
    db_statement_timeout_seconds: float = 30.0
    # How long *opening* a connection may take, which neither of the two above
    # covers — see `app.database._connect_timeout_arg` for why an unbounded
    # connect is a health-check outage rather than a slow request. Five seconds
    # is many times a healthy connect on this box (same host, unix-fast) and
    # well inside the 30s interval Caddy re-probes on. libpq's floor is 2; 0
    # disables the bound and restores the multi-minute kernel default.
    db_connect_timeout_seconds: float = 5.0
    # Log any statement that takes at least this long, with its duration. The
    # three timeouts above are all ceilings — they say what Herald refuses to
    # wait for, and by the time one fires the request is already lost. This is
    # the other half: the query that takes four seconds every time, succeeds,
    # and is therefore invisible to every one of them.
    #
    # Half a second is far above anything this schema should cost — the slowest
    # legitimate query here is the analytics roll-up, and it is indexed for —
    # so a healthy install logs nothing at all and the log stays worth reading.
    # See `app.database.install_slow_query_logging`, which explains why the
    # parameters are never logged with it. Set to 0 to disable.
    db_slow_query_ms: int = 500

    # ---- Redis / Celery ----
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    # When False, generation and publishing run inline in the request instead of
    # being handed to a worker. Intended for single-process deployments and
    # tests; production runs workers and leaves this on.
    celery_enabled: bool = True
    # Per-dependency budget for the /health probes. Short on purpose: Caddy polls
    # this every 30s and a health check that hangs is itself an outage.
    health_check_timeout_seconds: float = 2.0

    # ---- AI (OpenRouter — free models only) ----
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # The workhorse: fast and cheap enough to draft a post per repo push.
    openrouter_model: str = "openai/gpt-oss-20b:free"
    # The bigger free model, used when a piece needs the extra headroom —
    # long-form tutorials and comparisons rather than a 280-character tweet.
    openrouter_long_form_model: str = "openai/gpt-oss-120b:free"
    openrouter_app_url: str = "https://herald.local"
    openrouter_app_title: str = "Herald"

    # ---- AI fallback providers ---------------------------------------------
    # Tried in order after OpenRouter. All speak the OpenAI chat-completions
    # dialect, so one client handles the lot; a provider with no key configured
    # is simply skipped. Free tiers all round.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    # A rolling alias, not a pinned version. Google retires the pinned names —
    # ``gemini-2.0-flash``, which this used to be, started answering "is no
    # longer available" and the Gemini slot was dead for days before anyone
    # read the log closely enough to tell it from a rate limit. The alias is the
    # one value that cannot go stale that way; see
    # :class:`app.services.llm_router.LLMModelUnavailable` for what now happens
    # when a model does disappear.
    gemini_model: str = "gemini-flash-latest"

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"

    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    cerebras_model: str = "llama-3.3-70b"

    # Extra models to try on the *same* provider before moving to the next one,
    # comma-separated. Worth setting on the free tiers, where the quota is per
    # model rather than per key: a second model on a key that has exhausted its
    # first is a request that succeeds, and moving providers is not.
    openrouter_fallback_models: str = ""
    gemini_fallback_models: str = ""
    groq_fallback_models: str = ""
    cerebras_fallback_models: str = ""

    # Circuit breaker: after this many consecutive failures a provider is
    # skipped entirely for the cool-down, so one dead upstream costs a timeout
    # once rather than on every request.
    llm_breaker_threshold: int = 3
    llm_breaker_cooldown_seconds: int = 300
    # A provider that answers 429 with a `Retry-After` gets that as its
    # cool-down instead, capped here. The free tiers hand out day-long windows
    # when a daily quota is spent, and skipping for the default five minutes
    # means paying a refused request every five minutes until midnight.
    llm_breaker_max_cooldown_seconds: int = 3600

    # How many times the whole provider chain is swept before giving up. A
    # second sweep only happens when the first one failed on rate limits and
    # nothing else — see ``app.services.llm_router``. The per-minute free-tier
    # limits are what this exists for: they clear in seconds, and falling
    # through to a template because of one is the difference between a
    # published post and a blocked auto-publish.
    llm_max_attempts: int = 3
    # The longest a single request will be held waiting for a rate limit to
    # clear. Also the line between "wait for it" and "stand this provider
    # down": a provider asking for longer than this is treated as having spent
    # a daily quota rather than a per-minute one.
    llm_retry_max_backoff_seconds: float = 30.0
    # How long the per-completion accounting rows are kept before the daily
    # maintenance sweep prunes them. Bounded by age rather than by row count —
    # see `app.models.llm_usage`: the window is what /api/v1/metrics reports
    # relative to, and a cap of N rows would make that window depend on how
    # busy the week was. Comfortably wider than the endpoint's own longest
    # window so the numbers it quotes are never truncated by the purge.
    llm_usage_retention_days: int = 30

    # ---- GitHub (project change monitoring) ----
    # A classic or fine-grained PAT with `repo` read. Optional: without it the
    # monitor still works against public repos at the anonymous rate limit
    # (60/hour), which is enough for a handful of projects on a slow cadence.
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = 15.0

    # ---- Autopilot ----
    # How often the beat task sweeps registered repos for new commits/releases.
    autopilot_scan_interval_seconds: int = 3600
    # How often the beat task looks for scheduled content that has come due.
    publish_scan_interval_seconds: int = 300
    # How often published posts are re-polled for view/reaction counts.
    metrics_scan_interval_seconds: int = 21600
    # A release, or this many commits since the last generated piece, is what
    # counts as "worth writing about". Below it the autopilot stays quiet.
    autopilot_commit_threshold: int = 10
    # Generated content at or above this confidence may publish without review
    # when the project is set to auto-publish. Below it, everything queues.
    autopilot_auto_publish_confidence: float = 0.8
    # Ceiling on autopilot drafts per project per day, so a busy repo can't
    # turn into a content firehose.
    autopilot_daily_content_limit: int = 2
    # Maximum unused ideas kept per project. Oldest unused are pruned each scan.
    autopilot_ideas_cap: int = 50

    # ---- Duplicate content ----
    # How far back `app.services.dedup` looks when asking "have we written this
    # already?". A window rather than the whole history, because the answer is
    # about editorial repetition and repetition has a horizon: a follow-up post
    # a quarter after the original is a follow-up, and the same post a day after
    # the original is a mistake. Thirty days is roughly the point at which a
    # reader would not notice.
    dedup_window_days: int = 30
    # Most recent pieces compared against. The comparison is in Python over
    # titles, so this bounds both the query and the loop; a project writing
    # twice a day fills a thirty-day window with sixty pieces, and the cap
    # bites only on an account well past that.
    dedup_compare_limit: int = 200

    # ---- Triggers ----
    # How often the beat task sweeps polled triggers (RSS, GitHub, schedule) for
    # ones whose interval has elapsed. Must be comfortably shorter than the
    # shortest interval a trigger can ask for, or a trigger asking for hourly
    # gets checked every other hour.
    trigger_scan_interval_seconds: int = 600
    # What a trigger's interval is when it does not name one.
    trigger_default_interval_hours: float = 1.0
    # Ceiling on pieces written for one project by triggers per day. Separate
    # from the autopilot's own cap: a project can have five triggers, and an
    # RSS-heavy setup should not be able to spend the whole budget in an hour.
    trigger_daily_content_limit: int = 3
    # Consecutive failed checks before a polled trigger is deactivated. A feed
    # that has 404ed this many times running has moved or been deleted.
    trigger_disable_after_failures: int = 20
    # How long trigger event rows are kept before the maintenance sweep prunes
    # them. Long enough to answer "why didn't my trigger fire last week?".
    trigger_event_retention_days: int = 60
    # How long a firing may sit at `received` before the sweep concludes that
    # whatever was acting on it is gone. `triggers.record` commits the event
    # before generation starts, so a worker killed in between (OOM, a deploy
    # restart, the hard time limit) leaves a row nothing will ever settle — and
    # the firing cannot come back, because its dedupe key is spent. Must
    # comfortably exceed `check_trigger`'s hard time limit, and the inbound
    # webhook request that fires one on the API thread; inside that window the
    # generation is still running and the row is not stuck at all.
    trigger_event_stuck_after_seconds: int = 900
    # Per-request budget for reading a user-supplied feed.
    feed_timeout_seconds: float = 15.0
    # New feed entries acted on in one poll. A backfill of forty entries is not
    # forty pieces of news; the rest are still recorded as seen.
    feed_max_new_entries: int = 3
    # Inbound webhook triggers are unauthenticated by design — the token in the
    # URL is the credential — so the endpoint carries its own rate limit.
    rate_limit_trigger_inbound: str = "60/minute;1000/hour"

    # ---- Publishing ----
    # Requests to platform APIs. Publishing is a background task, so a generous
    # timeout is cheaper than a retry.
    publish_timeout_seconds: float = 30.0
    # A publication that fails is retried this many times with backoff before it
    # is parked as `failed` for a human to look at.
    publish_max_retries: int = 3
    # In-process retries *within* a single adapter call, for the blips that
    # resolve in seconds — a dropped connection, a 503 from a load balancer
    # mid-roll. Only replays calls where doing so cannot double-post; see
    # app.services.publishers.base._is_retryable. Set to 0 to rely solely on the
    # publication-level retry above.
    publish_request_retries: int = 2
    # First backoff window, doubled per attempt, with full jitter inside it.
    publish_retry_backoff_seconds: float = 1.0
    publish_retry_max_backoff_seconds: float = 30.0
    # Backoff *between* publication-level attempts — the outer layer, measured
    # in minutes rather than the seconds the two settings above deal in. A
    # retryable failure parks the row until this has elapsed instead of leaving
    # it at the front of the next sweep. Without it the three attempts above are
    # spent at the sweep cadence, so a platform having a twenty-minute outage
    # burns the whole budget inside ten and the piece needs a hand-retry nobody
    # is watching for. The default series is 5m, 10m, 20m — an hour of outage
    # survived by waiting, which is the one thing this layer can usefully do.
    publish_retry_defer_seconds: float = 300.0
    publish_retry_max_defer_seconds: float = 3600.0
    # Ceiling on how long a rate-limited publication is parked for. Platforms
    # occasionally answer Retry-After with something enormous, and a post that
    # silently disappears for a day looks like a bug rather than a queue.
    publish_rate_limit_max_defer_seconds: int = 3600
    # Circuit breaker in front of the platforms — the layer above the two retry
    # settings, and the only one that can see more than one publication at a
    # time. After this many consecutive failures on one account's route to one
    # platform, rows for that route are parked without being sent and without
    # spending an attempt, so a platform having a twenty-minute outage costs the
    # queue a wait rather than every row's retry budget. See
    # app.services.publishers.breaker for why the key is per account.
    publish_breaker_enabled: bool = True
    publish_breaker_threshold: int = 4
    publish_breaker_cooldown_seconds: int = 300
    # A platform that answers 429 with a `Retry-After` gets that as its
    # cool-down instead, capped here — the same reasoning as
    # llm_breaker_max_cooldown_seconds, and the same ceiling as the row-level
    # publish_rate_limit_max_defer_seconds above so the two layers cannot
    # disagree about how long an hour is.
    publish_breaker_max_cooldown_seconds: int = 3600
    # How long a row may sit in `publishing` before the sweep assumes the worker
    # that claimed it is gone and re-arms it. `publish_one` claims the row and
    # then commits, so a worker killed between the two (OOM, a deploy restart,
    # SIGKILL) leaves the row claimed forever: `due_publications` only looks at
    # pending and scheduled, and the redelivered task's own claim finds the row
    # already `publishing` and skips. Must comfortably exceed publish_one's hard
    # time limit — inside that window the task is still alive and re-arming it
    # would post twice.
    publish_stuck_after_seconds: int = 900

    # ---- Outbound webhooks ----
    # Per-request budget for a user's own endpoint. Short: a webhook is a
    # notification, and an endpoint that needs half a minute to acknowledge one
    # is an endpoint that should acknowledge first and work afterwards.
    webhook_timeout_seconds: float = 10.0
    # Attempts per delivery before it is parked as failed for a human to
    # redeliver. Covers a deploy window, not an outage.
    webhook_max_attempts: int = 5
    # First backoff window, doubled per attempt, capped. The default series is
    # 30s, 1m, 2m, 4m — about eight minutes end to end.
    webhook_retry_backoff_seconds: float = 30.0
    webhook_retry_max_backoff_seconds: float = 3600.0
    # Consecutive *fully failed* deliveries before the endpoint is deactivated.
    # An endpoint that has swallowed nothing for this many events in a row is
    # gone, and queueing for it only fills the delivery table.
    webhook_disable_after_failures: int = 20
    # How often the beat task sweeps for deliveries whose backoff has elapsed.
    webhook_scan_interval_seconds: int = 60
    # How long one worker owns a delivery it has claimed. A claim pushes
    # ``next_attempt_at`` out by this much, so a second worker sweeping in the
    # meantime does not see the row as due and cannot POST it again.
    #
    # Bounded on both sides. Below the per-request timeout above and the lease
    # expires while the request it covers is still open, which is the duplicate
    # it exists to prevent. Far above it and a delivery whose worker was killed
    # mid-attempt waits that long to be retried by anyone else — the lease is
    # also the crash-recovery window, since a claim is the only thing that can
    # leave a row owned by nobody. Two sweep intervals, twelve timeouts.
    webhook_claim_lease_seconds: float = 120.0
    # How long delivered/failed rows are kept before the maintenance sweep
    # prunes them. Long enough to debug last week's missing notification.
    webhook_delivery_retention_days: int = 30

    # ---- Link validation ----
    # HEAD-check every URL in a body before it goes out. Only a definitive 404 or
    # 410 blocks a publish; a timeout or a 403 is reported and ignored (see
    # app.services.link_check). Off means the pre-publish gate is skipped
    # entirely — the /content/{id}/links endpoint still works on demand.
    link_check_enabled: bool = True
    link_check_timeout_seconds: float = 10.0
    # Latency guard on a single publish request, not a policy about post length.
    link_check_max_urls: int = 25

    # ---- Claim validation ----
    # Hold a piece back from auto-publish when it names a product or feature
    # that nothing on file supports — see app.services.factcheck. Off means the
    # gate is skipped; the names are still computed and banked on the content
    # row so a reviewer can see them.
    factcheck_enabled: bool = True
    # Whether an unsupported name is worth one GitHub call to the project's
    # README before it is reported. Only ever paid for a piece that already
    # looks wrong, so the common case costs nothing — but it is still a call to
    # somebody else's API from inside a generation, and a deployment with no
    # GitHub token (or a repo the token cannot see) gets nothing for it.
    factcheck_readme_enabled: bool = True

    # ---- Content quality ----
    # Refuse to move a draft into review when it scores below the floor — see
    # app.services.quality for what the score is made of. The review queue is a
    # request for somebody's attention, and a piece that is unreadable, or that
    # is four fenced blocks and a sentence, is a request that should be turned
    # down at the point it is made rather than after a human has read it.
    #
    # Only that transition. A piece the autopilot routes *straight* to review
    # has already been through five gates and is in front of a human precisely
    # because one of them fired; failing it again here would leave nowhere for
    # it to go but the bin. Approve and archive are untouched for the same
    # reason — this is a floor on what may be asked for, not on what may exist.
    content_quality_gate_enabled: bool = True
    # Deliberately low. The score is arithmetic over syllables, fence widths and
    # the SEO envelope, not a judgement about writing, so it is set where it
    # separates "nobody should have to read this" from "this could be better" —
    # a code dump with a sentence on top scores under 50, an ordinary post
    # scores around 80. A stricter floor would start refusing pieces whose only
    # fault is being terse, which is a style a reviewer is allowed to hold.
    content_quality_min_score: int = 50

    # ---- Weekly digest ----
    # The window each digest reports on, and the comparison window is the one
    # immediately before it.
    digest_window_days: int = 7
    # When the sweep runs, UTC. Monday morning: a summary that lands before the
    # week's work starts is one that gets acted on.
    digest_send_weekday: int = 0  # 0 = Monday
    digest_send_hour: int = 8

    # ---- Headline testing ----
    # Evidence a headline's window needs before it is ranked at all. One poll an
    # hour after a swap says nothing about a headline.
    headline_min_snapshots: int = 2
    headline_min_window_hours: float = 24.0
    # How far ahead a challenger must be before the lead is called rather than
    # attributed to noise. Also what stops headlines flapping week to week.
    headline_winner_margin: float = 0.25
    # How often the beat task looks for a project whose headlines should be
    # re-judged. Daily: engagement moves slower than that, and a headline that
    # changes under the user more often than they look at it is a nuisance.
    headline_auto_select_interval_seconds: int = 86400

    # ---- Velocity and alerts ----
    # The two early windows every post is measured over, in hours since it went
    # live. The first is "did it land"; the second is the one posts are compared
    # against, because a single day is short enough that one poll landing late
    # skews it. Both must comfortably exceed metrics_scan_interval_seconds, or
    # no poll will fall inside them.
    velocity_early_window_hours: int = 24
    velocity_benchmark_window_hours: int = 48
    # Posts on a platform before its median is used to judge an individual one.
    # Three is the smallest sample where a median is not just "the other post".
    velocity_min_sample: int = 3
    # Below this fraction of the platform median, a post is flagged. Half is
    # deliberately far out: near-median variation is what content does, and an
    # alert that fires on a normal week trains the user to ignore the panel.
    underperformance_threshold: float = 0.5
    # How old a post may be and still be worth an underperformance warning.
    # The remedy that alert names — a new headline, a re-share — only moves a
    # post still being distributed, which is why the alert is framed as "worth
    # trying while it is still new". Nothing bounded it, so every post that ever
    # had a weak first two days stayed a warning for the life of the account,
    # and since warnings sort above notices and the list is truncated, the post
    # published on Tuesday was pushed off the end by one from last year. Two
    # weeks is comfortably more than `digest_window_days`, so no post can age
    # out between two weekly digests without having been reported at least once.
    underperformance_max_age_hours: int = 336
    # A post whose views over the trailing window fall to this fraction of its
    # own best equivalent window has stopped growing.
    velocity_stall_window_hours: int = 168  # one week
    velocity_stall_ratio: float = 0.1

    # ---- Learned posting times ----
    # Replace the static cadence table with hours derived from the user's own
    # results, once there is enough evidence. Off falls back to the table
    # everywhere, which is exactly the behaviour that shipped before.
    learned_cadence_enabled: bool = True
    # Posts on a platform before any of its timing is learned, and posts sharing
    # an hour before that hour can win. Both bars must clear: five posts spread
    # over five hours support no conclusion about any of them.
    learned_cadence_min_samples: int = 5
    learned_cadence_min_bucket: int = 2

    # ---- Scheduling ----
    # How far into the past a requested publish time may fall before it is
    # refused. Small but non-zero: a client clock a minute behind the server
    # should not turn "publish at 09:00" into an error.
    schedule_past_grace_seconds: int = 120
    # Ceiling on how far ahead something may be scheduled. Exists to catch a
    # mistyped year, which otherwise parks a post for a decade in silence.
    schedule_max_horizon_days: int = 365

    # ---- Syndication ----
    # How long the copies wait after the project's canonical platform when both
    # are queued in one go. Two jobs: it lets the original's URL land on the
    # content row so the copies can carry rel=canonical, and it gives crawlers a
    # window to see the original first. Nothing here is a barrier — the delay is
    # a `scheduled_for`, picked up by the same beat sweep as any other scheduled
    # publication, so it should comfortably exceed
    # `publish_scan_interval_seconds`. Set to 0 to publish everything at once.
    syndication_delay_seconds: int = 900

    @field_validator(
        "access_token_expire_minutes",
        "autopilot_daily_content_limit",
        "db_pool_recycle_seconds",
        "db_pool_size",
        # Both are windows the dedupe comparison is taken over, and zero is not
        # a way to switch it off — zero days is a window nothing falls in, and
        # zero rows is a comparison against nothing. Switching it off is what
        # a project's own settings are for; a config value that silently means
        # "never dedupe" is the kind of off-switch nobody knows is thrown.
        "dedup_compare_limit",
        "dedup_window_days",
        "feed_max_new_entries",
        "learned_cadence_min_bucket",
        "learned_cadence_min_samples",
        # Zero would not mean "keep forever" — it is a cutoff of `utcnow()`,
        # which deletes the rows the metrics endpoint is about the moment they
        # are written.
        "llm_usage_retention_days",
        "password_reset_token_ttl_minutes",
        "preview_link_default_ttl_hours",
        "preview_link_max_ttl_hours",
        "preview_link_retention_days",
        "publish_breaker_threshold",
        "publish_max_retries",
        "publish_stuck_after_seconds",
        "trigger_daily_content_limit",
        "trigger_disable_after_failures",
        "trigger_event_stuck_after_seconds",
        "trigger_scan_interval_seconds",
        "underperformance_max_age_hours",
        "velocity_benchmark_window_hours",
        "velocity_early_window_hours",
        "velocity_min_sample",
        "velocity_stall_window_hours",
        "webhook_disable_after_failures",
        "webhook_max_attempts",
    )
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("db_pool_timeout_seconds")
    @classmethod
    def _positive_seconds(cls, v: float) -> float:
        """Separate from :meth:`_positive` only because this one is a float.

        Zero would not mean "no wait" here — SQLAlchemy takes it as one, and a
        pool that refuses to wait at all turns every burst into an error.
        """
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("digest_send_weekday")
    @classmethod
    def _weekday(cls, v: int) -> int:
        if not 0 <= v <= 6:
            raise ValueError("must be 0 (Monday) through 6 (Sunday)")
        return v

    @field_validator("digest_send_hour")
    @classmethod
    def _hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("must be an hour of the day, 0-23")
        return v

    @field_validator("content_quality_min_score")
    @classmethod
    def _quality_floor(cls, v: int) -> int:
        """A floor on a 0–100 score has to be on the same scale as the score.

        Both ends matter. Below zero is not a lenient gate, it is a gate that
        can never fire — which is what ``content_quality_gate_enabled`` is for,
        and a disabled gate spelled as a number nobody would recognise. Above
        100 is worse: no piece can reach it, so every draft in the install
        stops being submittable at once, and the error names a score the
        reviewer can see on screen and cannot act on.
        """
        if not 0 <= v <= 100:
            raise ValueError(
                "must be a score between 0 and 100 — set "
                "CONTENT_QUALITY_GATE_ENABLED=false to turn the gate off"
            )
        return v

    @field_validator("digest_window_days", "schedule_max_horizon_days")
    @classmethod
    def _at_least_a_day(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be at least 1 day")
        return v

    @field_validator(
        # Zero is a real setting here: keep no unused ideas, so every scan
        # prunes what the last one suggested and did not use. Negative is not —
        # it would make `_prune_ideas` compute an excess larger than the number
        # of rows there are to prune.
        "autopilot_ideas_cap",
        # Zero is a real choice: a hard cap at `db_pool_size` with no burst.
        "db_max_overflow",
        "publish_request_retries",
        "publish_rate_limit_max_defer_seconds",
        # Zero is a real setting for both: a cool-down of zero means the breaker
        # counts failures and never actually skips, which is the honest way to
        # watch it before turning it on.
        "publish_breaker_cooldown_seconds",
        "publish_breaker_max_cooldown_seconds",
        "schedule_past_grace_seconds",
        # Zero disables the slow-query log, which is the spelling every other
        # threshold here uses for "off".
        "db_slow_query_ms",
    )
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be zero or positive")
        return v

    @field_validator(
        # Zero disables the ceiling, which is PostgreSQL's own spelling for it.
        "db_statement_timeout_seconds",
        # Zero is libpq's spelling for "no bound on connecting" too.
        "db_connect_timeout_seconds",
        "publish_retry_defer_seconds",
        "publish_retry_max_defer_seconds",
    )
    @classmethod
    def _non_negative_seconds(cls, v: float) -> float:
        """Separate from :meth:`_non_negative` only because these are floats.

        Zero is allowed and means something: no wait between publication-level
        attempts, which is how this behaved before the backoff existed.
        """
        if v < 0:
            raise ValueError("must be zero or positive")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_not_default_in_production(cls, v: str, info: ValidationInfo) -> str:
        """Refuse to start in production with a placeholder or weak JWT secret.

        Not being the default was never the same thing as being strong. This
        signs every session token Herald issues, with HMAC-SHA256 by default,
        and RFC 7518 §3.2 requires a key at least as long as the hash output —
        256 bits — for exactly one reason: anyone holding a single issued token
        can brute-force a shorter key offline, and the key is what the whole
        auth scheme rests on. PyJWT warns about it and carries on, which is the
        wrong moment to find out. ``JWT_SECRET=hunter2`` passed the old check.
        """
        env = (info.data.get("environment") or "development").lower()
        if env not in {"production", "prod"}:
            return v
        if v == "change-me-to-a-long-random-string":
            raise ValueError(
                "JWT_SECRET must be changed from its default value in production"
            )
        if len(v.encode("utf-8")) < _MIN_JWT_SECRET_BYTES:
            raise ValueError(
                f"JWT_SECRET must be at least {_MIN_JWT_SECRET_BYTES} bytes in "
                "production — a shorter key can be recovered offline from any "
                'token Herald has issued. Generate one with: python -c '
                '"import secrets; print(secrets.token_urlsafe(48))"'
            )
        return v

    @field_validator("token_encryption_key")
    @classmethod
    def _token_encryption_key_present_and_valid_in_production(
        cls, v: str, info: ValidationInfo
    ) -> str:
        """Refuse to start in production without a usable credential key.

        ``app.services.crypto.encrypt_credentials`` already refuses to write a
        platform credential in the clear when this is unset, so nothing leaks
        either way. What it cannot do is tell anyone *early*: the refusal
        arrives as a 500 the first time someone connects a platform, which may
        be weeks after the deploy that dropped the key, and it arrives at the
        user rather than at the operator who can fix it.

        Worse, it is only the *write* side that fails closed. A box running
        without the key still serves every stored connection, because a blob
        with no ``fernet:v1:`` prefix is read as plaintext JSON — so a key lost
        in a .env edit turns "credentials are encrypted at rest" into something
        nobody finds out is false until they look.

        Validated as a Fernet key, not merely as non-empty, for the same
        reason: a malformed one is indistinguishable from a good one until the
        moment it is used.

        The value may hold **several** keys, comma- or whitespace-separated,
        newest first — that is how a key is rotated without every stored
        credential going unreadable at once (see ``app.services.crypto``). Every
        one of them is validated, because a typo in the *old* key is the one
        nobody would notice: it only matters while rows are still encrypted
        under it, which is exactly the window where the sweep needs it to work.
        """
        env = (info.data.get("environment") or "development").lower()
        if env not in {"production", "prod"}:
            return v
        import re

        keys = [k for k in re.split(r"[,\s]+", v.strip()) if k]
        generate = (
            'generate one with: python -c "from cryptography.fernet import '
            'Fernet; print(Fernet.generate_key().decode())"'
        )
        if not keys:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be set in production — without it "
                "Herald cannot store a platform credential, and any credential "
                f"already stored is read back in the clear; {generate}"
            )
        from cryptography.fernet import Fernet

        for index, key in enumerate(keys):
            try:
                Fernet(key.encode("utf-8"))
            except (ValueError, TypeError) as exc:
                where = "TOKEN_ENCRYPTION_KEY" if index == 0 else (
                    f"key {index + 1} of TOKEN_ENCRYPTION_KEY"
                )
                raise ValueError(
                    f"{where} is not a valid Fernet key — {generate}"
                ) from exc
        return v

    @field_validator("bcrypt_rounds")
    @classmethod
    def _bcrypt_rounds_in_range_and_strong_in_production(cls, v: int, info: ValidationInfo) -> int:
        """Keep the work factor inside bcrypt's range, and at 12+ in production.

        The setting exists to let the tests drop to 4 (see the field comment).
        That is a fine thing to do to a suite and a catastrophic thing to do to
        a live password database — at 4 the whole cost of a guess is ~1ms, and
        an offline attacker with the hashes gets a ~250x discount on every one
        of them. Production is held to the default rather than trusted to
        re-state it, so the escape hatch cannot follow a copied .env onto a
        real box.
        """
        if not _BCRYPT_MIN_ROUNDS <= v <= _BCRYPT_MAX_ROUNDS:
            raise ValueError(
                f"BCRYPT_ROUNDS must be between {_BCRYPT_MIN_ROUNDS} and "
                f"{_BCRYPT_MAX_ROUNDS} — bcrypt refuses anything outside it"
            )
        env = (info.data.get("environment") or "development").lower()
        if env in {"production", "prod"} and v < _BCRYPT_PRODUCTION_MIN_ROUNDS:
            raise ValueError(
                f"BCRYPT_ROUNDS must be at least {_BCRYPT_PRODUCTION_MIN_ROUNDS} "
                "in production — a lower work factor is a test-suite shortcut, "
                "and it discounts every offline guess against the stored hashes"
            )
        return v

    @field_validator(
        "autopilot_auto_publish_confidence",
        "headline_winner_margin",
        "underperformance_threshold",
        "velocity_stall_ratio",
    )
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("must be between 0 and 1")
        return v

    @property
    def api_base_url(self) -> str:
        """Absolute base for URLs Herald gives to other systems, no trailing slash."""
        return (self.public_api_url or self.frontend_url).rstrip("/")

    @property
    def cors_origins(self) -> list[str]:
        """The CORS allow-list, split from the comma-separated setting.

        A list rather than the raw string because Starlette's middleware wants
        one, and blank entries are dropped so a trailing comma in an env file
        does not become an origin that matches nothing.

        In production the list is also filtered: ``*`` and any plaintext
        ``http://`` origin are dropped, with a warning naming each one. Both are
        development conveniences that mean something much worse on a live box —
        ``*`` lets any site on the internet read an authenticated response, and
        an ``http://`` origin is one downgrade away from the same thing.

        Filtered rather than refused at startup, which was the other candidate.
        A hard error is the better signal in general, and it is the wrong trade
        here: this property is read while the app is being built, so raising
        turns a too-broad CORS entry — which production does not even use, since
        Caddy serves the SPA and the API from one origin — into a box that will
        not boot. Dropping the entry fails towards the strict policy and leaves
        the operator a log line to act on. Localhost is left alone: it is
        already unreachable from anywhere that matters, and an operator running
        an SSH tunnel to debug a live box is a real thing to do.
        """
        origins = [o.strip() for o in self.backend_cors_origins.split(",") if o.strip()]
        if not self.is_production:
            return origins

        kept, dropped = [], []
        for origin in origins:
            if origin == "*" or (
                origin.startswith("http://") and not _is_loopback(origin)
            ):
                dropped.append(origin)
            else:
                kept.append(origin)
        if dropped:
            logging.getLogger(__name__).warning(
                "dropped %d insecure CORS origin(s) in production: %s — "
                "set BACKEND_CORS_ORIGINS to https:// origins only",
                len(dropped),
                ", ".join(dropped),
            )
        return kept

    @property
    def is_production(self) -> bool:
        """Whether the production rules apply — the switch, in one place.

        Both spellings are accepted because both get typed into env files, and a
        ``ENVIRONMENT=prod`` box silently running the development rules is the
        failure this guards: docs open, debug tracebacks on, weak-secret checks
        skipped.
        """
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is read once per process)."""
    return Settings()


settings = get_settings()

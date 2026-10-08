# H032 — Fence & Boundary Audit (M6)

**Date:** 2026-10-06
**Methodology:** M6 — fence & boundary audit (auth fences, tenant isolation, input validation, rate limiting, IDOR)
**Scope:** Full backend API surface — 14 router files, 124 endpoints, key services, models, schemas, middleware

---

## Summary

The Pulse (DoAide Pulse) backend is **exceptionally well-secured**. Every
authenticated endpoint uses `get_current_user` or `require_scope`. Every
resource access verifies ownership with a 404 (not 403) to prevent
enumeration. Every public endpoint is rate-limited. Input schemas carry
`max_length`, `ge`/`le`, and `extra="forbid"` constraints throughout. SSRF
protections resolve hostnames and check against private/loopback/link-local
ranges at both creation and delivery time.

One low-severity hardening item was found and fixed.

---

## Finding 1: Unbounded string path parameters on public endpoints

**Severity:** LOW (defence-in-depth hardening)
**Files:** `backend/app/routers/triggers.py`, `backend/app/routers/content.py`

**Problem:** Two unauthenticated endpoints accepted string path parameters
without explicit `Path(max_length=...)` constraints:

- `POST /triggers/inbound/{token}` — trigger token (expected: 43 chars)
- `GET /content/preview/{token}` — preview link token (expected: 43 chars)

While web servers and the body-size middleware impose practical URL length
limits, explicit constraints are the canonical FastAPI mechanism for input
validation and make the bound visible in the OpenAPI spec.

**Fix:** Added `Path(max_length=64)` to the trigger inbound token (matching
the `String(64)` column constraint) and `Path(max_length=256)` to the
preview link token (generous bound for the 43-char base64 value, since the
lookup is by hash).

---

## Audit checklist — what was verified

### Auth fences (every non-public endpoint requires authentication)

| Router | Endpoints | Auth mechanism | Status |
|--------|-----------|----------------|--------|
| `auth.py` | 7 | `get_current_user` on `/me` routes; public routes rate-limited | PASS |
| `projects.py` | 12 | `get_current_user` + `owned_project`; feed public + rate-limited | PASS |
| `content.py` | ~30 | `get_current_user` + `_owned_content`; preview public + rate-limited | PASS |
| `machine.py` | 4 | `require_scope` (API key auth, project from key not request) | PASS |
| `triggers.py` | 8 | `get_current_user` + `_owned`; inbound uses URL token + HMAC | PASS |
| `webhooks.py` | 9 | `get_current_user` + `_owned`; `/events` public + rate-limited | PASS |
| `settings.py` | 4 | `get_current_user` + ownership filter | PASS |
| `analytics.py` | 12 | `get_current_user` + `user.id` filter on all queries | PASS |
| `calendar.py` | ~6 | `get_current_user` + `user.id` filter; window capped at 365 days | PASS |
| `tags.py` | ~6 | `get_current_user` + project-join filter | PASS |
| `metrics.py` | 1 | `get_current_user`; `user.id` passed to service | PASS |
| `templates.py` | 7 | `get_current_user` + `_owned` (user_id check) | PASS |
| `revisions.py` | 4 | `get_current_user` + `_owned` (project.user_id check) | PASS |
| `translations.py` | 5 | `get_current_user` + `_owned`; translate rate-limited | PASS |
| `api_keys.py` | 5 | `get_current_user` + `_owned_key`; `/scopes` public + rate-limited | PASS |
| `misc.py` | 2 | `/health` public + rate-limited; `/health/detail` authenticated | PASS |

### Tenant isolation (ownership checks on all resource access)

Every router uses a local `_owned()` helper that returns 404 for both
missing and not-owned resources (anti-enumeration pattern). Patterns
verified:

- `content.project.user_id != user.id` (content, revisions, translations)
- `template.user_id != user.id` (templates)
- `webhook.user_id != user.id` (webhooks)
- `key.user_id != user.id` (API keys)
- `trigger.project.user_id != user.id` (triggers)
- `Project.user_id == user_id` in WHERE clauses (analytics, metrics, velocity, digest)
- Machine API reads `project_id` from the API key, never from the request body

### Input validation

- All integer path/query params use `RowId` (bounded to 2^31−1), `QueryRowId`, `ListOffset`
- Body size middleware caps requests at 1 MB with JSON depth limit of 32
- Pydantic schemas use `max_length` on all string fields, `ge`/`le` on numerics
- `ConfigDict(extra="forbid")` on all input schemas prevents extra field injection
- `reject_nulls()` utility catches null values on NOT NULL columns (prevents 500s)
- Resource count caps: 20 triggers/project, 20 webhooks/user, 20 API keys/project, templates per user

### Rate limiting

- All 10 public endpoints have explicit `@limiter.limit(...)` decorators
- AI/LLM endpoints (generate, repurpose, edit, headlines, translate, template use) rate-limited per account
- Outbound probe endpoints (ping, redeliver, verify connection) rate-limited per account
- Three key functions: `client_key` (IP), `account_key` (JWT subject), `api_key_key` (key prefix)
- `swallow_errors=True` ensures Redis outage doesn't take down the app

### IDOR (Insecure Direct Object References)

- 404 returned for both "not found" and "not yours" on every resource endpoint
- Machine API project ID comes from the API key row, not the request
- Bulk operations (`_owned_content_map`) filter ownership in the WHERE clause
- Velocity curve detail filters `publication_id` in the query with `user_id`

### Additional security measures verified

- **Password handling:** bcrypt with SHA-256 pre-hash for >72 byte passwords; dummy hash for timing safety on login
- **Token invalidation:** `tokens_valid_from` timestamp vs JWT `iat` claim
- **Password reset:** 32-byte token, SHA-256 hashed before storage, single-use, revokes all previous tokens, sets `tokens_valid_from`
- **Credential encryption:** Fernet (AES-128-CBC + HMAC) at rest; refuses plaintext in production; multi-key rotation support
- **SSRF protection:** Webhook URLs resolved and checked against private/loopback/link-local/reserved ranges at both creation and delivery; no redirect following
- **Webhook signing:** HMAC-SHA256 over `{timestamp}.{body}` (replay-resistant); GitHub signature scheme also supported
- **Security headers:** CSP, HSTS (TLS only), X-Frame-Options DENY, Referrer-Policy, X-Content-Type-Options
- **CORS:** Explicit origins, no credentials
- **Registration:** Closed by default; invite token uses constant-time comparison
- **API keys:** Stored as digest (SHA-256); plaintext returned only at creation; never returned again
- **Preview links:** Token hashed before storage; expiration; revocation; deactivated account check

---

## Pre-existing test failures (not introduced by this audit)

26 tests were already failing before this audit (environment/config issues
and pre-existing bugs unrelated to security fences). The two files changed
in this audit have their tests passing (66/66 in `test_triggers.py` and
`test_content.py`).

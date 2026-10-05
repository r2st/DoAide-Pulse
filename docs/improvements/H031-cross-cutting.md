# H031 — Cross-Cutting Concerns (M4), Pass 5

**Date:** 2026-10-06
**Methodology:** M4 — cross-cutting concerns (auth bypasses, missing validation, inconsistent middleware, CORS/CSP gaps, missing rate limits)
**Scope:** 2 findings, both MEDIUM

---

## Finding 1: Log injection via unsanitised `X-Request-ID` header

**Severity:** MEDIUM
**File:** `backend/app/main.py`

**Problem:** The `RequestIDMiddleware` accepted the client-supplied
`X-Request-ID` header verbatim:

```python
request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
```

This value was placed directly into the logging context (`request_id_var`) and
emitted in every log line via the format string
`%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s`.

A client sending `X-Request-ID: abc\n2026-10-06 ERROR [x] app.security: admin bypass`
could inject fake log lines that look indistinguishable from real entries. This
enables log spoofing (hiding real events in noise, planting false evidence for
incident response) and can break log parsers or alerting that assume
one-line-per-event.

The value was also echoed in the 500 error handler's JSON response body and in
the `X-Request-ID` response header, so control characters reached the caller
too.

**Fix:** Added sanitisation in `RequestIDMiddleware`:
- Strip all non-printable-ASCII characters (anything outside `0x20–0x7E`)
  via a compiled regex
- Cap length at 64 characters
- Fall back to a generated id if sanitisation leaves an empty string

**Tests:** `test_request_id_newlines_are_stripped`,
`test_request_id_control_chars_are_stripped`,
`test_request_id_truncated_to_64_chars`,
`test_empty_request_id_after_sanitisation_gets_generated`,
`test_clean_request_id_passes_through`,
`test_request_id_filter_does_not_inject_newlines`

---

## Finding 2: API key rotation bypasses the per-project key cap

**Severity:** MEDIUM
**File:** `backend/app/routers/api_keys.py`

**Problem:** `POST /api-keys` enforces `MAX_KEYS_PER_PROJECT` (20) before
minting a new key. `POST /api-keys/{id}/rotate` did not — it called
`api_keys.rotate()` unconditionally. With `grace_hours > 0`, both the old and
new key remain live during the grace window.

Repeated rotations with grace periods accumulated arbitrarily many live keys:
key A→B (both live), B→C (three live), C→D (four live), and so on. This
bypassed the cap that exists because "a list nobody can read is a list nobody
audits" (the create endpoint's own rationale). A compromised session could mint
dozens of live credentials before revocation.

Immediate rotation (`grace_hours=0`) was not affected — the old key is revoked
in the same transaction, so the live count stays the same.

**Fix:** Added a live-key count check in `rotate_api_key` when
`grace_hours > 0`, matching the check in `create_api_key`. The guard is skipped
for `grace_hours=0` because the old key is revoked immediately and the count
does not increase.

**Tests:** `test_rotation_with_grace_blocked_at_cap`,
`test_rotation_without_grace_succeeds_at_cap`,
`test_rotation_with_grace_succeeds_below_cap`

---

## Summary of changes

| File | Change |
|------|--------|
| `backend/app/main.py` | Sanitise `X-Request-ID`: strip control chars, cap at 64, fall back to generated id |
| `backend/app/routers/api_keys.py` | Check live-key cap before graceful rotation |
| `backend/tests/test_cross_cutting_h031.py` | 9 new tests covering both findings |
| `docs/improvements/H031-cross-cutting.md` | This report |

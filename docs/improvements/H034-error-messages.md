# H034 — Error Messages (M19)

**Date:** 2026-10-06
**Scope:** backend error messages — punctuation, internal leaks, format consistency

## Audit findings

Audited all ~100 `HTTPException` raises across 14 router files, `deps.py`,
`github_client.py`, and the error infrastructure (`schemas/errors.py`,
`services/error_class.py`, `services/errors.py`).

### 1. Inconsistent punctuation on short error messages

**17 "not found" messages** across 12 files ended without a period
(`"Content not found"`) while every other sentence-form error used one
(`"Registration is closed."`).  Two auth messages had the same issue:
`"Email already registered"` and `"Incorrect email or password"`.

**Files changed:** `deps.py`, `auth.py`, `content.py`, `projects.py`,
`calendar.py`, `revisions.py`, `translations.py`, `tags.py`, `templates.py`,
`triggers.py`, `api_keys.py`, `analytics.py`, `webhooks.py`.

### 2. GitHub `_json` error leaking response content-type header

`github_client._json()` included the remote server's `Content-Type` header
value in the error message (`"GitHub returned a non-JSON body for /repos/… (text/html; charset=utf-8)"`).
That header can expose server software or proxy details and adds no
actionable information for the user.

**Fix:** Moved the content-type to a `logger.warning()` call (available to
operators in logs) and replaced the user-facing message with a generic
sentence about an unexpected response.

### 3. Template PATCH `ValueError` catch unbounded

The `ValueError` fallback in the template PATCH handler passed `str(exc)`
directly to the response.  While the values reaching this arm are typically
short validation messages, a future change could surface Pydantic internals
or an unbounded string.

**Fix:** Capped `str(exc)` at 300 characters and falls back to
`"Invalid template data."` for anything longer.

## What was already solid

- The `ErrorOut` schema enforces `{"detail": string}` across the whole surface,
  and `test_error_bodies_match_the_declared_shape` sweeps every authenticated
  and id-addressed endpoint to verify it.
- `sanitize_unexpected_error()` and `friendly_network_error()` prevent
  catch-all arms from leaking class names, file paths, or connection strings.
- `clip_error()` and `redact()` bound stored error messages and strip
  credentials.
- Domain exceptions (`TranslationError`, `RevisionError`, `ScheduleError`,
  `ApiKeyError`) document themselves as user-facing.
- The error class taxonomy (`ErrorClass.RETRYABLE / PERMANENT / UNKNOWN`) is
  well-designed and conservative.
- The unhandled-exception handler includes `request_id` in both the log line
  and the response body.

## Changes

| File | Change |
|------|--------|
| `services/github_client.py` | `_json` error no longer leaks `Content-Type`; logged instead |
| `routers/templates.py` | `ValueError` fallback capped at 300 chars |
| `routers/auth.py` | Period added to "Email already registered" and "Incorrect email or password" |
| `deps.py` | Period added to "Project not found" |
| 11 router files | Period added to all "X not found" messages |

## Tests added

`tests/test_h034_error_messages.py` — 72 cases:
- Parametrised sweep: every id-addressed 404 message ends with punctuation
- Auth login/register error punctuation
- `_json` no longer leaks `Content-Type` header
- `sanitize_unexpected_error` still hides file paths and connection strings
- `_validation_detail` does not leak Pydantic class names
- Template PATCH caps overlong `ValueError` messages

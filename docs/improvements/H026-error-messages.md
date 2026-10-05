# H026 — Error Messages

**Methodology:** M19 (error messages)  
**Pass:** 4  
**Date:** 2026-10-06  
**Findings:** 1

---

## Finding 1: Template PATCH leaks Pydantic internals in 422 detail

**File:** `backend/app/routers/templates.py`  
**Lines:** 309–314 (before fix)

**Problem:** The template PATCH endpoint merges the stored template with the
incoming update and validates the result by constructing `TemplateCreate(**merged)`.
When the merged values fail a validator, the `ValueError` catch sends `str(exc)` as
the HTTP 422 `detail`:

```python
try:
    checked = TemplateCreate(**merged)
except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
    ) from exc
```

Pydantic's `ValidationError` inherits from `ValueError`, so this catch intercepts
it.  `str(ValidationError)` produces a verbose multi-line dump:

```
1 validation error for TemplateCreate
  Value error, This template uses {{headline}}, which is neither ...
    For further information visit https://errors.pydantic.dev/2.13/v/value_error
```

This leaks:

- **Internal model name** (`TemplateCreate`) — an implementation detail the user
  has no use for.
- **Pydantic docs URL** — confirms the framework and version to an attacker.
- **Field path notation** (`variables.0.name`) — internal schema structure.

The frontend displays `detail` directly in toast notifications, so the full
dump reached the user.

**Fix:** Catch `ValidationError` specifically before the broader `ValueError`
and extract just the human-readable `msg` from each error entry, stripping
Pydantic's `"Value error, "` prefix from custom validators.  Added a
`_validation_detail()` helper that joins the clean messages.

After the fix, the same error produces:

```
This template uses {{headline}}, which is neither a variable you declared nor
one Pulse fills in. Declare it, or fix the spelling.
```

**Test:** `tests/test_template_patch_error_format.py` — three cases covering
undeclared placeholders, duplicate variable names, and the variable-count ceiling.
Each asserts the detail is a string (not a list), contains no model name, no
Pydantic URL, and carries the relevant identifier from the validator's own message.

---

## Audit notes

The rest of the error-message surface is in excellent shape:

- **Custom domain exceptions** (`ScheduleError`, `ApiKeyError`, `EditUnavailable`,
  `TranslationError`, `GitHubError`, `WebhookUrlError`, `RequeueError`,
  `RevisionError`) all carry handwritten, user-facing messages.
- **The global 500 handler** (`main.py`) returns a generic message with a request
  id for correlation — no stack traces or internals.
- **Credential errors** are passed through `redact()` to strip token values and
  `clip_error()` to bound length.
- **Network errors** from `httpx` go through `friendly_network_error()`, which
  maps exception types to plain-English descriptions.
- **Unexpected catch-alls** use `sanitize_unexpected_error()`, which returns only
  the exception type name.
- **Error format is consistent**: every failure is `{"detail": "..."}` (the
  `errors.py` schema documents this), and the frontend handles both string and
  array formats from the API client.

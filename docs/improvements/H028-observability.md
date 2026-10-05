# H028 — Observability (M11, Pass 5)

**Date:** 2026-10-06
**Methodology:** M11 — observability gaps (missing structured logging, missing metrics, insufficient error context, blind spots in monitoring)

## Findings

### Finding 1: SMTP failure/success logs missing recipient context

**File:** `backend/app/services/mailer.py:79–85`

**Bug:** The SMTP error log line (`logger.error("SMTP send failed: %s", exc)`) included the exception but not the recipient or subject. The success log (`logger.info("sent %r", subject)`) included the subject but not the recipient. When a delivery fails — password reset or weekly digest — the on-call operator sees which SMTP error occurred but cannot tell *which user* was affected without cross-referencing other logs by request ID.

**Example:**
```
# Before
ERROR SMTP send failed: (535, b'bad credentials')
INFO  sent 'Password reset'

# After
ERROR SMTP send to=alice@example.com subject='Password reset' failed: (535, b'bad credentials')
INFO  sent 'Password reset' to alice@example.com
```

**Impact:** Two callers use `mailer.send`: password reset (auth router) and weekly digest. An SMTP failure during a digest sweep affects many users; without the recipient in the log, the operator must grep correlated request IDs to identify who missed their email.

**Fix:** Added `to` and `subject` to the error log, and `to` to the success log.

### Finding 2: Connection invalidation is a silent state change

**File:** `backend/app/services/publishing_service.py:500–517`

**Bug:** `_mark_connection_invalid` transitioned a platform connection from CONNECTED to INVALID and stored the error message, but emitted no log line. This state change blocks all future publishing for that user+platform until they reconnect. The caller (`execute()`, line 826) catches `CredentialError` and calls this function, but the only log visible was the generic `_fail()` message which doesn't mention the connection state change.

**Example:**
```
# Before — no log from _mark_connection_invalid; operator sees only:
ERROR publication 42 failed: CredentialError(...)

# After — the state change is explicit:
WARNING connection 15 for user 7 on devto marked invalid: Token rejected by platform
ERROR   publication 42 failed: CredentialError(...)
```

**Impact:** When a user reports "my posts stopped publishing", the operator had no log entry for when/why the connection was invalidated. The WARNING now provides the connection ID, user ID, platform, and error reason, making the root cause immediately visible.

**Fix:** Added `logger.warning(...)` after the status and error fields are set, including connection ID, user ID, platform, and clipped error.

## Files Changed

| File | Change |
|------|--------|
| `backend/app/services/mailer.py` | Added recipient to error and success log lines |
| `backend/app/services/publishing_service.py` | Added WARNING log on connection invalidation |
| `backend/tests/test_h028_observability.py` | 11 test cases covering both findings |

## Verification

All 11 new tests pass. Existing `test_mailer.py` (7 tests) passes with no regressions.

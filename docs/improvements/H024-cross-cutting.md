# H024 — Cross-Cutting Concerns (M4), Pass 4

**Date:** 2026-10-06
**Methodology:** M4 — cross-cutting concerns (logging, error handling, input validation, configuration, auth)
**Scope:** 1 finding, MEDIUM

---

## Finding 1: Content PATCH `scheduled_for` does not reset publication retry state

**Severity:** MEDIUM
**File:** `backend/app/routers/content.py`

**Problem:** Every path that moves a publication to a new `scheduled_for` resets
`publication.attempts = 0` and `publication.error = None` — the calendar's
`PATCH /calendar/content/{id}` does it explicitly with the comment "A move is a
fresh start: a row that had burned two retries should not arrive at its new slot
with one left", and the retry endpoint (`_rearm`) does the same.

The content `PATCH /content/{id}` endpoint also moves publications when
`scheduled_for` changes (lines 1966–1975), updating their `scheduled_for` and
`status` — but it did NOT reset `attempts` or `error`. A publication that had
burned 2 of 3 retries at its old time would arrive at the new time with only 1
retry left, and display a stale error message from the old attempt.

**Fix:** Added `publication.attempts = 0` and `publication.error = None` to the
content PATCH's `scheduled_for` handler, matching the calendar reschedule
behaviour.

**Tests:** `test_content_patch_scheduled_for_resets_attempts`,
`test_content_patch_scheduled_for_clears_error`,
`test_content_patch_scheduled_for_does_not_touch_settled_publications`

---

## Summary of changes

| File | Change |
|------|--------|
| `backend/app/routers/content.py` | Reset `attempts=0` and `error=None` on publications moved by content PATCH |
| `backend/tests/test_cross_cutting_h024.py` | 3 new tests covering the fix |

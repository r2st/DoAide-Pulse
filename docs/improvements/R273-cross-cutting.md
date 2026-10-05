# R273 — Cross-Cutting Concerns (M4)

**Date:** 2026-10-06
**Methodology:** M4 — cross-cutting concerns (logging, error handling, input validation, configuration, auth)
**Scope:** 3 findings, all MEDIUM

---

## Finding 1: Content PATCH missing `timezone` for `scheduled_for`

**Severity:** MEDIUM
**Files:** `backend/app/schemas/content.py`, `backend/app/routers/content.py`

**Problem:** Every endpoint that writes `scheduled_for` — `POST /publish`,
`POST /schedule`, `POST /bulk/publish`, `PATCH /calendar/content/{id}` —
accepts an optional `timezone` parameter and feeds it through
`scheduling.normalize(…, tz=…)` so that a naive wall-clock time like
`"2026-12-01T09:00"` is interpreted in the zone the user meant. The content
PATCH endpoint (`PATCH /content/{id}`) accepted `scheduled_for` but had no
`timezone` field, so naive datetimes were always read as UTC — the one surface
where a user editing a schedule could not say which clock they were using.

**Fix:** Added `timezone: Timezone | None` to `ContentUpdate` schema and
threaded it through to `scheduling.normalize()` in the PATCH handler. Timezone
is popped from the data dict before the setattr loop (it is not a model
column).

**Tests:** `test_content_patch_accepts_timezone`,
`test_content_patch_timezone_shifts_naive_datetime`,
`test_content_patch_timezone_ignored_when_offset_present`,
`test_content_patch_bad_timezone_is_422`,
`test_content_patch_timezone_without_scheduled_for_is_harmless`

---

## Finding 2: `headline_sync` uses ad-hoc `str(exc)[:300]` instead of `clip_error()`

**Severity:** MEDIUM
**File:** `backend/app/services/headline_sync.py`

**Problem:** Every service that truncates error strings for storage or return
uses `clip_error()` from `services.errors` (2 000-char limit with an ellipsis
marker). `headline_sync._sync_one` used `str(exc)[:300]` in two places — a
magic number that disagreed with every other truncation site and dropped the
ellipsis that tells an operator the message was cut.

Also fixed a pre-existing test regression in
`test_a_headline_swap_reaches_the_reader.py`: the
`test_an_encryption_key_that_no_longer_fits_is_reported_per_destination` test
monkeypatched `_credentials_for` to raise `CredentialEncryptionError` directly,
bypassing the `CredentialError` wrapping that the real function performs. After
commit `1fb3a21` split the catch-all into `except PublishError` (WARNING) +
`except Exception` (ERROR), the test's exception went through the wrong arm and
the WARNING-level log assertion failed.

**Fix:** Replaced `str(exc)[:300]` with `clip_error(str(exc))` on both
`except PublishError` arms. Fixed the test to raise `CredentialError` (a
`PublishError` subclass) matching the real code path.

**Tests:** `test_headline_sync_uses_clip_error_not_manual_truncation`,
`test_headline_sync_credential_error_uses_clip_error`

---

## Finding 3: `auth.update_me` hand-rolls null check instead of `reject_nulls`

**Severity:** MEDIUM
**File:** `backend/app/routers/auth.py`

**Problem:** Every PATCH endpoint that loops `model_dump(exclude_unset=True)`
through `setattr` calls `reject_nulls(Model, data)` first, so an explicit
`null` on a NOT NULL column is a 422 rather than a 500 (scalar) or a silently
stored JSON null (JSON column). `auth.update_me` used the same setattr pattern
but hand-rolled a null check for only one field (`weekly_digest_enabled`). Any
future NOT NULL field added to `PreferencesUpdate` would have been unguarded.
The `_patch.py` module docstring itself called this out as a known gap.

**Fix:** Replaced the hand-rolled check with `reject_nulls(User, fields)`.
Updated the `_patch.py` docstring that referenced the old pattern.

**Tests:** `test_update_me_rejects_null_on_not_null_column`,
`test_update_me_allows_null_on_nullable_column`,
`test_update_me_reject_nulls_error_message_names_the_field`

---

## Summary of changes

| File | Change |
|------|--------|
| `backend/app/schemas/content.py` | Added `timezone` field to `ContentUpdate` |
| `backend/app/routers/content.py` | Pass `tz` to `scheduling.normalize()` in content PATCH |
| `backend/app/services/headline_sync.py` | `str(exc)[:300]` → `clip_error(str(exc))` (×2) |
| `backend/app/routers/auth.py` | `reject_nulls(User, fields)` replaces manual null check |
| `backend/app/routers/_patch.py` | Removed stale docstring referencing auth.update_me gap |
| `backend/tests/test_cross_cutting_r273.py` | 10 new tests covering all three findings |
| `backend/tests/test_a_headline_swap_reaches_the_reader.py` | Fixed pre-existing test regression (wrong exception type) |

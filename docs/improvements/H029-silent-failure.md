# H029 — Silent Failure Audit (M5, Pass 5)

**Result: CLEAN PASS**

## Methodology

M5 targets silent failures: swallowed exceptions, empty catch blocks, missing
error propagation, functions that fail without signaling, and data loss without
warning.

## Scan coverage

Every `except` handler across all service, task, and router modules was
examined — 160+ handlers in total. The scan covered:

- **Bare `except: pass` blocks** — three found, all intentional chained-parse
  fallthrough (try float, then try date) in `llm_router._retry_after`,
  `feeds._parse_date`, and `publishers.base._retry_after`.
- **`except Exception` with return/continue** — every instance either logs with
  `logger.exception`/`logger.warning` before returning, or carries an extensive
  comment documenting why the exception is intentionally absorbed.
- **`contextlib.suppress`** — two uses in `llm_usage.record`, both on
  close/rollback of a session that is already broken (the primary failure is
  logged on the line above).
- **`db.get()` returning None** — every call site checks for `None` before
  accessing attributes.
- **Batch loops with per-item error handling** — every sweep (publish, metrics,
  headlines, triggers, digests, webhooks) isolates failures per item with
  `db.rollback()` + `logger.exception()` inside the loop body, and documents
  why one item's failure must not end the pass.

## Why this pass is clean

The codebase follows a consistent defensive pattern:

1. Every `except` that absorbs an exception logs it — usually at WARNING or
   higher, never at DEBUG where production would suppress it.
2. Every sweep that processes items in a loop rolls back and logs per item,
   with comments explaining the `PendingRollbackError` risk that motivated
   the rollback.
3. Return values that encode failure (`False`, `None`, empty list) are
   documented in docstrings and handled by callers.
4. The few places that intentionally return defaults on parse failure (date
   parsing, content-type lookup, format detection) affect non-critical display
   values, not data writes.

No findings to report.

---

Audited by Pulse improvement pass H029, methodology M5 (silent failure),
pass 5, cycle C94.

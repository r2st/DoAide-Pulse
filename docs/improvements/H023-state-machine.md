# H023 — State Machine Audit (M3, Pass 4)

**Date:** 2026-10-06
**Method:** M3 — state machine audit (transitions, guards, stuck states)
**Scope:** Webhook delivery lifecycle (`DeliveryStatus`), trigger event lifecycle (`TriggerEventStatus`)

Prior passes (1–3) focused on `ContentStatus` and `PublicationStatus`. This pass audits the remaining stateful entities: webhook deliveries, trigger events, translations, and connections. Two findings; both fixed.

---

## Finding 1: `requeue` accepts in-flight PENDING deliveries

**File:** `backend/app/services/webhooks.py` — `requeue()`
**Severity:** Medium — double-delivery and corrupted attempt counter

### Problem

`requeue()` unconditionally reset a delivery to PENDING with `attempts=0`, regardless of its current status. A PENDING delivery may be mid-attempt: a worker that has already claimed it (via the conditional UPDATE in `claim()`) is sending the POST right now. Calling `requeue` on such a delivery:

1. Overwrites the claim lease (`next_attempt_at`)
2. Resets the attempt counter
3. Allows a second worker (or the same `deliver()` call in the redeliver endpoint) to claim and send the same payload

The receiver sees two POSTs with the same delivery id and signature — a double-delivery that looks like a legitimate retry.

### Fix

Guard `requeue()` to reject PENDING deliveries with a `RequeueError`. The redeliver endpoint returns 409 Conflict when the delivery is still in flight.

### Valid transitions (after fix)

```
PENDING  →  DELIVERED  (success)
PENDING  →  FAILED     (retries spent or terminal error)
PENDING  →  PENDING    (backoff: next attempt scheduled)
FAILED   →  PENDING    (requeue by user)
DELIVERED → PENDING    (requeue by user — replay)
```

The `PENDING → PENDING` via `requeue` path is now blocked.

---

## Finding 2: Daily trigger limit ignores in-flight events

**File:** `backend/app/services/triggers.py` — `_daily_count()`
**Severity:** Low — limit exceeded by small amounts under concurrent load

### Problem

`_daily_count()` counted only `GENERATED` events when checking the daily trigger limit. Events in `RECEIVED` state (committed by `record()` but not yet finished generating) were invisible to the counter. When multiple signals arrive concurrently for the same project:

1. All pass `record()` — committed as RECEIVED
2. All check `_daily_count()` — see 0 GENERATED events
3. All proceed to generate content

With a limit of 5, 8 concurrent fires could produce 8 pieces.

### Fix

Count both `GENERATED` and `RECEIVED` events in `_daily_count()`. The caller's own event (just committed by `record()` and not yet generating) is excluded via `exclude_event_id` so it doesn't count against itself.

### Valid transitions (trigger events)

```
(new)     →  RECEIVED   (record — committed before generation)
RECEIVED  →  SKIPPED    (dedupe, limit, autopilot off, etc.)
RECEIVED  →  GENERATED  (content created successfully)
RECEIVED  →  FAILED     (generation crashed)
RECEIVED  →  GENERATED  (reclaim_stuck_events — adopted)
RECEIVED  →  FAILED     (reclaim_stuck_events — abandoned)
```

All four terminal states (SKIPPED, GENERATED, FAILED) are reachable. RECEIVED is the only non-terminal state, and `reclaim_stuck_events` settles orphaned RECEIVED rows.

---

## Entities audited with no issues

- **TranslationStatus** (PENDING → READY / NEEDS_REVIEW / FAILED): All transitions guarded. `upsert_pending` and `translate` set status and quality_issues atomically. `for_publishing` correctly refuses PENDING, FAILED, and quality-flagged translations.
- **ConnectionStatus** (CONNECTED / INVALID / DISCONNECTED): Transitions are straightforward — set on upsert/verify/publish-failure. The `publish_recovery` sweep correctly re-arms publications only when a connection returns to CONNECTED.
- **DeliveryStatus** (PENDING → DELIVERED / FAILED): Claim-based concurrency control prevents double-delivery. Lease expiry handles crash recovery. The `_record_skipped` path correctly avoids counting a deactivated endpoint against `consecutive_failures`.

## Test coverage

`backend/tests/test_state_machine_h023.py` — 5 tests covering both findings.

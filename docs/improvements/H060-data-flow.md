# H060 — M2 computation correctness (Pass 6): data flow

**Methodology:** M2 — float precision, rounding, off-by-one, unit mismatch, formula bugs  
**Prior passes:** H027 (Pass 4), H035 (Pass 5)  
**Scope:** Backend services + frontend lib helpers — full data-flow surface

## Findings

### Bug 1 — `engagement_alerts._latest_totals` double-counts on same-second metrics

**File:** `backend/app/services/engagement_alerts.py:100-116`  
**Category:** duplicate-row join  

The subquery picked the latest metric per publication with
`func.max(ContentMetric.captured_at)`. If two `ContentMetric` rows share
the same `publication_id` and `captured_at` (race between the periodic
sweep and the refresh button, or sub-second DB clock granularity), the
join returned **both** rows and the accumulation loop double-counted
engagement and views.

**Example:** sweep creates a metric at 12:00:00, user clicks "refresh"
at 12:00:00 → two rows with identical `(pub_id, captured_at)` →
engagement reported as 14 instead of 7.

**Fix:** join on `func.max(ContentMetric.id)` instead. IDs are unique
and monotonically increasing, and the existing
`ix_content_metrics_pub_latest` index on `(publication_id, id)` covers
the query.

### Bug 2 — `llm_usage._weighted_mean` returns 0 instead of None

**File:** `backend/app/services/llm_usage.py:246-252`  
**Category:** misleading zero  

`_weighted_mean` returned `0` when no LLM calls had been recorded. The
identical function in `ops_metrics.py:220` returns `None` with an
explicit comment: *"an install that has never timed a publish has not got
an average latency of zero milliseconds, and zero is the one value that
would look like the fastest possible install on a dashboard."*

The `0` reached the `/api/v1/ops/metrics` response as
`avg_duration_ms: 0`, making a fresh install or a quiet window look like
instant LLM generation rather than "no data".

**Fix:** return `None`, matching `ops_metrics._weighted_mean`. Updated
type hint to `int | None`.

## Files changed

| File | Change |
|------|--------|
| `backend/app/services/engagement_alerts.py` | `max(captured_at)` → `max(id)` in `_latest_totals` |
| `backend/app/services/llm_usage.py` | `_weighted_mean` returns `None` when no calls |
| `backend/tests/test_h060_data_flow.py` | 4 tests covering both bugs |

## Files audited (no bugs found)

- `backend/app/services/content_pipeline.py` — quality gates, confidence comparison
- `backend/app/services/publishing_service.py` — retry backoff, weighted mean, UTM truncation
- `backend/app/services/content_engagement.py` — nullable field sums, trend merging
- `backend/app/services/digest.py` — Movement.change division, delta computation
- `backend/app/services/triggers.py` — schedule interval gating, calendar-day comparison
- `backend/app/services/signals.py` — SHA-256 digest key
- `backend/app/services/ops_metrics.py` — age_days, rate, weighted mean (correctly returns None)
- `backend/app/tasks/metrics_tasks.py` — keyset pagination, batched commits
- `backend/app/tasks/autopilot_tasks.py` — daily count, commit threshold
- `frontend/src/lib/analytics.js` — comparePeriods, platformShares, bestPlatform
- `frontend/src/lib/calendar.js` — bucketByDay, dropTarget, canDropOn
- `frontend/src/lib/readTime.js` — lengthPayoff, attentionShares
- `frontend/src/lib/editorStats.js` — roundHalfToEven, countWords, readMinutes
- `frontend/src/lib/format.js` — formatWhen, formatDuration, formatRate
- `frontend/src/pages/tools/SubjectLineTester.jsx` — scoreSubjectLine (clamped 0-100)

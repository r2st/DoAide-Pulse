# H035 — Computation Audit (M2, Pass 5)

**Date:** 2026-10-06
**Methodology:** M2 — computation correctness (float precision, rounding errors, off-by-one dates, currency math)

## Scope

Every computation-heavy module in the backend:

- **Analytics:** `analytics_service.py` — rates, averages, daily timelines, trend gains, read-time bands, token spend charts
- **Velocity:** `velocity.py` — growth curves, stall detection, peak gain, views-per-day, sliding-window maximum, Julian-day SQL
- **Quality:** `quality.py` — Flesch-Kincaid readability, code-ratio scoring, weighted quality score
- **SEO:** `seo.py` — keyword density, score deductions, meta-description bounds, heading hierarchy
- **Alerts:** `alerts.py` — underperformance ratio, stall-window days, leave-one-out median
- **Digest:** `digest.py` — cumulative counter subtraction, prior-window baselines, reader-minutes weighting
- **Scheduling:** `scheduling.py`, `cadence.py`, `learned_cadence.py` — DST handling, next-slot search, best-hour median
- **Engagement:** `content_engagement.py` — cross-platform merged timelines, cursor-based aggregation
- **LLM usage:** `llm_usage.py` — average duration, daily token bucketing
- **Triggers:** `triggers.py` — interval gating, whole-day comparison
- **Formats:** `formats.py` — character-limit splitting
- **Frontend (JSX):** no computation logic found

**Currency math:** no currency handling exists in the codebase; all "cost" is expressed as token counts.

## Findings

### Finding 1: `int()` truncation in underperformance alert percentage

**File:** `backend/app/services/alerts.py:121`

**Bug:** The underperformance alert message formatted the ratio as `int(ratio * 100)`, which truncates toward zero instead of rounding. A ratio of 0.459 displayed as "45% of your usual..." instead of the correct "46%".

**Example:**
```
observed=459, expected=1000
ratio = round(459 / 1000, 3) = 0.459

Before: int(0.459 * 100) = int(45.9) = 45   → "45% of your usual..."
After:  round(0.459 * 100) = round(45.9) = 46 → "46% of your usual..."
```

**Impact:** Every underperformance alert understated the percentage by up to 1 point. The underlying detection logic was correct — only the message the user read was wrong.

**Fix:** `int(ratio * 100)` → `round(ratio * 100)`.

### Finding 2: `int()` truncation in stall alert day count

**File:** `backend/app/services/alerts.py:146`

**Bug:** The stall alert message computed the window's day count as `int(window / 24) or 1`, which truncates toward zero. For a 36-hour stall window, `int(36/24) = 1` instead of the correct 2. For a sub-24-hour window (e.g. 12h), `int(12/24) = 0`, rescued to 1 by the `or 1` fallback — but the rescued path relied on a falsy-zero coincidence rather than an explicit floor.

**Example:**
```
window = 36 hours

Before: int(36 / 24) or 1 = int(1.5) or 1 = 1     → "last 1 day"
After:  max(1, round(36 / 24)) = max(1, 2) = 2     → "last 2 days"
```

**Impact:** Latent — the default stall window is 168 hours (exactly 7 days), so the truncation had no visible effect with default settings. A deployment that configured a non-multiple-of-24 window would have seen the understated day count.

**Fix:** `int(window / 24) or 1` → `max(1, round(window / 24))`.

## Areas Audited — No Issues Found

| Area | What was checked |
|------|-----------------|
| `_rate()` division | Guarded by `not denominator` and `reported <= 0` checks |
| `views_per_day` | Guarded by `age_hours <= 0` returning `None` |
| `read_minutes_for` | All call sites use the canonical 220-wpm function (H027 fixed the last inconsistency) |
| Quality score weights | Sum to 1.0 in both the 3-component and 2-component (no readability) paths |
| `code_points` denominator | `1.0 - 0.40 = 0.6`, never zero |
| `seo_score` bounds | `max(0, score)` on line 618; maximum total deduction is 95 from 100 |
| Daily chart bucket count | All three timeline functions use `range(days + 1)` consistently |
| `window_start` midnight floor | Explicitly documented, prevents partial first-day buckets |
| Digest gains subtraction | Cumulative counters with monotonic clamp; baselines fetched separately |
| Trend `_daily_gains` | Running `max()` clamp ensures non-negative deltas |
| Velocity sliding-window | `observed_peak_gain` correctly reuses `end` pointer without reset |
| Velocity Julian-day SQL | `~2.1e11` in float64 gives ~0.1ms resolution; 1ms slack added |
| Scheduling DST | Spring gap → error; fall overlap → earlier interpretation (`fold=0`) |
| `next_slot` day iteration | `<= after` with strict inequality; 28-day search with fallback |
| Learned cadence `_best_bucket` | `max()` returns first match, which is the smallest key from `sorted()` |
| Engagement merged timeline | `round(hours, 2)` applied consistently to both stamps and cursor |
| Trigger interval gating | `round(hours/24)` only reached when `hours % 24 == 0`; always exact |
| LLM token chart | `tokens_per_call` guarded by `if day_calls` |
| Float equality comparisons | None found in computation code |

## Files Changed

| File | Change |
|------|--------|
| `backend/app/services/alerts.py` | `int(ratio * 100)` → `round(ratio * 100)`; `int(window / 24) or 1` → `max(1, round(window / 24))` |
| `backend/tests/test_h035_computation.py` | 6 test cases covering both findings |

## Test Results

- 6 new tests: all pass
- 74 existing alert tests: all pass

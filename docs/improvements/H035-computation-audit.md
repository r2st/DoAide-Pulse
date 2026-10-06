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
- **Frontend lib:** `format.js` — relative time display, `editorStats.js` — word count and banker's rounding
- **Frontend tools:** `SendTimeOptimizer.jsx` — timezone hour wrapping, `NewsletterRoiCalculator.jsx` — ROI funnel math

**Currency math:** `NewsletterRoiCalculator.jsx` has USD revenue/cost calculations (presentation-only tool, not transactional).

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

### Finding 3: Timezone hour wrapping used single +24/-24 instead of modulo

**File:** `frontend/src/pages/tools/SendTimeOptimizer.jsx:51`

**Bug:** `adjustHour` shifted a base hour by a timezone offset and wrapped with `if (h < 0) h += 24; if (h >= 24) h -= 24`. A single correction only works when the shift is less than 24 hours. Although current offsets (IST to PST = -13.5h) stay within that range, a future timezone entry or a base hour near the boundary could produce a value below -24 or above 48 that the single correction would leave out of [0, 24).

**Example:**
```
baseHour = 6, tz offset diff = -13.5
h = 6 + (-13.5) = -7.5
Before: h += 24 → 16.5  (correct by luck)

baseHour = 6, hypothetical diff = -30
h = 6 + (-30) = -24
Before: h += 24 → 0, then h >= 24 check → no-op → 0  (correct by luck)

h = 6 + (-31) = -25
Before: h += 24 → -1 → still negative, not caught
After:  ((-25 % 24) + 24) % 24 = 23
```

**Fix:** `if/else` wrapping → `((h % 24) + 24) % 24` (JavaScript-safe double-modulo).

### Finding 4: `formatWhen` rounded hours/days up, producing "7d ago" at the calendar-date cutoff

**File:** `frontend/src/lib/format.js:22-23`

**Bug:** The relative time formatter used `Math.round(abs / 60)` for hours and `Math.round(abs / 1440)` for days. At 10079 minutes (~6d 23h 59m), `Math.round(10079 / 1440) = Math.round(6.999) = 7`, displaying "7d ago" — but the code switches to a calendar date at exactly 10080 minutes (7 days). The user would see "7d ago" for one minute, then abruptly switch to a calendar date the next minute. Using `Math.round` for hours also showed "2h ago" at 1h 31m.

**Example:**
```
abs = 10079 minutes (6 days, 23 hours, 59 minutes)
Before: Math.round(10079 / 1440) = 7 → "7d ago"
After:  Math.floor(10079 / 1440) = 6 → "6d ago"

abs = 119 minutes (1 hour, 59 minutes)
Before: Math.round(119 / 60) = 2 → "2h ago"
After:  Math.floor(119 / 60) = 1 → "1h ago"
```

**Impact:** Cosmetic — relative time labels overstated age by up to one unit near boundaries.

**Fix:** `Math.round` → `Math.floor` for hour and day magnitudes.

### Finding 5: ROI calculator allowed negative inputs, producing nonsensical results

**File:** `frontend/src/pages/tools/NewsletterRoiCalculator.jsx:27-32`

**Bug:** HTML `min={0}` attributes on number inputs do not prevent programmatic or typed-in negative values (the constraint is advisory and only enforced on form submission, not on `onChange`). Negative subscribers or rates flowed through the funnel math unclamped, producing negative opens, negative clicks, and misleading revenue figures.

**Example:**
```
subscribers = -100, openRate = 35
Before: opens = -100 * 0.35 = -35  → displayed as "-35" opens
After:  safeSubs = max(0, -100) = 0 → opens = 0
```

**Fix:** Clamp all six inputs at computation time: `Math.max(0, ...)` for counts and costs, `Math.min(100, Math.max(0, ...))` for percentage rates. NaN values (from empty inputs) fall through to 0 via the `|| 0` guard.

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
| `editorStats.js` `roundHalfToEven` | Exact halves at multiples of 110 are exactly representable in float64; nearest non-half values are ±1/220 ≈ 0.005 from 0.5, well outside epsilon risk |
| `analytics.js` `comparePeriods` | Midpoint split uses `Math.floor(len / 2)`, correct for both even and odd series |
| `readTime.js` `lengthPayoff` | Division by zero guarded by explicit `long === 0` check |
| `calendar.js` date bucketing | Uses `localDayKey` with local getters, not `toISOString()` |

## Files Changed

| File | Change |
|------|--------|
| `backend/app/services/alerts.py` | `int(ratio * 100)` → `round(ratio * 100)`; `int(window / 24) or 1` → `max(1, round(window / 24))` |
| `backend/tests/test_h035_computation.py` | 6 test cases covering both backend findings |
| `frontend/src/pages/tools/SendTimeOptimizer.jsx` | `if/else` hour wrapping → `((h % 24) + 24) % 24` |
| `frontend/src/pages/tools/NewsletterRoiCalculator.jsx` | Clamp all six inputs with `Math.max(0, ...)` and rate cap at 100 |
| `frontend/src/lib/format.js` | `Math.round` → `Math.floor` for hour and day magnitudes in `formatWhen` |
| `frontend/src/pages/tools/SendTimeOptimizer.test.jsx` | 2 new tests for large positive/negative timezone offsets |
| `frontend/src/pages/tools/NewsletterRoiCalculator.test.jsx` | 2 new tests for negative inputs and rates above 100 |
| `frontend/src/lib/format.test.js` | 2 new tests for boundary rounding in `formatWhen` |

## Test Results

- **Backend:** 6 new tests pass; 74 existing alert tests pass
- **Frontend:** 6 new tests pass; all existing tests pass (3 pre-existing failures in App.routes.test.jsx from jsdom matchMedia — unrelated)

# R272 — Performance

**Methodology:** M8 (performance)
**Base commit:** 3ba5a0f

## Findings

### 1. Redundant 4-table subquery in `_read_minutes_by_publication` (MEDIUM)

**File:** `backend/app/services/analytics_service.py`

**Problem:** `engagement_trend()` calls `_read_minutes_by_publication()` after its main query has already joined `ContentMetric → Publication → Content → Project` and built a `content_of` dict mapping every `publication_id` to its `content_id`. Despite receiving this dict, `_read_minutes_by_publication` ignored the known content ids and ran a second 4-table join (`Publication → ContentMetric → Content → Project`) as a DISTINCT subquery just to re-derive the same set, then used that subquery inside a third query to fetch `word_count`. This added two unnecessary queries per `engagement_trend` call.

**Fix:** Use `set(content_of.values())` directly in a single `SELECT id, word_count FROM content WHERE id IN (...)` query. The set is bounded by how many distinct pieces a user published within the engagement window — well within Postgres's 65,535-parameter limit for any realistic account.

**Impact:** Eliminates 1 unnecessary query (the 4-table DISTINCT subquery) per engagement_trend call. On accounts with hundreds of publications, the eliminated subquery scanned the full `content_metrics` table.

### 2. Dashboard fires separate query for review count (MEDIUM)

**File:** `backend/app/services/analytics_service.py`, `backend/app/routers/analytics.py`

**Problem:** The `/analytics/dashboard` endpoint ran a standalone `SELECT COUNT(*) ... WHERE status = 'review'` query, while `dashboard_summary()` already executed a status-count query inside `_totals_from_cached()` that counted `total` and `published` content rows. The review count piggybacks on the same `GROUP BY status` scan — adding it to the existing query is free.

**Fix:** Extracted `_status_counts()` which returns `(total, published, review)` in one query. `_totals_from_cached()` now returns `(Totals, review_count)` and `dashboard_summary()` passes the review count through to the router. The router's separate `SELECT COUNT(*)` for review items is removed.

**Impact:** Eliminates 1 query per dashboard page load. The dashboard is the most-loaded page in the app.

## Tests

**File:** `backend/tests/test_r272_performance.py` — 8 tests

- `test_read_minutes_uses_known_content_ids` — correctness: word counts map to the right publications
- `test_read_minutes_empty_content_of` — edge case: empty dict skips all queries
- `test_read_minutes_fewer_queries_than_before` — exactly 1 query for 3 publications
- `test_engagement_trend_returns_correct_shape` — integration: trend data shape is preserved
- `test_dashboard_summary_returns_review_count` — correctness: review count matches
- `test_dashboard_summary_review_count_zero_when_none` — edge case: 0 when no reviews
- `test_dashboard_endpoint_uses_consolidated_review_count` — integration: endpoint returns correct review count
- `test_dashboard_fewer_queries_than_before` — query count sanity check

All 8 new tests pass. All 63 existing analytics tests pass.

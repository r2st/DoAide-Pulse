# H033 — Performance Audit

**Scope**: Hot-path profiling across services, tasks, and routers.  
**Audited**: `analytics_service`, `content_pipeline`, `publishing_service`, `velocity`, `digest`, `tags`, all routers in `content.py` and `analytics.py`, all task files (`metrics_tasks`, `publish_tasks`, `autopilot_tasks`).

---

## Findings

### 1. Per-row commits in metric collection sweep (HIGH)

**Location**: `publishing_service.collect_metrics()` line 1679, called from `metrics_tasks.collect_all_metrics()`.

**Problem**: The metrics sweep iterates every published publication on the install and calls `collect_metrics` per row. Each call did `db.commit()`, producing one database round trip per publication. On an install with 1000 published posts, that is 1000 commits per sweep (every few hours). Each commit also expired the SQLAlchemy session, causing re-reads of subsequent publication rows.

**Fix**: 
- Added `commit` parameter to `collect_metrics` (default `True` for backward compatibility).
- The sweep now passes `commit=False` and commits in batches of 50 rows.
- Set `db.expire_on_commit = False` on the sweep's session to prevent re-reads between iterations — the same pattern `release_approved_content` already uses.
- On per-row failure, pending batch is committed first (the exception comes from the platform API, before `db.add()`), preserving successful metrics.

**Impact**: ~20x fewer commits for a typical install (1000 publications → 20 commits instead of 1000). Eliminates per-row re-reads of publication entities.

### 2. Unbounded `due_publications` query (MEDIUM)

**Location**: `publishing_service.due_publications()` line 1561.

**Problem**: No `LIMIT` or `ORDER BY` clause. On an install with a large backlog of pending/scheduled publications, this loaded every matching row into memory. The `publish_due` task called this and immediately extracted just the IDs, so full entity loading was also wasteful.

**Fix**:
- Added `LIMIT 500` (via `_DUE_PUBLICATIONS_LIMIT` constant).
- Added deterministic ordering: `scheduled_for ASC NULLS FIRST, id`. Pending rows (no `scheduled_for`) sort first so they are never starved by a backlog of scheduled publications. Tie-breaking on `id` ensures stable paging.

**Impact**: Bounds memory to at most 500 rows. Deterministic ordering prevents the same publication from being skipped or processed twice across sweeps.

### 3. Well-optimized areas (no changes needed)

The audit covered several areas that turned out to be well-designed:

- **`analytics_service.overview()`**: Runs the expensive 4-table join once and passes cached results to 5 sub-functions. No redundant queries.
- **`velocity.summary()`**: 3-pass design (prefix curves → SQL aggregates → full series only for unsettled verdicts) avoids loading full metric history.
- **`content_pipeline.generate_and_route()`**: Sequential quality gates are inherently serial (each needs the piece). The `publishable_destinations()` loop iterates a small list (project's platforms).
- **`digest.build()`**: Uses narrow column selects instead of full entities, explicit joins, and bounded queries throughout.
- **`tags.tree()`**: Bounded by `TREE_SCAN_LIMIT = 5000`, reads only `Content.tags` column.
- **`routers/content.py`**: Uses `defer(Content.body_markdown)` on listings, `joinedload` for relationships, `_owned_content_map` for batch ownership checks.
- **`routers/analytics.py:dashboard()`**: Uses column selects instead of entity loading for recent content, failed publications, and scheduled publications.

---

## Files changed

| File | Change |
|------|--------|
| `backend/app/services/publishing_service.py` | `collect_metrics`: added `commit` param; `due_publications`: added LIMIT + ORDER BY |
| `backend/app/tasks/metrics_tasks.py` | Batched commits, `expire_on_commit = False` |
| `backend/tests/test_h033_performance_audit.py` | 6 new tests covering all fixes |
| `backend/tests/test_a_switched_off_account_stops_being_polled.py` | Stub accepts new kwarg |
| `backend/tests/test_security_fixes.py` | Stub accepts new kwarg |

## Tests

6 new tests added in `test_h033_performance_audit.py`:
- `test_due_publications_has_a_limit` — query is bounded
- `test_due_publications_ordered_pending_first` — deterministic ordering with pending-first
- `test_collect_metrics_skips_commit_when_asked` — `commit=False` defers the write
- `test_collect_metrics_commits_by_default` — backward compatibility
- `test_sweep_batches_commits` — batch commit count is bounded
- `test_sweep_failure_does_not_lose_prior_batch` — failed row preserves prior metrics

All 255 related existing tests pass without regression.

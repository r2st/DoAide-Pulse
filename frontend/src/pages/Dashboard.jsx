import { Link } from "react-router-dom";
import { SectionBoundary } from "../components/ErrorBoundary";
import ReadTimePanel from "../components/ReadTimePanel";
import {
  Empty,
  ErrorBanner,
  SectionHeader,
  Skeleton,
  Stat,
  StatusBadge,
} from "../components/ui/Bits";
import { useApi } from "../hooks/useApi";
import { alertHref, alertLabel, alertTone, formatAlertRatio } from "../lib/alerts";
import { api } from "../lib/api";
import {
  formatCount,
  formatDateTime,
  formatRate,
  formatWhen,
  titleize,
} from "../lib/format";

/**
 * The home page: how much has gone out, what is waiting on you, what is next.
 *
 * Ordered by urgency rather than by data model — anything that needs a human
 * comes first, because a dashboard that leads with vanity counters buries the
 * one thing the user actually has to do.
 */
export default function Dashboard() {
  const { data, error, loading, reload } = useApi(() => api.dashboard(), []);

  if (loading && !data) {
    return (
      <div className="space-y-6">
        <h1 className="page-title">Dashboard</h1>
        <Skeleton rows={4} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <h1 className="page-title">Dashboard</h1>
        <ErrorBanner message={error} onRetry={reload} />
      </div>
    );
  }

  const {
    totals,
    needs_review,
    failed_publications,
    upcoming,
    recent_content,
    by_project,
    // Absent from an older cached response; an empty list renders as nothing.
    alerts = [],
  } = data;
  const nothingYet = totals.content_count === 0;

  return (
    <div className="stagger space-y-8">
      <div className="flex items-end justify-between gap-4">
        <h1 className="page-title">Dashboard</h1>
        <div className="flex items-center gap-2">
          {/* The read-time panel at the bottom of this page is a taste of the
              analytics; this is where the rest of them live. */}
          <Link to="/analytics" className="btn-quiet">
            Analytics
          </Link>
          <Link to="/content" className="btn-primary">
            Write something
          </Link>
        </div>
      </div>

      {nothingYet ? (
        <Empty
          title="Nothing written yet"
          hint="Register a project, then let Herald draft the first post about it."
          action={
            <Link to="/projects" className="btn-primary mt-1">
              Register a project
            </Link>
          }
        />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Pieces" value={formatCount(totals.content_count)} />
          <Stat
            label="Published"
            value={formatCount(totals.published_count)}
            hint={`${formatCount(totals.publication_count)} across all platforms`}
          />
          <Stat
            label="Views"
            value={formatCount(totals.views)}
            // A rate the platforms did not report comes back null, and renders
            // as "—" rather than a 0% that would read as nobody clicking.
            hint={`${formatRate(totals.click_through_rate)} click-through`}
          />
          <Stat
            label="Engagement"
            value={formatCount(totals.engagement)}
            hint={`${formatRate(totals.engagement_rate)} of views`}
          />
        </div>
      )}

      {/* ---- Things waiting on a human ---- */}
      {(needs_review > 0 || failed_publications.length > 0 || alerts.length > 0) && (
        <section className="space-y-3">
          <SectionHeader title="Needs you" />
          {needs_review > 0 && (
            <Link
              to="/content?status=review"
              className="panel flex items-center justify-between px-5 py-4 transition-shadow hover:shadow-lift"
            >
              <span className="text-sm text-ink-700">
                <strong className="text-ink-900">{needs_review}</strong>{" "}
                {needs_review === 1 ? "draft is" : "drafts are"} waiting for review
              </span>
              <span className="text-sm text-brand-500">Review →</span>
            </Link>
          )}
          {failed_publications.map((failure) => (
            <Link
              key={failure.id}
              to="/publish"
              className="panel flex items-start justify-between gap-4 px-5 py-4 transition-shadow hover:shadow-lift"
            >
              <span className="min-w-0 text-sm">
                <span className="font-medium text-ink-900">
                  {titleize(failure.platform)}
                </span>{" "}
                <span className="text-ink-500">publication failed</span>
                <span className="mt-1 block truncate font-mono text-xs text-bad">
                  {failure.error}
                </span>
              </span>
              <span className="shrink-0 text-sm text-brand-500">Fix →</span>
            </Link>
          ))}

          {/* Below the failures on purpose: a publish that never went out is a
              broken thing, and a post that went out quietly is only a
              disappointing one. */}
          {alerts.map((alert) => (
            <Link
              key={`${alert.publication_id}-${alert.kind}`}
              to={alertHref(alert)}
              className="panel flex items-start justify-between gap-4 px-5 py-4 transition-shadow hover:shadow-lift"
            >
              <span className="min-w-0 text-sm">
                <span className="flex items-center gap-2">
                  <span
                    className={`chip ${
                      alertTone(alert.severity) === "bad"
                        ? "text-bad"
                        : "text-ink-500"
                    }`}
                  >
                    {alertLabel(alert.kind)}
                  </span>
                  <span className="truncate font-medium text-ink-900">
                    {alert.title}
                  </span>
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-ink-500">
                  {alert.message}
                </span>
              </span>
              <span className="shrink-0 text-right">
                {formatAlertRatio(alert.ratio) && (
                  <span className="block font-mono text-xs text-ink-400">
                    {formatAlertRatio(alert.ratio)}
                  </span>
                )}
                <span className="text-sm text-brand-500">Open →</span>
              </span>
            </Link>
          ))}
        </section>
      )}

      {/* ---- What is scheduled ---- */}
      <section>
        <SectionHeader
          title="Going out next"
          action={
            <Link to="/calendar" className="btn-quiet">
              Calendar
            </Link>
          }
        />
        {upcoming.length === 0 ? (
          <p className="panel px-5 py-6 text-sm text-ink-500">
            Nothing scheduled. Approve a draft and pick a slot on the calendar.
          </p>
        ) : (
          <ul className="panel divide-y divide-line">
            {upcoming.map((item) => (
              <li key={item.id} className="flex items-center justify-between gap-4 px-5 py-3">
                <Link
                  to={`/content/${item.content_id}`}
                  className="min-w-0 truncate text-sm text-ink-900 hover:text-brand-500"
                >
                  {item.title}
                </Link>
                <span className="flex shrink-0 items-center gap-3">
                  <span className="chip">{titleize(item.platform)}</span>
                  <span className="font-mono text-xs text-ink-500">
                    {formatDateTime(item.scheduled_for)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* ---- Recent work ---- */}
        <section>
          <SectionHeader
            title="Recent"
            action={
              <Link to="/content" className="btn-quiet">
                All content
              </Link>
            }
          />
          {recent_content.length === 0 ? (
            <p className="panel px-5 py-6 text-sm text-ink-500">Nothing written yet.</p>
          ) : (
            <ul className="panel divide-y divide-line">
              {recent_content.map((item) => (
                <li key={item.id} className="px-5 py-3">
                  <Link
                    to={`/content/${item.id}`}
                    className="flex items-start justify-between gap-3"
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm text-ink-900 hover:text-brand-500">
                        {item.title}
                      </span>
                      <span className="mt-0.5 block text-xs text-ink-400">
                        {item.project_name} · {titleize(item.content_type)} ·{" "}
                        {formatWhen(item.created_at)}
                      </span>
                    </span>
                    <StatusBadge status={item.status} />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ---- Per-project traction ---- */}
        <section>
          <SectionHeader
            title="By project"
            action={
              <Link to="/projects" className="btn-quiet">
                Projects
              </Link>
            }
          />
          {by_project.length === 0 ? (
            <p className="panel px-5 py-6 text-sm text-ink-500">
              No projects registered yet.
            </p>
          ) : (
            <ul className="panel divide-y divide-line">
              {by_project.map((row) => (
                <li
                  key={row.project_id}
                  className="flex items-center justify-between gap-4 px-5 py-3"
                >
                  <span className="min-w-0 truncate text-sm text-ink-900">{row.name}</span>
                  <span className="flex shrink-0 items-center gap-4 font-mono text-xs text-ink-500">
                    <span title="Published pieces">{row.published} pub</span>
                    <span title="Views">{formatCount(row.views)} views</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {/* Last on the page, and loaded on its own: analysis, not a to-do. Which
          is also why it gets a boundary — the failed publication above it is
          the thing somebody has to act on, and a read-time chart that cannot be
          drawn must not be what stops them seeing it. */}
      {!nothingYet && (
        <SectionBoundary name="dashboard:read-time">
          <ReadTimePanel />
        </SectionBoundary>
      )}
    </div>
  );
}

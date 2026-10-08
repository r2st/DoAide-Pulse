import { useState } from "react";
import { Link } from "react-router-dom";
import { SectionBoundary } from "../components/ErrorBoundary";
import QuickActions from "../components/QuickActions";
import {
  Empty,
  ErrorBanner,
  SectionHeader,
  Skeleton,
  StatusBadge,
} from "../components/ui/Bits";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import {
  formatCount,
  formatDateTime,
  formatWhen,
  titleize,
} from "../lib/format";

export default function Dashboard() {
  const { data, error, loading, reload } = useApi(() => api.dashboard(), []);
  const { data: reviewItems, reload: reloadReview } = useApi(
    () => api.reviewQueue(),
    [],
  );

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
  } = data;
  const nothingYet = totals.content_count === 0;
  const reviewCount = reviewItems?.length ?? needs_review;

  function handleApproved() {
    reload();
    reloadReview();
  }

  return (
    <div className="stagger space-y-8 pb-24">
      {/* Header */}
      <div className="flex items-end justify-between gap-4">
        <h1 className="page-title">Dashboard</h1>
        <Link to="/analytics" className="btn-quiet">
          Analytics
        </Link>
      </div>

      {/* Quick stats bar */}
      {nothingYet ? (
        <Empty
          title="Nothing written yet"
          hint="Register a project — the robot drafts the first post."
          action={
            <Link to="/projects" className="btn-primary mt-1">
              Register a project
            </Link>
          }
        />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <QuickStat
            label="Projects"
            value={formatCount(by_project.length)}
            icon={<FolderIcon />}
          />
          <QuickStat
            label="Published"
            value={formatCount(totals.published_count)}
            icon={<CheckCircleIcon />}
          />
          <QuickStat
            label="In Review"
            value={formatCount(reviewCount)}
            icon={<ClockIcon />}
            highlight={reviewCount > 0}
          />
          <QuickStat
            label="Scheduled"
            value={formatCount(upcoming.length)}
            icon={<CalendarIcon />}
          />
        </div>
      )}

      {/* Review queue preview */}
      {reviewCount > 0 && (
        <SectionBoundary name="dashboard:review-queue">
          <ReviewQueuePreview
            items={reviewItems ?? []}
            totalCount={reviewCount}
            onApproved={handleApproved}
          />
        </SectionBoundary>
      )}

      {/* Failed publications */}
      {failed_publications.length > 0 && (
        <section className="space-y-3">
          <SectionHeader title="Failed publications" />
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
        </section>
      )}

      {/* Recent activity feed */}
      <section>
        <SectionHeader
          title="Recent activity"
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
            {recent_content.slice(0, 10).map((item) => (
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

      {/* Upcoming scheduled */}
      {upcoming.length > 0 && (
        <section>
          <SectionHeader
            title="Scheduled"
            action={
              <Link to="/calendar" className="btn-quiet">
                Calendar
              </Link>
            }
          />
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
        </section>
      )}

      <QuickActions reviewCount={reviewCount} onApproved={handleApproved} />
    </div>
  );
}

function ReviewQueuePreview({ items, totalCount, onApproved }) {
  return (
    <section className="space-y-3">
      <SectionHeader
        title={`In review (${totalCount})`}
        action={
          <Link to="/content?status=review" className="btn-quiet">
            View all
          </Link>
        }
      />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {items.slice(0, 6).map((item) => (
          <ReviewCard key={item.id} item={item} onApproved={onApproved} />
        ))}
      </div>
      {totalCount > 6 && (
        <Link
          to="/content?status=review"
          className="block text-center text-sm text-brand-500 hover:text-brand-600"
        >
          +{totalCount - 6} more →
        </Link>
      )}
    </section>
  );
}

function ReviewCard({ item, onApproved }) {
  const [busy, setBusy] = useState(false);

  async function approve() {
    setBusy(true);
    try {
      await api.approveContent(item.id);
      onApproved?.();
    } catch {
      setBusy(false);
    }
  }

  const confidence = item.confidence != null ? Math.round(item.confidence * 100) : null;
  const confidenceTone =
    confidence >= 80 ? "text-good" : confidence >= 50 ? "text-warn" : "text-ink-400";

  return (
    <div className="panel flex flex-col gap-2 p-4">
      <Link
        to={`/content/${item.id}`}
        className="min-w-0"
      >
        <p className="line-clamp-2 text-sm font-medium text-ink-900 hover:text-brand-500">
          {item.title}
        </p>
        <p className="mt-1 text-xs text-ink-400">
          {item.project_name} · {titleize(item.content_type)}
        </p>
      </Link>

      {confidence !== null && (
        <div className="flex items-center gap-2">
          <div className="h-1.5 flex-1 rounded-full bg-ink-400/15">
            <div
              className={`h-full rounded-full ${
                confidence >= 80 ? "bg-good" : confidence >= 50 ? "bg-warn" : "bg-ink-400"
              }`}
              style={{ width: `${confidence}%` }}
            />
          </div>
          <span className={`font-mono text-[11px] ${confidenceTone}`}>
            {confidence}%
          </span>
        </div>
      )}

      <div className="mt-auto flex items-center gap-2 pt-1">
        <button
          className="min-h-[44px] flex-1 rounded-lg bg-good px-3 py-2 text-sm font-medium text-canvas transition-colors hover:bg-good/90 active:scale-[0.98] disabled:opacity-40"
          onClick={approve}
          disabled={busy}
        >
          {busy ? "…" : "Approve"}
        </button>
        <Link
          to={`/content/${item.id}`}
          className="min-h-[44px] flex-1 rounded-lg border border-line px-3 py-2 text-center text-sm text-ink-600 transition-colors hover:bg-canvas"
        >
          Review
        </Link>
      </div>
    </div>
  );
}

function QuickStat({ label, value, icon, highlight = false }) {
  return (
    <div className={`panel flex items-center gap-3 px-4 py-3 ${highlight ? "border-brand-500/50" : ""}`}>
      <span className={`flex h-9 w-9 items-center justify-center rounded-lg ${highlight ? "bg-brand-50 text-brand-500" : "bg-ink-400/10 text-ink-500"}`}>
        {icon}
      </span>
      <div className="min-w-0">
        <p className="font-display text-xl leading-tight text-ink-900">{value}</p>
        <p className="text-xs text-ink-400">{label}</p>
      </div>
    </div>
  );
}

function FolderIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1.5 4a1.5 1.5 0 011.5-1.5h3l1.5 1.5h5A1.5 1.5 0 0114.5 5.5v6A1.5 1.5 0 0113 13H3A1.5 1.5 0 011.5 11.5V4z" />
    </svg>
  );
}

function CheckCircleIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M5.5 8.5l2 2 3.5-4" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 4.5V8l2.5 1.5" />
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="12" height="11" rx="1.5" />
      <path d="M5.5 1.5v2.5M10.5 1.5v2.5M2 7h12" />
    </svg>
  );
}

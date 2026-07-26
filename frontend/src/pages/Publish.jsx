import { Link } from "react-router-dom";
import { Empty, ErrorBanner, SectionHeader, Skeleton, StatusBadge } from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatCount, formatDateTime, titleize } from "../lib/format";

/**
 * The publication queue and how each platform is performing.
 *
 * Failures lead. Everything else on this page is information; a failed
 * publication is work, and it is the only thing here that gets worse if
 * ignored.
 */
export default function Publish() {
  const queue = useApi(() => api.publicationQueue(), []);
  const platforms = useApi(() => api.platforms(), []);
  const analytics = useApi(() => api.analytics(), []);

  const items = queue.data ?? [];
  const failed = items.filter((item) => item.status === "failed");
  const waiting = items.filter((item) => item.status !== "failed");

  return (
    <div className="stagger space-y-8">
      <div>
        <h1 className="page-title">Publish</h1>
        <p className="mt-1 text-sm text-ink-500">
          What is queued, what went wrong, and where the reach is.
        </p>
      </div>

      <ErrorBanner message={queue.error} onRetry={queue.reload} />

      {failed.length > 0 && (
        <section>
          <SectionHeader
            title="Failed"
            subtitle="These will not retry on their own."
          />
          <ul className="panel divide-y divide-line">
            {failed.map((item) => (
              <QueueRow key={item.id} item={item} onChanged={queue.reload} />
            ))}
          </ul>
        </section>
      )}

      <section>
        <SectionHeader
          title="Queue"
          subtitle="Pending and scheduled publications."
          action={
            <Link to="/calendar" className="btn-quiet">
              Calendar
            </Link>
          }
        />
        {queue.loading && !queue.data ? (
          <Skeleton rows={3} />
        ) : waiting.length === 0 ? (
          <Empty
            title="Nothing queued"
            hint="Approve a draft and send it to a platform from the content editor."
            action={
              <Link to="/content" className="btn-primary mt-1">
                Go to content
              </Link>
            }
          />
        ) : (
          <ul className="panel divide-y divide-line">
            {waiting.map((item) => (
              <QueueRow key={item.id} item={item} onChanged={queue.reload} />
            ))}
          </ul>
        )}
      </section>

      <section>
        <SectionHeader
          title="Platforms"
          subtitle="Connection state and lifetime reach."
          action={
            <Link to="/settings" className="btn-quiet">
              Manage
            </Link>
          }
        />
        {platforms.loading && !platforms.data ? (
          <Skeleton rows={3} />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {(platforms.data ?? []).map((platform) => {
              const stats = (analytics.data?.by_platform ?? []).find(
                (row) => row.platform === platform.platform,
              );
              return (
                <div key={platform.platform} className="panel p-4">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium text-ink-900">
                      {platform.display_name}
                    </span>
                    <ConnectionPill platform={platform} />
                  </div>
                  <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-ink-500">
                    <span>{formatCount(stats?.published ?? 0)} published</span>
                    <span>{formatCount(stats?.views ?? 0)} views</span>
                    {(stats?.failed ?? 0) > 0 && (
                      <span className="text-bad">{stats.failed} failed</span>
                    )}
                  </dl>
                  {platform.caveat && (
                    <p className="mt-2 text-xs leading-relaxed text-ink-400">
                      {platform.caveat}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {analytics.data?.by_content_type?.length > 0 && (
        <section>
          <SectionHeader
            title="What performs"
            subtitle="Average views per published piece, by type."
          />
          <ul className="panel divide-y divide-line">
            {analytics.data.by_content_type.map((row) => (
              <li
                key={row.content_type}
                className="flex items-center justify-between gap-4 px-5 py-3"
              >
                <span className="text-sm text-ink-900">{row.label}</span>
                <span className="flex items-center gap-5 font-mono text-xs text-ink-500">
                  <span>{row.publications} pub</span>
                  <span>{formatCount(row.views)} views</span>
                  <span className="w-24 text-right">
                    {row.avg_views === null ? "—" : `${formatCount(row.avg_views)} avg`}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {analytics.data?.top_content?.length > 0 && (
        <section>
          <SectionHeader title="Best performing" />
          <ul className="panel divide-y divide-line">
            {analytics.data.top_content.map((row) => (
              <li
                key={row.content_id}
                className="flex items-center justify-between gap-4 px-5 py-3"
              >
                <Link
                  to={`/content/${row.content_id}`}
                  className="min-w-0 truncate text-sm text-ink-900 hover:text-brand-500"
                >
                  {row.title}
                </Link>
                <span className="flex shrink-0 items-center gap-4 font-mono text-xs text-ink-500">
                  <span>{formatCount(row.views)} views</span>
                  <span>{formatCount(row.engagement)} eng</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function ConnectionPill({ platform }) {
  if (!platform.implemented) {
    return <span className="badge bg-canvas text-ink-400">not built</span>;
  }
  const status = platform.connection?.status;
  if (status === "connected") {
    return <span className="badge bg-good-wash text-good">connected</span>;
  }
  if (status === "invalid") {
    return <span className="badge bg-bad-wash text-bad">reconnect</span>;
  }
  return <span className="badge bg-canvas text-ink-500">not connected</span>;
}

function QueueRow({ item, onChanged }) {
  const toast = useToast();

  async function retry() {
    try {
      await api.retryPublication(item.content_id, item.id);
      toast.success("Retrying");
      onChanged();
    } catch (err) {
      toast.error(err.message);
    }
  }

  return (
    <li className="flex items-start justify-between gap-4 px-5 py-4">
      <div className="min-w-0">
        <Link
          to={`/content/${item.content_id}`}
          className="text-sm font-medium text-ink-900 hover:text-brand-500"
        >
          {titleize(item.platform)}
        </Link>
        <p className="mt-1 font-mono text-[11px] text-ink-400">
          {item.scheduled_for
            ? formatDateTime(item.scheduled_for)
            : "as soon as a worker picks it up"}
          {item.attempts > 0 && ` · ${item.attempts} attempt${item.attempts === 1 ? "" : "s"}`}
        </p>
        {item.error && (
          <p className="mt-2 break-words rounded bg-bad-wash px-2 py-1 font-mono text-[11px] text-bad">
            {item.error}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <StatusBadge status={item.status} />
        {item.status === "failed" && (
          <button className="btn-quiet" onClick={retry}>
            Retry
          </button>
        )}
      </div>
    </li>
  );
}

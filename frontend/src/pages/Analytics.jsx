import { useState } from "react";
import { Link } from "react-router-dom";
import { SectionBoundary } from "../components/ErrorBoundary";
import {
  Empty,
  ErrorBanner,
  SectionHeader,
  Skeleton,
  Stat,
} from "../components/ui/Bits";
import { AreaChart, BarRow, CompareBars, MultiLineChart } from "../components/ui/Charts";
import { useApi } from "../hooks/useApi";
import {
  bestPlatform,
  comparePeriods,
  earlyViewsKey,
  platformShares,
} from "../lib/analytics";
import { alertHref, alertLabel, alertTone, formatAlertRatio } from "../lib/alerts";
import { api } from "../lib/api";
import { formatCount, formatRate, formatWhen, titleize } from "../lib/format";

/** Windows for the trend chart. 90 is the API's practical ceiling. */
const WINDOWS = [7, 30, 90];

/**
 * What the writing actually did.
 *
 * The dashboard answers "what needs me now"; this answers "was any of it worth
 * doing", which is a different question asked at a different frequency and
 * deserves its own page rather than another panel below the to-do list.
 *
 * Loaded as four independent requests. The overview is one round trip by
 * design, but the velocity series and the alert pass are the expensive parts
 * and neither blocks reading the counters — a page that waits for its slowest
 * section shows nothing for as long as the slowest section takes.
 */
export default function Analytics() {
  const [days, setDays] = useState(30);
  const overview = useApi(() => api.analytics(), []);
  const trend = useApi(() => api.engagementTrend(days), [days]);
  const velocity = useApi(() => api.velocity(), []);
  const alerts = useApi(() => api.alerts(20), []);

  if (overview.loading && !overview.data) {
    return (
      <div className="space-y-6">
        <h1 className="page-title">Analytics</h1>
        <Skeleton rows={5} />
      </div>
    );
  }

  if (overview.error) {
    return (
      <div className="space-y-6">
        <h1 className="page-title">Analytics</h1>
        <ErrorBanner message={overview.error} onRetry={overview.reload} />
      </div>
    );
  }

  const { totals, by_platform, by_content_type, top_content, timeline } = overview.data;

  // Nothing has been published, so every panel below would be an empty state
  // repeating the same sentence. One is enough.
  if (totals.published_count === 0) {
    return (
      <div className="space-y-6">
        <h1 className="page-title">Analytics</h1>
        <Empty
          title="Nothing has gone out yet"
          hint="Numbers appear here once a piece is published and the platforms start reporting on it."
          action={
            <Link to="/content" className="btn-primary mt-1">
              Go to your drafts
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div className="stagger space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Analytics</h1>
          <p className="mt-1 text-sm text-ink-500">
            What was published, who read it, and what it earned once they did.
          </p>
        </div>
      </div>

      <Counters totals={totals} />

      <Section name="reach">
        <ReachOverTime
          trend={trend}
          days={days}
          onWindow={setDays}
          clicksReported={totals.clicks_reported > 0}
        />
      </Section>

      <Section name="alerts">
        <NeedsAttention alerts={alerts} />
      </Section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Section name="platforms">
          <PlatformPanel rows={by_platform} />
        </Section>
        <Section name="content-types">
          <ContentTypePanel rows={by_content_type} />
        </Section>
      </div>

      <Section name="platform-share">
        <PlatformShare rows={by_platform} />
      </Section>

      <Section name="velocity">
        <VelocityPanel velocity={velocity} />
      </Section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Section name="top-content">
          <TopContent rows={top_content} />
        </Section>
        <Section name="rhythm">
          <PublishingRhythm timeline={timeline} />
        </Section>
      </div>
    </div>
  );
}

/**
 * One section of this page, boundaried.
 *
 * Every section below the counters draws a chart from a shape it derives
 * itself — a median over a snapshot series, a share of a share, a scale taken
 * from the maximum of a list. That is where a field the API stopped sending
 * turns into a throw during render, and there is no reason a stalled-posts
 * table should be able to take the view count at the top of the page with it.
 *
 * The counters are outside on purpose: they read four numbers straight off the
 * response, and if those are missing the page has nothing to say anyway.
 */
function Section({ name, children }) {
  return <SectionBoundary name={`analytics:${name}`}>{children}</SectionBoundary>;
}

/**
 * The four headline figures.
 *
 * Two counts and two rates, deliberately: a count alone rewards volume, and a
 * rate alone hides that it was measured on three visitors.
 */
function Counters({ totals }) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Stat
        label="Views"
        value={formatCount(totals.views)}
        hint={`across ${formatCount(totals.publication_count)} publications`}
      />
      <Stat
        label="Engagement"
        value={formatCount(totals.engagement)}
        hint={`${formatRate(totals.engagement_rate)} of views`}
      />
      <Stat
        label="Click-through"
        // Null rather than zero when no platform reported clicks at all — "—"
        // says "nobody counts this", where 0% would say "nobody clicked".
        value={formatRate(totals.click_through_rate)}
        hint={
          totals.clicks_reported === 0
            ? "no platform reports clicks"
            : `${formatCount(totals.clicks)} clicks`
        }
      />
      <Stat
        label="Read rate"
        value={formatRate(totals.read_rate)}
        hint={
          totals.reads_reported === 0
            ? "no platform reports reads"
            : `${formatCount(totals.reads)} read to the end`
        }
      />
    </div>
  );
}

/**
 * Views, reads and clicks per day, on one scale.
 *
 * One chart rather than three because the three are nested — every read was a
 * view, every click came from one — so the gaps between the lines are the
 * actual finding, and three separately scaled charts would hide exactly that.
 */
function ReachOverTime({ trend, days, onWindow, clicksReported }) {
  const points = trend.data ?? [];
  const views = points.map((day) => ({ label: day.date, value: day.views }));
  const verdict = comparePeriods(views, { noun: "Views" });

  const series = [
    { key: "views", label: "Views", points: views },
    {
      key: "reads",
      label: "Reads",
      points: points.map((day) => ({ label: day.date, value: day.reads })),
    },
  ];
  // Only drawn when something counts them. An always-zero line along the
  // baseline reads as "nobody clicked" rather than "nobody is counting".
  if (clicksReported) {
    series.push({
      key: "clicks",
      label: "Clicks",
      points: points.map((day) => ({ label: day.date, value: day.clicks })),
    });
  }

  return (
    <section>
      <SectionHeader title="Reach over time" />
      <div className="panel">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-line px-5 py-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-ink-900">
              Views, reads and clicks
            </p>
            <p className="mt-0.5 text-xs text-ink-400">
              {trend.data ? verdict.sentence : "Counted from every metric snapshot each day."}
            </p>
          </div>
          <div className="flex shrink-0 gap-1" role="group" aria-label="Window">
            {WINDOWS.map((window) => (
              <button
                key={window}
                onClick={() => onWindow(window)}
                aria-pressed={days === window}
                className={`rounded-md px-2 py-1 font-mono text-[11px] transition-colors ${
                  days === window
                    ? "bg-brand-50 text-brand-600"
                    : "text-ink-400 hover:bg-ink-900/[0.04] hover:text-ink-700"
                }`}
              >
                {window}d
              </button>
            ))}
          </div>
        </div>
        <div className="px-5 py-4">
          {trend.error ? (
            <ErrorBanner message={trend.error} onRetry={trend.reload} />
          ) : !trend.data ? (
            <Skeleton rows={2} />
          ) : (
            <MultiLineChart
              label="Views, reads and clicks per day"
              series={series}
              formatValue={formatCount}
              emptyHint={`Nothing recorded in the last ${days} days.`}
            />
          )}
        </div>
      </div>
    </section>
  );
}

/** The full alert list — the dashboard carries a capped version of this. */
function NeedsAttention({ alerts }) {
  if (alerts.error) {
    return (
      <section>
        <SectionHeader title="Needs attention" />
        <ErrorBanner message={alerts.error} onRetry={alerts.reload} />
      </section>
    );
  }
  const rows = alerts.data?.alerts ?? [];
  if (!alerts.data || rows.length === 0) return null;

  return (
    <section>
      <SectionHeader
        title="Needs attention"
        subtitle="Posts doing measurably worse than your own normal."
      />
      <ul className="panel divide-y divide-line">
        {rows.map((alert) => (
          <li key={`${alert.publication_id}-${alert.kind}`}>
            <Link
              to={alertHref(alert)}
              className="flex items-start justify-between gap-4 px-5 py-3.5 transition-colors hover:bg-canvas"
            >
              <span className="min-w-0 text-sm">
                <span className="flex items-center gap-2">
                  <span
                    className={`chip ${
                      alertTone(alert.severity) === "bad" ? "text-bad" : "text-ink-500"
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
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * Per-platform reach, with the failure count kept in view.
 *
 * "Bluesky gets no views" and "Bluesky rejected every post" look identical in a
 * views-only table and want completely different responses.
 */
function PlatformPanel({ rows }) {
  const best = bestPlatform(rows);
  const max = Math.max(...rows.map((row) => row.views), 0);

  return (
    <section>
      <SectionHeader title="Where it lands" />
      <div className="panel">
        {/* The page as a whole is past its own "nothing published yet" gate by
            the time this renders, so an empty breakdown means something
            narrower: publications exist but the grouping came back with no
            rows. Saying so beats a panel whose body is a single hairline. */}
        {rows.length === 0 ? (
          <p className="px-5 py-6 text-sm text-ink-500">
            No platform breakdown yet.
          </p>
        ) : (
          <div className="divide-y divide-line">
            {rows.map((row) => (
              <BarRow
                key={row.platform}
                label={titleize(row.platform)}
                value={row.views}
                max={max}
                display={`${formatCount(row.views)} views`}
                tone={best && row.platform === best.platform ? "brand" : "muted"}
                hint={[
                  `${formatCount(row.published)} published`,
                  row.failed > 0 ? `${formatCount(row.failed)} failed` : null,
                  `${formatRate(row.engagement_rate)} engaged`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              />
            ))}
          </div>
        )}
        <p className="border-t border-line px-5 py-3 text-xs leading-relaxed text-ink-500">
          {best
            ? `${titleize(best.platform)} turns a view into an interaction most often, at ${formatRate(best.engagement_rate)}.`
            : "Not enough views on any one platform yet to say which converts best."}
        </p>
      </div>
    </section>
  );
}

/** Which kinds of piece earn their keep, per view rather than per post. */
function ContentTypePanel({ rows }) {
  const max = Math.max(...rows.map((row) => row.engagement_rate ?? 0), 0);

  return (
    <section>
      <SectionHeader title="What works" />
      <div className="panel">
        {rows.length === 0 ? (
          <p className="px-5 py-6 text-sm text-ink-500">
            No content-type breakdown yet.
          </p>
        ) : (
          <div className="divide-y divide-line">
            {rows.map((row) => (
              <BarRow
                key={row.content_type}
                label={row.label ?? titleize(row.content_type)}
                value={row.engagement_rate ?? 0}
                max={max}
                display={formatRate(row.engagement_rate)}
                tone="brand"
                hint={`${formatCount(row.publications)} published · ${formatCount(row.views)} views · ${
                  row.avg_views === null ? "—" : formatCount(row.avg_views)
                } avg`}
              />
            ))}
          </div>
        )}
        <p className="border-t border-line px-5 py-3 text-xs leading-relaxed text-ink-500">
          Engagement per view, so a type does not score well merely for being the
          one you publish most.
        </p>
      </div>
    </section>
  );
}

/** Share of output against share of attention, per platform. */
function PlatformShare({ rows }) {
  const shares = platformShares(rows);
  if (shares.length < 2) return null;

  return (
    <section>
      <SectionHeader
        title="Effort against attention"
        subtitle="What share of the posts each platform takes, and what share of the views it returns."
      />
      <div className="panel">
        <CompareBars
          aLabel="Posts"
          bLabel="Views"
          rows={shares.map((row) => ({
            label: titleize(row.platform),
            a: row.publicationShare,
            b: row.viewShare,
            hint: `${formatRate(row.publicationShare, 0)} / ${formatRate(row.viewShare, 0)}`,
          }))}
        />
      </div>
    </section>
  );
}

/**
 * How fast posts found an audience, and which have stopped growing.
 *
 * Read from the stored snapshot series rather than the latest counter per
 * publication, which is what makes "the first day" a real measurement rather
 * than a guess about a cumulative number.
 */
function VelocityPanel({ velocity }) {
  if (velocity.error) {
    return (
      <section>
        <SectionHeader title="How fast it travels" />
        <ErrorBanner message={velocity.error} onRetry={velocity.reload} />
      </section>
    );
  }
  if (!velocity.data) {
    return (
      <section>
        <SectionHeader title="How fast it travels" />
        <Skeleton rows={2} />
      </section>
    );
  }

  const { benchmarks, fastest, stalled, early_window_hours, publications } =
    velocity.data;
  const earlyKey = earlyViewsKey(early_window_hours);

  if (publications === 0) {
    return null;
  }

  return (
    <section className="space-y-4">
      <SectionHeader
        title="How fast it travels"
        subtitle={`Measured over the first ${early_window_hours} hours, from the snapshot series rather than a single cumulative counter.`}
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="panel">
          <div className="border-b border-line px-5 py-3">
            <p className="text-sm font-medium text-ink-900">A normal first day</p>
            <p className="mt-0.5 text-xs text-ink-400">
              Your own median, per platform.
            </p>
          </div>
          {benchmarks.length === 0 ? (
            <p className="px-5 py-6 text-sm text-ink-500">Nothing measured yet.</p>
          ) : (
            <ul className="divide-y divide-line">
              {benchmarks.map((benchmark) => (
                <li key={benchmark.platform} className="px-5 py-3">
                  <p className="flex items-center justify-between gap-2 text-sm text-ink-900">
                    {titleize(benchmark.platform)}
                    <span className="font-mono text-xs text-ink-700">
                      {benchmark.median_early_views === null
                        ? "—"
                        : formatCount(benchmark.median_early_views)}
                    </span>
                  </p>
                  <p className="mt-0.5 text-xs text-ink-400">
                    {/* Whether the median is something to judge a post against,
                        or just an interesting number. The panel shows it either
                        way; only one of them is a benchmark. */}
                    {benchmark.reliable
                      ? `from ${benchmark.early_sample} posts`
                      : `only ${benchmark.early_sample} post${benchmark.early_sample === 1 ? "" : "s"} — not a benchmark yet`}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="panel">
          <div className="border-b border-line px-5 py-3">
            <p className="text-sm font-medium text-ink-900">Fastest starts</p>
            <p className="mt-0.5 text-xs text-ink-400">
              Most views in the first {early_window_hours} hours.
            </p>
          </div>
          {fastest.length === 0 ? (
            <p className="px-5 py-6 text-sm text-ink-500">Nothing measured yet.</p>
          ) : (
            <ul className="divide-y divide-line">
              {fastest.map((curve) => (
                <li key={curve.publication_id} className="px-5 py-3">
                  <Link
                    to={`/content/${curve.content_id}`}
                    className="flex items-start justify-between gap-3"
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm text-ink-900 hover:text-brand-500">
                        {curve.title}
                      </span>
                      <span className="mt-0.5 block text-xs text-ink-400">
                        {titleize(curve.platform)} · {formatWhen(curve.published_at)}
                      </span>
                    </span>
                    <span className="shrink-0 font-mono text-xs text-ink-700">
                      {formatCount(curve[earlyKey])}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="panel">
          <div className="border-b border-line px-5 py-3">
            <p className="text-sm font-medium text-ink-900">Stopped growing</p>
            <p className="mt-0.5 text-xs text-ink-400">
              Still live, no longer gaining. Worth a re-share.
            </p>
          </div>
          {stalled.length === 0 ? (
            <p className="px-5 py-6 text-sm text-ink-500">
              Nothing has stalled — everything published is still picking up
              views.
            </p>
          ) : (
            <ul className="divide-y divide-line">
              {stalled.map((curve) => (
                <li key={curve.publication_id} className="px-5 py-3">
                  <Link
                    to={`/content/${curve.content_id}`}
                    className="flex items-start justify-between gap-3"
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm text-ink-900 hover:text-brand-500">
                        {curve.title}
                      </span>
                      <span className="mt-0.5 block text-xs text-ink-400">
                        {titleize(curve.platform)} · {formatCount(curve.views)} views
                      </span>
                    </span>
                    <span className="shrink-0 font-mono text-xs text-ink-400">
                      {curve.views_per_day === null
                        ? "—"
                        : `${formatCount(curve.views_per_day)}/day`}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

/** The individual pieces that did best. */
function TopContent({ rows }) {
  return (
    <section>
      <SectionHeader
        title="Best performing"
        action={
          <Link to="/content" className="btn-quiet">
            All content
          </Link>
        }
      />
      {rows.length === 0 ? (
        <p className="panel px-5 py-6 text-sm text-ink-500">
          No metrics collected yet.
        </p>
      ) : (
        <ul className="panel divide-y divide-line">
          {rows.map((row) => (
            <li key={row.content_id} className="px-5 py-3">
              <Link
                to={`/content/${row.content_id}`}
                className="flex items-start justify-between gap-3"
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm text-ink-900 hover:text-brand-500">
                    {row.title}
                  </span>
                  <span className="mt-0.5 block text-xs text-ink-400">
                    {titleize(row.content_type)} · {formatRate(row.engagement_rate)}{" "}
                    engaged · {row.read_minutes} min read
                  </span>
                </span>
                <span className="shrink-0 font-mono text-xs text-ink-700">
                  {formatCount(row.views)} views
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Publish events per day — output, as opposed to the reader activity above. */
function PublishingRhythm({ timeline }) {
  return (
    <section>
      <SectionHeader title="Publishing rhythm" />
      <div className="panel px-5 py-4">
        <AreaChart
          label="Publications per day"
          points={(timeline ?? []).map((day) => ({
            label: day.date,
            value: day.publications,
          }))}
          formatValue={formatCount}
          emptyHint="Nothing published in the last 30 days."
        />
      </div>
    </section>
  );
}

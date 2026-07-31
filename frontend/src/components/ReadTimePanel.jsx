import { useState } from "react";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import {
  formatCount,
  formatDuration,
  formatRate,
  formatReadLength,
} from "../lib/format";
import { attentionShares, bandLabel, lengthPayoff } from "../lib/readTime";
import { AreaChart, BarRow, CompareBars } from "./ui/Charts";
import { ErrorBanner, SectionHeader, Skeleton, Stat } from "./ui/Bits";

/** Windows for the reader-minutes chart. 90 is the API's practical ceiling. */
const WINDOWS = [7, 30, 90];

/**
 * Read-time analytics: how much reading has gone out, how much of it was
 * actually done, and whether the long pieces earn their length.
 *
 * Loads separately from the rest of the dashboard rather than riding on
 * `/analytics/dashboard`. It is the last section on the page and the most
 * expensive to compute, and the things above it — a failed publication, a
 * draft waiting on review — are the ones somebody has to act on.
 */
export default function ReadTimePanel() {
  const [days, setDays] = useState(30);
  const readTime = useApi(() => api.readTime(), []);
  const trend = useApi(() => api.engagementTrend(days), [days]);

  if (readTime.error) {
    return (
      <section>
        <SectionHeader title="Read time" />
        <ErrorBanner message={readTime.error} onRetry={readTime.reload} />
      </section>
    );
  }

  if (!readTime.data) {
    return (
      <section>
        <SectionHeader title="Read time" />
        <Skeleton rows={3} />
      </section>
    );
  }

  const data = readTime.data;
  const bands = data.by_length ?? [];
  const shares = attentionShares(bands);
  const payoff = lengthPayoff(bands);
  // Zero here means no platform reports reads, not that nobody read anything —
  // the distinction is the difference between "quiet week" and "we are blind".
  const blind = data.publications_reporting_reads === 0;

  if (data.published_pieces === 0) {
    return (
      <section>
        <SectionHeader
          title="Read time"
          subtitle="How much reading Herald has published, and whether length pays off."
        />
        <p className="panel px-5 py-6 text-sm text-ink-500">
          Nothing published yet. Reading time appears once a piece goes out.
        </p>
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <SectionHeader
        title="Read time"
        subtitle="How much reading Herald has published, and whether length pays off."
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Reader-minutes"
          value={blind ? "—" : formatDuration(data.reader_minutes)}
          hint={
            blind
              ? "No platform you publish to counts reads"
              : `across ${formatCount(data.publications_reporting_reads)} publications that count reads`
          }
        />
        <Stat
          label="Average length"
          value={
            data.avg_read_minutes === null ? "—" : `${data.avg_read_minutes} min`
          }
          hint={`${formatCount(data.total_words)} words published`}
        />
        <Stat
          label="Read rate"
          value={formatRate(data.read_rate)}
          hint="of views that finished the piece"
        />
        <Stat
          label="Published"
          value={formatCount(data.published_pieces)}
          hint={
            shares.totalPublications > 0
              ? `${formatCount(shares.totalPublications)} publications measured`
              : "no metrics collected yet"
          }
        />
      </div>

      {blind && (
        <p className="rounded-lg border border-line bg-canvas px-4 py-3 text-xs leading-relaxed text-ink-500">
          Reader-minutes count <em>reads</em>, not views — a visitor arriving is
          not a visitor reading. Only Dev.to and Medium report reads; until a
          piece goes out on one of them, this stays empty rather than guessing.
        </p>
      )}

      {/* ---- Reader-minutes over time ---- */}
      <div className="panel">
        <div className="flex items-center justify-between gap-4 border-b border-line px-5 py-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-ink-900">Reader-minutes</p>
            <p className="mt-0.5 text-xs text-ink-400">
              Reads each day, weighted by how long the piece takes to read.
            </p>
          </div>
          <div className="flex shrink-0 gap-1" role="group" aria-label="Window">
            {WINDOWS.map((window) => (
              <button
                key={window}
                onClick={() => setDays(window)}
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
            <AreaChart
              label="Reader-minutes per day"
              points={(trend.data ?? []).map((day) => ({
                label: day.date,
                value: day.reader_minutes ?? 0,
              }))}
              formatValue={formatDuration}
              emptyHint={
                blind
                  ? "No platform you publish to counts reads, so there are no reader-minutes to plot."
                  : `No reads recorded in the last ${days} days.`
              }
            />
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* ---- Does length pay off? ---- */}
        <div className="panel">
          <div className="border-b border-line px-5 py-3">
            <p className="text-sm font-medium text-ink-900">Does length pay off?</p>
            <p className="mt-0.5 text-xs text-ink-400">
              Engagement per view, so a long piece gets no credit for the extra
              views its promotion earned.
            </p>
          </div>
          <div className="divide-y divide-line">
            {bands.map((band, index) => (
              <BarRow
                key={band.band}
                label={bandLabel(bands, index).full}
                value={band.engagement_rate ?? 0}
                max={Math.max(...bands.map((b) => b.engagement_rate ?? 0), 0)}
                display={formatRate(band.engagement_rate)}
                tone={payoff.verdict !== "unknown" && isWinner(band, payoff) ? "brand" : "muted"}
                hint={
                  band.publications === 0
                    ? "nothing published at this length"
                    : `${formatCount(band.publications)} published · ${formatCount(band.views)} views · ${formatRate(band.read_rate)} read`
                }
              />
            ))}
          </div>
          <p className="border-t border-line px-5 py-3 text-xs leading-relaxed text-ink-500">
            {payoff.sentence}
          </p>
        </div>

        {/* ---- Where the reading actually happens ---- */}
        <div className="panel">
          <div className="border-b border-line px-5 py-3">
            <p className="text-sm font-medium text-ink-900">
              Output against attention
            </p>
            <p className="mt-0.5 text-xs text-ink-400">
              What share of the pieces sits in each band, and what share of the
              minutes spent reading them does.
            </p>
          </div>
          {shares.totalMinutes === 0 ? (
            <p className="px-5 py-6 text-sm text-ink-500">
              {blind
                ? "Nothing here counts reads, so there are no minutes to attribute."
                : "No reads recorded yet."}
            </p>
          ) : (
            <CompareBars
              aLabel="Pieces"
              bLabel="Minutes read"
              rows={shares.rows.map((row) => ({
                label: row.label,
                a: row.publicationShare,
                b: row.minuteShare,
                hint: `${formatRate(row.publicationShare, 0)} / ${formatRate(row.minuteShare, 0)}`,
              }))}
            />
          )}
        </div>
      </div>

      {/* ---- Distribution across content ---- */}
      <div className="panel">
        <div className="border-b border-line px-5 py-3">
          <p className="text-sm font-medium text-ink-900">Length distribution</p>
          <p className="mt-0.5 text-xs text-ink-400">
            Publications by how long the piece takes to read.
          </p>
        </div>
        <div className="divide-y divide-line">
          {bands.map((band, index) => (
            <BarRow
              key={band.band}
              label={bandLabel(bands, index).full}
              value={band.publications}
              max={Math.max(...bands.map((b) => b.publications), 0)}
              display={`${formatCount(band.publications)} pub`}
              tone="brand"
              hint={
                band.avg_read_minutes === null
                  ? "nothing published at this length"
                  : `${formatReadLength(band.avg_read_minutes)} on average · ${formatDuration(band.reader_minutes ?? 0)} read`
              }
            />
          ))}
        </div>
      </div>
    </section>
  );
}

/** The band the payoff verdict is pointing at, if it is pointing at one. */
function isWinner(band, payoff) {
  if (payoff.verdict === "longer") return band.band === payoff.longer?.band;
  if (payoff.verdict === "shorter") return band.band === payoff.shorter?.band;
  return false;
}

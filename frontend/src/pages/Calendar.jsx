import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner, Skeleton } from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { cadenceProvenance, isLearned } from "../lib/alerts";
import { api } from "../lib/api";
import {
  UNROUTED,
  bucketByDay,
  canDropOn,
  dropTarget,
  filterEntries,
  platformsIn,
  summarize,
  visibleEntries,
} from "../lib/calendar";
import { formatDateTime, localDayKey, statusTone, titleize } from "../lib/format";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Monday-first grid covering the whole month plus the padding days. */
function monthGrid(anchor) {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  // getDay() is Sunday-first; shift so Monday is 0.
  const lead = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - lead);

  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    return date;
  });
}

/**
 * The content calendar.
 *
 * Drag-and-drop moves an item to another day, keeping its time of day — a post
 * scheduled for 1pm Tuesday that you drag to Thursday should be 1pm Thursday,
 * not midnight. Published items are not draggable: the past is a record.
 */
export default function Calendar() {
  const toast = useToast();
  const [anchor, setAnchor] = useState(() => new Date());
  const [dragging, setDragging] = useState(null);
  const [hoverDay, setHoverDay] = useState(null);
  // Null on either facet means "not filtering on this".
  const [platform, setPlatform] = useState(null);
  const [bucket, setBucket] = useState(null);
  // The one day currently showing everything it holds. One rather than a set:
  // expanding a day is a glance at it, and a month with six stretched rows is
  // the ragged grid the cap exists to prevent.
  const [expandedDay, setExpandedDay] = useState(null);

  const range = useMemo(() => {
    const days = monthGrid(anchor);
    return {
      start: days[0].toISOString(),
      end: new Date(days[41].getTime() + 86_400_000 - 1).toISOString(),
      days,
    };
  }, [anchor]);

  const { data, error, loading, reload } = useApi(
    () => api.calendar({ start: range.start, end: range.end }),
    [range.start, range.end],
  );

  const entries = data?.entries ?? [];
  // Offered from the unfiltered set, so choosing a platform never removes the
  // chip you would need to get back.
  const platforms = useMemo(() => platformsIn(entries), [entries]);
  const shown = useMemo(
    () => filterEntries(entries, { platform, bucket }),
    [entries, platform, bucket],
  );
  const byDay = useMemo(() => bucketByDay(shown), [shown]);
  const counts = useMemo(() => summarize(entries), [entries]);

  async function drop(day) {
    setHoverDay(null);
    const entry = dragging;
    setDragging(null);
    if (!entry) return;

    // Preserve the time of day, change only the date.
    const original = new Date(entry.when);
    const target = dropTarget(entry, day);
    if (target.getTime() === original.getTime()) return;
    // Belt and braces: the cell should not have accepted the drop, but a
    // long drag can outlive the moment that made it legal.
    if (!canDropOn(entry, day)) return;

    try {
      await api.reschedule(entry.content_id, {
        scheduled_for: target.toISOString(),
        publication_id: entry.publication_id,
      });
      toast.success(`Moved to ${formatDateTime(target)}`);
      reload();
    } catch (err) {
      toast.error(err.message);
    }
  }

  const monthLabel = anchor.toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
  const todayKey = localDayKey(new Date());

  return (
    <div className="stagger space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Calendar</h1>
          <p className="mt-1 text-sm text-ink-500">
            {/* What the window holds, so "is anything going out?" does not mean
                scanning 42 squares for a coloured chip. */}
            {counts.total === 0
              ? "Nothing on the calendar in this window."
              : [
                  `${counts.upcoming} going out`,
                  `${counts.published} published`,
                  counts.failed > 0 ? `${counts.failed} failed` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
            . Drag a scheduled item to another day to move it.
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button
            className="btn-ghost"
            onClick={() =>
              setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() - 1, 1))
            }
            aria-label="Previous month"
          >
            ←
          </button>
          <span className="min-w-[9rem] text-center text-sm font-medium text-ink-900">
            {monthLabel}
          </span>
          <button
            className="btn-ghost"
            onClick={() =>
              setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1))
            }
            aria-label="Next month"
          >
            →
          </button>
          <button className="btn-quiet ml-2" onClick={() => setAnchor(new Date())}>
            Today
          </button>
        </div>
      </div>

      <ErrorBanner message={error} onRetry={reload} />

      {counts.total > 0 && (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <FilterRow
            label="Status"
            value={bucket}
            onChange={setBucket}
            options={[
              { value: "upcoming", label: "Going out", count: counts.upcoming },
              { value: "published", label: "Published", count: counts.published },
              // Only offered when there is one. A chip that always filters to
              // nothing teaches the user to ignore the row.
              ...(counts.failed > 0
                ? [{ value: "failed", label: "Failed", count: counts.failed }]
                : []),
            ]}
          />
          {platforms.length > 1 && (
            <FilterRow
              label="Platform"
              value={platform}
              onChange={setPlatform}
              options={platforms.map((value) => ({
                value,
                label: value === UNROUTED ? "Not routed" : titleize(value),
              }))}
            />
          )}
        </div>
      )}

      {counts.total > 0 && shown.length === 0 && (
        // An empty grid with a filter on looks identical to an empty month.
        <p className="rounded-lg border border-line bg-canvas px-4 py-3 text-sm text-ink-500">
          Nothing in this month matches the filter.{" "}
          <button
            className="text-brand-500 hover:underline"
            onClick={() => {
              setPlatform(null);
              setBucket(null);
            }}
          >
            Clear it
          </button>
          .
        </p>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_280px]">
        {loading && !data ? (
          <Skeleton rows={6} />
        ) : (
          <div className="panel overflow-hidden">
            <div className="grid grid-cols-7 border-b border-line bg-canvas">
              {WEEKDAYS.map((day) => (
                <div key={day} className="eyebrow px-2 py-2 text-center">
                  {day}
                </div>
              ))}
            </div>
            <div className="grid grid-cols-7">
              {range.days.map((day) => {
                const key = localDayKey(day);
                const { shown: dayEntries, hidden } = visibleEntries(byDay.get(key), {
                  expanded: expandedDay === key,
                });
                const inMonth = day.getMonth() === anchor.getMonth();
                // Only while something is being dragged: a day that cannot
                // receive *this* entry is not a day that is broken, and dimming
                // the past unprompted would be noise on a calendar whose whole
                // left-hand side is history.
                const rejects = dragging !== null && !canDropOn(dragging, day);
                return (
                  <div
                    key={key}
                    onDragOver={(event) => {
                      if (!dragging || rejects) return;
                      // Not calling preventDefault is what makes the browser
                      // show its own "no drop" cursor — a self-explanatory
                      // refusal, rather than a round-trip that comes back as a
                      // red toast saying the time is in the past.
                      event.preventDefault();
                      setHoverDay(key);
                    }}
                    onDragLeave={() => setHoverDay((c) => (c === key ? null : c))}
                    onDrop={() => drop(day)}
                    aria-disabled={rejects || undefined}
                    className={[
                      "min-h-[104px] border-b border-r border-line p-1.5 transition-colors",
                      inMonth ? "bg-paper" : "bg-canvas/60",
                      hoverDay === key ? "bg-brand-50 ring-1 ring-inset ring-brand-300" : "",
                      rejects ? "opacity-50" : "",
                    ].join(" ")}
                  >
                    <div className="mb-1 flex items-center justify-between px-1">
                      <span
                        className={[
                          "font-mono text-[11px]",
                          key === todayKey
                            ? "rounded bg-brand-500 px-1.5 py-0.5 text-white"
                            : inMonth
                              ? "text-ink-500"
                              : "text-ink-400/60",
                        ].join(" ")}
                      >
                        {day.getDate()}
                      </span>
                    </div>
                    <div className="space-y-1">
                      {dayEntries.map((entry) => (
                        <CalendarChip
                          key={`${entry.content_id}-${entry.publication_id ?? "none"}`}
                          entry={entry}
                          onDragStart={() => entry.movable && setDragging(entry)}
                          onDragEnd={() => {
                            setDragging(null);
                            setHoverDay(null);
                          }}
                        />
                      ))}
                      {hidden > 0 && (
                        <button
                          className="block w-full rounded px-1.5 py-0.5 text-left text-[11px] text-ink-500 hover:bg-ink-900/[0.04] hover:text-ink-900"
                          onClick={() => setExpandedDay(key)}
                        >
                          +{hidden} more
                        </button>
                      )}
                      {expandedDay === key && (
                        <button
                          className="block w-full rounded px-1.5 py-0.5 text-left text-[11px] text-ink-500 hover:bg-ink-900/[0.04] hover:text-ink-900"
                          onClick={() => setExpandedDay(null)}
                        >
                          Show less
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <aside className="space-y-4">
          <div className="panel p-5">
            <h2 className="mb-3 text-sm font-semibold text-ink-900">Suggested slots</h2>
            {(data?.suggested_slots ?? []).length === 0 ? (
              <p className="text-xs text-ink-400">
                Connect a platform in Settings and Herald will suggest good times to
                post.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {data.suggested_slots.map((slot) => (
                  <li key={slot} className="font-mono text-[11px] text-ink-500">
                    {formatDateTime(slot)}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="panel p-5">
            <h2 className="mb-3 text-sm font-semibold text-ink-900">Cadence</h2>
            {(data?.cadence ?? []).length === 0 ? (
              <p className="text-xs text-ink-400">
                No platforms connected yet.
              </p>
            ) : (
              <ul className="space-y-3">
                {data.cadence.map((item) => (
                  <li key={item.platform}>
                    <p className="flex items-center justify-between gap-2 text-xs font-medium text-ink-900">
                      {titleize(item.platform)}
                      {/* Which of the two answered — the user's own results, or
                          the generic table. A suggested time nobody can
                          interrogate is one they are right to ignore. */}
                      <span
                        className={`chip ${isLearned(item) ? "text-brand-500" : "text-ink-400"}`}
                        title={
                          isLearned(item)
                            ? "Derived from your own first-day views on this platform"
                            : "Published guidance — not enough of your own posts yet"
                        }
                      >
                        {cadenceProvenance(item)}
                      </span>
                    </p>
                    <p className="mt-0.5 font-mono text-[11px] text-ink-500">
                      ≤{item.max_per_week}/week · {item.best_weekdays.join(", ")} ·{" "}
                      {item.best_time_utc} UTC
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-ink-400">
                      {item.rationale}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

/**
 * One facet of the filter row, as toggle chips.
 *
 * Clicking the active chip clears it rather than doing nothing — "All" is the
 * absence of a filter, not a fourth option, and a row that needs a separate
 * reset control for two chips is a row with three chips in it.
 */
function FilterRow({ label, value, onChange, options }) {
  return (
    <div className="flex items-center gap-2" role="group" aria-label={label}>
      <span className="eyebrow">{label}</span>
      <div className="flex flex-wrap gap-1">
        {options.map((option) => {
          const active = value === option.value;
          return (
            <button
              key={option.value}
              aria-pressed={active}
              onClick={() => onChange(active ? null : option.value)}
              className={`rounded-md px-2 py-1 font-mono text-[11px] transition-colors ${
                active
                  ? "bg-brand-50 text-brand-600"
                  : "text-ink-400 hover:bg-ink-900/[0.04] hover:text-ink-700"
              }`}
            >
              {option.label}
              {option.count !== undefined && (
                <span className="ml-1.5 text-ink-400">{option.count}</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CalendarChip({ entry, onDragStart, onDragEnd }) {
  return (
    <Link
      to={`/content/${entry.content_id}`}
      draggable={entry.movable}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      title={`${entry.title} — ${entry.project_name} — ${formatDateTime(entry.when)}`}
      className={[
        "block truncate rounded px-1.5 py-1 text-[11px] leading-tight transition-shadow",
        statusTone(entry.status),
        entry.movable ? "cursor-grab active:cursor-grabbing hover:shadow-card" : "cursor-default",
      ].join(" ")}
    >
      {entry.platform && (
        <span className="mr-1 font-mono opacity-70">
          {entry.platform.slice(0, 2).toUpperCase()}
        </span>
      )}
      {entry.title}
    </Link>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner, Skeleton } from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { cadenceProvenance, isLearned, weekdaysLearned } from "../lib/alerts";
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
 * Moving an item to another day keeps its time of day — a post scheduled for 1pm
 * Tuesday that lands on Thursday should be 1pm Thursday, not midnight. Published
 * items do not move at all: the past is a record.
 *
 * Two ways to do it, one meaning. Drag-and-drop, and the arrow keys on a focused
 * chip. The keyboard route is not an accessibility afterthought bolted beside the
 * real one — it goes through the same `move`, with the same `canDropOn` guard, so
 * there is no second implementation to drift. Until it existed, rescheduling was
 * the one thing on this page a keyboard could not do at all.
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

  // The chip a keyboard move should get focus back to, once the reload that
  // move triggered has redrawn the month. A ref rather than state: it is read
  // once by the effect below and must not cause a render of its own.
  const refocus = useRef(null);
  useEffect(() => {
    const key = refocus.current;
    if (key === null) return;
    refocus.current = null;
    document.querySelector(`[data-chip="${key}"]`)?.focus();
  }, [data]);

  /**
   * Put `entry` on `day`, keeping its time of day.
   *
   * Shared by the drag and the arrow keys so the two cannot come to disagree
   * about what a move means. `explain` is the one difference: a drag onto a cell
   * that will not take it has already been refused visibly, by the browser's own
   * "no drop" cursor over a dimmed square, where a key press has no such
   * feedback and silence reads as a key that does not work.
   *
   * `canDropOn` refuses for two reasons — the entry cannot move at all, or the
   * target is in the past — but only one of them can reach the message below.
   * `explain` comes from `nudge` alone, `nudge` from `CalendarChip`'s
   * `onKeyDown`, and that handler is not wired at all unless `entry.movable`.
   * So an explained refusal is always a movable entry aimed at the past.
   */
  async function move(entry, day, { explain = false } = {}) {
    // Preserve the time of day, change only the date.
    const original = new Date(entry.when);
    const target = dropTarget(entry, day);
    if (target.getTime() === original.getTime()) return;
    // Belt and braces for a drag: the cell should not have accepted the drop,
    // but a long drag can outlive the moment that made it legal.
    if (!canDropOn(entry, day)) {
      if (explain) toast.error(`${formatDateTime(target)} is in the past.`);
      refocus.current = null;
      return;
    }

    try {
      await api.reschedule(entry.content_id, {
        scheduled_for: target.toISOString(),
        publication_id: entry.publication_id,
      });
      toast.success(`Moved to ${formatDateTime(target)}`);
      reload();
    } catch (err) {
      toast.error(err.message);
      refocus.current = null;
    }
  }

  async function drop(day) {
    setHoverDay(null);
    const entry = dragging;
    setDragging(null);
    if (!entry) return;
    await move(entry, day);
  }

  /**
   * The keyboard's version of the drag: ±1 day sideways, ±1 week vertically,
   * matching the direction the chip would visibly travel on the grid.
   *
   * One request per press rather than a pick-up/put-down mode. Rescheduling is
   * already undoable by moving it back, and a mode would need its own state, its
   * own escape key and its own way of telling the user they are in it — for an
   * interaction that is nearly always a nudge of a day or two.
   */
  function nudge(entry, days) {
    const day = new Date(entry.when);
    day.setDate(day.getDate() + days);
    // The chip is about to be re-rendered into a different cell; remember which
    // one to hand focus back to, or a keyboard user loses their place on every
    // move and has to tab back in from the top of the month.
    refocus.current = chipKey(entry);
    return move(entry, day, { explain: true });
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
              ? "Nothing scheduled."
              : [
                  `${counts.upcoming} going out`,
                  `${counts.published} published`,
                  counts.failed > 0 ? `${counts.failed} failed` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
            . Drag or arrow-key to reschedule.
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
                            ? "rounded bg-brand-500 px-1.5 py-0.5 text-canvas"
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
                          key={chipKey(entry)}
                          entry={entry}
                          onDragStart={() => entry.movable && setDragging(entry)}
                          onDragEnd={() => {
                            setDragging(null);
                            setHoverDay(null);
                          }}
                          onNudge={(days) => nudge(entry, days)}
                        />
                      ))}
                      {hidden > 0 && (
                        <button
                          className="block w-full rounded px-1.5 py-0.5 text-left text-[11px] text-ink-500 hover:bg-ink-400/10 hover:text-ink-900"
                          onClick={() => setExpandedDay(key)}
                        >
                          +{hidden} more
                        </button>
                      )}
                      {expandedDay === key && (
                        <button
                          className="block w-full rounded px-1.5 py-0.5 text-left text-[11px] text-ink-500 hover:bg-ink-400/10 hover:text-ink-900"
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
            <h2 className="mb-3 text-sm font-semibold text-ink-900">Color key</h2>
            <ul className="space-y-1.5 text-[11px]">
              <li className="flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-[#60A5FA]" />
                <span className="text-ink-500">Blog / Article</span>
              </li>
              <li className="flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-[#34D399]" />
                <span className="text-ink-500">Social</span>
              </li>
              <li className="flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-[#D4AF37]" />
                <span className="text-ink-500">Spotlight</span>
              </li>
            </ul>
          </div>

          <div className="panel p-5">
            <h2 className="mb-3 text-sm font-semibold text-ink-900">Suggested slots</h2>
            {(data?.suggested_slots ?? []).length === 0 ? (
              <p className="text-xs text-ink-400">
                Connect a platform to get slot suggestions.
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
                          !isLearned(item)
                            ? "Published guidance — not enough of your own posts yet"
                            : weekdaysLearned(item)
                              ? "Derived from your own first-day views on this platform"
                              : "The hour comes from your own first-day views; the days are still published guidance — no single weekday has enough posts behind it yet"
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
                  : "text-ink-400 hover:bg-ink-400/10 hover:text-ink-700"
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

/** Identifies one chip across a reload, for handing focus back after a move. */
function chipKey(entry) {
  return `${entry.content_id}-${entry.publication_id ?? "none"}`;
}

/** Arrow key to day offset. Sideways by a day, vertically by a grid row. */
const NUDGES = {
  ArrowLeft: -1,
  ArrowRight: 1,
  ArrowUp: -7,
  ArrowDown: 7,
};

/** Color by content type for visual pipeline health. Falls back to status
 *  tone for terminal states (failed, published) where the status matters more. */
function typeTone(entry) {
  if (entry.status === "failed") return "bg-bad-wash text-bad";
  if (entry.status === "published") return "bg-good-wash text-good";
  const type = entry.content_type;
  if (type === "social_thread") return "bg-[rgba(52,211,153,0.15)] text-[#34D399]";
  if (type === "product_spotlight" || type === "feature_spotlight")
    return "bg-[rgba(240,180,41,0.12)] text-[#D4AF37]";
  return "bg-[rgba(96,165,250,0.12)] text-[#60A5FA]";
}

function CalendarChip({ entry, onDragStart, onDragEnd, onNudge }) {
  return (
    <Link
      to={`/content/${entry.content_id}`}
      data-chip={chipKey(entry)}
      draggable={entry.movable}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onKeyDown={
        entry.movable
          ? (event) => {
              const days = NUDGES[event.key];
              if (days === undefined) return;
              event.preventDefault();
              onNudge(days);
            }
          : undefined
      }
      aria-keyshortcuts={entry.movable ? "ArrowLeft ArrowRight ArrowUp ArrowDown" : undefined}
      title={[
        entry.title,
        entry.project_name,
        titleize(entry.content_type),
        formatDateTime(entry.when),
        entry.movable ? "arrow keys move it" : null,
      ]
        .filter(Boolean)
        .join(" — ")}
      className={[
        "block truncate rounded px-1.5 py-1 text-[11px] leading-tight transition-shadow",
        typeTone(entry),
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

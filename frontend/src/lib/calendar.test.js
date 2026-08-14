import { describe, expect, it } from "vitest";
import {
  DAY_ENTRY_CAP,
  UNROUTED,
  bucketByDay,
  canDropOn,
  dropTarget,
  filterEntries,
  platformsIn,
  statusBucket,
  summarize,
  visibleEntries,
} from "./calendar";

function entry(overrides = {}) {
  return {
    content_id: 1,
    publication_id: 1,
    title: "A post",
    project_id: 1,
    project_name: "Herald",
    content_type: "announcement",
    platform: "devto",
    status: "scheduled",
    when: "2026-08-04T13:00:00Z",
    movable: true,
    ...overrides,
  };
}

describe("statusBucket", () => {
  it("collapses the many statuses into the three that change the answer", () => {
    expect(statusBucket(entry({ status: "published" }))).toBe("published");
    expect(statusBucket(entry({ status: "failed" }))).toBe("failed");
    for (const status of ["scheduled", "pending", "approved", "draft", "review"]) {
      expect(statusBucket(entry({ status }))).toBe("upcoming");
    }
  });
});

describe("platformsIn", () => {
  it("offers only what is actually on the calendar", () => {
    // A chip that filters the month down to nothing is a control that only
    // ever disappoints.
    const entries = [entry({ platform: "devto" }), entry({ platform: "bluesky" })];
    expect(platformsIn(entries)).toEqual(["bluesky", "devto"]);
  });

  it("gives unrouted content its own bucket, pinned last", () => {
    const entries = [
      entry({ platform: "devto" }),
      entry({ platform: null, publication_id: null }),
    ];
    expect(platformsIn(entries)).toEqual(["devto", UNROUTED]);
  });

  it("says nothing about an empty calendar", () => {
    expect(platformsIn([])).toEqual([]);
    expect(platformsIn(undefined)).toEqual([]);
  });
});

describe("filterEntries", () => {
  const entries = [
    entry({ content_id: 1, platform: "devto", status: "published" }),
    entry({ content_id: 2, platform: "bluesky", status: "scheduled" }),
    entry({ content_id: 3, platform: null, status: "approved" }),
    entry({ content_id: 4, platform: "devto", status: "failed" }),
  ];

  it("returns everything when neither facet is set", () => {
    expect(filterEntries(entries, {})).toHaveLength(4);
    expect(filterEntries(entries)).toHaveLength(4);
  });

  it("filters nothing into nothing while the month is still loading", () => {
    expect(filterEntries(undefined, { platform: "devto" })).toEqual([]);
    expect(filterEntries(undefined)).toEqual([]);
  });

  it("filters by platform", () => {
    expect(filterEntries(entries, { platform: "devto" }).map((e) => e.content_id)).toEqual([
      1, 4,
    ]);
  });

  it("finds the pieces that are not routed anywhere", () => {
    // The set most worth finding: these go nowhere until someone picks a
    // destination.
    expect(
      filterEntries(entries, { platform: UNROUTED }).map((e) => e.content_id),
    ).toEqual([3]);
  });

  it("filters by status bucket", () => {
    expect(
      filterEntries(entries, { bucket: "upcoming" }).map((e) => e.content_id),
    ).toEqual([2, 3]);
  });

  it("applies both facets together", () => {
    expect(
      filterEntries(entries, { platform: "devto", bucket: "failed" }).map(
        (e) => e.content_id,
      ),
    ).toEqual([4]);
  });
});

describe("bucketByDay", () => {
  it("groups by the local day, not the UTC one", () => {
    // A 23:00 UTC post is the next day in Berlin and the same day in London.
    // Whichever the runner is in, the key must match what the grid computes
    // for the square it draws.
    const late = entry({ when: "2026-08-04T23:30:00Z" });
    const buckets = bucketByDay([late]);
    const expected = new Date("2026-08-04T23:30:00Z");
    const key = `${expected.getFullYear()}-${String(expected.getMonth() + 1).padStart(2, "0")}-${String(expected.getDate()).padStart(2, "0")}`;
    expect([...buckets.keys()]).toEqual([key]);
  });

  it("keeps several entries on one day in the order given", () => {
    const buckets = bucketByDay([
      entry({ content_id: 1, when: "2026-08-04T09:00:00Z" }),
      entry({ content_id: 2, when: "2026-08-04T13:00:00Z" }),
    ]);
    expect([...buckets.values()][0].map((e) => e.content_id)).toEqual([1, 2]);
  });

  it("survives an empty calendar", () => {
    expect(bucketByDay(undefined).size).toBe(0);
  });
});

describe("visibleEntries", () => {
  const many = Array.from({ length: 6 }, (_, i) => entry({ content_id: i }));

  it("shows everything when it fits", () => {
    const three = many.slice(0, DAY_ENTRY_CAP);
    expect(visibleEntries(three)).toEqual({ shown: three, hidden: 0 });
  });

  it("holds the rest back rather than stretching the row", () => {
    const { shown, hidden } = visibleEntries(many);
    expect(shown).toHaveLength(DAY_ENTRY_CAP);
    expect(hidden).toBe(6 - DAY_ENTRY_CAP);
  });

  it("never reports a hidden count of zero as an overflow", () => {
    // The caller renders "+N more" on this number, and "+0 more" is a lie.
    expect(visibleEntries(many.slice(0, DAY_ENTRY_CAP)).hidden).toBe(0);
    expect(visibleEntries([]).hidden).toBe(0);
    expect(visibleEntries(undefined).hidden).toBe(0);
  });

  it("shows all of them once the day is expanded", () => {
    expect(visibleEntries(many, { expanded: true })).toEqual({ shown: many, hidden: 0 });
  });
});

describe("summarize", () => {
  it("counts each bucket and the total", () => {
    const counts = summarize([
      entry({ status: "published" }),
      entry({ status: "published" }),
      entry({ status: "scheduled" }),
      entry({ status: "failed" }),
    ]);
    expect(counts).toEqual({ published: 2, upcoming: 1, failed: 1, total: 4 });
  });

  it("counts an empty calendar as empty", () => {
    expect(summarize([])).toEqual({ published: 0, upcoming: 0, failed: 0, total: 0 });
    // The header line renders before the month's first response arrives.
    expect(summarize(undefined)).toEqual({
      published: 0,
      upcoming: 0,
      failed: 0,
      total: 0,
    });
  });
});

describe("dropTarget", () => {
  it("changes the date and keeps the time of day", () => {
    // 13:00 UTC — read back in local time, whatever that is here.
    const original = new Date("2026-08-04T13:00:00Z");
    const target = dropTarget(entry(), new Date(2026, 7, 6));

    expect(target.getFullYear()).toBe(2026);
    expect(target.getMonth()).toBe(7);
    expect(target.getDate()).toBe(6);
    expect(target.getHours()).toBe(original.getHours());
    expect(target.getMinutes()).toBe(original.getMinutes());
  });

  it("zeroes the seconds so a move is not a fraction off its slot", () => {
    const target = dropTarget(entry({ when: "2026-08-04T13:00:37Z" }), new Date(2026, 7, 6));
    expect(target.getSeconds()).toBe(0);
    expect(target.getMilliseconds()).toBe(0);
  });
});

describe("canDropOn", () => {
  const now = new Date(2026, 7, 4, 12, 0, 0);

  it("accepts a day in the future", () => {
    expect(canDropOn(entry(), new Date(2026, 7, 20), { now })).toBe(true);
  });

  it("refuses a day in the past", () => {
    // The API refuses this too — a schedule behind "now" is a publish on the
    // next sweep wearing a date. Refusing here means the browser shows its own
    // "no drop" cursor instead of a red toast after a round-trip.
    expect(canDropOn(entry(), new Date(2026, 6, 20), { now })).toBe(false);
  });

  it("judges the resulting moment, not the day", () => {
    // Dropping a 09:00 post onto today at noon still lands in the past; a rule
    // that only compared dates would wave it through.
    const morning = entry({ when: new Date(2026, 7, 4, 9, 0, 0).toISOString() });
    const evening = entry({ when: new Date(2026, 7, 4, 18, 0, 0).toISOString() });
    expect(canDropOn(morning, new Date(2026, 7, 4), { now })).toBe(false);
    expect(canDropOn(evening, new Date(2026, 7, 4), { now })).toBe(true);
  });

  it("refuses anything the calendar already marks unmovable", () => {
    const published = entry({ movable: false });
    expect(canDropOn(published, new Date(2026, 7, 20), { now })).toBe(false);
  });

  it("refuses when there is nothing being dragged", () => {
    expect(canDropOn(null, new Date(2026, 7, 20), { now })).toBe(false);
    expect(canDropOn(undefined, new Date(2026, 7, 20), { now })).toBe(false);
  });
});

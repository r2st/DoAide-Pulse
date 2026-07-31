import { describe, expect, it } from "vitest";
import { attentionShares, bandLabel, lengthPayoff } from "./readTime";

/** A band as `/analytics/read-time` sends it, with the interesting bits set. */
function band(name, max, overrides = {}) {
  return {
    band: name,
    max_read_minutes: max,
    publications: 0,
    avg_read_minutes: null,
    views: 0,
    reads: 0,
    clicks: 0,
    engagement: 0,
    reader_minutes: 0,
    avg_views: null,
    click_through_rate: null,
    read_rate: null,
    engagement_rate: null,
    ...overrides,
  };
}

const SHORT = (o) => band("short", 3, o);
const MEDIUM = (o) => band("medium", 8, o);
const LONG = (o) => band("long", null, o);

describe("bandLabel", () => {
  it("derives each band's range from its neighbour's upper bound", () => {
    const bands = [SHORT(), MEDIUM(), LONG()];
    expect(bandLabel(bands, 0)).toMatchObject({ name: "Short", range: "1–3 min" });
    expect(bandLabel(bands, 1)).toMatchObject({ name: "Medium", range: "4–8 min" });
    expect(bandLabel(bands, 2)).toMatchObject({ name: "Long", range: "9+ min" });
    expect(bandLabel(bands, 2).full).toBe("Long · 9+ min");
  });

  it("collapses a one-minute band rather than printing '1–1 min'", () => {
    expect(bandLabel([band("short", 1)], 0).range).toBe("1 min");
  });
});

describe("lengthPayoff", () => {
  it("says long wins when long earns more per view", () => {
    const result = lengthPayoff([
      SHORT({ publications: 6, views: 1000, engagement: 40, engagement_rate: 0.04 }),
      MEDIUM({ publications: 3, views: 500, engagement: 40, engagement_rate: 0.08 }),
      LONG({ publications: 2, views: 400, engagement: 48, engagement_rate: 0.12 }),
    ]);
    expect(result.verdict).toBe("longer");
    expect(result.ratio).toBeCloseTo(3);
    expect(result.sentence).toBe(
      "Long pieces earn 3.0× the engagement per view of short ones.",
    );
  });

  it("says short wins when the short pieces earn more per view", () => {
    const result = lengthPayoff([
      SHORT({ publications: 6, views: 1000, engagement: 100, engagement_rate: 0.1 }),
      LONG({ publications: 2, views: 400, engagement: 8, engagement_rate: 0.02 }),
    ]);
    expect(result.verdict).toBe("shorter");
    expect(result.sentence).toContain("Short pieces earn 5.0×");
  });

  it("compares per view, not per post", () => {
    // Long collects five times the raw engagement — entirely because it was
    // shown to five times as many people. Per view it is behind.
    const result = lengthPayoff([
      SHORT({ publications: 6, views: 100, engagement: 10, engagement_rate: 0.1 }),
      LONG({ publications: 2, views: 1000, engagement: 50, engagement_rate: 0.05 }),
    ]);
    expect(result.verdict).toBe("shorter");
  });

  it("calls a difference inside a tenth even", () => {
    const result = lengthPayoff([
      SHORT({ publications: 4, views: 500, engagement: 20, engagement_rate: 0.04 }),
      LONG({ publications: 4, views: 500, engagement: 21, engagement_rate: 0.042 }),
    ]);
    expect(result.verdict).toBe("even");
    expect(result.sentence).toContain("not deciding this");
  });

  it("cannot answer from one band alone", () => {
    const result = lengthPayoff([
      SHORT({ publications: 6, views: 1000, engagement: 40, engagement_rate: 0.04 }),
      MEDIUM(),
      LONG(),
    ]);
    expect(result.verdict).toBe("unknown");
    expect(result.ratio).toBeNull();
    expect(result.sentence).toContain("Not enough range");
  });

  it("ignores bands with views but no reported rate", () => {
    // A platform that reports nothing must not be read as a zero.
    const result = lengthPayoff([
      SHORT({ publications: 2, views: 300, engagement_rate: null }),
      LONG({ publications: 2, views: 300, engagement: 30, engagement_rate: 0.1 }),
    ]);
    expect(result.verdict).toBe("unknown");
  });

  it("handles a zero denominator without dividing by it", () => {
    const result = lengthPayoff([
      SHORT({ publications: 4, views: 500, engagement: 0, engagement_rate: 0 }),
      LONG({ publications: 4, views: 500, engagement: 25, engagement_rate: 0.05 }),
    ]);
    expect(result.verdict).toBe("longer");
    expect(result.ratio).toBeNull();
    expect(result.sentence).toContain("has earned an interaction");
    expect(result.sentence).not.toContain("Infinity");
  });

  it("survives an empty payload", () => {
    expect(lengthPayoff([]).verdict).toBe("unknown");
    expect(lengthPayoff(undefined).verdict).toBe("unknown");
  });
});

describe("attentionShares", () => {
  it("splits output and attention against their own totals", () => {
    const { rows, totalPublications, totalMinutes } = attentionShares([
      SHORT({ publications: 8, reader_minutes: 200 }),
      MEDIUM({ publications: 0, reader_minutes: 0 }),
      LONG({ publications: 2, reader_minutes: 800 }),
    ]);
    expect(totalPublications).toBe(10);
    expect(totalMinutes).toBe(1000);
    // Four fifths of the pieces, a fifth of the minutes: the gap is the point.
    expect(rows[0].publicationShare).toBeCloseTo(0.8);
    expect(rows[0].minuteShare).toBeCloseTo(0.2);
    expect(rows[2].publicationShare).toBeCloseTo(0.2);
    expect(rows[2].minuteShare).toBeCloseTo(0.8);
    expect(rows[1].label).toBe("Medium · 4–8 min");
  });

  it("returns zero shares rather than NaN when nothing has been read", () => {
    const { rows, totalMinutes } = attentionShares([SHORT(), LONG()]);
    expect(totalMinutes).toBe(0);
    expect(rows.every((row) => row.minuteShare === 0)).toBe(true);
  });
});

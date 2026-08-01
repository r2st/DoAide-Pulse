import { describe, expect, it, vi } from "vitest";
import {
  formatCount,
  formatDuration,
  formatRate,
  formatReadLength,
  formatWhen,
  localDayKey,
  statusTone,
  titleize,
} from "./format";

describe("formatWhen", () => {
  it("handles both directions and empty values", () => {
    expect(formatWhen(null)).toBe("—");
    expect(formatWhen(new Date(Date.now() - 2 * 3600_000))).toBe("2h ago");
    expect(formatWhen(new Date(Date.now() + 3 * 86_400_000))).toBe("in 3d");
  });

  it("falls back to a calendar date past a week", () => {
    const old = new Date(Date.now() - 60 * 86_400_000);
    expect(formatWhen(old)).not.toContain("ago");
  });
});

describe("titleize", () => {
  it("turns an enum value into a label", () => {
    expect(titleize("feature_spotlight")).toBe("Feature Spotlight");
    expect(titleize("devto")).toBe("Devto");
    expect(titleize("")).toBe("");
  });
});

describe("formatCount", () => {
  it("distinguishes zero from missing", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe("—");
    expect(formatCount(undefined)).toBe("—");
  });
});

describe("formatRate", () => {
  it("renders a fraction as a percentage", () => {
    expect(formatRate(0.0425)).toBe("4.3%");
    expect(formatRate(1)).toBe("100.0%");
    expect(formatRate(0.05, 0)).toBe("5%");
  });

  it("keeps zero apart from unknown", () => {
    // Nobody clicked, versus the platform not counting clicks at all.
    expect(formatRate(0)).toBe("0.0%");
    expect(formatRate(null)).toBe("—");
    expect(formatRate(undefined)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("scales the unit to the magnitude", () => {
    expect(formatDuration(45)).toBe("45m");
    expect(formatDuration(60)).toBe("1h");
    expect(formatDuration(200)).toBe("3h 20m");
    expect(formatDuration(1440)).toBe("1d");
    expect(formatDuration(18420)).toBe("12d 19h");
  });

  it("keeps zero as an answer and unknown as a dash", () => {
    // Nobody has read anything yet, versus no platform counting reads.
    expect(formatDuration(0)).toBe("0m");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration("nonsense")).toBe("—");
  });
});

describe("formatReadLength", () => {
  it("reads the way the piece would label itself", () => {
    expect(formatReadLength(6)).toBe("6 min read");
    expect(formatReadLength(null)).toBe("—");
  });
});

describe("statusTone", () => {
  it("gives published and failed distinct tones", () => {
    expect(statusTone("published")).not.toBe(statusTone("failed"));
    expect(statusTone("anything-else")).toContain("ink");
  });

  it("reads a trigger event that produced a draft as a success", () => {
    // "generated" is the trigger-event equivalent of a publication landing.
    expect(statusTone("generated")).toBe(statusTone("published"));
  });

  it("keeps a skipped firing quiet and a failed one loud", () => {
    expect(statusTone("skipped")).toContain("ink");
    expect(statusTone("failed")).not.toBe(statusTone("skipped"));
  });
});

describe("localDayKey", () => {
  it("buckets by the local day, not the UTC one", () => {
    // 23:30 UTC on the 21st is still the 21st for a UTC+0 test runner, but the
    // point is that the key comes from local getters, not toISOString().
    const spy = vi.spyOn(Date.prototype, "getDate").mockReturnValue(22);
    expect(localDayKey("2026-07-21T23:30:00Z")).toMatch(/-22$/);
    spy.mockRestore();
  });

  it("zero-pads month and day", () => {
    expect(localDayKey(new Date(2026, 0, 5))).toBe("2026-01-05");
  });
});

import { describe, expect, it } from "vitest";
import {
  alertHref,
  alertLabel,
  alertTone,
  cadenceProvenance,
  formatAlertRatio,
  isLearned,
  summarizeAlerts,
  weekdaysLearned,
} from "./alerts";

/** An alert as `/analytics/alerts` sends it. */
function alert(overrides = {}) {
  return {
    kind: "underperforming",
    severity: "warning",
    content_id: 7,
    publication_id: 12,
    platform: "devto",
    title: "A post",
    message: "20% of your usual first 48 hours on devto",
    ratio: 0.2,
    observed: 40,
    expected: 200,
    ...overrides,
  };
}

describe("alertTone", () => {
  it("gives warnings visual weight and notices none", () => {
    expect(alertTone("warning")).toBe("bad");
    expect(alertTone("info")).toBe("muted");
  });

  it("treats an unrecognised severity as a notice", () => {
    expect(alertTone("catastrophe")).toBe("muted");
  });
});

describe("alertLabel", () => {
  it("names the two kinds", () => {
    expect(alertLabel("underperforming")).toBe("Underperforming");
    expect(alertLabel("stalled")).toBe("Stalled");
  });

  it("falls back rather than rendering a raw slug", () => {
    expect(alertLabel("something_new")).toBe("Attention");
  });
});

describe("alertHref", () => {
  it("points at the piece, which is where both kinds are answered", () => {
    expect(alertHref(alert())).toBe("/content/7");
  });
});

describe("formatAlertRatio", () => {
  it("renders a fraction as a percentage of usual", () => {
    expect(formatAlertRatio(0.27)).toBe("27% of usual");
  });

  it("keeps zero, which is a real answer", () => {
    expect(formatAlertRatio(0)).toBe("0% of usual");
  });

  it("returns null when there is no comparison to show", () => {
    expect(formatAlertRatio(null)).toBeNull();
    expect(formatAlertRatio(undefined)).toBeNull();
  });
});

describe("summarizeAlerts", () => {
  it("counts each severity separately", () => {
    const summary = summarizeAlerts([
      alert(),
      alert({ severity: "warning" }),
      alert({ kind: "stalled", severity: "info" }),
    ]);
    expect(summary).toBe("2 under your usual · 1 that stopped growing");
  });

  it("omits the half that is empty", () => {
    expect(summarizeAlerts([alert({ kind: "stalled", severity: "info" })])).toBe(
      "1 that stopped growing",
    );
  });

  it("says nothing at all about an empty set", () => {
    expect(summarizeAlerts([])).toBe("");
    expect(summarizeAlerts()).toBe("");
  });
});

describe("cadence provenance", () => {
  const fullyLearned = { source: "learned", weekdays_source: "learned" };

  it("recognises a learned cadence", () => {
    expect(isLearned({ ...fullyLearned, sample: 12 })).toBe(true);
    expect(cadenceProvenance({ ...fullyLearned, sample: 12 })).toBe(
      "Learned from 12 posts",
    );
  });

  it("singularises one post", () => {
    expect(cadenceProvenance({ ...fullyLearned, sample: 1 })).toBe(
      "Learned from 1 post",
    );
  });

  it("credits only the hour when the days are still the table's", () => {
    // The two clear the evidence bar separately: enough posts at 07:00 to name
    // an hour, no single weekday with enough of its own to name a rota. The
    // days rendered beside this chip are the published table's, and a bare
    // "Learned from 6 posts" would be read as a claim about them too.
    const entry = { source: "learned", weekdays_source: "table", sample: 6 };

    expect(isLearned(entry)).toBe(true);
    expect(weekdaysLearned(entry)).toBe(false);
    expect(cadenceProvenance(entry)).toBe("Hour learned from 6 posts");
  });

  it("treats a response with no source as the generic table", () => {
    // An older cached response has no `source`. Claiming it was learned would
    // be the one misleading answer in a panel built to be interrogable.
    expect(isLearned({ best_time_utc: "13:00" })).toBe(false);
    expect(isLearned(undefined)).toBe(false);
    expect(cadenceProvenance({ best_time_utc: "13:00" })).toBe("Generic guidance");
  });

  it("understates rather than overstates a response predating the field", () => {
    // `weekdays_source` is newer than `source`, so a cached entry can have one
    // and not the other. Falling back to "table" costs a word of precision;
    // falling back the other way would put "Learned from 9 posts" next to days
    // that came from the table, which is the failure this split exists to fix.
    expect(weekdaysLearned({ source: "learned", sample: 9 })).toBe(false);
    expect(cadenceProvenance({ source: "learned", sample: 9 })).toBe(
      "Hour learned from 9 posts",
    );
  });
});

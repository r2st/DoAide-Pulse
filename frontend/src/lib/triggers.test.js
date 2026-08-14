import { describe, expect, it } from "vitest";
import {
  configFromForm,
  describeTrigger,
  fieldsFor,
  formFromConfig,
  isPolled,
  KIND_FIELDS,
  summarizeCheck,
  triggerHealth,
} from "./triggers";

describe("fieldsFor", () => {
  it("knows every kind the API offers", () => {
    for (const kind of ["rss", "github", "schedule", "webhook"]) {
      expect(fieldsFor(kind).length).toBeGreaterThan(0);
    }
  });

  it("is empty rather than throwing for a kind it has never heard of", () => {
    // A kind added server-side first must not blank the page.
    expect(fieldsFor("carrier_pigeon")).toEqual([]);
  });

  it("offers content_type and instructions on every kind", () => {
    for (const kind of Object.keys(KIND_FIELDS)) {
      const keys = fieldsFor(kind).map((f) => f.key);
      expect(keys).toContain("content_type");
      expect(keys).toContain("instructions");
    }
  });

  it("only offers every_hours on the kinds Herald polls", () => {
    for (const kind of Object.keys(KIND_FIELDS)) {
      const has = fieldsFor(kind).some((f) => f.key === "every_hours");
      expect(has).toBe(isPolled(kind));
    }
  });
});

describe("configFromForm", () => {
  it("drops blank optional fields rather than sending empty strings", () => {
    // The server validates types and refuses `every_hours: ""`. An absent key
    // is how "use the default" is spelled.
    const config = configFromForm("rss", {
      feed_url: "https://example.com/feed.xml",
      every_hours: "",
      content_type: "",
      instructions: "",
    });
    expect(config).toEqual({ feed_url: "https://example.com/feed.xml" });
  });

  it("keeps a blank required field so the server's own message is shown", () => {
    expect(configFromForm("rss", { feed_url: "" })).toEqual({ feed_url: "" });
  });

  it("coerces numbers", () => {
    const config = configFromForm("schedule", {
      every_hours: "24",
      hour_utc: "9",
      topic: "This week",
    });
    expect(config.every_hours).toBe(24);
    expect(config.hour_utc).toBe(9);
    expect(config.topic).toBe("This week");
  });

  it("accepts a fractional interval", () => {
    expect(configFromForm("rss", { feed_url: "u", every_hours: "0.5" }).every_hours).toBe(0.5);
  });

  it("passes an unparseable number through for the server to reject", () => {
    // Dropping it would save a trigger that does not do what the form said.
    expect(configFromForm("rss", { feed_url: "u", every_hours: "soon" }).every_hours).toBe(
      "soon",
    );
  });

  it("treats zero as a value, not as blank", () => {
    expect(configFromForm("schedule", { hour_utc: "0" }).hour_utc).toBe(0);
  });

  it("sends a checkbox only when it is on", () => {
    expect(configFromForm("webhook", { require_signature: true }).require_signature).toBe(true);
    expect(configFromForm("webhook", { require_signature: false })).not.toHaveProperty(
      "require_signature",
    );
  });

  it("trims text", () => {
    expect(configFromForm("github", { repo: "  owner/name  " }).repo).toBe("owner/name");
  });

  it("ignores form keys the kind does not accept", () => {
    // The server refuses unknown keys outright, so a stale field left in state
    // by switching kinds must not reach it.
    const config = configFromForm("rss", {
      feed_url: "https://example.com/feed.xml",
      topic: "left over from the schedule form",
    });
    expect(config).not.toHaveProperty("topic");
  });
});

describe("formFromConfig", () => {
  it("gives every field a defined value, so no input goes uncontrolled", () => {
    const form = formFromConfig("webhook", {});
    for (const field of fieldsFor("webhook")) {
      expect(form[field.key]).toBeDefined();
    }
  });

  it("stringifies numbers for text inputs", () => {
    const form = formFromConfig("schedule", { every_hours: 24, hour_utc: 0 });
    expect(form.every_hours).toBe("24");
    expect(form.hour_utc).toBe("0");
  });

  it("reads a checkbox as a boolean", () => {
    expect(formFromConfig("webhook", { require_signature: true }).require_signature).toBe(true);
    expect(formFromConfig("webhook", {}).require_signature).toBe(false);
  });

  it("survives a null config", () => {
    expect(() => formFromConfig("rss", null)).not.toThrow();
    expect(formFromConfig("rss", null).feed_url).toBe("");
  });

  it("round-trips a config through the form unchanged", () => {
    const config = {
      feed_url: "https://example.com/feed.xml",
      every_hours: 6,
      content_type: "announcement",
      instructions: "Mention the changelog.",
    };
    expect(configFromForm("rss", formFromConfig("rss", config))).toEqual(config);
  });
});

describe("describeTrigger", () => {
  it("names the feed, the repo and the topic", () => {
    expect(describeTrigger({ kind: "rss", config: { feed_url: "https://x/f.xml" } })).toBe(
      "https://x/f.xml",
    );
    expect(describeTrigger({ kind: "github", config: { repo: "r2st/Herald" } })).toBe(
      "r2st/Herald",
    );
    expect(describeTrigger({ kind: "schedule", config: { topic: "Weekly" } })).toBe("Weekly");
  });

  it("falls back to what the trigger will do when nothing is configured", () => {
    expect(describeTrigger({ kind: "rss", config: {} })).toBe("a feed");
    expect(describeTrigger({ kind: "github", config: {} })).toBe("the project's repo");
    expect(describeTrigger({ kind: "schedule", config: {} })).toBe("a recurring piece");
  });

  it("falls back for a trigger stored without a config at all", () => {
    // `config` is nullable on the server, and a row written before a kind grew
    // its fields comes back with none — the list still has to render a line.
    expect(describeTrigger({ kind: "rss" })).toBe("a feed");
    expect(describeTrigger({ kind: "webhook" })).toBe("any signed or unsigned POST");
  });

  it("says whether a webhook demands a signature", () => {
    expect(describeTrigger({ kind: "webhook", config: { require_signature: true } })).toMatch(
      /signed/,
    );
  });

  it("returns a string for an unknown kind", () => {
    expect(describeTrigger({ kind: "??", config: {} })).toBe("");
    expect(describeTrigger(null)).toBe("");
  });
});

describe("triggerHealth", () => {
  const base = { is_active: true, consecutive_failures: 0, fire_count: 3 };

  it("shows a working trigger as active", () => {
    expect(triggerHealth(base).label).toBe("Active");
  });

  it("distinguishes paused from failing", () => {
    expect(triggerHealth({ ...base, is_active: false }).label).toBe("Paused");
  });

  it("flags an active trigger whose source keeps failing", () => {
    // The state a simple on/off list hides: still enabled, but not working.
    const health = triggerHealth({
      ...base,
      consecutive_failures: 9,
      last_error: "404 Not Found",
    });
    expect(health.label).toBe("Failing (9×)");
    expect(health.title).toBe("404 Not Found");
  });

  it("omits the tooltip when a failing trigger recorded no error text", () => {
    // `title=""` renders an empty tooltip box on hover, which reads as "we know
    // nothing" rather than "there is nothing to show".
    const health = triggerHealth({ ...base, consecutive_failures: 2, last_error: "" });

    expect(health.label).toBe("Failing (2×)");
    expect(health.title).toBeUndefined();
  });

  it("does not claim a trigger is healthy before it has ever fired", () => {
    expect(triggerHealth({ ...base, fire_count: 0 }).label).toBe("Never fired");
  });

  it("prefers paused over failing — the user turned it off, that is the fact", () => {
    expect(
      triggerHealth({ ...base, is_active: false, consecutive_failures: 5 }).label,
    ).toBe("Paused");
  });
});

describe("isPolled", () => {
  it("is true for the kinds Herald has to go and look at", () => {
    expect(isPolled("rss")).toBe(true);
    expect(isPolled("github")).toBe(true);
    expect(isPolled("schedule")).toBe(true);
  });

  it("is false for an inbound webhook, which cannot be checked on demand", () => {
    expect(isPolled("webhook")).toBe(false);
  });
});

describe("summarizeCheck", () => {
  it("prefers the server's error text", () => {
    expect(summarizeCheck({ status: "error", error: "That host is private." })).toBe(
      "That host is private.",
    );
  });

  it("explains a first look rather than calling it 'nothing happened'", () => {
    expect(summarizeCheck({ status: "baselined", entries: 12 })).toMatch(/12 entries/);
    expect(summarizeCheck({ status: "baselined", entries: 1 })).toMatch(/1 entry/);
  });

  it("reads the same for no news and a duplicate", () => {
    expect(summarizeCheck({ status: "no_news" })).toBe(summarizeCheck({ status: "duplicate" }));
  });

  it("says how many commits fell short of the threshold", () => {
    expect(summarizeCheck({ status: "below_threshold", commits: 1 })).toMatch(/1 new commit\b/);
    expect(summarizeCheck({ status: "below_threshold", commits: 2 })).toMatch(/2 new commits/);
  });

  it("points at the draft when one was written", () => {
    expect(summarizeCheck({ status: "generated", content_id: 4 })).toMatch(/draft/);
  });

  it("passes a skip reason through", () => {
    expect(summarizeCheck({ status: "skipped", detail: "Daily limit reached" })).toBe(
      "Daily limit reached",
    );
  });

  it("has something to say about a status it has never seen", () => {
    expect(summarizeCheck({ status: "brand_new_status" })).toBe("brand_new_status");
    expect(summarizeCheck(null)).toBe("Checked.");
  });

  it("still says something when the server sends a result with no status at all", () => {
    // `null` is handled a line earlier; this is the shape that reaches the
    // switch and falls off the end of it, where returning undefined would
    // render the check button's result line as empty and look like a hang.
    expect(summarizeCheck({})).toBe("Checked.");
    expect(summarizeCheck({ status: "" })).toBe("Checked.");
  });

  it("describes a first look that found nothing to count", () => {
    // A feed whose response the server did not enumerate: still baselined, but
    // "0 entries noted" would read as a broken feed rather than a fresh one.
    expect(summarizeCheck({ status: "baselined" })).toBe(
      "Connected. Herald writes about what happens next.",
    );
  });

  it("counts zero commits rather than saying 'undefined new commits'", () => {
    expect(summarizeCheck({ status: "below_threshold" })).toMatch(/^0 new commits/);
  });

  it("explains that an interval has not elapsed instead of implying no news", () => {
    // "Nothing new" and "not looked yet" are different answers, and a user who
    // just pressed Check now needs to know which one they got.
    expect(summarizeCheck({ status: "not_due" })).toMatch(/interval has not elapsed/);
    expect(summarizeCheck({ status: "not_due" })).not.toBe(
      summarizeCheck({ status: "no_news" }),
    );
  });

  it("explains that a webhook has nothing to poll", () => {
    expect(summarizeCheck({ status: "not_polled" })).toMatch(/POSTs to its URL/);
  });

  it("confirms an inbound firing that produced no draft of its own", () => {
    expect(summarizeCheck({ status: "received" })).toBe("Fired.");
  });

  it("falls back to a plain reason when a skip or failure carries no detail", () => {
    expect(summarizeCheck({ status: "skipped" })).toBe("Skipped.");
    expect(summarizeCheck({ status: "failed" })).toMatch(/generating failed/);
  });

  it("prefers the server's reason for a failure over the generic one", () => {
    expect(summarizeCheck({ status: "failed", detail: "The model timed out." })).toBe(
      "The model timed out.",
    );
  });
});

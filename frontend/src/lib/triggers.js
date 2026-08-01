// What each kind of trigger needs, and how a form turns into a config.
//
// The backend keeps `config` in a JSON column because four kinds want four
// different shapes, and it validates that shape at the edge (see
// `app/schemas/trigger.py`). This is the client half of the same contract: the
// field list the builder renders, and the two conversions between a form's
// all-strings state and the typed config the API stores.
//
// Kept out of the page component so the conversions can be tested without
// mounting a form. They are where the fiddly cases live — a blank optional
// field must be *absent* rather than `""`, because the API refuses an unknown
// or malformed value rather than ignoring it, and `""` for `every_hours` is
// malformed where "not set" is fine.

/**
 * Content types the generator understands. Mirrors `ContentType`.
 *
 * The last two are not articles — they produce a thread and a changelog. The
 * shape a type takes is the server's to decide (`content_format` on the piece),
 * so nothing here needs to know which is which.
 */
export const CONTENT_TYPES = [
  "announcement",
  "tutorial",
  "feature_spotlight",
  "comparison",
  "how_to",
  "social_thread",
  "changelog",
];

/**
 * Field specs per kind, in the order the builder shows them.
 *
 * `type` drives the input: "text", "number", "textarea", "select", "checkbox".
 * Anything not listed here for a kind is not offered, which keeps the form and
 * the server's `ALLOWED_CONFIG` from drifting into a 422 nobody can act on.
 */
const COMMON_FIELDS = [
  {
    key: "content_type",
    label: "Write a",
    type: "select",
    options: CONTENT_TYPES,
    blank: "Let Herald choose",
    hint: "What kind of piece this trigger produces.",
  },
  {
    key: "instructions",
    label: "Standing instructions",
    type: "textarea",
    placeholder: "Always mention the changelog link. Keep it under 400 words.",
    hint: "Added to the prompt every time this trigger fires.",
  },
];

/** The poll interval, for the kinds Herald has to go and look at. */
const EVERY_HOURS = {
  key: "every_hours",
  label: "Check every",
  type: "number",
  unit: "hours",
  step: "0.5",
  min: "0.5",
  placeholder: "1",
  hint: "How often Herald looks. Blank uses the default.",
};

export const KIND_FIELDS = {
  rss: [
    {
      key: "feed_url",
      label: "Feed URL",
      type: "text",
      required: true,
      mono: true,
      placeholder: "https://example.com/changelog.rss",
      hint: "An RSS or Atom feed. Herald writes when it gains an entry.",
    },
    EVERY_HOURS,
    ...COMMON_FIELDS,
  ],
  github: [
    {
      key: "repo",
      label: "Repository",
      type: "text",
      mono: true,
      placeholder: "owner/name",
      hint: "Blank watches the repo on the project itself.",
    },
    {
      key: "commit_threshold",
      label: "Write after",
      type: "number",
      unit: "commits",
      step: "1",
      min: "1",
      placeholder: "3",
      hint: "A release always fires, however few commits came with it.",
    },
    EVERY_HOURS,
    ...COMMON_FIELDS,
  ],
  schedule: [
    {
      key: "topic",
      label: "Topic",
      type: "text",
      placeholder: "This week in the project",
      hint: "What the recurring piece is about. There is no external event to describe.",
    },
    {
      key: "hour_utc",
      label: "At hour",
      type: "number",
      unit: "UTC",
      step: "1",
      min: "0",
      max: "23",
      placeholder: "9",
      hint: "Blank fires whenever the interval next elapses.",
    },
    EVERY_HOURS,
    ...COMMON_FIELDS,
  ],
  webhook: [
    {
      key: "require_signature",
      label: "Require a signature",
      type: "checkbox",
      hint: "Refuse requests that are not HMAC-signed with this trigger's secret. Worth taking whenever the sender can do it — a URL leaks by being pasted into a chat window.",
    },
    {
      key: "headline_path",
      label: "Headline field",
      type: "text",
      mono: true,
      placeholder: "release.name",
      hint: "Dotted path into the POSTed JSON. Blank lets Herald guess.",
    },
    {
      key: "summary_path",
      label: "Summary field",
      type: "text",
      mono: true,
      placeholder: "release.body",
    },
    {
      key: "url_path",
      label: "Link field",
      type: "text",
      mono: true,
      placeholder: "release.html_url",
    },
    {
      key: "dedupe_path",
      label: "Unique-id field",
      type: "text",
      mono: true,
      placeholder: "release.id",
      hint: "Two deliveries carrying the same value here are one event. Blank means every delivery is new.",
    },
    ...COMMON_FIELDS,
  ],
};

/** Every field key a kind accepts. */
export function fieldsFor(kind) {
  return KIND_FIELDS[kind] ?? [];
}

/**
 * A form's values as the config the API stores.
 *
 * Blank optional fields are dropped rather than sent as `""`: the server
 * validates types, so `every_hours: ""` is a 422 where an absent key is the
 * documented way to say "use the default". A required field is passed through
 * even when blank so the server's own message is what the user sees, rather
 * than the client inventing a different wording for the same rule.
 */
export function configFromForm(kind, form) {
  const config = {};
  for (const field of fieldsFor(kind)) {
    const raw = form[field.key];

    if (field.type === "checkbox") {
      // Only when on. An explicit `false` is the same as the default and would
      // only clutter the stored config.
      if (raw) config[field.key] = true;
      continue;
    }

    const value = typeof raw === "string" ? raw.trim() : raw;
    if (value === "" || value === null || value === undefined) {
      if (field.required) config[field.key] = "";
      continue;
    }

    if (field.type === "number") {
      const number = Number(value);
      // Not a number: send it through and let the server say so. Silently
      // dropping it would save a trigger that does not do what the form said.
      config[field.key] = Number.isFinite(number) ? number : value;
      continue;
    }

    config[field.key] = value;
  }
  return config;
}

/** A stored config as form values — every field present, every value a string. */
export function formFromConfig(kind, config = {}) {
  const form = {};
  for (const field of fieldsFor(kind)) {
    const value = config?.[field.key];
    if (field.type === "checkbox") {
      form[field.key] = Boolean(value);
    } else {
      form[field.key] = value === null || value === undefined ? "" : String(value);
    }
  }
  return form;
}

/** One line saying what this trigger actually watches. */
export function describeTrigger(trigger) {
  const config = trigger?.config ?? {};
  switch (trigger?.kind) {
    case "rss":
      return config.feed_url || "a feed";
    case "github":
      return config.repo || "the project's repo";
    case "schedule":
      return config.topic || "a recurring piece";
    case "webhook":
      return config.require_signature ? "signed requests only" : "any signed or unsigned POST";
    default:
      return "";
  }
}

/**
 * Whether this trigger is working, as a pill.
 *
 * The interesting state is "active but failing": a feed that has 404ed nine
 * times is still `is_active`, and a list that only distinguishes on/off shows
 * it as healthy right up until the server deactivates it.
 */
export function triggerHealth(trigger) {
  if (!trigger?.is_active) {
    return { tone: "bg-canvas text-ink-500", label: "Paused" };
  }
  if (trigger.consecutive_failures > 0) {
    const times = trigger.consecutive_failures;
    return {
      tone: "bg-bad-wash text-bad",
      label: `Failing (${times}×)`,
      title: trigger.last_error || undefined,
    };
  }
  if (!trigger.fire_count) {
    return { tone: "bg-canvas text-ink-500", label: "Never fired" };
  }
  return { tone: "bg-good-wash text-good", label: "Active" };
}

/** Polled kinds can be checked on demand; an inbound webhook cannot. */
export function isPolled(kind) {
  return kind === "rss" || kind === "github" || kind === "schedule";
}

/**
 * A one-line result for the "Check now" button.
 *
 * The vocabulary is `services/triggers.check` plus the four `TriggerEventStatus`
 * values it passes through when a firing actually happened.
 */
export function summarizeCheck(result) {
  if (!result) return "Checked.";
  if (result.error) return result.error;

  // Explicit plural: "entry" does not take a bare -s, and the one place this
  // shows is the sentence a user reads right after connecting a feed.
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  switch (result.status) {
    // A first look establishes where the source is *now*. Writing about a feed's
    // entire back catalogue on the day you connect it is the one thing nobody
    // wants, so the first poll only takes the watermark.
    case "baselined":
      return result.entries !== undefined
        ? `Connected. ${count(result.entries, "entry", "entries")} noted — Herald writes about what comes next.`
        : "Connected. Herald writes about what happens next.";
    case "no_news":
    case "duplicate":
      return "Nothing new since the last check.";
    case "not_due":
      return "Not due yet — the interval has not elapsed.";
    case "below_threshold":
      return `${count(result.commits ?? 0, "new commit", "new commits")} — below this trigger's threshold.`;
    case "not_polled":
      return "This trigger fires when something POSTs to its URL.";
    case "generated":
      return "Fired — a draft is waiting for you.";
    case "received":
      return "Fired.";
    case "skipped":
      return result.detail || "Skipped.";
    case "failed":
      return result.detail || "The trigger fired, but generating failed.";
    default:
      return result.detail || result.status || "Checked.";
  }
}

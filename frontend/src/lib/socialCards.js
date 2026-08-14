// Predicting the link card, in the browser, while you type.
//
// The backend owns the same rules (app/services/social_cards.py) and is the
// authority for the meta tags that actually ship. This file exists because the
// panel has to update on every keystroke against the *unsaved* draft, and a
// round trip per character is not that. So the two agree by construction on the
// numbers below and are tested independently against them.
//
// What is deliberately NOT duplicated here: tag generation. The editor fetches
// that from `/content/{id}/social` when the user asks to copy it, because tags
// are a thing you paste once rather than watch change.
//
// Every limit is observed rendering behaviour, not a published contract —
// networks retune them and lay out by viewport too. They say "roughly where it
// clips", which is enough to stop you shipping a title that dies mid-word.

/** Roughly where each network stops showing the title in a feed. */
const TITLE_CLIP = { x: 70, linkedin: 119, facebook: 88, slack: 75 };

/** Same, for the description line under it. */
const DESCRIPTION_CLIP = { x: 125, linkedin: 110, facebook: 155, slack: 190 };

const LABELS = {
  x: "X / Twitter",
  linkedin: "LinkedIn",
  facebook: "Facebook",
  slack: "Slack",
};

/** Render order. Stable, so the panel does not reshuffle as you type. */
export const NETWORKS = ["x", "linkedin", "facebook", "slack"];

/**
 * The network that clips a title first — the only one `audit` warns against.
 *
 * Derived rather than written down so it cannot go stale when a limit above is
 * retuned, but derived once: it is a fact about two constants in this file, and
 * `audit` runs on every keystroke in the editor.
 */
const TIGHTEST_TITLE = NETWORKS.reduce((a, b) => (TITLE_CLIP[a] <= TITLE_CLIP[b] ? a : b));

/**
 * The one size that satisfies every network at once (Facebook's 1.91:1).
 *
 * Not exported: the only thing that ever wanted these numbers is the advice
 * `audit` gives about a missing cover, which is in this file.
 */
const RECOMMENDED_IMAGE = { width: 1200, height: 630 };

/** Crawlers fetch from their own servers, so a relative path is never an image. */
const ABSOLUTE_URL = /^https?:\/\//i;

/** Extensions no major unfurler renders. SVG is the one that surprises people. */
const BAD_IMAGE_EXT = [".svg", ".webp", ".avif", ".bmp", ".tiff", ".ico"];

/**
 * Collapse a field to the single line a card renders.
 *
 * Markdown markers are stripped because a title carrying `**bold**` shows the
 * asterisks in the unfurl, and newlines go because a card has no line breaks.
 */
export function flatten(text) {
  return String(text ?? "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`>#[\]()]/g, "")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
}

/**
 * Truncate to `limit` on a word boundary, with the ellipsis inside the budget.
 *
 * Mirrors the networks rather than `String.slice`: they cut at a word and
 * append a single-character ellipsis, so a title that fits exactly survives
 * whole and one that does not loses a word rather than half of one.
 */
export function clip(text, limit) {
  const flat = flatten(text);
  if (flat.length <= limit) return flat;
  const cut = flat.slice(0, Math.max(0, limit - 1));
  // A single word longer than the budget has no boundary to fall back to, and
  // `lastIndexOf` returns -1 there — which `slice(0, -1)` would quietly read as
  // "drop the last character" rather than "no match".
  const boundary = cut.lastIndexOf(" ");
  return `${boundary > 0 ? cut.slice(0, boundary) : cut}…`;
}

/** The bare domain a card shows above the title. `www.` goes, as it does there. */
export function domainOf(url) {
  if (!url) return "";
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    // A half-typed URL is the normal case in a live panel, not an error.
    return "";
  }
}

/**
 * The description a crawler would settle on, in the order it tries them.
 *
 * Falling through to the body is the point: seeing the raw first sentence in
 * the panel is how you learn the meta description is doing nothing.
 */
export function cardDescription({ metaDescription, excerpt, body }) {
  for (const candidate of [metaDescription, excerpt]) {
    const flat = flatten(candidate);
    if (flat) return flat;
  }
  return clip(body, 200);
}

/** The cover image, or null when a crawler could not use it. */
export function usableImage(coverImageUrl) {
  const trimmed = String(coverImageUrl ?? "").trim();
  return trimmed && ABSOLUTE_URL.test(trimmed) ? trimmed : null;
}

/**
 * One preview per network, in `NETWORKS` order.
 *
 * `draft` takes the editor's field names so the panel can pass its state
 * straight in without a mapping layer that could drift.
 */
export function previews(draft = {}) {
  const title = flatten(draft.title);
  const description = cardDescription({
    metaDescription: draft.meta_description,
    excerpt: draft.excerpt,
    body: draft.body_markdown,
  });
  const image = usableImage(draft.cover_image_url);
  const domain = domainOf(draft.url);

  return NETWORKS.map((network) => ({
    network,
    label: LABELS[network],
    title: clip(title, TITLE_CLIP[network]),
    description: clip(description, DESCRIPTION_CLIP[network]),
    titleClipped: title.length > TITLE_CLIP[network],
    descriptionClipped: description.length > DESCRIPTION_CLIP[network],
    domain,
    imageUrl: image,
    // Whether there is an image decides the layout, and the layout changes the
    // whole shape of the card — it is the single biggest lever here.
    cardType: image ? "summary_large_image" : "summary",
  }));
}

/**
 * What will visibly degrade the card, worst first.
 *
 * Only what is knowable without a network call. Whether the image URL actually
 * resolves is answered by the live thumbnail in the SEO panel, which makes the
 * real request.
 */
export function audit(draft = {}) {
  const issues = [];
  const title = flatten(draft.title);
  const description = cardDescription({
    metaDescription: draft.meta_description,
    excerpt: draft.excerpt,
    body: draft.body_markdown,
  });
  const cover = String(draft.cover_image_url ?? "").trim();

  if (!title) {
    issues.push({ level: "error", field: "title", message: "No title — the card will show the URL." });
  }

  if (!cover) {
    issues.push({
      level: "error",
      field: "cover_image_url",
      message:
        "No cover image. The link unfurls as a small text-only card — add one at " +
        `${RECOMMENDED_IMAGE.width}×${RECOMMENDED_IMAGE.height} for the large layout everywhere.`,
    });
  } else if (!ABSOLUTE_URL.test(cover)) {
    issues.push({
      level: "error",
      field: "cover_image_url",
      message:
        "The cover image is a relative path. Crawlers fetch from their own " +
        "servers and cannot resolve it — use an absolute https:// URL.",
    });
  } else {
    const path = cover.toLowerCase().split("?")[0];
    const bad = BAD_IMAGE_EXT.find((ext) => path.endsWith(ext));
    if (bad) {
      issues.push({
        level: "warn",
        field: "cover_image_url",
        message: `A ${bad} cover is not rendered by most unfurlers. Use PNG or JPEG.`,
      });
    }
  }

  if (!description) {
    issues.push({
      level: "warn",
      field: "meta_description",
      message: "No description — the card shows the title alone.",
    });
  } else if (description.length < 50) {
    issues.push({
      level: "warn",
      field: "meta_description",
      message:
        `The description is ${description.length} characters. Under about 50 the ` +
        "card looks unfinished on LinkedIn and Slack.",
    });
  }

  if (title.length > TITLE_CLIP[TIGHTEST_TITLE]) {
    issues.push({
      level: "warn",
      field: "title",
      message:
        `The title is ${title.length} characters and clips at about ` +
        `${TITLE_CLIP[TIGHTEST_TITLE]} on ${LABELS[TIGHTEST_TITLE]}.`,
    });
  }

  const order = { error: 0, warn: 1 };
  return issues.sort((a, b) => (order[a.level] ?? 2) - (order[b.level] ?? 2));
}

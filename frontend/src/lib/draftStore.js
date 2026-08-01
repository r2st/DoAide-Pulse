// Keeping unsaved edits when the tab does not survive.
//
// The editor saves explicitly — a piece is a record, and autosaving every
// keystroke to the server would persist experiments the author has not
// committed to and fight the `locked` rule for published work. But an explicit
// Save means there is always a window where the work exists only in a React
// state object, and a closed tab, a reload or a click on the nav loses a
// morning's writing with no warning at all.
//
// So the draft is mirrored to localStorage and offered back on return. The
// server's copy stays authoritative; this is a recovery buffer, never a source
// of truth, which is why `load` returns the stored draft *and* what it was
// last known to differ from, and the editor asks rather than restoring
// silently.
//
// Everything here is defensive about storage failing: Safari's private mode
// throws on setItem, the quota can be full, and a user with cleared storage is
// the normal case rather than an error. A recovery buffer that breaks the
// editor when it is unavailable would be worse than no buffer.

const PREFIX = "herald:draft:";

/** Drafts older than this are not offered back. */
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/** The fields the editor owns. Anything else in a stored blob is ignored. */
export const DRAFT_FIELDS = [
  "title",
  "body_markdown",
  "excerpt",
  "meta_description",
  "keywords",
  "tags",
  "cover_image_url",
];

function storage() {
  try {
    // Accessing localStorage itself throws in some privacy configurations,
    // before any read or write.
    return window.localStorage;
  } catch {
    return null;
  }
}

function keyFor(contentId) {
  return `${PREFIX}${contentId}`;
}

/** True when two drafts differ in any field the editor owns. */
export function differs(a, b) {
  if (!a || !b) return false;
  return DRAFT_FIELDS.some((field) => (a[field] ?? "") !== (b[field] ?? ""));
}

/**
 * Mirror a draft, or clear it when it matches what the server already has.
 *
 * Passing the saved copy is what keeps this from accumulating junk: once you
 * hit Save, the mirror for that piece is deleted rather than left behind to be
 * offered back as a "recovery" of something already stored.
 */
export function save(contentId, draft, saved, now = Date.now()) {
  const store = storage();
  if (!store || contentId == null) return false;

  if (!differs(draft, saved)) {
    return clear(contentId);
  }
  try {
    const payload = {};
    for (const field of DRAFT_FIELDS) payload[field] = draft[field] ?? "";
    store.setItem(keyFor(contentId), JSON.stringify({ at: now, draft: payload }));
    return true;
  } catch {
    // Full or refused. The editor keeps working; only recovery is lost, and
    // announcing that would be noise about a thing the user cannot fix.
    return false;
  }
}

/**
 * The stored draft for a piece, or null.
 *
 * Null covers every failure — absent, unparseable, stale, or written by an
 * older shape of this module. A recovery prompt built from a half-understood
 * blob is worse than no prompt.
 */
export function load(contentId, now = Date.now()) {
  const store = storage();
  if (!store || contentId == null) return null;

  let raw;
  try {
    raw = store.getItem(keyFor(contentId));
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw);
    if (!parsed?.draft || typeof parsed.at !== "number") return null;
    if (now - parsed.at > MAX_AGE_MS) {
      clear(contentId);
      return null;
    }
    const draft = {};
    for (const field of DRAFT_FIELDS) draft[field] = parsed.draft[field] ?? "";
    return { draft, at: parsed.at };
  } catch {
    return null;
  }
}

/** Drop the mirror for one piece. */
export function clear(contentId) {
  const store = storage();
  if (!store || contentId == null) return false;
  try {
    store.removeItem(keyFor(contentId));
    return true;
  } catch {
    return false;
  }
}

/**
 * Delete every stored draft older than the cutoff.
 *
 * Called on editor mount. Without it a user who abandons drafts accumulates
 * them until the origin's quota is the thing that breaks, and the failure
 * would surface as "saving stopped working" somewhere else entirely.
 */
export function prune(now = Date.now()) {
  const store = storage();
  if (!store) return 0;

  let removed = 0;
  try {
    // Collected first: removing while iterating over `key(i)` shifts the
    // indices underneath the loop and silently skips entries.
    const stale = [];
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i);
      if (!key?.startsWith(PREFIX)) continue;
      try {
        const parsed = JSON.parse(store.getItem(key));
        if (typeof parsed?.at !== "number" || now - parsed.at > MAX_AGE_MS) {
          stale.push(key);
        }
      } catch {
        stale.push(key);
      }
    }
    for (const key of stale) {
      store.removeItem(key);
      removed += 1;
    }
  } catch {
    return removed;
  }
  return removed;
}

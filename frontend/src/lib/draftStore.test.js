import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DRAFT_FIELDS, clear, differs, load, prune, save } from "./draftStore";

const ID = 42;
const DAY = 24 * 60 * 60 * 1000;

/**
 * A minimal Storage, because this environment has none.
 *
 * Node 22 exposes a native `localStorage` gated behind `--localstorage-file`,
 * and it shadows the one jsdom would otherwise provide — so `window.localStorage`
 * is genuinely undefined under vitest here. Real browsers have it; the module's
 * own guards cover its absence (see the last describe block), and these tests
 * are about the logic on top, so a faithful stub is what they need.
 *
 * `length` and `key(i)` are index-ordered over insertion, which is what `prune`
 * iterates and what the shift-while-removing test depends on.
 */
function makeStorage() {
  let map = new Map();
  return {
    get length() {
      return map.size;
    },
    key: (index) => [...map.keys()][index] ?? null,
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => void map.set(key, String(value)),
    removeItem: (key) => void map.delete(key),
    clear: () => void (map = new Map()),
  };
}

function draft(overrides = {}) {
  return {
    title: "Shipping Pulse v2",
    body_markdown: "## Why\n\nA paragraph.",
    excerpt: "",
    meta_description: "",
    keywords: "",
    tags: "",
    cover_image_url: "",
    ...overrides,
  };
}

beforeEach(() => {
  Object.defineProperty(window, "localStorage", {
    value: makeStorage(),
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("differs", () => {
  it("is false for identical drafts", () => {
    expect(differs(draft(), draft())).toBe(false);
  });

  it("is true when any owned field changed", () => {
    expect(differs(draft(), draft({ title: "Renamed" }))).toBe(true);
  });

  it("ignores fields the editor does not own", () => {
    // The server sends more than the editor edits; a change there is not the
    // user's unsaved work.
    expect(differs(draft({ id: 1 }), draft({ id: 2 }))).toBe(false);
  });

  it("treats missing and empty as the same", () => {
    const withField = draft({ excerpt: "" });
    const without = { ...draft() };
    delete without.excerpt;

    expect(differs(withField, without)).toBe(false);
  });

  it("is false when either side is absent", () => {
    expect(differs(null, draft())).toBe(false);
    expect(differs(draft(), undefined)).toBe(false);
  });
});

describe("save and load", () => {
  it("round-trips a draft that differs from the saved copy", () => {
    save(ID, draft({ title: "Edited" }), draft());

    expect(load(ID).draft.title).toBe("Edited");
  });

  it("stores only the fields the editor owns", () => {
    save(ID, { ...draft({ title: "Edited" }), secret: "nope" }, draft());

    expect(Object.keys(load(ID).draft).sort()).toEqual([...DRAFT_FIELDS].sort());
  });

  it("clears rather than stores when the draft matches what is saved", () => {
    // Otherwise every visit leaves a mirror to be offered back as a recovery
    // of something already on the server.
    save(ID, draft({ title: "Edited" }), draft());
    save(ID, draft(), draft());

    expect(load(ID)).toBe(null);
  });

  it("keeps drafts for different pieces apart", () => {
    save(1, draft({ title: "One" }), draft());
    save(2, draft({ title: "Two" }), draft());

    expect(load(1).draft.title).toBe("One");
    expect(load(2).draft.title).toBe("Two");
  });

  it("returns null for a piece with nothing stored", () => {
    expect(load(ID)).toBe(null);
  });

  it("returns null for a missing content id", () => {
    expect(load(null)).toBe(null);
    expect(save(null, draft({ title: "x" }), draft())).toBe(false);
  });
});

describe("load rejects anything it cannot fully understand", () => {
  it("returns null for unparseable json", () => {
    window.localStorage.setItem(`pulse:draft:${ID}`, "{not json");

    expect(load(ID)).toBe(null);
  });

  it("returns null for a blob written by an older shape", () => {
    window.localStorage.setItem(`pulse:draft:${ID}`, JSON.stringify({ title: "bare" }));

    expect(load(ID)).toBe(null);
  });

  it("returns null and cleans up a draft older than a week", () => {
    save(ID, draft({ title: "Old" }), draft(), 0);

    expect(load(ID, 8 * DAY)).toBe(null);
    // Not merely hidden — dropped, so it stops occupying quota.
    expect(window.localStorage.getItem(`pulse:draft:${ID}`)).toBe(null);
  });

  it("still returns a draft just under the cutoff", () => {
    save(ID, draft({ title: "Recent" }), draft(), 0);

    expect(load(ID, 6 * DAY).draft.title).toBe("Recent");
  });
});

describe("clear", () => {
  it("drops the mirror for one piece and leaves the others", () => {
    save(1, draft({ title: "One" }), draft());
    save(2, draft({ title: "Two" }), draft());

    clear(1);

    expect(load(1)).toBe(null);
    expect(load(2).draft.title).toBe("Two");
  });

  it("reports failure for a missing content id rather than clearing 'undefined'", () => {
    // The editor calls this from the recovery banner, which can render for a
    // beat before the route param resolves. Keying off `undefined` would build
    // a real key and delete a real draft belonging to nobody.
    save(1, draft({ title: "One" }), draft());

    expect(clear(undefined)).toBe(false);
    expect(clear(null)).toBe(false);
    expect(load(1).draft.title).toBe("One");
  });
});

describe("prune", () => {
  it("removes stale drafts and keeps fresh ones", () => {
    save(1, draft({ title: "Old" }), draft(), 0);
    save(2, draft({ title: "Fresh" }), draft(), 8 * DAY);

    expect(prune(8 * DAY)).toBe(1);
    expect(load(2, 8 * DAY).draft.title).toBe("Fresh");
  });

  it("removes entries it cannot parse", () => {
    window.localStorage.setItem("pulse:draft:9", "garbage");

    expect(prune()).toBe(1);
  });

  it("leaves keys belonging to anything else alone", () => {
    window.localStorage.setItem("pulse:token", "abc");
    save(1, draft({ title: "Old" }), draft(), 0);

    prune(8 * DAY);

    expect(window.localStorage.getItem("pulse:token")).toBe("abc");
  });

  it("removes every stale entry, not every other one", () => {
    // Removing while iterating over key(i) shifts the indices underneath the
    // loop; this fails at three entries if that mistake is made.
    for (const id of [1, 2, 3]) save(id, draft({ title: `Old ${id}` }), draft(), 0);

    expect(prune(8 * DAY)).toBe(3);
    expect(window.localStorage.length).toBe(0);
  });
});

describe("when storage is unavailable", () => {
  it("save reports failure instead of throwing", () => {
    // Safari's private mode throws on setItem rather than refusing quietly.
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(() => save(ID, draft({ title: "Edited" }), draft())).not.toThrow();
    expect(save(ID, draft({ title: "Edited" }), draft())).toBe(false);
  });

  it("load returns null instead of throwing", () => {
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });

    expect(load(ID)).toBe(null);
  });

  it("prune reports nothing removed instead of throwing", () => {
    vi.spyOn(window.localStorage, "removeItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    save(1, draft({ title: "Old" }), draft(), 0);

    expect(() => prune(8 * DAY)).not.toThrow();
  });

  it("clear reports failure instead of throwing", () => {
    vi.spyOn(window.localStorage, "removeItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });

    expect(clear(ID)).toBe(false);
  });
});

describe("when reaching localStorage at all throws", () => {
  // Not the same failure as a refused write: some privacy configurations throw
  // on the property access itself, so the module never gets an object to guard
  // against. Every entry point has to survive that, because the editor calls
  // them on mount before the user has done anything.
  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      get() {
        throw new Error("SecurityError: access is denied for this document");
      },
      configurable: true,
    });
  });

  it("save reports failure", () => {
    expect(save(ID, draft({ title: "Edited" }), draft())).toBe(false);
  });

  it("load offers nothing back", () => {
    expect(load(ID)).toBe(null);
  });

  it("clear reports failure", () => {
    expect(clear(ID)).toBe(false);
  });

  it("prune counts nothing removed", () => {
    expect(prune()).toBe(0);
  });
});

describe("missing fields are read as empty rather than undefined", () => {
  it("differs treats a field absent on either side as empty", () => {
    // The two sides come from different places — one from React state, one from
    // the server's JSON — so either can be the one missing a key. Comparing
    // undefined against "" would mark a pristine draft dirty and nag on load.
    const { cover_image_url: _a, ...noCover } = draft();
    expect(differs(draft({ cover_image_url: "" }), noCover)).toBe(false);
    expect(differs(noCover, draft({ cover_image_url: "" }))).toBe(false);
  });

  it("save stores an absent field as empty rather than dropping it", () => {
    const { tags: _t, ...noTags } = draft({ title: "Edited" });

    expect(save(ID, noTags, draft())).toBe(true);
    expect(load(ID).draft.tags).toBe("");
  });

  it("load fills in a field a stored blob never had", () => {
    // A draft written before a field existed still has to be offerable, or an
    // upgrade silently strands whatever the user had unsaved at the time.
    window.localStorage.setItem(
      `pulse:draft:${ID}`,
      JSON.stringify({ at: 0, draft: { title: "Half a draft" } }),
    );

    const stored = load(ID, 1000);
    expect(stored.draft.title).toBe("Half a draft");
    for (const field of DRAFT_FIELDS) expect(stored.draft[field]).toBeTypeOf("string");
    expect(stored.draft.body_markdown).toBe("");
  });
});

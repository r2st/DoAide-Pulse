import { describe, expect, it } from "vitest";
import {
  NETWORKS,
  audit,
  cardDescription,
  clip,
  domainOf,
  flatten,
  previews,
  usableImage,
} from "./socialCards";

// Long enough to clip on every network including LinkedIn's generous 119 —
// otherwise the per-network assertions pass for the wrong reason.
const LONG_TITLE =
  "How we cut our continuous integration pipeline from forty minutes down to " +
  "under four using aggressive caching, a better runner, and far fewer steps";

const GOOD_DESCRIPTION =
  "A description that comfortably clears the fifty-character floor and reads like a real sentence.";

describe("flatten", () => {
  it("strips markdown a card would show literally", () => {
    expect(flatten("**Bold** and `code`")).toBe("Bold and code");
  });

  it("collapses newlines, because a card has no line breaks", () => {
    expect(flatten("First line\n\nSecond line")).toBe("First line Second line");
  });

  it("keeps link text and drops the URL", () => {
    expect(flatten("See [the docs](https://example.com)")).toBe("See the docs");
  });

  it("drops images entirely", () => {
    expect(flatten("![alt](https://example.com/a.png) after")).toBe("after");
  });

  it("treats null and undefined as empty", () => {
    expect(flatten(null)).toBe("");
    expect(flatten(undefined)).toBe("");
  });
});

describe("clip", () => {
  it("leaves a string within the limit untouched", () => {
    expect(clip("Short enough", 70)).toBe("Short enough");
  });

  it("keeps every character of a string exactly at the limit", () => {
    const text = "x".repeat(70);
    expect(clip(text, 70)).toBe(text);
  });

  it("cuts on a word boundary and never mid-word", () => {
    const clipped = clip(LONG_TITLE, 40);

    expect(clipped.endsWith("…")).toBe(true);
    expect(clipped.length).toBeLessThanOrEqual(40);
    expect(LONG_TITLE.startsWith(clipped.slice(0, -1))).toBe(true);
    expect(clipped.slice(0, -1).endsWith(" ")).toBe(false);
  });

  it("cuts mid-word only when a single word exceeds the budget", () => {
    expect(clip("supercalifragilistic", 10)).toBe("supercali…");
  });
});

describe("domainOf", () => {
  it.each([
    ["https://example.com/blog/post", "example.com"],
    ["https://www.example.com/post", "example.com"],
    ["https://Blog.Example.COM/post", "blog.example.com"],
  ])("reads %s as %s", (url, expected) => {
    expect(domainOf(url)).toBe(expected);
  });

  it("returns empty for a half-typed URL rather than throwing", () => {
    // The normal case in a panel that re-renders on every keystroke.
    expect(domainOf("htt")).toBe("");
    expect(domainOf("")).toBe("");
    expect(domainOf(undefined)).toBe("");
  });
});

describe("cardDescription", () => {
  const body = "# Heading\n\nThe first real paragraph of the post, long enough to count.";

  it("prefers the meta description", () => {
    expect(
      cardDescription({ metaDescription: "The meta.", excerpt: "The excerpt.", body }),
    ).toBe("The meta.");
  });

  it("falls back to the excerpt", () => {
    expect(cardDescription({ metaDescription: "", excerpt: "The excerpt.", body })).toBe(
      "The excerpt.",
    );
  });

  it("falls all the way back to the body, as a crawler does", () => {
    expect(cardDescription({ metaDescription: "", excerpt: "", body })).toContain(
      "first real paragraph",
    );
  });
});

describe("usableImage", () => {
  it("accepts an absolute URL", () => {
    expect(usableImage("https://cdn.example.com/c.png")).toBe("https://cdn.example.com/c.png");
  });

  it("rejects a relative path a crawler could not resolve", () => {
    expect(usableImage("/img/cover.png")).toBe(null);
  });

  it("rejects blank and missing values", () => {
    expect(usableImage("   ")).toBe(null);
    expect(usableImage(undefined)).toBe(null);
  });
});

describe("previews", () => {
  it("returns one card per network in a stable order", () => {
    expect(previews({ title: "Hello" }).map((p) => p.network)).toEqual(NETWORKS);
  });

  it("clips per network and flags what was cut", () => {
    const byNetwork = Object.fromEntries(
      previews({ title: LONG_TITLE }).map((p) => [p.network, p]),
    );

    // X clips hardest, LinkedIn is the most generous.
    expect(byNetwork.x.title.length).toBeLessThan(byNetwork.linkedin.title.length);
    expect(byNetwork.x.titleClipped).toBe(true);
    expect(byNetwork.linkedin.titleClipped).toBe(true);
  });

  it("flags nothing for a short title", () => {
    const result = previews({ title: "A short title" });

    expect(result.every((p) => p.titleClipped)).toBe(false);
    expect(result.every((p) => p.title === "A short title")).toBe(true);
  });

  it("selects the large layout when there is a usable image", () => {
    const result = previews({ title: "Hello", cover_image_url: "https://cdn.e.com/c.png" });

    expect(result.every((p) => p.cardType === "summary_large_image")).toBe(true);
  });

  it("degrades to the small card for a relative cover", () => {
    // Promising an image the crawler will never fetch would be the one lie the
    // panel could tell.
    const result = previews({ title: "Hello", cover_image_url: "/img/c.png" });

    expect(result.every((p) => p.cardType === "summary")).toBe(true);
    expect(result.every((p) => p.imageUrl === null)).toBe(true);
  });

  it("reads the domain from the url field", () => {
    const result = previews({ title: "Hello", url: "https://blog.example.com/p" });

    expect(result.every((p) => p.domain === "blog.example.com")).toBe(true);
  });
});

describe("audit", () => {
  const fields = (issues, level) =>
    new Set(issues.filter((i) => !level || i.level === level).map((i) => i.field));

  it("reports a missing cover image as an error", () => {
    const issues = audit({ title: "A fine title", meta_description: GOOD_DESCRIPTION });

    expect(fields(issues, "error").has("cover_image_url")).toBe(true);
  });

  it("reports a relative cover image as an error", () => {
    const issues = audit({
      title: "A fine title",
      meta_description: GOOD_DESCRIPTION,
      cover_image_url: "/img/c.png",
    });

    expect(fields(issues, "error").has("cover_image_url")).toBe(true);
  });

  it("reports an svg cover as a warning, not an error", () => {
    // It is a real image and loads in a browser; it just will not unfurl.
    const issues = audit({
      title: "A fine title",
      meta_description: GOOD_DESCRIPTION,
      cover_image_url: "https://cdn.e.com/c.svg",
    });

    expect(fields(issues, "warn").has("cover_image_url")).toBe(true);
    expect(fields(issues, "error").has("cover_image_url")).toBe(false);
  });

  it("does not let a query string hide the extension", () => {
    const issues = audit({
      title: "A fine title",
      meta_description: GOOD_DESCRIPTION,
      cover_image_url: "https://cdn.e.com/c.svg?w=1200",
    });

    expect(fields(issues, "warn").has("cover_image_url")).toBe(true);
  });

  it("flags a missing description differently from a short one", () => {
    // Nothing at all is not "a bit thin" — the card renders as the title on a
    // line by itself, and the fix is to write one rather than lengthen one.
    const missing = audit({
      title: "A fine title",
      cover_image_url: "https://cdn.e.com/c.png",
    });
    const short = audit({
      title: "A fine title",
      meta_description: "Too short.",
      cover_image_url: "https://cdn.e.com/c.png",
    });

    const only = (issues) => issues.filter((i) => i.field === "meta_description");
    expect(only(missing)).toHaveLength(1);
    expect(only(missing)[0].level).toBe("warn");
    expect(only(missing)[0].message).not.toMatch(/characters/);
    expect(only(short)[0].message).toMatch(/characters/);
  });

  it("treats an empty description the same as an absent one", () => {
    const issues = audit({
      title: "A fine title",
      meta_description: "   ",
      cover_image_url: "https://cdn.e.com/c.png",
    });

    expect(issues.filter((i) => i.field === "meta_description")).toHaveLength(1);
  });

  it("flags a description that is too short to fill the card", () => {
    const issues = audit({
      title: "A fine title",
      meta_description: "Too short.",
      cover_image_url: "https://cdn.e.com/c.png",
    });

    expect(fields(issues, "warn").has("meta_description")).toBe(true);
  });

  it("reports a long title once, not once per network", () => {
    const issues = audit({
      title: LONG_TITLE,
      meta_description: GOOD_DESCRIPTION,
      cover_image_url: "https://cdn.e.com/c.png",
    });

    expect(issues.filter((i) => i.field === "title")).toHaveLength(1);
  });

  it("sorts errors before warnings", () => {
    const issues = audit({ title: LONG_TITLE, meta_description: "Short." });
    const levels = issues.map((i) => i.level);

    expect(levels).toEqual([...levels].sort((a, b) => ({ error: 0, warn: 1 })[a] - ({ error: 0, warn: 1 })[b]));
  });

  it("finds nothing wrong with a complete post", () => {
    expect(
      audit({
        title: "A clean, well-sized headline",
        meta_description: GOOD_DESCRIPTION,
        cover_image_url: "https://cdn.e.com/cover.png",
      }),
    ).toEqual([]);
  });
});

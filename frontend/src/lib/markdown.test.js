import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";

describe("renderMarkdown", () => {
  it("renders headings, paragraphs and lists", () => {
    const html = renderMarkdown("## Why\n\nBecause.\n\n- one\n- two\n");
    expect(html).toContain("<h2>Why</h2>");
    expect(html).toContain("<p>Because.</p>");
    expect(html).toContain("<ul>");
    expect(html).toContain("<li>one</li>");
    expect(html).toContain("</ul>");
  });

  it("closes an ordered list before starting an unordered one", () => {
    const html = renderMarkdown("1. first\n\n- bullet\n");
    expect(html.indexOf("</ol>")).toBeLessThan(html.indexOf("<ul>"));
  });

  it("keeps fenced code literal", () => {
    const html = renderMarkdown("```python\n# not a heading\nprint('*hi*')\n```");
    expect(html).toContain('<pre><code class="language-python">');
    // Syntax inside a fence must not be interpreted.
    expect(html).not.toContain("<h1>");
    expect(html).not.toContain("<em>hi</em>");
  });

  it("closes an unterminated fence rather than swallowing the rest", () => {
    const html = renderMarkdown("```\nunterminated");
    expect(html).toContain("</code></pre>");
  });

  it("escapes HTML so a post cannot inject markup", () => {
    const html = renderMarkdown('<script>alert("x")</script>');
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("renders inline emphasis, code and links", () => {
    const html = renderMarkdown(
      "Some **bold**, some *italic*, some `code`, a [link](https://x.com).",
    );
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>italic</em>");
    expect(html).toContain("<code>code</code>");
    expect(html).toContain('href="https://x.com"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("leaves a non-http link as plain text", () => {
    const html = renderMarkdown("[click](javascript:alert(1))");
    expect(html).not.toContain("<a ");
    expect(html).toContain("[click]");
  });

  it("does not treat emphasis inside inline code as markup", () => {
    expect(renderMarkdown("`a *b* c`")).toContain("<code>a *b* c</code>");
  });

  it("returns an empty string for empty input", () => {
    expect(renderMarkdown("")).toBe("");
    expect(renderMarkdown(null)).toBe("");
  });

  it("renders a blockquote", () => {
    // Regression: escapeHtml runs over the whole document first, so by the time
    // the blockquote branch sees the line the marker is `&gt;`, not `>`. The
    // branch tested for the raw character and so never fired — every quote in
    // every generated post rendered as a paragraph beginning with a stray `>`.
    expect(renderMarkdown("> quoted")).toContain("<blockquote>quoted</blockquote>");
  });

  it("ends a blockquote at the paragraph after it", () => {
    const html = renderMarkdown("intro\n\n> a quote\n\nafter");
    expect(html).toContain("<p>intro</p>");
    expect(html).toContain("<blockquote>a quote</blockquote>");
    expect(html).toContain("<p>after</p>");
  });

  it("still escapes markup inside a blockquote", () => {
    const html = renderMarkdown("> <img src=x onerror=alert(1)>");
    expect(html).toContain("<blockquote>");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  it("cannot be made to break out of a link's href attribute", () => {
    // The body is generated from feeds and commit messages Herald does not
    // control, and this string is the shape that turns a quoted attribute into
    // an event handler. The up-front escape neutralises the quotes, so the
    // whole thing stays inside the href — or, as here, is not a link at all.
    const html = renderMarkdown('[click](https://x.com/" onmouseover="alert(1))');
    expect(html).not.toContain("onmouseover=\"alert");
    expect(html).toContain("&quot;");
  });
});

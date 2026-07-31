const { Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell, WidthType, AlignmentType, BorderStyle, ShadingType, PageBreak } = require("docx");
const fs = require("fs");

const BLUE = "2563EB";
const GRAY = "6B7280";
const LIGHT_BG = "F3F4F6";

function heading(text, level = HeadingLevel.HEADING_1) {
  return new Paragraph({ heading: level, children: [new TextRun({ text, bold: true })] });
}

function para(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 120 },
    children: [new TextRun({ text, size: 22, ...opts })],
  });
}

function boldPara(label, text) {
  return new Paragraph({
    spacing: { after: 120 },
    children: [
      new TextRun({ text: label, bold: true, size: 22 }),
      new TextRun({ text, size: 22 }),
    ],
  });
}

function bullet(text, level = 0) {
  return new Paragraph({
    spacing: { after: 80 },
    bullet: { level },
    children: [new TextRun({ text, size: 22 })],
  });
}

function boldBullet(label, text) {
  return new Paragraph({
    spacing: { after: 80 },
    bullet: { level: 0 },
    children: [
      new TextRun({ text: label, bold: true, size: 22 }),
      new TextRun({ text, size: 22 }),
    ],
  });
}

function tableCell(text, opts = {}) {
  return new TableCell({
    width: { size: opts.width || 2400, type: WidthType.DXA },
    shading: opts.shading ? { type: ShadingType.CLEAR, fill: opts.shading } : undefined,
    children: [new Paragraph({
      children: [new TextRun({ text, size: 20, bold: !!opts.bold, color: opts.color })],
    })],
  });
}

const doc = new Document({
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 } } },
    children: [
      // Title
      new Paragraph({
        spacing: { after: 100 },
        children: [new TextRun({ text: "FEATURE DOCUMENT", size: 20, color: BLUE, bold: true })],
      }),
      new Paragraph({
        spacing: { after: 60 },
        children: [new TextRun({ text: "Herald SEO Enhancement", size: 44, bold: true })],
      }),
      new Paragraph({
        spacing: { after: 300 },
        children: [
          new TextRun({ text: "Date: July 31, 2026  |  Author: Claude  |  Status: Proposal", size: 20, color: GRAY }),
        ],
      }),

      // 1. Context
      heading("1. Context", HeadingLevel.HEADING_1),
      para("Herald already has a solid SEO foundation. The content generator requests meta_description, keywords, and tags from the LLM. An seo.py module audits title length, meta description range (70-155 chars), keyword presence in title, heading hierarchy, cover images, and word count. Canonical URLs are supported across all publishing adapters."),
      para("However, Herald is currently reactive — it generates SEO metadata as a side effect of content creation, not as a strategic driver. The features below close that gap by making SEO a first-class input to the content pipeline, not just an output check."),

      // 2. What Already Works
      heading("2. What Already Works", HeadingLevel.HEADING_1),

      new Table({
        width: { size: 9600, type: WidthType.DXA },
        columnWidths: [3200, 3200, 3200],
        rows: [
          new TableRow({
            children: [
              tableCell("Feature", { bold: true, shading: BLUE, color: "FFFFFF", width: 3200 }),
              tableCell("Location", { bold: true, shading: BLUE, color: "FFFFFF", width: 3200 }),
              tableCell("Status", { bold: true, shading: BLUE, color: "FFFFFF", width: 3200 }),
            ],
          }),
          new TableRow({ children: [
            tableCell("Keyword injection in prompts", { width: 3200 }),
            tableCell("content_generator.py:160", { width: 3200 }),
            tableCell("Working", { width: 3200 }),
          ]}),
          new TableRow({ children: [
            tableCell("Meta description generation", { width: 3200, shading: LIGHT_BG }),
            tableCell("seo.py + content_generator.py", { width: 3200, shading: LIGHT_BG }),
            tableCell("Working (70-155 chars)", { width: 3200, shading: LIGHT_BG }),
          ]}),
          new TableRow({ children: [
            tableCell("SEO audit (title, headings, etc.)", { width: 3200 }),
            tableCell("seo.py:audit()", { width: 3200 }),
            tableCell("Working", { width: 3200 }),
          ]}),
          new TableRow({ children: [
            tableCell("Canonical URL support", { width: 3200, shading: LIGHT_BG }),
            tableCell("All adapters", { width: 3200, shading: LIGHT_BG }),
            tableCell("Working", { width: 3200, shading: LIGHT_BG }),
          ]}),
          new TableRow({ children: [
            tableCell("Tag/keyword normalization", { width: 3200 }),
            tableCell("seo.py:normalize_keywords()", { width: 3200 }),
            tableCell("Working", { width: 3200 }),
          ]}),
        ],
      }),

      new Paragraph({ spacing: { after: 200 }, children: [] }),

      // 3. Proposed Features
      heading("3. Proposed Features", HeadingLevel.HEADING_1),

      // 3.1
      heading("3.1 Focus Keyword Per Content Piece", HeadingLevel.HEADING_2),
      para("Add a focus_keyword field to the Content model. The content generator already receives project-level keywords, but there's no per-article primary keyword that drives the SEO audit. With a focus keyword, the audit can check keyword density (target: 1-2%), presence in the first paragraph, title, meta description, and at least one H2."),
      boldPara("Schema change: ", "ALTER TABLE content ADD COLUMN focus_keyword VARCHAR(100);"),
      boldPara("Effort: ", "Small — field + audit enhancement in seo.py"),

      // 3.2
      heading("3.2 Enhanced SEO Audit Scoring", HeadingLevel.HEADING_2),
      para("Extend seo.audit() from a pass/fail checklist to a 0-100 score. Add these checks:"),
      boldBullet("Keyword density: ", "focus keyword appears 1-2% of word count"),
      boldBullet("First paragraph: ", "focus keyword in opening paragraph"),
      boldBullet("Subheading keywords: ", "focus keyword in at least one H2/H3"),
      boldBullet("Internal links: ", "at least one link to another piece of content from the same project"),
      boldBullet("Image alt text: ", "all images have alt attributes containing relevant keywords"),
      boldBullet("Readability: ", "Flesch-Kincaid grade level 8-12 for developer content"),
      boldBullet("URL slug: ", "slug contains the focus keyword, under 60 chars"),
      para("The score gates autopilot publishing — content scoring below a configurable threshold (default: 70) stays in review instead of auto-publishing."),
      boldPara("Effort: ", "Medium — scoring logic + threshold config on project"),

      // 3.3
      heading("3.3 Structured Data (JSON-LD)", HeadingLevel.HEADING_2),
      para("Generate Article/BlogPosting JSON-LD schema markup and include it in published content. This helps Google show rich snippets (author, date, image) in search results."),
      para("For GIT adapter: inject a <script type=\"application/ld+json\"> block at the end of the markdown (most static site generators pass it through). For WordPress: add to post meta. Dev.to and Medium handle their own structured data, so no action needed there."),
      boldPara("Effort: ", "Small — template in seo.py, inject in GIT and WordPress adapters"),

      // 3.4
      heading("3.4 Sitemap Generation for GIT Blogs", HeadingLevel.HEADING_2),
      para("When publishing via the GIT adapter, auto-generate or update a sitemap.xml in the blog repository. Each published post gets an entry with loc, lastmod, changefreq, and priority. This ensures search engines discover all content immediately."),
      boldPara("Effort: ", "Small — XML template, commit alongside post in GIT adapter"),

      // 3.5
      heading("3.5 Internal Linking Suggestions", HeadingLevel.HEADING_2),
      para("After generating content, scan the body for opportunities to link to other published content from the same project. Use keyword overlap between the new piece's body and existing pieces' focus keywords/titles to suggest 2-3 relevant internal links. The LLM can place them naturally in a second pass."),
      para("Internal links improve SEO by distributing page authority and reducing bounce rate."),
      boldPara("Effort: ", "Medium — keyword matching + LLM second pass for link placement"),

      // 3.6
      heading("3.6 WordPress Adapter Fixes", HeadingLevel.HEADING_2),
      para("The WordPress adapter currently doesn't send meta_description or canonical_url. These are critical for SEO on self-hosted blogs. Fix the adapter to send:"),
      boldBullet("yoast_wpseo_metadesc: ", "meta_description (if Yoast SEO plugin is installed)"),
      boldBullet("_yoast_wpseo_focuskw: ", "focus_keyword"),
      boldBullet("canonical_url: ", "via the wp:post_meta REST endpoint"),
      boldPara("Effort: ", "Small — adapter enhancement"),

      // 3.7
      heading("3.7 Git Front Matter Enhancement", HeadingLevel.HEADING_2),
      para("Add keywords and focus_keyword to the YAML front matter in GIT-published posts. Many static site generators (Astro, Hugo, Next.js) use front matter for meta tags. Current front matter includes title, description, date, tags, draft, image, canonicalURL but is missing keywords."),
      boldPara("Effort: ", "Trivial — add two fields to git.py front matter template"),

      new Paragraph({ children: [new PageBreak()] }),

      // 4. Priority & Roadmap
      heading("4. Implementation Priority", HeadingLevel.HEADING_1),

      new Table({
        width: { size: 9600, type: WidthType.DXA },
        columnWidths: [1200, 4000, 2200, 2200],
        rows: [
          new TableRow({ children: [
            tableCell("Phase", { bold: true, shading: BLUE, color: "FFFFFF", width: 1200 }),
            tableCell("Feature", { bold: true, shading: BLUE, color: "FFFFFF", width: 4000 }),
            tableCell("Effort", { bold: true, shading: BLUE, color: "FFFFFF", width: 2200 }),
            tableCell("SEO Impact", { bold: true, shading: BLUE, color: "FFFFFF", width: 2200 }),
          ]}),
          new TableRow({ children: [
            tableCell("1", { width: 1200, shading: LIGHT_BG }),
            tableCell("WordPress adapter fixes", { width: 4000, shading: LIGHT_BG }),
            tableCell("Small", { width: 2200, shading: LIGHT_BG }),
            tableCell("High (bug fix)", { width: 2200, shading: LIGHT_BG }),
          ]}),
          new TableRow({ children: [
            tableCell("1", { width: 1200 }),
            tableCell("Git front matter keywords", { width: 4000 }),
            tableCell("Trivial", { width: 2200 }),
            tableCell("Medium", { width: 2200 }),
          ]}),
          new TableRow({ children: [
            tableCell("1", { width: 1200, shading: LIGHT_BG }),
            tableCell("Focus keyword field + audit scoring", { width: 4000, shading: LIGHT_BG }),
            tableCell("Medium", { width: 2200, shading: LIGHT_BG }),
            tableCell("High", { width: 2200, shading: LIGHT_BG }),
          ]}),
          new TableRow({ children: [
            tableCell("2", { width: 1200 }),
            tableCell("Structured data (JSON-LD)", { width: 4000 }),
            tableCell("Small", { width: 2200 }),
            tableCell("High (rich snippets)", { width: 2200 }),
          ]}),
          new TableRow({ children: [
            tableCell("2", { width: 1200, shading: LIGHT_BG }),
            tableCell("Sitemap generation", { width: 4000, shading: LIGHT_BG }),
            tableCell("Small", { width: 2200, shading: LIGHT_BG }),
            tableCell("Medium", { width: 2200, shading: LIGHT_BG }),
          ]}),
          new TableRow({ children: [
            tableCell("3", { width: 1200 }),
            tableCell("Internal linking suggestions", { width: 4000 }),
            tableCell("Medium", { width: 2200 }),
            tableCell("High", { width: 2200 }),
          ]}),
        ],
      }),

      new Paragraph({ spacing: { after: 200 }, children: [] }),

      para("Phase 1 items are low-effort, high-impact fixes that can ship together. Phase 2 adds structured data for rich search results. Phase 3 introduces the LLM-powered internal linking pass, which has the highest complexity but also the strongest long-term SEO benefit."),

      // 5. What This Won't Do
      heading("5. Out of Scope", HeadingLevel.HEADING_1),
      boldBullet("Keyword research / search volume data: ", "requires third-party APIs (Ahrefs, SEMrush, Google Search Console). Can be added later but isn't needed for Phase 1-3."),
      boldBullet("Backlink tracking: ", "external tool territory (Ahrefs, Moz). Herald tracks publications, not inbound links."),
      boldBullet("Google Search Console integration: ", "valuable for measuring results but separate from content generation. Future phase."),
      boldBullet("OG/Twitter card meta tags: ", "platform-specific; most platforms and static site generators handle these automatically from title + description + image."),
    ],
  }],
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync("/sessions/eager-adoring-turing/mnt/projects/Products/Herald/docs/herald-seo-features.docx", buffer);
  console.log("Document written.");
});

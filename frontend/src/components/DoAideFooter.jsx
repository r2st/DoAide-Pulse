const TOOLS = [
  { icon: "\u{1F4C4}", name: "Docs", url: "https://docs.doaide.com", desc: "Free document generators" },
  { icon: "\u{1F4DD}", name: "Resume", url: "https://resume.doaide.com", desc: "AI resume builder" },
  { icon: "\u{1F4CA}", name: "409A", url: "https://409a.doaide.com", desc: "Startup valuations" },
  { icon: "\u{1F3F7}️", name: "GST Bot", url: "https://gst.doaide.com", desc: "GST filing & compliance" },
  { icon: "\u{1F6E1}️", name: "InsureKit", url: "https://insure.doaide.com", desc: "Insurance calculators" },
  { icon: "\u{1F4B0}", name: "TaxFile", url: "https://tax.doaide.com", desc: "Tax & financial calculators" },
  { icon: "\u{1F9FE}", name: "Invoicer", url: "https://invoicer.doaide.com", desc: "GST invoices in seconds" },
  { icon: "\u{1F4DD}", name: "Contracts", url: "https://contracts.doaide.com", desc: "Business contracts" },
  { icon: "\u{1F3E0}", name: "HomeNex", url: "https://homenex.aiknol.com", desc: "AI CRM for real estate" },
];

export default function DoAideFooter() {
  return (
    <footer
      className="mt-12 border-t border-line bg-canvas px-4 py-8"
      aria-label="More free tools from DoAide"
    >
      <div className="mx-auto max-w-[900px]">
        <p className="eyebrow mb-4">
          More free tools from DoAide
        </p>
        <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))" }}>
          {TOOLS.map((t) => (
            <a
              key={t.url}
              href={t.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-start gap-2 rounded-lg border border-line bg-paper p-3 text-ink-900 no-underline transition-colors hover:border-brand-500"
            >
              <span className="shrink-0 text-xl leading-none" aria-hidden="true">{t.icon}</span>
              <span>
                <strong className="block text-sm">{t.name}</strong>
                <span className="text-xs text-ink-500">{t.desc}</span>
              </span>
            </a>
          ))}
        </div>
        <p className="mt-4 text-sm">
          <a href="https://doaide.com" target="_blank" rel="noopener noreferrer" className="text-brand-500 no-underline hover:text-brand-600">
            View all 40+ tools &rarr;
          </a>
        </p>
      </div>
    </footer>
  );
}

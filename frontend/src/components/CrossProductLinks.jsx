const CROSS_LINKS = {
  blog: [
    {
      href: "https://gst.doaide.com",
      label: "GSTBot",
      text: "Free AI GST compliance assistant — WhatsApp-based, no login required",
    },
    {
      href: "https://insure.doaide.com",
      label: "InsureKit",
      text: "LIC plan comparisons, premium calculators, and portfolio management for agents",
    },
    {
      href: "https://tax.doaide.com",
      label: "TaxFile",
      text: "File your ITR in minutes — old vs new regime comparison with Section 80C optimizer",
    },
    {
      href: "https://docs.doaide.com",
      label: "DoAide Docs",
      text: "Generate rent receipts, salary slips, and experience letters instantly",
    },
    {
      href: "https://resume.doaide.com",
      label: "DoAide Resume",
      text: "Build ATS-optimized resumes for free with AI-powered suggestions",
    },
  ],
  landing: [
    {
      href: "https://gst.doaide.com",
      label: "GSTBot",
      text: "Free WhatsApp-based GST compliance assistant for Indian businesses",
    },
    {
      href: "https://tax.doaide.com",
      label: "TaxFile",
      text: "Compare old vs new tax regime and file your ITR with AI guidance",
    },
    {
      href: "https://resume.doaide.com",
      label: "DoAide Resume",
      text: "Build ATS-optimized professional resumes with AI suggestions — free",
    },
    {
      href: "https://docs.doaide.com",
      label: "DoAide Docs",
      text: "Generate professional documents instantly — rent receipts, salary slips, and more",
    },
  ],
};

export default function CrossProductLinks({ page }) {
  const links = CROSS_LINKS[page];
  if (!links) return null;

  return (
    <aside className="mt-10 border-t border-chrome-200 pt-6 dark:border-chrome-700" aria-label="Explore more DoAide tools">
      <h3 className="mb-4 text-lg font-semibold text-ink-900 dark:text-chrome-50">
        Explore More Tools
      </h3>
      <div className="grid gap-3 sm:grid-cols-2">
        {links.map((link) => (
          <a
            key={link.href}
            href={link.href}
            className="group relative flex flex-col gap-1 rounded-lg border border-chrome-200 bg-paper p-4 no-underline transition-all hover:border-brand-400 hover:shadow-sm dark:border-chrome-700 dark:bg-chrome-800 dark:hover:border-brand-500"
            target="_blank"
            rel="noopener noreferrer"
          >
            <strong className="text-sm text-brand-600 group-hover:text-brand-700 dark:text-brand-400 dark:group-hover:text-brand-300">
              {link.label}
            </strong>
            <span className="text-xs leading-relaxed text-ink-500 dark:text-chrome-400">
              {link.text}
            </span>
            <span className="absolute right-3 top-3 text-xs text-ink-300 dark:text-chrome-500" aria-hidden="true">
              ↗
            </span>
          </a>
        ))}
      </div>
    </aside>
  );
}

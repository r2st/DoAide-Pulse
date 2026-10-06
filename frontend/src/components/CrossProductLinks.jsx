const CROSS_LINKS = {
  blog: [
    {
      href: "https://job.doaide.com",
      label: "DoAide Jobs",
      text: "Apply for jobs in this industry — AI-powered job matching",
    },
    {
      href: "https://resume.doaide.com",
      label: "Resume Builder",
      text: "Build a professional resume that stands out to recruiters",
    },
  ],
  landing: [
    {
      href: "https://resume.doaide.com",
      label: "Resume Builder",
      text: "Build your professional resume with AI-powered suggestions",
    },
    {
      href: "https://write.doaide.com",
      label: "DoAide Write",
      text: "Generate professional content — blogs, emails, and marketing copy",
    },
    {
      href: "https://contracts.doaide.com",
      label: "Contract Generator",
      text: "Draft freelancer agreements, NDAs, and service contracts",
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

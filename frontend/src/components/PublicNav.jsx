import { useState } from "react";
import { Link } from "react-router-dom";

export default function PublicNav() {
  const [toolsOpen, setToolsOpen] = useState(false);
  return (
    <header className="border-b border-line">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
        <Link to="/" className="flex items-center gap-2 text-lg font-bold text-ink-900">
          DoAide <span className="text-brand-500">Pulse</span>
        </Link>
        <nav className="flex items-center gap-5 text-sm">
          <div className="relative">
            <button
              onClick={() => setToolsOpen(!toolsOpen)}
              onBlur={() => setTimeout(() => setToolsOpen(false), 150)}
              className="text-ink-500 hover:text-brand-500"
            >
              Free Tools ▾
            </button>
            {toolsOpen && (
              <div className="absolute right-0 top-full z-10 mt-2 w-56 rounded-lg border border-line bg-paper py-1 shadow-lift">
                <Link to="/tools/subject-line-tester" className="block px-4 py-2 text-sm text-ink-700 hover:bg-canvas hover:text-brand-500">Subject Line Tester</Link>
                <Link to="/tools/send-time-optimizer" className="block px-4 py-2 text-sm text-ink-700 hover:bg-canvas hover:text-brand-500">Send Time Optimizer</Link>
                <Link to="/tools/newsletter-roi-calculator" className="block px-4 py-2 text-sm text-ink-700 hover:bg-canvas hover:text-brand-500">ROI Calculator</Link>
                <Link to="/tools/content-idea-generator" className="block px-4 py-2 text-sm text-ink-700 hover:bg-canvas hover:text-brand-500">Content Idea Generator</Link>
              </div>
            )}
          </div>
          <Link to="/templates" className="text-ink-500 hover:text-brand-500">Templates</Link>
          <Link to="/gallery" className="text-ink-500 hover:text-brand-500">Gallery</Link>
          <Link to="/blog" className="text-ink-500 hover:text-brand-500">Blog</Link>
          <Link to="/embed" className="text-ink-500 hover:text-brand-500">Embed</Link>
          <Link to="/login" className="btn-primary !py-1.5">Log in</Link>
        </nav>
      </div>
    </header>
  );
}

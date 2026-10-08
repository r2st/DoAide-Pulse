import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import PublicNav from "../components/PublicNav";
import DoAideFooter from "../components/DoAideFooter";
import CrossProductLinks from "../components/CrossProductLinks";

const NEWSLETTERS = [
  {
    id: "ai-weekly",
    name: "The AI Weekly",
    category: "Technology",
    description: "A curated roundup of the week's most important AI developments, research papers, and industry moves.",
    stats: { generatedIn: "12s", openRate: "42%" },
    colors: { primary: "#6366F1", secondary: "#818CF8", bg: "#1E1B4B" },
  },
  {
    id: "startup-pulse-india",
    name: "Startup Pulse India",
    category: "Business",
    description: "Weekly coverage of India's startup ecosystem — funding rounds, product launches, and founder interviews.",
    stats: { generatedIn: "18s", openRate: "38%" },
    colors: { primary: "#F97316", secondary: "#FB923C", bg: "#431407" },
  },
  {
    id: "design-digest",
    name: "Design Digest",
    category: "Design",
    description: "Handpicked design inspiration, tool reviews, and career advice for UI/UX designers.",
    stats: { generatedIn: "9s", openRate: "45%" },
    colors: { primary: "#EC4899", secondary: "#F472B6", bg: "#500724" },
  },
  {
    id: "ecommerce-insider",
    name: "E-commerce Insider",
    category: "Retail",
    description: "Trends, tactics, and case studies for online sellers — from D2C brands to marketplace operators.",
    stats: { generatedIn: "15s", openRate: "35%" },
    colors: { primary: "#10B981", secondary: "#34D399", bg: "#022C22" },
  },
  {
    id: "developer-notes",
    name: "Developer Notes",
    category: "Engineering",
    description: "Technical deep dives, new releases, and battle-tested patterns for software engineers.",
    stats: { generatedIn: "11s", openRate: "48%" },
    colors: { primary: "#3B82F6", secondary: "#60A5FA", bg: "#172554" },
  },
  {
    id: "wellness-weekly",
    name: "Wellness Weekly",
    category: "Lifestyle",
    description: "Evidence-based health tips, mindfulness practices, and fitness advice delivered every Monday.",
    stats: { generatedIn: "10s", openRate: "40%" },
    colors: { primary: "#A78BFA", secondary: "#C4B5FD", bg: "#2E1065" },
  },
];

const FAQS = [
  {
    q: "How quickly can AI generate a newsletter?",
    a: "DoAide Pulse generates complete newsletters in 10-20 seconds. The AI handles research, writing, and formatting — you review and customize before sending.",
  },
  {
    q: "Can I create a newsletter similar to these examples?",
    a: "Yes. Sign up for free and choose a template or describe the newsletter you want. The AI creates a personalized version matching your industry, audience, and brand.",
  },
  {
    q: "What open rates can I expect with AI-generated newsletters?",
    a: "Our users see average open rates of 35-48%, significantly above the industry average of 21%. AI-optimized subject lines, send timing, and personalization drive the difference.",
  },
];

function NewsletterPreview({ newsletter, colors }) {
  return (
    <div className="rounded-lg overflow-hidden border border-[#2A2A2D]" style={{ background: "#0A0A0B" }}>
      <div className="h-16 flex items-center justify-center px-4" style={{ background: `linear-gradient(135deg, ${colors.bg} 0%, #0A0A0B 100%)` }}>
        <div className="text-center">
          <div className="text-[11px] font-bold tracking-wider" style={{ color: colors.primary }}>{newsletter.name.toUpperCase()}</div>
          <div className="text-[8px] text-[#6B7280] mt-0.5">Issue #47 · Oct 2026</div>
        </div>
      </div>
      <div className="p-3 space-y-2">
        <div className="rounded h-2.5 w-3/4" style={{ background: `${colors.primary}20` }} />
        <div className="rounded h-2 w-full bg-[#1A1A1D]" />
        <div className="rounded h-2 w-5/6 bg-[#1A1A1D]" />
        <div className="rounded h-2 w-2/3 bg-[#1A1A1D]" />
        <div className="mt-3 flex gap-2">
          <div className="rounded h-8 flex-1" style={{ background: `${colors.primary}15` }} />
          <div className="rounded h-8 flex-1 bg-[#1A1A1D]" />
        </div>
        <div className="rounded h-2 w-full bg-[#1A1A1D]" />
        <div className="rounded h-2 w-4/5 bg-[#1A1A1D]" />
        <div className="mt-2 mx-auto rounded h-6 w-24 flex items-center justify-center" style={{ background: colors.primary }}>
          <span className="text-[7px] font-bold text-[#0A0A0B]">READ MORE</span>
        </div>
      </div>
    </div>
  );
}

function NewsletterCard({ newsletter, isExpanded, onToggle }) {
  const { colors } = newsletter;
  return (
    <div className="group rounded-2xl border border-[#2A2A2D] bg-[#1A1A1D] overflow-hidden transition-all duration-300 hover:border-[#F0B429]/20 hover:shadow-[0_0_20px_rgba(240,180,41,0.06)]">
      <div className="p-4">
        <NewsletterPreview newsletter={newsletter} colors={colors} />
      </div>

      <div className="px-5 pb-5">
        <div className="flex items-center gap-2 mb-2">
          <span className="inline-block w-2 h-2 rounded-full" style={{ background: colors.primary }} />
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-[#6B7280]">{newsletter.category}</span>
        </div>
        <h3 className="font-display text-lg text-white mb-1">{newsletter.name}</h3>
        <p className="text-sm text-[#9CA3AF] mb-3 leading-relaxed">{newsletter.description}</p>

        <div className="flex items-center gap-4 mb-4 font-mono text-[11px] text-[#6B7280]">
          <span>Generated in <span className="text-[#F0B429]">{newsletter.stats.generatedIn}</span></span>
          <span>Open rate <span className="text-[#F0B429]">{newsletter.stats.openRate}</span></span>
        </div>

        <div className="flex gap-2">
          <button
            onClick={onToggle}
            className="flex-1 rounded-lg border border-[#2A2A2D] bg-[#111113] px-4 py-2 text-sm text-[#E5E7EB] transition hover:border-[#F0B429]/40 hover:text-[#F0B429]"
          >
            {isExpanded ? "Close" : "Expand"}
          </button>
          <Link
            to="/"
            className="flex-1 rounded-lg bg-[#F0B429] px-4 py-2 text-center text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]"
          >
            Create Similar
          </Link>
        </div>
      </div>

      {isExpanded && (
        <div className="border-t border-[#2A2A2D] bg-[#111113] p-5">
          <div className="rounded-xl border border-[#2A2A2D] overflow-hidden" style={{ background: "#0A0A0B" }}>
            <div className="h-24 flex flex-col items-center justify-center" style={{ background: `linear-gradient(135deg, ${colors.bg} 0%, ${colors.primary}22 100%)` }}>
              <div className="text-lg font-bold tracking-wider" style={{ color: colors.primary }}>{newsletter.name}</div>
              <div className="text-xs text-[#9CA3AF] mt-1">Issue #47 · October 8, 2026</div>
            </div>
            <div className="p-6 space-y-5">
              <div>
                <div className="text-sm font-semibold text-[#E5E7EB] mb-2">Featured Story</div>
                <div className="space-y-1.5">
                  <div className="h-2.5 rounded bg-[#1A1A1D] w-full" />
                  <div className="h-2.5 rounded bg-[#1A1A1D] w-11/12" />
                  <div className="h-2.5 rounded bg-[#1A1A1D] w-4/5" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg p-3 border border-[#2A2A2D]" style={{ background: `${colors.primary}08` }}>
                  <div className="h-2 rounded w-3/4 mb-2" style={{ background: `${colors.primary}30` }} />
                  <div className="h-2 rounded w-full bg-[#1A1A1D]" />
                  <div className="h-2 rounded w-2/3 bg-[#1A1A1D] mt-1" />
                </div>
                <div className="rounded-lg p-3 border border-[#2A2A2D]" style={{ background: `${colors.primary}08` }}>
                  <div className="h-2 rounded w-3/4 mb-2" style={{ background: `${colors.primary}30` }} />
                  <div className="h-2 rounded w-full bg-[#1A1A1D]" />
                  <div className="h-2 rounded w-2/3 bg-[#1A1A1D] mt-1" />
                </div>
              </div>
              <div className="text-center pt-2">
                <div className="inline-block rounded-lg px-8 py-2 text-sm font-medium text-white" style={{ background: colors.primary }}>
                  Read Full Issue
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function NewsletterGallery() {
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    document.title = "Newsletter Gallery | DoAide Pulse";
    const meta = document.querySelector('meta[name="description"]');
    if (meta) meta.setAttribute("content", "Explore AI-generated newsletter examples across industries — technology, startups, design, e-commerce, engineering, and lifestyle. See what DoAide Pulse can create.");
  }, []);

  useEffect(() => {
    const ld = document.createElement("script");
    ld.type = "application/ld+json";
    ld.textContent = JSON.stringify([
      {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        name: "AI-Generated Newsletter Gallery",
        description: "Examples of newsletters created with DoAide Pulse AI newsletter platform",
        publisher: { "@type": "Organization", name: "DoAide Pulse", url: "https://pulse.doaide.com" },
        hasPart: NEWSLETTERS.map((n) => ({
          "@type": "CreativeWork",
          name: n.name,
          description: n.description,
          genre: n.category,
        })),
      },
      {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        mainEntity: FAQS.map((f) => ({
          "@type": "Question",
          name: f.q,
          acceptedAnswer: { "@type": "Answer", text: f.a },
        })),
      },
    ]);
    document.head.appendChild(ld);
    return () => document.head.removeChild(ld);
  }, []);

  return (
    <div className="min-h-screen" style={{ background: "#0A0A0B", color: "#E5E7EB" }}>
      <PublicNav />

      <div className="mx-auto max-w-6xl px-4 py-12">
        <div className="text-center mb-12">
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-[#F0B429] mb-3 block">Gallery</span>
          <h1 className="font-display text-4xl md:text-5xl text-white mb-4">Newsletters built with AI</h1>
          <p className="text-lg text-[#9CA3AF] max-w-2xl mx-auto">
            See what DoAide Pulse creates. Each newsletter was researched, written, and optimized by AI — then reviewed by a human editor in minutes.
          </p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-6 mb-16">
          {NEWSLETTERS.map((n) => (
            <NewsletterCard
              key={n.id}
              newsletter={n}
              isExpanded={expanded === n.id}
              onToggle={() => setExpanded(expanded === n.id ? null : n.id)}
            />
          ))}
        </div>

        <div className="rounded-2xl border border-[#2A2A2D] bg-[#1A1A1D] p-8 md:p-12 text-center mb-16">
          <h2 className="font-display text-2xl md:text-3xl text-white mb-4">Create your own AI newsletter</h2>
          <p className="text-[#9CA3AF] mb-6 max-w-lg mx-auto">
            Tell us your topic and audience. The AI researches, writes, and optimizes — you review and publish. First newsletter in under 5 minutes.
          </p>
          <Link to="/" className="inline-block rounded-lg bg-[#F0B429] px-8 py-3 text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]">
            Start creating free
          </Link>
        </div>

        <div className="mb-16">
          <h2 className="font-display text-2xl text-white mb-6 text-center">Frequently asked questions</h2>
          <div className="max-w-2xl mx-auto space-y-4">
            {FAQS.map((faq, i) => (
              <details key={i} className="group rounded-xl border border-[#2A2A2D] bg-[#1A1A1D]">
                <summary className="cursor-pointer px-6 py-4 text-[#E5E7EB] font-medium list-none flex items-center justify-between">
                  {faq.q}
                  <span className="text-[#F0B429] transition-transform group-open:rotate-45 text-lg">+</span>
                </summary>
                <div className="px-6 pb-4 text-sm text-[#9CA3AF] leading-relaxed">{faq.a}</div>
              </details>
            ))}
          </div>
        </div>

        <CrossProductLinks page="landing" />
      </div>

      <DoAideFooter />
    </div>
  );
}

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import PublicNav from "../components/PublicNav";
import DoAideFooter from "../components/DoAideFooter";
import CrossProductLinks from "../components/CrossProductLinks";

const TEMPLATES = [
  {
    id: "marketing",
    name: "Marketing Campaign",
    category: "Promotion",
    description: "Bold promotional layout with hero banner, feature highlights, and prominent CTA buttons for product launches and sales.",
    preview: {
      headerBg: "linear-gradient(135deg, #F0B429 0%, #D4A017 100%)",
      headerText: "SUMMER SALE",
      subhead: "Up to 50% off everything",
      sections: ["Feature highlights grid", "Customer testimonials", "Limited-time CTA"],
    },
  },
  {
    id: "product-update",
    name: "Product Update",
    category: "Changelog",
    description: "Clean changelog-style newsletter with version badges, feature lists, and what's-new sections for SaaS products.",
    preview: {
      headerBg: "linear-gradient(135deg, #6366F1 0%, #4F46E5 100%)",
      headerText: "v3.2 Release",
      subhead: "New features and improvements",
      sections: ["Version badge + date", "Feature list with icons", "Bug fixes & improvements"],
    },
  },
  {
    id: "weekly-digest",
    name: "Weekly Digest",
    category: "Curation",
    description: "Curated links format with numbered items, category tags, and read-time estimates for content roundups.",
    preview: {
      headerBg: "linear-gradient(135deg, #10B981 0%, #059669 100%)",
      headerText: "This Week",
      subhead: "5 stories you should not miss",
      sections: ["Numbered article list", "Category tags", "Quick-read summaries"],
    },
  },
  {
    id: "announcement",
    name: "Announcement",
    category: "News",
    description: "Minimal, focused single-message layout with decorative borders for important company updates and announcements.",
    preview: {
      headerBg: "linear-gradient(135deg, #F59E0B 0%, #D97706 100%)",
      headerText: "Big News",
      subhead: "We have an announcement",
      sections: ["Centered headline", "Single focused message", "Action button"],
    },
  },
  {
    id: "educational",
    name: "Educational",
    category: "Tutorial",
    description: "Tutorial and how-to format with numbered steps, code blocks, and tip callouts for teaching your audience.",
    preview: {
      headerBg: "linear-gradient(135deg, #8B5CF6 0%, #7C3AED 100%)",
      headerText: "How-To Guide",
      subhead: "Step-by-step walkthrough",
      sections: ["Numbered steps", "Code snippets & tips", "Key takeaways box"],
    },
  },
  {
    id: "community",
    name: "Community",
    category: "Roundup",
    description: "Community roundup style with member spotlights, upcoming events, polls, and discussion highlights.",
    preview: {
      headerBg: "linear-gradient(135deg, #EC4899 0%, #DB2777 100%)",
      headerText: "Community",
      subhead: "Monthly member roundup",
      sections: ["Member spotlight", "Upcoming events", "Poll results & discussion"],
    },
  },
  {
    id: "welcome-series",
    name: "Welcome Email Series",
    category: "Onboarding",
    description: "A warm welcome sequence for new subscribers with brand introduction, value highlights, and a clear next step to keep them engaged from day one.",
    preview: {
      headerBg: "linear-gradient(135deg, #14B8A6 0%, #0D9488 100%)",
      headerText: "Welcome!",
      subhead: "We are glad you are here",
      sections: ["Personal greeting", "What to expect", "Quick-start CTA"],
    },
  },
  {
    id: "product-launch",
    name: "Product Launch",
    category: "Launch",
    description: "High-impact product launch announcement with hero image area, key feature bullets, pricing highlights, and early-access CTA.",
    preview: {
      headerBg: "linear-gradient(135deg, #F43F5E 0%, #E11D48 100%)",
      headerText: "NOW LIVE",
      subhead: "Introducing our newest product",
      sections: ["Hero product showcase", "3 key benefits", "Early-access pricing"],
    },
  },
  {
    id: "weekly-roundup",
    name: "Weekly Roundup",
    category: "Digest",
    description: "Structured weekly digest with top stories, editor picks, industry stats, and a curated resources section for content-heavy newsletters.",
    preview: {
      headerBg: "linear-gradient(135deg, #3B82F6 0%, #2563EB 100%)",
      headerText: "Week in Review",
      subhead: "Your weekly briefing",
      sections: ["Top 3 stories", "Editor's pick", "Stats and resources"],
    },
  },
  {
    id: "event-invitation",
    name: "Event Invitation",
    category: "Events",
    description: "Clean event invitation with date, time, venue details, speaker lineup, agenda preview, and prominent RSVP button.",
    preview: {
      headerBg: "linear-gradient(135deg, #A855F7 0%, #9333EA 100%)",
      headerText: "You're Invited",
      subhead: "Join us for a special event",
      sections: ["Date, time & venue", "Speaker lineup", "RSVP button"],
    },
  },
  {
    id: "feedback-survey",
    name: "Feedback Request",
    category: "Survey",
    description: "Friendly feedback and survey request template with clear ask, estimated time, incentive mention, and a single prominent survey link.",
    preview: {
      headerBg: "linear-gradient(135deg, #F97316 0%, #EA580C 100%)",
      headerText: "We'd Love to Hear",
      subhead: "Share your thoughts in 2 minutes",
      sections: ["Personal ask", "What we'll improve", "Take the survey CTA"],
    },
  },
];

const FAQS = [
  {
    q: "Are these newsletter templates free to use?",
    a: "Yes, all templates are completely free. Sign up for DoAide Pulse to customize them with your branding, content, and subscriber lists.",
  },
  {
    q: "Can I customize the templates with my own branding?",
    a: "Absolutely. Every template is fully customizable — change colors, fonts, logos, layouts, and content sections to match your brand identity.",
  },
  {
    q: "Do the templates work with dark mode email clients?",
    a: "Yes, all templates are tested for dark mode compatibility across Gmail, Outlook, Apple Mail, and other major email clients.",
  },
];

function TemplateCard({ template, isExpanded, onToggle }) {
  return (
    <div className="group rounded-2xl border border-[#2A2A2D] bg-[#1A1A1D] overflow-hidden transition-all duration-300 hover:border-[#F0B429]/30 hover:shadow-[0_0_30px_rgba(240,180,41,0.08)]">
      <div className="relative h-48 overflow-hidden" style={{ background: "#111113" }}>
        <div className="absolute inset-3 rounded-lg overflow-hidden border border-[#2A2A2D]" style={{ background: "#0A0A0B" }}>
          <div className="h-12 flex items-center justify-center text-xs font-bold tracking-wider text-[#0A0A0B]" style={{ background: template.preview.headerBg }}>
            {template.preview.headerText}
          </div>
          <div className="px-3 pt-2">
            <div className="text-[9px] text-[#9CA3AF] mb-2">{template.preview.subhead}</div>
            {template.preview.sections.map((s, i) => (
              <div key={i} className="flex items-center gap-1.5 mb-1.5">
                <div className="w-1 h-1 rounded-full bg-[#F0B429] shrink-0" />
                <div className="text-[8px] text-[#6B7280] truncate">{s}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="p-5">
        <div className="flex items-center gap-2 mb-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-[#F0B429] bg-[#F0B429]/10 px-2 py-0.5 rounded">
            {template.category}
          </span>
        </div>
        <h3 className="font-display text-lg text-white mb-1">{template.name}</h3>
        <p className="text-sm text-[#9CA3AF] mb-4 leading-relaxed">{template.description}</p>

        <div className="flex gap-2">
          <button
            onClick={onToggle}
            className="flex-1 rounded-lg border border-[#2A2A2D] bg-[#111113] px-4 py-2 text-sm text-[#E5E7EB] transition hover:border-[#F0B429]/40 hover:text-[#F0B429]"
          >
            {isExpanded ? "Close Preview" : "Preview"}
          </button>
          <Link
            to="/"
            className="flex-1 rounded-lg bg-[#F0B429] px-4 py-2 text-center text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]"
          >
            Use Template
          </Link>
        </div>
      </div>

      {isExpanded && (
        <div className="border-t border-[#2A2A2D] bg-[#111113] p-5">
          <div className="rounded-xl border border-[#2A2A2D] overflow-hidden bg-[#0A0A0B]">
            <div className="h-20 flex flex-col items-center justify-center" style={{ background: template.preview.headerBg }}>
              <div className="text-xl font-bold text-[#0A0A0B] tracking-wide">{template.preview.headerText}</div>
              <div className="text-xs text-[#0A0A0B]/70 mt-1">{template.preview.subhead}</div>
            </div>
            <div className="p-6 space-y-4">
              {template.preview.sections.map((section, i) => (
                <div key={i} className="flex items-start gap-3">
                  <div className="mt-1.5 w-6 h-6 rounded-full bg-[#F0B429]/10 flex items-center justify-center shrink-0">
                    <span className="text-[10px] font-bold text-[#F0B429]">{i + 1}</span>
                  </div>
                  <div>
                    <div className="text-sm text-[#E5E7EB] font-medium mb-1">{section}</div>
                    <div className="h-2 w-48 rounded bg-[#1A1A1D]" />
                    <div className="h-2 w-36 rounded bg-[#1A1A1D] mt-1.5" />
                  </div>
                </div>
              ))}
              <div className="pt-4 text-center">
                <div className="inline-block rounded-lg bg-[#F0B429] px-8 py-2 text-sm font-medium text-[#0A0A0B]">
                  Call to Action
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function TemplatesGallery() {
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    document.title = "Free Newsletter Templates | DoAide Pulse";
    const meta = document.querySelector('meta[name="description"]');
    if (meta) meta.setAttribute("content", "Browse free newsletter templates for marketing, product updates, digests, announcements, tutorials, and community roundups. Customize and send with DoAide Pulse.");
  }, []);

  useEffect(() => {
    const ld = document.createElement("script");
    ld.type = "application/ld+json";
    ld.textContent = JSON.stringify([
      {
        "@context": "https://schema.org",
        "@type": "ItemList",
        name: "Free Newsletter Templates",
        description: "Professional newsletter templates for email marketing",
        numberOfItems: TEMPLATES.length,
        itemListElement: TEMPLATES.map((t, i) => ({
          "@type": "ListItem",
          position: i + 1,
          name: t.name,
          description: t.description,
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
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-[#F0B429] mb-3 block">Free Templates</span>
          <h1 className="font-display text-4xl md:text-5xl text-white mb-4">Newsletter templates that convert</h1>
          <p className="text-lg text-[#9CA3AF] max-w-2xl mx-auto">
            Start with a professionally designed template. Customize colors, content, and layout to match your brand — then publish with AI-powered optimization.
          </p>
        </div>

        <div className="grid md:grid-cols-2 gap-6 mb-16">
          {TEMPLATES.map((t) => (
            <TemplateCard
              key={t.id}
              template={t}
              isExpanded={expanded === t.id}
              onToggle={() => setExpanded(expanded === t.id ? null : t.id)}
            />
          ))}
        </div>

        <div className="rounded-2xl border border-[#2A2A2D] bg-[#1A1A1D] p-8 md:p-12 text-center mb-16">
          <h2 className="font-display text-2xl md:text-3xl text-white mb-4">Ready to build your newsletter?</h2>
          <p className="text-[#9CA3AF] mb-6 max-w-lg mx-auto">
            Pick a template, customize it with your brand, and let AI handle the content. Your first newsletter goes out in minutes.
          </p>
          <Link to="/" className="inline-block rounded-lg bg-[#F0B429] px-8 py-3 text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]">
            Get started free
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

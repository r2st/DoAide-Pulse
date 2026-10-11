import { Fragment, useEffect, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import AnalyticsMockup from "../components/AnalyticsMockup";
import CrossProductLinks from "../components/CrossProductLinks";
import DoAideFooter from "../components/DoAideFooter";
import trackEvent from "../lib/trackEvent";
import "./LandingPage.css";

const ACCENT = "#F0B429";

const TYPEWRITER_PHRASES = [
  "AI-researched content",
  "One-click publishing",
  "Smart audience targeting",
  "Automated newsletters",
];

const DOAIDE_PRODUCTS = [
  { name: "Desk", url: "https://desk.doaide.com" },
  { name: "Jobs", url: "https://job.doaide.com" },
  { name: "409A", url: "https://409a.doaide.com" },
  { name: "GST", url: "https://gst.doaide.com" },
  { name: "Pulse", url: "https://pulse.doaide.com" },
  { name: "Med", url: "https://med.doaide.com" },
  { name: "Realty", url: "https://realty.doaide.com" },
  { name: "Reach", url: "https://reach.doaide.com" },
  { name: "Trade", url: "https://trade.doaide.com" },
];

const PARTICLE_POSITIONS = [
  { left: "5%", top: "15%", delay: 0, duration: 7 },
  { left: "12%", top: "45%", delay: 1.2, duration: 8 },
  { left: "20%", top: "75%", delay: 0.5, duration: 6 },
  { left: "28%", top: "30%", delay: 2.1, duration: 9 },
  { left: "35%", top: "60%", delay: 0.8, duration: 7.5 },
  { left: "42%", top: "20%", delay: 1.5, duration: 8.5 },
  { left: "50%", top: "80%", delay: 0.3, duration: 6.5 },
  { left: "55%", top: "35%", delay: 2.5, duration: 7 },
  { left: "62%", top: "55%", delay: 1.0, duration: 9.5 },
  { left: "70%", top: "25%", delay: 1.8, duration: 8 },
  { left: "75%", top: "70%", delay: 0.7, duration: 7.5 },
  { left: "82%", top: "40%", delay: 2.3, duration: 6 },
  { left: "88%", top: "65%", delay: 1.3, duration: 8.5 },
  { left: "93%", top: "10%", delay: 0.9, duration: 7 },
  { left: "8%", top: "85%", delay: 2.0, duration: 9 },
  { left: "45%", top: "50%", delay: 1.6, duration: 6.5 },
];

const PIPELINE_STAGES = [
  {
    label: "Research",
    icon: (
      <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="#0A0A0B" strokeWidth="2" strokeLinecap="round">
        <circle cx="8.5" cy="8.5" r="5" />
        <line x1="12" y1="12" x2="17" y2="17" />
      </svg>
    ),
  },
  {
    label: "Write",
    icon: (
      <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="#0A0A0B" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2.5l3.5 3.5L6 17.5H2.5V14z" />
      </svg>
    ),
  },
  {
    label: "Publish",
    icon: (
      <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="#0A0A0B" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 2L2 9l7 3 3 7z" />
        <path d="M18 2L9 12" />
      </svg>
    ),
  },
  {
    label: "Grow",
    icon: (
      <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="#0A0A0B" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="2 15 6 10 10 13 18 4" />
        <polyline points="13 4 18 4 18 9" />
      </svg>
    ),
  },
];

const STATS = [
  { value: "3x", label: "Higher engagement" },
  { value: "10s", label: "To generate" },
  { value: "42%", label: "Avg open rate" },
  { value: "500+", label: "Newsletters sent" },
];

const FEATURES = [
  {
    title: "AI Content Research",
    desc: "Scans hundreds of sources to surface stories your audience cares about.",
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="7" />
        <path d="M16 16l5 5" />
        <path d="M11 8v6M8 11h6" />
      </svg>
    ),
  },
  {
    title: "Smart Subject Lines",
    desc: "AI-scored subject lines that boost open rates by 22% on average.",
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 4h16v16H4z" />
        <path d="M4 10h16M10 10v10" />
      </svg>
    ),
  },
  {
    title: "Send Time Optimization",
    desc: "Delivers to each subscriber when they are most likely to read.",
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 3" />
      </svg>
    ),
  },
  {
    title: "One-Click Publishing",
    desc: "Review the AI draft, make your edits, and publish to your list instantly.",
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 2L2 11l8 3 3 8z" />
        <path d="M22 2L10 14" />
      </svg>
    ),
  },
  {
    title: "Analytics Dashboard",
    desc: "Track opens, clicks, conversions, and subscriber growth in real time.",
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 20V12M10 20V8M16 20V4M22 20V10" />
      </svg>
    ),
  },
  {
    title: "Multi-Language Support",
    desc: "Generate newsletters in Hindi, Tamil, Telugu, and more Indian languages.",
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#F0B429" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="9" />
        <path d="M2 12h20M12 3a15 15 0 014 9 15 15 0 01-4 9 15 15 0 01-4-9 15 15 0 014-9z" />
      </svg>
    ),
  },
];

const TESTIMONIALS = [
  { quote: "Pulse cut our newsletter production from 4 hours to 30 minutes. The AI research alone is worth it.", author: "Priya S.", role: "Founder, SaaS startup" },
  { quote: "Our open rates jumped from 18% to 41% after switching to AI-optimized subject lines and send times.", author: "Rahul M.", role: "Marketing lead, D2C brand" },
  { quote: "Finally a newsletter tool that understands Indian audiences. Regional language support is a game changer.", author: "Anita K.", role: "Content strategist" },
];

function RobotFace({ size = 32, color }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width={size} height={size}>
      <line x1="16" y1="6" x2="16" y2="2" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="16" cy="1.5" r="1.5" fill={color} />
      <rect x="5" y="6" width="22" height="17" rx="5" fill={color} />
      <ellipse cx="11" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <ellipse cx="21" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <circle cx="11.5" cy="12.5" r="1" fill={color} opacity="0.6" />
      <circle cx="21.5" cy="12.5" r="1" fill={color} opacity="0.6" />
      <path d="M12 19Q16 22 20 19" stroke="#0A0A0B" strokeWidth="1.2" fill="none" strokeLinecap="round" />
      <rect x="1" y="10" width="4" height="5" rx="2" fill={color} opacity="0.8" />
      <rect x="27" y="10" width="4" height="5" rx="2" fill={color} opacity="0.8" />
    </svg>
  );
}

function HeroRobot({ color }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 150" width="180" height="150" className="landing-hero-robot">
      <line x1="90" y1="28" x2="90" y2="10" stroke={color} strokeWidth="3" strokeLinecap="round" />
      <circle cx="90" cy="7" r="5" fill={color} className="landing-antenna-glow" />
      <circle cx="90" cy="7" r="2.5" fill="#F7CC5F" />
      <rect x="35" y="28" width="110" height="80" rx="22" fill={color} />
      <rect x="50" y="40" width="80" height="58" rx="14" fill="#D4A017" opacity="0.3" />
      <ellipse cx="62" cy="62" rx="12" ry="14" fill="#0A0A0B" />
      <ellipse cx="118" cy="62" rx="12" ry="14" fill="#0A0A0B" />
      <circle cx="65" cy="59" r="5" fill="#F7CC5F" />
      <circle cx="121" cy="59" r="5" fill="#F7CC5F" />
      <circle cx="67" cy="56" r="2" fill="white" opacity="0.5" />
      <circle cx="123" cy="56" r="2" fill="white" opacity="0.5" />
      <path d="M68 88Q90 102 112 88" stroke="#0A0A0B" strokeWidth="3" fill="none" strokeLinecap="round" />
      <rect x="14" y="48" width="18" height="28" rx="7" fill={color} opacity="0.8" />
      <rect x="148" y="48" width="18" height="28" rx="7" fill={color} opacity="0.8" />
      <rect x="75" y="108" width="30" height="10" rx="4" fill="#D4A017" />
      <rect x="52" y="118" width="76" height="28" rx="12" fill={color} />
      <circle cx="90" cy="130" r="5" fill="#0A0A0B" />
    </svg>
  );
}

function TypewriterEffect() {
  const [phraseIndex, setPhraseIndex] = useState(0);
  const [charIndex, setCharIndex] = useState(0);
  const [isDeleting, setIsDeleting] = useState(false);

  useEffect(() => {
    const phrase = TYPEWRITER_PHRASES[phraseIndex];

    if (!isDeleting && charIndex === phrase.length) {
      const timer = setTimeout(() => setIsDeleting(true), 2000);
      return () => clearTimeout(timer);
    }

    if (isDeleting && charIndex === 0) {
      setIsDeleting(false);
      setPhraseIndex((prev) => (prev + 1) % TYPEWRITER_PHRASES.length);
      return;
    }

    const speed = isDeleting ? 30 : 60;
    const timer = setTimeout(() => {
      setCharIndex((prev) => prev + (isDeleting ? -1 : 1));
    }, speed);

    return () => clearTimeout(timer);
  }, [charIndex, isDeleting, phraseIndex]);

  return (
    <div className="landing-typewriter-wrap">
      <span className="landing-typewriter">
        {TYPEWRITER_PHRASES[phraseIndex].substring(0, charIndex)}
        <span className="landing-cursor" aria-hidden="true">|</span>
      </span>
    </div>
  );
}

function PipelineGraphic() {
  return (
    <div className="landing-pipeline">
      {PIPELINE_STAGES.map((stage, i) => (
        <Fragment key={stage.label}>
          {i > 0 && (
            <div className="landing-pipeline-connector">
              <div className="landing-pipeline-dot" style={{ animationDelay: `${(i - 1) * 0.3}s` }} />
              <div className="landing-pipeline-dot" style={{ animationDelay: `${(i - 1) * 0.3 + 0.7}s` }} />
            </div>
          )}
          <div className="landing-pipeline-stage">
            <div className="landing-pipeline-node">{stage.icon}</div>
            <span className="landing-pipeline-label">{stage.label}</span>
          </div>
        </Fragment>
      ))}
    </div>
  );
}

function StatsBar() {
  return (
    <div className="landing-stats">
      {STATS.map((stat, i) => (
        <Fragment key={stat.label}>
          {i > 0 && <div className="landing-stats-divider" />}
          <div className="landing-stat">
            <span className="landing-stat-value">{stat.value}</span>
            <span className="landing-stat-label">{stat.label}</span>
          </div>
        </Fragment>
      ))}
    </div>
  );
}

function FeaturesGrid() {
  return (
    <div className="landing-features">
      <div className="landing-section-header">
        <span className="landing-section-eyebrow">Features</span>
        <h2 className="landing-section-title">Everything you need to publish</h2>
        <p className="landing-section-subtitle">
          AI handles research, writing, and optimization. You keep the editorial control.
        </p>
      </div>
      <div className="landing-features-grid">
        {FEATURES.map((f) => (
          <div key={f.title} className="landing-feature-card">
            <div className="landing-feature-icon">{f.icon}</div>
            <h3 className="landing-feature-title">{f.title}</h3>
            <p className="landing-feature-desc">{f.desc}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function TestimonialsSection() {
  return (
    <div className="landing-testimonials">
      <div className="landing-section-header">
        <span className="landing-section-eyebrow">Social proof</span>
        <h2 className="landing-section-title">Trusted by newsletter creators</h2>
      </div>
      <div className="landing-testimonials-grid">
        {TESTIMONIALS.map((t) => (
          <div key={t.author} className="landing-testimonial-card">
            <p className="landing-testimonial-quote">&ldquo;{t.quote}&rdquo;</p>
            <div className="landing-testimonial-author">
              <div className="landing-testimonial-avatar">
                {t.author.charAt(0)}
              </div>
              <div>
                <div className="landing-testimonial-name">{t.author}</div>
                <div className="landing-testimonial-role">{t.role}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const COMPARISON_FEATURES = [
  { feature: "AI content research", pulse: true, mailchimp: false, substack: false },
  { feature: "AI subject line scoring", pulse: true, mailchimp: false, substack: false },
  { feature: "Send time optimization", pulse: true, mailchimp: "Paid", substack: false },
  { feature: "Indian language support", pulse: true, mailchimp: false, substack: false },
  { feature: "Free tier", pulse: "Unlimited", mailchimp: "500 contacts", substack: "Free newsletters" },
  { feature: "Email templates", pulse: "11+", mailchimp: "100+", substack: "1" },
  { feature: "Drag-and-drop editor", pulse: true, mailchimp: true, substack: false },
  { feature: "Dark mode tested", pulse: true, mailchimp: true, substack: true },
  { feature: "Automation", pulse: true, mailchimp: "Paid", substack: false },
  { feature: "Analytics dashboard", pulse: true, mailchimp: true, substack: "Basic" },
  { feature: "Custom domain", pulse: true, mailchimp: "Paid", substack: "Paid" },
  { feature: "Built for India", pulse: true, mailchimp: false, substack: false },
];

function ComparisonCheck({ value }) {
  if (value === true) {
    return (
      <>
        <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="#10B981" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <polyline points="3 8 7 12 13 4" />
        </svg>
        <span className="sr-only">Yes</span>
      </>
    );
  }
  if (value === false) {
    return (
      <>
        <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="#6B7280" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <line x1="4" y1="8" x2="12" y2="8" />
        </svg>
        <span className="sr-only">No</span>
      </>
    );
  }
  return <span className="landing-comparison-text">{value}</span>;
}

function ComparisonTable() {
  return (
    <div className="landing-comparison">
      <div className="landing-section-header">
        <span className="landing-section-eyebrow">Compare</span>
        <h2 className="landing-section-title">Why creators choose Pulse</h2>
        <p className="landing-section-subtitle">
          See how DoAide Pulse stacks up against popular alternatives.
        </p>
      </div>
      <div className="landing-comparison-table-wrap">
        <table className="landing-comparison-table">
          <thead>
            <tr>
              <th className="landing-comparison-feature-th">Feature</th>
              <th className="landing-comparison-brand-th">Pulse</th>
              <th>Mailchimp</th>
              <th>Substack</th>
            </tr>
          </thead>
          <tbody>
            {COMPARISON_FEATURES.map((row) => (
              <tr key={row.feature}>
                <td className="landing-comparison-feature">{row.feature}</td>
                <td className="landing-comparison-brand-cell"><ComparisonCheck value={row.pulse} /></td>
                <td><ComparisonCheck value={row.mailchimp} /></td>
                <td><ComparisonCheck value={row.substack} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const PRICING_TIERS = [
  {
    name: "Free",
    price: "0",
    period: "forever",
    highlight: false,
    features: [
      "Unlimited subscribers",
      "AI content research",
      "11 email templates",
      "Subject line scoring",
      "Basic analytics",
      "Indian language support",
    ],
    cta: "Get started",
  },
  {
    name: "Pro",
    price: "999",
    period: "/month",
    highlight: true,
    features: [
      "Everything in Free",
      "Advanced analytics dashboard",
      "Send time optimization",
      "Custom domain",
      "Priority support",
      "Automation workflows",
      "Team collaboration",
    ],
    cta: "Start free trial",
  },
  {
    name: "Business",
    price: "2,999",
    period: "/month",
    highlight: false,
    features: [
      "Everything in Pro",
      "Dedicated IP",
      "API access",
      "White-label sending",
      "SLA guarantee",
      "Onboarding support",
    ],
    cta: "Contact us",
  },
];

function PricingSection() {
  return (
    <div className="landing-pricing">
      <div className="landing-section-header">
        <span className="landing-section-eyebrow">Pricing</span>
        <h2 className="landing-section-title">Simple, transparent pricing</h2>
        <p className="landing-section-subtitle">
          Start free. Upgrade when you need advanced features. No hidden fees.
        </p>
      </div>
      <div className="landing-pricing-grid">
        {PRICING_TIERS.map((tier) => (
          <div
            key={tier.name}
            className={`landing-pricing-card ${tier.highlight ? "landing-pricing-highlight" : ""}`}
          >
            <div className="landing-pricing-header">
              <span className="landing-pricing-name">{tier.name}</span>
              <div className="landing-pricing-price-row">
                <span className="landing-pricing-currency">&#8377;</span>
                <span className="landing-pricing-price">{tier.price}</span>
                <span className="landing-pricing-period">{tier.period}</span>
              </div>
            </div>
            <ul className="landing-pricing-features">
              {tier.features.map((f) => (
                <li key={f} className="landing-pricing-feature">
                  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke={tier.highlight ? "#F0B429" : "#6B7280"} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="3 8 7 12 13 4" />
                  </svg>
                  {f}
                </li>
              ))}
            </ul>
            <Link
              to="/login"
              className={tier.highlight ? "landing-pricing-cta-primary" : "landing-pricing-cta-secondary"}
              onClick={() => trackEvent("cta-click", { source: "pricing", tier: tier.name })}
            >
              {tier.cta}
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}

function CtaSection() {
  return (
    <div className="landing-cta-section">
      <h2 className="landing-cta-title">Start your AI newsletter today</h2>
      <p className="landing-cta-subtitle">
        Free to start. No credit card required. Your first newsletter goes out in minutes.
      </p>
      <div className="landing-cta-actions">
        <Link to="/login" className="landing-cta-primary" onClick={() => trackEvent("cta-click", { source: "landing-bottom" })}>Create free account</Link>
        <Link to="/gallery" className="landing-cta-secondary" onClick={() => trackEvent("cta-click", { source: "landing-examples" })}>See examples</Link>
      </div>
    </div>
  );
}

function QuickLinks() {
  return (
    <div className="landing-quick-links">
      <Link to="/templates" className="landing-quick-link">
        <span className="landing-quick-link-icon">
          <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="14" height="14" rx="2" />
            <path d="M3 8h14M8 8v9" />
          </svg>
        </span>
        <span>
          <strong className="landing-quick-link-title">Free Templates</strong>
          <span className="landing-quick-link-desc">Browse 11 ready-to-use designs</span>
        </span>
      </Link>
      <Link to="/gallery" className="landing-quick-link">
        <span className="landing-quick-link-icon">
          <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="2" y="3" width="7" height="6" rx="1" />
            <rect x="11" y="3" width="7" height="6" rx="1" />
            <rect x="2" y="11" width="7" height="6" rx="1" />
            <rect x="11" y="11" width="7" height="6" rx="1" />
          </svg>
        </span>
        <span>
          <strong className="landing-quick-link-title">Newsletter Gallery</strong>
          <span className="landing-quick-link-desc">See AI-generated examples</span>
        </span>
      </Link>
      <Link to="/blog" className="landing-quick-link">
        <span className="landing-quick-link-icon">
          <svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 3h10a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2z" />
            <path d="M7 7h6M7 10h6M7 13h3" />
          </svg>
        </span>
        <span>
          <strong className="landing-quick-link-title">Blog</strong>
          <span className="landing-quick-link-desc">Newsletter tips and strategies</span>
        </span>
      </Link>
    </div>
  );
}

export default function LandingPage() {
  const { user, login, register } = useAuth();
  const [mode, setMode] = useState("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState(false);

  const isSignUp = mode === "signup";

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));
  }, []);

  useEffect(() => {
    document.title = "DoAide Pulse — AI Newsletter Platform for Indian Businesses";
    const meta = document.querySelector('meta[name="description"]');
    if (meta) meta.setAttribute("content", "Create, publish, and grow AI-powered newsletters. Research, write, and optimize — all automated. Built for Indian businesses with multilingual support.");
  }, []);

  useEffect(() => {
    const ld = document.createElement("script");
    ld.type = "application/ld+json";
    ld.textContent = JSON.stringify([
      {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        name: "DoAide Pulse",
        applicationCategory: "BusinessApplication",
        operatingSystem: "Web",
        description: "AI-powered newsletter platform for creating, publishing, and growing email newsletters",
        url: "https://pulse.doaide.com",
        publisher: { "@type": "Organization", name: "DoAide", url: "https://doaide.com" },
        offers: { "@type": "Offer", price: "0", priceCurrency: "INR", description: "Free to start" },
      },
      {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        mainEntity: [
          { "@type": "Question", name: "What is DoAide Pulse?", acceptedAnswer: { "@type": "Answer", text: "DoAide Pulse is an AI-powered newsletter platform that helps businesses research, write, and publish professional newsletters automatically." } },
          { "@type": "Question", name: "Is DoAide Pulse free?", acceptedAnswer: { "@type": "Answer", text: "Yes, DoAide Pulse is free to start with no credit card required. Create your first AI-powered newsletter in minutes." } },
          { "@type": "Question", name: "Does it support Indian languages?", acceptedAnswer: { "@type": "Answer", text: "Yes, DoAide Pulse supports content generation in Hindi, Tamil, Telugu, Bengali, Marathi, and other Indian languages." } },
        ],
      },
    ]);
    document.head.appendChild(ld);
    return () => document.head.removeChild(ld);
  }, []);

  if (user) return <Navigate to="/dashboard" replace />;

  async function onSubmit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (isSignUp) await register(email, password, fullName || null);
      else await login(email, password);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="landing-root">
      <div className="landing-particles" aria-hidden="true">
        {PARTICLE_POSITIONS.map((p, i) => (
          <div
            key={i}
            className="landing-particle-dot"
            style={{
              left: p.left,
              top: p.top,
              animationDelay: `${p.delay}s`,
              animationDuration: `${p.duration}s`,
            }}
          />
        ))}
      </div>

      <header className={`landing-header ${visible ? "landing-visible" : ""}`}>
        <a href="https://doaide.com" className="landing-brand">
          <RobotFace size={28} color={ACCENT} />
          <span className="landing-brand-text">
            DoAide <em>Pulse</em>
          </span>
        </a>
        <nav className="landing-header-nav">
          <Link to="/templates" className="landing-header-link">Templates</Link>
          <Link to="/gallery" className="landing-header-link">Gallery</Link>
          <Link to="/blog" className="landing-header-link">Blog</Link>
        </nav>
      </header>

      <main className={`landing-main ${visible ? "landing-visible" : ""}`}>
        <div className="landing-left">
          <div className="landing-hero-robot-wrap">
            <HeroRobot color={ACCENT} />
          </div>
          <h1 className="landing-title">AI newsletters, effortless.</h1>
          <p className="landing-subtitle">
            Research, write, and publish newsletters — AI handles the heavy lifting. Built for Indian businesses.
          </p>
          <TypewriterEffect />
          <PipelineGraphic />
        </div>

        <div className="landing-right">
          <div className="landing-auth-card">
            <div className="landing-auth-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                className={`landing-auth-tab ${!isSignUp ? "active" : ""}`}
                aria-selected={!isSignUp}
                onClick={() => { setMode("signin"); setError(null); }}
              >
                Sign in
              </button>
              <button
                type="button"
                role="tab"
                className={`landing-auth-tab ${isSignUp ? "active" : ""}`}
                aria-selected={isSignUp}
                onClick={() => { setMode("signup"); setError(null); }}
              >
                Create account
              </button>
            </div>

            <form onSubmit={onSubmit} className="landing-auth-form">
              {isSignUp && (
                <div className="landing-field">
                  <label className="landing-label" htmlFor="name">
                    Name{" "}
                    <span className="landing-label-hint">(optional)</span>
                  </label>
                  <input
                    id="name"
                    className="landing-input"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    autoComplete="name"
                  />
                </div>
              )}

              <div className="landing-field">
                <label className="landing-label" htmlFor="email">Email</label>
                <input
                  id="email"
                  type="email"
                  required
                  className="landing-input"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  autoComplete="email"
                />
              </div>

              <div className="landing-field">
                <label className="landing-label" htmlFor="password">Password</label>
                <input
                  id="password"
                  type="password"
                  required
                  minLength={8}
                  className="landing-input"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={isSignUp ? "At least 8 characters" : "••••••••"}
                  autoComplete={isSignUp ? "new-password" : "current-password"}
                />
              </div>

              {error && <p className="landing-error" role="alert">{error}</p>}

              <button type="submit" className="landing-submit" disabled={busy}>
                {busy ? "One moment…" : isSignUp ? "Create account" : "Sign in"}
              </button>
            </form>
          </div>
        </div>
      </main>

      <StatsBar />
      <FeaturesGrid />
      <AnalyticsMockup />
      <ComparisonTable />
      <PricingSection />
      <TestimonialsSection />
      <QuickLinks />
      <CtaSection />

      <div className="mx-auto max-w-3xl px-4 py-4">
        <CrossProductLinks page="landing" />
      </div>

      <DoAideFooter />

      <footer className="landing-footer">
        <div className="landing-footer-products">
          {DOAIDE_PRODUCTS.map((p) => (
            <a key={p.name} href={p.url} className="landing-footer-link">
              {p.name}
            </a>
          ))}
        </div>
        <div className="landing-footer-bottom">
          <a href="https://doaide.com" className="landing-footer-home">
            <RobotFace size={16} color={ACCENT} />
            doaide.com
          </a>
          <span className="landing-footer-copy">&copy; 2026 DoAide</span>
        </div>
      </footer>
    </div>
  );
}

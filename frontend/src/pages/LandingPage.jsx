import { Fragment, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
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
            Do<em>Aide</em>
          </span>
        </a>
      </header>

      <main className={`landing-main ${visible ? "landing-visible" : ""}`}>
        <div className="landing-left">
          <div className="landing-hero-robot-wrap">
            <HeroRobot color={ACCENT} />
          </div>
          <h1 className="landing-title">AI newsletters, effortless.</h1>
          <p className="landing-subtitle">
            Research, write, and publish newsletters — AI handles the heavy lifting.
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

              {error && <p className="landing-error">{error}</p>}

              <button type="submit" className="landing-submit" disabled={busy}>
                {busy ? "One moment…" : isSignUp ? "Create account" : "Sign in"}
              </button>
            </form>
          </div>
        </div>
      </main>

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

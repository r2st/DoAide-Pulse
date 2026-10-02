import { useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";

export default function Login() {
  const { user, login, register } = useAuth();
  const [mode, setMode] = useState("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const isSignUp = mode === "signup";

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
    <>
      <style>{LOGIN_CSS}</style>
      <div className="login-page">
        <div className="login-wrap stagger">
          <div className="login-header">
            <svg
              viewBox="0 0 400 320"
              className="login-logo"
              aria-hidden="true"
            >
              <defs>
                <linearGradient id="hg-login" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#F0B429" />
                  <stop offset="100%" stopColor="#D4A017" />
                </linearGradient>
              </defs>
              <line x1="200" y1="45" x2="200" y2="20" stroke="#F0B429" strokeWidth="6" strokeLinecap="round" />
              <circle cx="200" cy="14" r="10" fill="#F0B429" />
              <circle cx="200" cy="14" r="5" fill="#F7CC5F" />
              <rect x="110" y="50" width="180" height="140" rx="35" fill="url(#hg-login)" />
              <rect x="130" y="68" width="140" height="105" rx="25" fill="#D4A017" opacity="0.4" />
              <ellipse cx="165" cy="115" rx="18" ry="20" fill="#0A0A0B" />
              <ellipse cx="235" cy="115" rx="18" ry="20" fill="#0A0A0B" />
              <circle cx="170" cy="113" r="8" fill="#F7CC5F" />
              <circle cx="240" cy="113" r="8" fill="#F7CC5F" />
              <circle cx="174" cy="109" r="3" fill="white" opacity="0.7" />
              <circle cx="244" cy="109" r="3" fill="white" opacity="0.7" />
              <path d="M170 155Q200 178 230 155" stroke="#0A0A0B" strokeWidth="4" fill="none" strokeLinecap="round" />
              <rect x="92" y="95" width="22" height="45" rx="8" fill="#D4A017" />
              <rect x="286" y="95" width="22" height="45" rx="8" fill="#D4A017" />
              <rect x="175" y="190" width="50" height="14" rx="5" fill="#D4A017" />
              <rect x="145" y="204" width="110" height="55" rx="18" fill="url(#hg-login)" />
              <circle cx="200" cy="228" r="7" fill="#0A0A0B" />
              <circle cx="200" cy="228" r="3.5" fill="#0A0A0B" />
              <path d="M145 218Q118 223 113 240Q108 257 120 262" stroke="#D4A017" strokeWidth="9" fill="none" strokeLinecap="round" />
              <circle cx="120" cy="265" r="7" fill="#D4A017" />
              <path d="M255 218Q282 223 287 240Q292 257 280 262" stroke="#D4A017" strokeWidth="9" fill="none" strokeLinecap="round" />
              <circle cx="280" cy="265" r="7" fill="#D4A017" />
            </svg>

            <h1 className="login-title">
              DoAide <span className="login-title-accent">Pulse</span>
            </h1>
            <p className="login-subtitle">
              Your digital robot for newsletters.
            </p>
          </div>

          <form onSubmit={onSubmit} className="login-card">
            {isSignUp && (
              <div className="login-field">
                <label className="login-label" htmlFor="name">
                  Name{" "}
                  <span style={{ textTransform: "none", letterSpacing: "normal" }}>
                    (optional)
                  </span>
                </label>
                <input
                  id="name"
                  className="login-input"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  autoComplete="name"
                />
              </div>
            )}

            <div className="login-field">
              <label className="login-label" htmlFor="email">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                className="login-input"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
              />
            </div>

            <div className="login-field">
              <label className="login-label" htmlFor="password">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                minLength={8}
                className="login-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={isSignUp ? "At least 8 characters" : "••••••••"}
                autoComplete={isSignUp ? "new-password" : "current-password"}
              />
            </div>

            {error && <p className="login-error">{error}</p>}

            <button type="submit" className="login-btn" disabled={busy}>
              {busy ? "One moment…" : isSignUp ? "Create account" : "Sign in"}
            </button>
          </form>

          <p className="login-toggle">
            {isSignUp ? "Already have an account?" : "First time here?"}{" "}
            <button
              type="button"
              className="login-toggle-link"
              onClick={() => {
                setMode(isSignUp ? "signin" : "signup");
                setError(null);
              }}
            >
              {isSignUp ? "Sign in" : "Create one"}
            </button>
          </p>
        </div>
      </div>
    </>
  );
}

const LOGIN_CSS = `
.login-page {
  display: flex;
  min-height: 100vh;
  align-items: center;
  justify-content: center;
  padding: 64px 20px;
  background-color: #0A0A0B !important;
}
.login-wrap {
  width: 100%;
  max-width: 380px;
}
.login-header {
  margin-bottom: 36px;
  text-align: center;
}
.login-logo {
  margin: 0 auto 20px;
  height: 80px;
  width: 100px;
  color: #F0B429;
  display: block;
}
.login-title {
  font-family: "Instrument Serif", Georgia, serif !important;
  font-size: 42px;
  line-height: 1;
  color: #ffffff !important;
  margin: 0;
  font-weight: 400;
}
.login-title-accent {
  font-style: italic;
  color: #F0B429 !important;
}
.login-subtitle {
  margin-top: 16px;
  font-size: 14px;
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.45) !important;
}
.login-card {
  background-color: rgba(16, 16, 18, 0.8) !important;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.07) !important;
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 1px 0 0 rgba(255, 255, 255, .04) inset,
              0 8px 24px -12px rgba(0, 0, 0, .9);
}
.login-field {
  margin-bottom: 16px;
}
.login-label {
  display: block;
  font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, monospace !important;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  color: rgba(255, 255, 255, 0.35) !important;
  margin-bottom: 8px;
}
.login-input {
  width: 100%;
  background-color: rgba(10, 10, 11, 0.6) !important;
  border: 1px solid rgba(255, 255, 255, 0.12) !important;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 14px;
  color: #ffffff !important;
  font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, monospace !important;
  outline: none;
  box-sizing: border-box;
  transition: border-color 150ms ease;
}
.login-input:focus {
  border-color: rgba(240, 180, 41, 0.5) !important;
  box-shadow: none !important;
}
.login-input::placeholder {
  color: rgba(255, 255, 255, 0.25) !important;
}
.login-error {
  border-radius: 8px;
  border: 1px solid rgba(248, 113, 113, 0.3);
  background-color: rgba(248, 113, 113, 0.1);
  padding: 8px 12px;
  font-size: 12px;
  color: #F87171 !important;
  margin-bottom: 16px;
}
.login-btn {
  width: 100%;
  background-color: #F0B429 !important;
  color: #0A0A0B !important;
  border: none !important;
  border-radius: 8px;
  padding: 10px 16px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  font-family: "Schibsted Grotesk", system-ui, -apple-system, sans-serif;
  transition: background-color 150ms ease;
}
.login-btn:hover:not(:disabled) {
  background-color: #F7D070 !important;
}
.login-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.login-toggle {
  margin-top: 24px;
  text-align: center;
  font-size: 14px;
  color: rgba(255, 255, 255, 0.35) !important;
}
.login-toggle-link {
  background: none !important;
  border: none !important;
  color: #F0B429 !important;
  font-size: 14px;
  cursor: pointer;
  padding: 0;
  text-decoration: none;
  font-family: inherit;
}
.login-toggle-link:hover {
  text-decoration: underline;
  text-underline-offset: 4px;
}
`;

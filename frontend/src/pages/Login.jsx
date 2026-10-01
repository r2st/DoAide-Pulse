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

  if (user) return <Navigate to="/" replace />;

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
              viewBox="0 0 48 48"
              className="login-logo"
              aria-hidden="true"
            >
              <g fill="none">
                <line x1="24" y1="8" x2="24" y2="3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="24" cy="2" r="1.8" fill="currentColor" opacity="0.9" />
                <circle cx="24" cy="2" r="2.8" fill="currentColor" opacity="0.25" />
                <rect x="14" y="8" width="20" height="14" rx="4" fill="currentColor" />
                <circle cx="19.5" cy="14" r="2.2" fill="#0A0A0B" />
                <circle cx="28.5" cy="14" r="2.2" fill="#0A0A0B" />
                <path d="M20 18.5 Q24 21.5 28 18.5" stroke="#0A0A0B" strokeWidth="1.4" fill="none" strokeLinecap="round" />
                <rect x="16" y="23" width="16" height="12" rx="3" fill="currentColor" />
                <rect x="8" y="24" width="7" height="3.5" rx="1.8" fill="currentColor" />
                <rect x="33" y="24" width="7" height="3.5" rx="1.8" fill="currentColor" />
                <rect x="19" y="36" width="3.5" height="5" rx="1.5" fill="currentColor" />
                <rect x="25.5" y="36" width="3.5" height="5" rx="1.5" fill="currentColor" />
                <g transform="translate(36, 28)">
                  <rect x="-2.5" y="0" width="7" height="5.5" rx="1" fill="#0A0A0B" stroke="currentColor" strokeWidth="0.8" />
                  <path d="M-0.5 0 v-1.2 a1.2 1.2 0 0 1 1.2-1.2 h0.6 a1.2 1.2 0 0 1 1.2 1.2 v1.2" stroke="currentColor" strokeWidth="0.7" fill="none" />
                  <rect x="0" y="2" width="2" height="1" rx="0.3" fill="currentColor" />
                </g>
              </g>
            </svg>

            <h1 className="login-title">
              DoAide <span className="login-title-accent">Herald</span>
            </h1>
            <p className="login-subtitle">
              AI-powered newsletters that write themselves
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
  height: 48px;
  width: 48px;
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

import { useState, useEffect } from "react";

export default function InstallPrompt() {
  const [deferredPrompt, setDeferredPrompt] = useState(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const handler = (e) => {
      e.preventDefault();
      setDeferredPrompt(e);
      const dismissed = sessionStorage.getItem("pwa-dismissed");
      if (!dismissed) setVisible(true);
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, []);

  if (!visible) return null;

  const install = async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    setDeferredPrompt(null);
    setVisible(false);
  };

  const dismiss = () => {
    setVisible(false);
    sessionStorage.setItem("pwa-dismissed", "1");
  };

  return (
    <div
      style={{
        position: "fixed",
        bottom: "1rem",
        left: "1rem",
        right: "1rem",
        zIndex: 1000,
        maxWidth: "28rem",
        margin: "0 auto",
        borderRadius: "12px",
        background: "rgb(var(--c-paper, 249 250 251))",
        border: "1px solid rgb(var(--c-line, 229 231 235))",
        padding: "1rem",
        boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
        animation: "fadeUp 0.3s ease-out",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
        <div
          style={{
            width: 40,
            height: 40,
            borderRadius: 8,
            background: "rgba(148,107,12,0.12)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
            color: "rgb(var(--c-brand-500, 148 107 12))",
            fontWeight: 700,
            fontSize: "1.1rem",
          }}
        >
          P
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ margin: 0, fontSize: "0.875rem", fontWeight: 500, color: "rgb(var(--c-ink-900, 17 24 39))" }}>
            Install DoAide Pulse
          </p>
          <p style={{ margin: 0, fontSize: "0.75rem", color: "rgb(var(--c-ink-500, 107 114 128))" }}>
            Use offline like an app
          </p>
        </div>
        <button
          onClick={install}
          style={{
            fontSize: "0.75rem",
            padding: "0.375rem 0.75rem",
            borderRadius: 8,
            flexShrink: 0,
            background: "rgb(var(--c-brand-500, 148 107 12))",
            color: "#fff",
            border: "none",
            cursor: "pointer",
            fontWeight: 600,
          }}
        >
          Install
        </button>
        <button
          onClick={dismiss}
          aria-label="Dismiss"
          style={{
            background: "none",
            border: "none",
            color: "rgb(var(--c-ink-400, 156 163 175))",
            cursor: "pointer",
            padding: 4,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M18 6L6 18M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  );
}

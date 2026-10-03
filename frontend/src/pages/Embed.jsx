import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import PublicNav from "../components/PublicNav";

export default function Embed() {
  const [name, setName] = useState("My Newsletter");
  const [color, setColor] = useState("#F0B429");
  const [layout, setLayout] = useState("compact");

  useEffect(() => {
    document.title = "Embed Widget Generator | DoAide Pulse";
  }, []);

  const origin =
    typeof window !== "undefined"
      ? window.location.origin
      : "https://pulse.doaide.com";

  const code =
    layout === "compact"
      ? `<div data-pulse-signup data-name="${name}" data-color="${color}"></div>\n<script src="${origin}/embed.js" async></script>`
      : `<div data-pulse-signup data-name="${name}" data-color="${color}" data-layout="full"></div>\n<script src="${origin}/embed.js" async></script>`;

  return (
    <>
      <PublicNav />
      <div className="mx-auto max-w-2xl px-4 py-10">
        <h1 className="page-title mb-2">Embed Widget Generator</h1>
        <p className="mb-6 text-ink-500">
          Add a newsletter signup form to your website with two lines of code.
        </p>

        <div className="panel p-5 space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="wn" className="label">
                Newsletter name
              </label>
              <input
                id="wn"
                className="input"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="wc" className="label">
                Accent color
              </label>
              <div className="flex gap-2">
                <input
                  id="wc"
                  type="color"
                  value={color}
                  onChange={(e) => setColor(e.target.value)}
                  className="h-10 w-10 cursor-pointer rounded border border-line"
                />
                <input
                  className="input flex-1"
                  value={color}
                  onChange={(e) => setColor(e.target.value)}
                  aria-label="Color hex"
                />
              </div>
            </div>
          </div>
          <div>
            <label htmlFor="wl" className="label">
              Layout
            </label>
            <select
              id="wl"
              className="input"
              value={layout}
              onChange={(e) => setLayout(e.target.value)}
            >
              <option value="compact">Compact</option>
              <option value="full">Full</option>
            </select>
          </div>
        </div>

        <div className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-ink-900">Preview</h2>
          <div
            className="panel p-5"
            style={{ borderColor: color }}
            data-testid="embed-preview"
          >
            <p className="mb-3 font-semibold text-ink-900">
              Subscribe to {name}
            </p>
            {layout === "full" && (
              <p className="mb-3 text-sm text-ink-500">
                Get the latest insights delivered to your inbox.
              </p>
            )}
            <div className="flex gap-2">
              <input
                className="input flex-1"
                placeholder="you@example.com"
                disabled
              />
              <button
                className="btn text-sm"
                style={{ backgroundColor: color, color: "#0A0A0B" }}
                disabled
              >
                Subscribe
              </button>
            </div>
          </div>
        </div>

        <div className="mt-6">
          <label htmlFor="code" className="label">
            Embed code
          </label>
          <textarea
            id="code"
            readOnly
            rows={3}
            className="input font-mono text-xs"
            value={code}
            onFocus={(e) => e.target.select()}
          />
        </div>

        <div className="panel mt-10 p-5 text-center">
          <p className="mb-3 text-ink-900">
            Grow your subscriber list with DoAide Pulse.
          </p>
          <Link to="/" className="btn-primary">
            Get started free
          </Link>
        </div>
      </div>
    </>
  );
}

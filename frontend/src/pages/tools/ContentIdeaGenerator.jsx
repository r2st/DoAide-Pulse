import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ShareButtons from "../../components/ShareButtons";
import PublicNav from "../../components/PublicNav";
import DoAideFooter from "../../components/DoAideFooter";
import { api } from "../../lib/api";

const EXAMPLE_NICHES = [
  "SaaS marketing",
  "AI tools for developers",
  "Personal finance in India",
  "Fitness for busy professionals",
  "Web3 and blockchain",
];

export default function ContentIdeaGenerator() {
  const [niche, setNiche] = useState("");
  const [count, setCount] = useState(5);
  const [ideas, setIdeas] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    document.title = "Free Content Idea Generator | DoAide Pulse";
    const meta = document.querySelector('meta[name="description"]');
    if (meta)
      meta.setAttribute(
        "content",
        "Generate fresh content ideas for your newsletter or blog with AI. Free, no login required. Powered by DoAide Pulse."
      );
  }, []);

  async function handleGenerate(e) {
    e.preventDefault();
    if (!niche.trim()) return;
    setLoading(true);
    setError("");
    setIdeas(null);
    try {
      const data = await api.generateContentIdeas(niche.trim(), count);
      setIdeas(data);
    } catch (err) {
      setError(err.message || "Something went wrong. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  const origin =
    typeof window !== "undefined"
      ? window.location.origin
      : "https://pulse.doaide.com";

  return (
    <div className="min-h-screen bg-canvas text-ink-700">
      <PublicNav />

      <div className="mx-auto max-w-2xl px-4 py-10">
        <div className="text-center mb-8">
          <span className="eyebrow mb-3 block text-brand-500">
            Free Tool
          </span>
          <h1 className="font-display text-3xl md:text-4xl text-ink-900 mb-3">
            Content Idea Generator
          </h1>
          <p className="text-ink-500 max-w-lg mx-auto">
            Enter your niche or topic and get AI-generated content ideas instantly.
            No login required.
          </p>
        </div>

        <form onSubmit={handleGenerate} className="panel p-6 space-y-4">
          <div>
            <label htmlFor="niche" className="label">
              Your niche or topic
            </label>
            <input
              id="niche"
              type="text"
              className="input"
              placeholder="e.g., AI tools for developers"
              value={niche}
              onChange={(e) => setNiche(e.target.value)}
              maxLength={200}
            />
          </div>
          <div className="flex flex-wrap gap-2">
            {EXAMPLE_NICHES.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => setNiche(ex)}
                className="rounded-full border border-line px-3 py-1 text-xs text-ink-500 hover:border-brand-500/30 hover:text-brand-500 transition"
              >
                {ex}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-4">
            <label htmlFor="count" className="text-sm text-ink-500">
              Ideas:
            </label>
            <select
              id="count"
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
              className="input w-auto"
            >
              {[3, 5, 7, 10].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={loading || !niche.trim()}
            className="btn-primary w-full"
          >
            {loading ? "Generating ideas…" : "Generate ideas"}
          </button>
        </form>

        {error && (
          <div role="alert" className="mt-4 rounded-xl border border-bad/30 bg-bad-wash p-4 text-center text-sm text-bad">
            {error}
          </div>
        )}

        {ideas && (
          <div className="mt-6 space-y-3" data-testid="ideas-list">
            <h2 className="text-lg font-semibold text-ink-900">
              Ideas for &ldquo;{ideas.niche}&rdquo;
            </h2>
            {ideas.ideas.map((idea, i) => (
              <div
                key={i}
                className="panel p-5 transition hover:border-brand-500/20"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1">
                    <h3 className="font-medium text-ink-900">{idea.title}</h3>
                    <p className="mt-1 text-sm text-ink-500">{idea.hook}</p>
                  </div>
                  <span className="chip shrink-0">
                    {idea.content_type.replace(/_/g, " ")}
                  </span>
                </div>
              </div>
            ))}

            <div className="panel mt-6 p-5">
              <p className="mb-3 text-sm font-medium text-ink-900">Share these ideas</p>
              <ShareButtons
                url={`${origin}/tools/content-idea-generator`}
                title="Free AI Content Idea Generator"
                variant="prominent"
              />
            </div>
          </div>
        )}

        <div className="mt-10 rounded-2xl border border-brand-500/20 bg-brand-50 p-8 text-center">
          <h2 className="font-display text-xl text-ink-900 mb-2">
            Turn these ideas into published newsletters
          </h2>
          <p className="text-sm text-ink-500 mb-4">
            DoAide Pulse writes, schedules, and publishes your content with AI.
            Free to start, no credit card required.
          </p>
          <Link
            to="/"
            className="btn-primary"
          >
            Try DoAide Pulse for free
          </Link>
        </div>
      </div>

      <DoAideFooter />
    </div>
  );
}

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
    <div className="min-h-screen" style={{ background: "#0A0A0B", color: "#E5E7EB" }}>
      <PublicNav />

      <div className="mx-auto max-w-2xl px-4 py-10">
        <div className="text-center mb-8">
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-[#F0B429] mb-3 block">
            Free Tool
          </span>
          <h1 className="font-display text-3xl md:text-4xl text-white mb-3">
            Content Idea Generator
          </h1>
          <p className="text-[#9CA3AF] max-w-lg mx-auto">
            Enter your niche or topic and get AI-generated content ideas instantly.
            No login required.
          </p>
        </div>

        <form onSubmit={handleGenerate} className="rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] p-6 space-y-4">
          <div>
            <label htmlFor="niche" className="block text-sm font-medium text-white mb-1">
              Your niche or topic
            </label>
            <input
              id="niche"
              type="text"
              className="w-full rounded-lg border border-[#2A2A2D] bg-[#0A0A0B] px-4 py-2.5 text-white placeholder-[#6B7280] focus:border-[#F0B429] focus:outline-none"
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
                className="rounded-full border border-[#2A2A2D] px-3 py-1 text-xs text-[#9CA3AF] hover:border-[#F0B429]/30 hover:text-[#F0B429] transition"
              >
                {ex}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-4">
            <label htmlFor="count" className="text-sm text-[#9CA3AF]">
              Ideas:
            </label>
            <select
              id="count"
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
              className="rounded-lg border border-[#2A2A2D] bg-[#0A0A0B] px-3 py-1.5 text-sm text-white focus:border-[#F0B429] focus:outline-none"
            >
              {[3, 5, 7, 10].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={loading || !niche.trim()}
            className="w-full rounded-lg bg-[#F0B429] py-2.5 text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017] disabled:opacity-50"
          >
            {loading ? "Generating ideas…" : "Generate ideas"}
          </button>
        </form>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-center text-sm text-red-400">
            {error}
          </div>
        )}

        {ideas && (
          <div className="mt-6 space-y-3" data-testid="ideas-list">
            <h2 className="text-lg font-semibold text-white">
              Ideas for &ldquo;{ideas.niche}&rdquo;
            </h2>
            {ideas.ideas.map((idea, i) => (
              <div
                key={i}
                className="rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] p-5 transition hover:border-[#F0B429]/20"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1">
                    <h3 className="font-medium text-white">{idea.title}</h3>
                    <p className="mt-1 text-sm text-[#9CA3AF]">{idea.hook}</p>
                  </div>
                  <span className="shrink-0 font-mono text-[9px] uppercase tracking-wider text-[#F0B429] bg-[#F0B429]/10 px-1.5 py-0.5 rounded">
                    {idea.content_type.replace(/_/g, " ")}
                  </span>
                </div>
              </div>
            ))}

            <div className="mt-6 rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] p-5">
              <p className="mb-3 text-sm font-medium text-white">Share these ideas</p>
              <ShareButtons
                url={`${origin}/tools/content-idea-generator`}
                title="Free AI Content Idea Generator"
                variant="prominent"
              />
            </div>
          </div>
        )}

        <div className="mt-10 rounded-2xl border border-[#F0B429]/20 bg-gradient-to-br from-[#1A1A1D] to-[#F0B429]/5 p-8 text-center">
          <h2 className="font-display text-xl text-white mb-2">
            Turn these ideas into published newsletters
          </h2>
          <p className="text-sm text-[#9CA3AF] mb-4">
            DoAide Pulse writes, schedules, and publishes your content with AI.
            Free to start, no credit card required.
          </p>
          <Link
            to="/"
            className="inline-block rounded-lg bg-[#F0B429] px-6 py-2.5 text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]"
          >
            Try DoAide Pulse for free
          </Link>
        </div>
      </div>

      <DoAideFooter />
    </div>
  );
}

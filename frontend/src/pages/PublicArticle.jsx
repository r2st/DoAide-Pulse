import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { Skeleton } from "../components/ui/Bits";
import Logo from "../components/ui/Logo";
import PublicNav from "../components/PublicNav";
import DoAideFooter from "../components/DoAideFooter";
import ShareButtons from "../components/ShareButtons";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatCount, formatReadLength } from "../lib/format";
import { renderMarkdown } from "../lib/markdown";

export default function PublicArticle() {
  const { slug } = useParams();
  const { data, error, loading } = useApi(() => api.publicArticle(slug), [slug]);

  useEffect(() => {
    if (data) {
      document.title = `${data.title} | DoAide Pulse`;
      const meta = document.querySelector('meta[name="description"]');
      if (meta) meta.setAttribute("content", data.meta_description || data.excerpt);
    }
  }, [data]);

  const origin = typeof window !== "undefined" ? window.location.origin : "https://pulse.doaide.com";
  const articleUrl = `${origin}/article/${slug}`;

  return (
    <div className="min-h-screen" style={{ background: "#0A0A0B", color: "#E5E7EB" }}>
      <PublicNav />

      <div className="mx-auto max-w-2xl px-4 py-10">
        {loading && (
          <div>
            <p role="status" className="sr-only">Loading article…</p>
            <Skeleton rows={5} />
          </div>
        )}

        {error && (
          <div className="rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] p-8 text-center">
            <p className="text-[#9CA3AF]">
              This article isn't available — it may have been removed or unpublished.
            </p>
            <Link to="/" className="mt-4 inline-block text-sm text-[#F0B429] hover:underline">
              Go to DoAide Pulse
            </Link>
          </div>
        )}

        {data && (
          <>
            <article className="stagger">
              {data.cover_image_url && (
                <img
                  src={data.cover_image_url}
                  alt={`Cover image for ${data.title}`}
                  loading="lazy"
                  className="mb-6 aspect-[1200/630] w-full rounded-xl border border-[#2A2A2D] object-cover"
                />
              )}

              <div className="mb-4 flex flex-wrap items-center gap-2">
                {data.tags.map((tag) => (
                  <span
                    key={tag}
                    className="font-mono text-[9px] uppercase tracking-wider text-[#F0B429] bg-[#F0B429]/10 px-1.5 py-0.5 rounded"
                  >
                    {tag}
                  </span>
                ))}
              </div>

              <p className="text-xs text-[#6B7280] mb-4">
                {data.project_name && <>{data.project_name} · </>}
                {formatCount(data.word_count)} words · {formatReadLength(data.read_minutes)}
                {data.published_at && <> · {new Date(data.published_at).toLocaleDateString()}</>}
              </p>

              <div
                className="prose-pulse rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] px-7 py-6"
                dangerouslySetInnerHTML={{
                  __html: `<h1 style="color:#fff;margin-top:0">${escapeText(data.title)}</h1>${renderMarkdown(data.body_markdown)}`,
                }}
              />
            </article>

            <div className="mt-8 rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] p-6">
              <p className="mb-3 text-sm font-medium text-white">Share this article</p>
              <ShareButtons url={articleUrl} title={data.title} variant="prominent" />
            </div>

            <div className="mt-8 rounded-2xl border border-[#F0B429]/20 bg-gradient-to-br from-[#1A1A1D] to-[#F0B429]/5 p-8 text-center">
              <div className="flex items-center justify-center gap-2 mb-3">
                <Logo className="h-5 w-5" />
                <span className="text-xs font-medium text-[#F0B429] uppercase tracking-wider">
                  Published with DoAide Pulse
                </span>
              </div>
              <h2 className="font-display text-xl text-white mb-2">
                Create AI-powered newsletters in minutes
              </h2>
              <p className="text-sm text-[#9CA3AF] mb-4">
                Turn your ideas into polished newsletters with AI. Free to start.
              </p>
              <Link
                to="/"
                className="inline-block rounded-lg bg-[#F0B429] px-6 py-2.5 text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]"
              >
                Try DoAide Pulse for free
              </Link>
            </div>
          </>
        )}
      </div>

      <DoAideFooter />
    </div>
  );
}

function escapeText(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

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
    <div className="min-h-screen bg-canvas text-ink-700">
      <PublicNav />

      <div className="mx-auto max-w-2xl px-4 py-10">
        {loading && (
          <div>
            <p role="status" className="sr-only">Loading article…</p>
            <Skeleton rows={5} />
          </div>
        )}

        {error && (
          <div className="panel p-8 text-center">
            <p className="text-ink-500">
              This article isn't available — it may have been removed or unpublished.
            </p>
            <Link to="/" className="mt-4 inline-block text-sm text-brand-500 hover:underline">
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
                  className="mb-6 aspect-[1200/630] w-full rounded-xl border border-line object-cover"
                />
              )}

              <div className="mb-4 flex flex-wrap items-center gap-2">
                {data.tags.map((tag) => (
                  <span
                    key={tag}
                    className="chip text-brand-500"
                  >
                    {tag}
                  </span>
                ))}
              </div>

              <p className="text-xs text-ink-400 mb-4">
                {data.project_name && <>{data.project_name} · </>}
                {formatCount(data.word_count)} words · {formatReadLength(data.read_minutes)}
                {data.published_at && <> · {new Date(data.published_at).toLocaleDateString()}</>}
              </p>

              <div
                className="prose-pulse panel px-7 py-6"
                dangerouslySetInnerHTML={{
                  __html: `<h1 style="margin-top:0">${escapeText(data.title)}</h1>${renderMarkdown(data.body_markdown)}`,
                }}
              />
            </article>

            <div className="panel mt-8 p-6">
              <p className="mb-3 text-sm font-medium text-ink-900">Share this article</p>
              <ShareButtons url={articleUrl} title={data.title} variant="prominent" />
            </div>

            <div className="mt-8 rounded-2xl border border-brand-500/20 bg-brand-50 p-8 text-center">
              <div className="flex items-center justify-center gap-2 mb-3">
                <Logo className="h-5 w-5" />
                <span className="eyebrow text-brand-500">
                  Published with DoAide Pulse
                </span>
              </div>
              <h2 className="font-display text-xl text-ink-900 mb-2">
                Create AI-powered newsletters in minutes
              </h2>
              <p className="text-sm text-ink-500 mb-4">
                Turn your ideas into polished newsletters with AI. Free to start.
              </p>
              <Link
                to="/"
                className="btn-primary"
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

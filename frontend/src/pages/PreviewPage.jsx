import { useParams } from "react-router-dom";
import { Skeleton } from "../components/ui/Bits";
import Logo from "../components/ui/Logo";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatCount, formatReadLength } from "../lib/format";
import { renderMarkdown } from "../lib/markdown";

/**
 * A reviewer's view of one draft — no sign-in, no navigation, nothing about
 * where else it might go out. Reachable only by holding the link itself;
 * see app.services.preview_links on the backend for how that link expires
 * and can be revoked.
 */
export default function PreviewPage() {
  const { token } = useParams();
  const { data, error, loading } = useApi(() => api.publicPreview(token), [token]);

  return (
    <div className="min-h-screen px-5 py-12">
      <div className="mx-auto max-w-2xl">
        <div className="mb-8 flex items-center gap-2 text-ink-400">
          <Logo className="h-5 w-5" />
          <span className="eyebrow">Pulse preview</span>
        </div>

        {/* The shape the article arrives in: a line of meta, then the body in
            its panel. `Skeleton` is `aria-hidden`, so the announcement that
            used to come free with the word "Loading…" is made explicit. */}
        {loading && (
          <div>
            <p role="status" className="sr-only">
              Loading preview…
            </p>
            <div
              aria-hidden="true"
              className="relative mb-3 h-3 w-48 overflow-hidden rounded border border-line bg-paper"
            >
              <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-canvas to-transparent" />
            </div>
            <Skeleton rows={5} />
          </div>
        )}

        {error && (
          <div className="panel p-6 text-center">
            <p className="text-sm text-ink-700">
              This link isn't available anymore — it may have expired or been
              revoked.
            </p>
          </div>
        )}

        {data && (
          <article className="stagger">
            {data.cover_image_url && (
              <img
                src={data.cover_image_url}
                alt="Newsletter cover image"
                loading="lazy"
                className="mb-6 aspect-[1200/630] w-full rounded-xl border border-line object-cover"
              />
            )}
            <p className="eyebrow mb-2">
              {data.project_name}
              {data.project_name && " · "}
              {formatCount(data.word_count)} words · {formatReadLength(data.read_minutes)}
            </p>
            <div
              className="prose-pulse panel px-7 py-6"
              dangerouslySetInnerHTML={{
                __html: `<h1>${escapeText(data.title)}</h1>${renderMarkdown(
                  data.body_markdown,
                )}`,
              }}
            />
          </article>
        )}
      </div>
    </div>
  );
}

function escapeText(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

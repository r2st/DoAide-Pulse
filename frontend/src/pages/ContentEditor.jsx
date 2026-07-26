import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Confidence, ErrorBanner, Skeleton, StatusBadge } from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import { formatDateTime, formatWhen, titleize } from "../lib/format";
import { renderMarkdown } from "../lib/markdown";

/**
 * Edit one piece, see what SEO thinks of it, and send it somewhere.
 *
 * Edit and preview are tabs rather than a split pane: at this measure a
 * side-by-side gives you two unreadable columns, and the preview is something
 * you check rather than watch.
 */
export default function ContentEditor() {
  const { contentId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();

  const { data, error, loading, reload, setData } = useApi(
    () => api.getContent(contentId),
    [contentId],
  );
  const { data: platforms } = useApi(() => api.platforms(), []);

  const [draft, setDraft] = useState(null);
  const [tab, setTab] = useState("write");
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    if (data) {
      setDraft({
        title: data.title,
        body_markdown: data.body_markdown,
        excerpt: data.excerpt,
        meta_description: data.meta_description,
        keywords: (data.keywords ?? []).join(", "),
        tags: (data.tags ?? []).join(", "),
      });
    }
  }, [data]);

  const dirty = useMemo(() => {
    if (!data || !draft) return false;
    return (
      draft.title !== data.title ||
      draft.body_markdown !== data.body_markdown ||
      draft.excerpt !== data.excerpt ||
      draft.meta_description !== data.meta_description ||
      draft.keywords !== (data.keywords ?? []).join(", ") ||
      draft.tags !== (data.tags ?? []).join(", ")
    );
  }, [data, draft]);

  // A published piece is a record of what went out. Editing it here would
  // change nothing on the platforms, so the fields are read-only.
  const locked = data?.status === "published";

  if (loading && !data) return <Skeleton rows={6} />;
  if (error) return <ErrorBanner message={error} onRetry={reload} />;
  if (!data || !draft) return null;

  const set = (key) => (event) =>
    setDraft((current) => ({ ...current, [key]: event.target.value }));

  const splitList = (value) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

  async function save() {
    setSaving(true);
    try {
      const updated = await api.updateContent(data.id, {
        ...draft,
        keywords: splitList(draft.keywords),
        tags: splitList(draft.tags),
      });
      setData(updated);
      toast.success("Saved");
    } catch (err) {
      toast.error(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function approve() {
    try {
      await api.approveContent(data.id);
      reload();
      toast.success("Approved — ready to publish");
    } catch (err) {
      toast.error(err.message);
    }
  }

  async function remove() {
    if (!window.confirm(`Delete "${data.title}"? This cannot be undone.`)) return;
    try {
      await api.deleteContent(data.id);
      toast.success("Deleted");
      navigate("/content");
    } catch (err) {
      toast.error(err.message);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <Link to="/content" className="btn-quiet -ml-2.5">
            ← All content
          </Link>
          <h1 className="mt-1 font-display text-2xl leading-tight text-ink-900">
            {data.title}
          </h1>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-400">
            <span>{data.project_name}</span>
            <span>{titleize(data.content_type)}</span>
            <span>{data.word_count} words</span>
            <span>{data.read_minutes} min read</span>
            <span>updated {formatWhen(data.updated_at)}</span>
            {data.generated_by_model && (
              <span title="Which model wrote it">{data.generated_by_model}</span>
            )}
            <Confidence value={data.confidence} />
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={data.status} />
          {!locked && (
            <button className="btn-ghost" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : dirty ? "Save" : "Saved"}
            </button>
          )}
          {(data.status === "draft" || data.status === "review") && (
            <button className="btn-ghost" onClick={approve}>
              Approve
            </button>
          )}
          <button
            className="btn-primary"
            onClick={() => setPublishing(true)}
            disabled={locked}
          >
            Publish
          </button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="space-y-4">
          <div className="flex gap-1 border-b border-line">
            {["write", "preview"].map((value) => (
              <button
                key={value}
                onClick={() => setTab(value)}
                className={[
                  "-mb-px border-b-2 px-3 py-2 text-sm transition-colors",
                  tab === value
                    ? "border-brand-500 text-ink-900"
                    : "border-transparent text-ink-500 hover:text-ink-900",
                ].join(" ")}
              >
                {titleize(value)}
              </button>
            ))}
          </div>

          {tab === "write" ? (
            <div className="space-y-4">
              <div>
                <label className="label" htmlFor="c-title">
                  Title
                </label>
                <input
                  id="c-title"
                  className="input"
                  value={draft.title}
                  onChange={set("title")}
                  disabled={locked}
                />
              </div>
              <div>
                <label className="label" htmlFor="c-body">
                  Body <span className="normal-case tracking-normal">(Markdown)</span>
                </label>
                <textarea
                  id="c-body"
                  className="input min-h-[520px] resize-y font-mono text-[13px] leading-relaxed"
                  value={draft.body_markdown}
                  onChange={set("body_markdown")}
                  disabled={locked}
                  spellCheck
                />
              </div>
            </div>
          ) : (
            <article
              className="prose-herald panel px-7 py-6"
              // The renderer escapes everything before emitting a tag and has no
              // raw-HTML passthrough — see lib/markdown.js.
              dangerouslySetInnerHTML={{
                __html: `<h1>${escapeText(draft.title)}</h1>${renderMarkdown(
                  draft.body_markdown,
                )}`,
              }}
            />
          )}
        </div>

        <aside className="space-y-4">
          <SeoPanel
            issues={data.seo_issues}
            draft={draft}
            onChange={set}
            locked={locked}
          />
          <PublicationsPanel content={data} onChanged={reload} />
          {!locked && (
            <button className="btn-quiet w-full text-bad" onClick={remove}>
              Delete this piece
            </button>
          )}
        </aside>
      </div>

      {publishing && (
        <PublishDialog
          content={data}
          platforms={platforms ?? []}
          onClose={() => setPublishing(false)}
          onDone={() => {
            setPublishing(false);
            reload();
            toast.success("Queued for publishing");
          }}
          onError={(message) => toast.error(message)}
        />
      )}
    </div>
  );
}

function escapeText(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function SeoPanel({ issues, draft, onChange, locked }) {
  return (
    <div className="panel space-y-4 p-5">
      <h2 className="text-sm font-semibold text-ink-900">SEO</h2>

      <div>
        <label className="label" htmlFor="c-meta">
          Meta description
        </label>
        <textarea
          id="c-meta"
          rows={3}
          className="input resize-y text-[13px]"
          value={draft.meta_description}
          onChange={onChange("meta_description")}
          disabled={locked}
        />
        <p className="mt-1 text-right font-mono text-[10px] text-ink-400">
          {draft.meta_description.length}/155
        </p>
      </div>

      <div>
        <label className="label" htmlFor="c-keywords">
          Keywords
        </label>
        <input
          id="c-keywords"
          className="input text-[13px]"
          value={draft.keywords}
          onChange={onChange("keywords")}
          disabled={locked}
        />
      </div>

      <div>
        <label className="label" htmlFor="c-tags">
          Platform tags
        </label>
        <input
          id="c-tags"
          className="input text-[13px]"
          value={draft.tags}
          onChange={onChange("tags")}
          disabled={locked}
        />
        <p className="mt-1 text-xs text-ink-400">
          Dev.to takes 4, Medium 5. Extras are dropped at publish time.
        </p>
      </div>

      <div>
        <label className="label" htmlFor="c-excerpt">
          Excerpt
        </label>
        <textarea
          id="c-excerpt"
          rows={2}
          className="input resize-y text-[13px]"
          value={draft.excerpt}
          onChange={onChange("excerpt")}
          disabled={locked}
        />
      </div>

      {issues.length === 0 ? (
        <p className="rounded-lg bg-good-wash px-3 py-2 text-xs text-good">
          No SEO issues.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {issues.map((issue, index) => (
            <li
              key={index}
              className={`rounded-lg px-3 py-2 text-xs ${
                issue.level === "error"
                  ? "bg-bad-wash text-bad"
                  : "bg-warn-wash text-warn"
              }`}
            >
              {issue.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PublicationsPanel({ content, onChanged }) {
  const toast = useToast();

  async function retry(publicationId) {
    try {
      await api.retryPublication(content.id, publicationId);
      onChanged();
      toast.success("Retrying");
    } catch (err) {
      toast.error(err.message);
    }
  }

  if (content.publications.length === 0) {
    return (
      <div className="panel p-5">
        <h2 className="mb-1 text-sm font-semibold text-ink-900">Publications</h2>
        <p className="text-xs text-ink-400">Not sent anywhere yet.</p>
      </div>
    );
  }

  return (
    <div className="panel p-5">
      <h2 className="mb-3 text-sm font-semibold text-ink-900">Publications</h2>
      <ul className="space-y-3">
        {content.publications.map((publication) => (
          <li key={publication.id} className="text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-ink-900">
                {titleize(publication.platform)}
              </span>
              <StatusBadge status={publication.status} />
            </div>
            {publication.external_url && (
              <a
                href={publication.external_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 block truncate font-mono text-[11px] text-brand-500 hover:underline"
              >
                {publication.external_url}
              </a>
            )}
            {publication.scheduled_for && publication.status === "scheduled" && (
              <p className="mt-1 font-mono text-[11px] text-ink-400">
                {formatDateTime(publication.scheduled_for)}
              </p>
            )}
            {publication.error && (
              <p className="mt-1 break-words rounded bg-bad-wash px-2 py-1 font-mono text-[10px] text-bad">
                {publication.error}
              </p>
            )}
            {publication.status === "failed" && (
              <button className="btn-quiet mt-1 -ml-2.5" onClick={() => retry(publication.id)}>
                Retry
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Pick platforms and a time. Unconnected and unfinished platforms are shown
 *  but disabled, with the reason — hiding them makes the list look arbitrary. */
function PublishDialog({ content, platforms, onClose, onDone, onError }) {
  const [selected, setSelected] = useState([]);
  const [when, setWhen] = useState("");
  const [asDraft, setAsDraft] = useState(false);
  const [busy, setBusy] = useState(false);

  const alreadyLive = new Set(
    content.publications
      .filter((p) => p.status === "published")
      .map((p) => p.platform),
  );

  function toggle(platform) {
    setSelected((current) =>
      current.includes(platform)
        ? current.filter((p) => p !== platform)
        : [...current, platform],
    );
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.publishContent(content.id, {
        platforms: selected,
        // datetime-local has no timezone; the browser's own offset is the
        // right interpretation of what the user typed.
        scheduled_for: when ? new Date(when).toISOString() : null,
        as_draft: asDraft,
      });
      onDone();
    } catch (err) {
      onError(err.message);
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ink-900/25 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label="Publish"
      onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}
    >
      <form onSubmit={submit} className="panel my-auto w-full max-w-lg space-y-4 p-6 shadow-pop">
        <h2 className="font-display text-2xl text-ink-900">Publish</h2>

        <div className="space-y-2">
          {platforms.map((platform) => {
            const live = alreadyLive.has(platform.platform);
            const connected = platform.connection?.status === "connected";
            const disabled = !platform.implemented || !connected || live;
            const reason = live
              ? "Already published here"
              : !platform.implemented
                ? "Adapter not finished"
                : !connected
                  ? "Not connected — add credentials in Settings"
                  : null;

            return (
              <label
                key={platform.platform}
                className={`flex items-start gap-3 rounded-lg border px-3 py-2.5 ${
                  disabled
                    ? "border-line bg-canvas opacity-60"
                    : "border-line-strong bg-paper"
                }`}
              >
                <input
                  type="checkbox"
                  className="mt-0.5"
                  disabled={disabled}
                  checked={selected.includes(platform.platform)}
                  onChange={() => toggle(platform.platform)}
                />
                <span className="min-w-0">
                  <span className="block text-sm text-ink-900">
                    {platform.display_name}
                  </span>
                  {reason && (
                    <span className="mt-0.5 block text-xs text-ink-400">{reason}</span>
                  )}
                </span>
              </label>
            );
          })}
        </div>

        <div>
          <label className="label" htmlFor="pub-when">
            When <span className="normal-case tracking-normal">(blank = now)</span>
          </label>
          <input
            id="pub-when"
            type="datetime-local"
            className="input"
            value={when}
            onChange={(e) => setWhen(e.target.value)}
          />
        </div>

        <label className="flex items-start gap-2.5 text-sm text-ink-700">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={asDraft}
            onChange={(e) => setAsDraft(e.target.checked)}
          />
          <span>
            Create as a draft on the platform
            <span className="mt-0.5 block text-xs text-ink-400">
              Stages it there so you can hit publish yourself.
            </span>
          </span>
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={busy || selected.length === 0}
          >
            {busy ? "Queueing…" : when ? "Schedule" : "Publish now"}
          </button>
        </div>
      </form>
    </div>
  );
}

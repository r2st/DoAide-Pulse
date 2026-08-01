import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import SocialPreview from "../components/SocialPreview";
import { Confidence, ErrorBanner, Skeleton, StatusBadge } from "../components/ui/Bits";
import { useToast } from "../components/ui/Toast";
import { useApi } from "../hooks/useApi";
import { api } from "../lib/api";
import * as draftStore from "../lib/draftStore";
import { differs } from "../lib/draftStore";
import { editorStats } from "../lib/editorStats";
import {
  formatCount,
  formatDateTime,
  formatReadLength,
  formatWhen,
  titleize,
} from "../lib/format";
import { renderMarkdown } from "../lib/markdown";

/**
 * How long the typing has to stop before the draft is written.
 *
 * Long enough that a pause for thought mid-paragraph does not trigger a
 * request, short enough that stepping away from the keyboard leaves the work
 * saved rather than sitting in a tab.
 */
const AUTOSAVE_DELAY_MS = 2000;

/** The server's copy, in the shape the editor's fields hold.
 *
 *  One function so `dirty`, the recovery comparison and the storage mirror all
 *  agree on what "the same draft" means — three hand-rolled field lists would
 *  drift, and the one that drifted would either nag about nothing or lose an
 *  edit. */
function draftFrom(content) {
  return {
    title: content.title,
    body_markdown: content.body_markdown,
    excerpt: content.excerpt,
    meta_description: content.meta_description,
    keywords: (content.keywords ?? []).join(", "),
    tags: (content.tags ?? []).join(", "),
    cover_image_url: content.cover_image_url ?? "",
  };
}

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
  // An unsaved draft found in local storage on arrival. Offered, never applied
  // on its own: the server's copy is what the user last committed to, and
  // silently replacing it with something a crashed tab left behind is the kind
  // of help that loses work rather than saving it.
  const [recovered, setRecovered] = useState(null);
  // What the auto-save last did. `at` is kept through a failure so the status
  // line can still say when the text was last known to be on the server —
  // which is the thing you want to know when a save has just stopped working.
  const [autoSave, setAutoSave] = useState({ status: "idle", at: null, error: null });

  const saved = useMemo(() => (data ? draftFrom(data) : null), [data]);

  // Load the server's copy into the fields when the *piece* changes — not
  // every time `data` does.
  //
  // The unconditional version of this clobbered anything typed while a save
  // was in flight: the response lands, `data` changes, and every keystroke
  // since the request went out is replaced by what the server was told a
  // moment ago. Saving by hand made that a narrow window; saving every couple
  // of seconds would make it a routine way to lose a sentence. Taking the
  // server's copy after a save is `persist`'s job, and it only does so when
  // the fields still hold the text it sent.
  const loadedId = useRef(null);
  useEffect(() => {
    if (!data || loadedId.current === data.id) return;
    loadedId.current = data.id;
    setDraft(draftFrom(data));
  }, [data]);

  const dirty = useMemo(
    () => Boolean(saved && draft && differs(draft, saved)),
    [draft, saved],
  );

  // A published piece is a record of what went out. Editing it here would
  // change nothing on the platforms, so the fields are read-only.
  const locked = data?.status === "published";

  // Look for a recovery buffer once per piece, on arrival, and take the chance
  // to drop everyone else's stale ones while we are here.
  useEffect(() => {
    draftStore.prune();
    if (!saved || locked) return;
    const stored = draftStore.load(contentId);
    // Only interesting if it still says something the server does not. A
    // buffer that matches what was since saved is noise.
    setRecovered(stored && differs(stored.draft, saved) ? stored : null);
  }, [contentId, saved, locked]);

  // Mirror every edit. Cheap, synchronous, and cleared by `save` itself once
  // the draft matches the server again.
  //
  // Suspended while an offer is on screen. On arrival `draft` is initialised to
  // the server's copy, so mirroring it would immediately write "no difference"
  // over the buffer and delete the very work the banner is pointing at —
  // recovery would survive exactly one page load, and opening the editor would
  // be what destroyed it.
  useEffect(() => {
    if (!draft || !saved || locked || recovered) return;
    draftStore.save(contentId, draft, saved);
  }, [contentId, draft, saved, locked, recovered]);

  // Auto-save, debounced from the last keystroke rather than run on a fixed
  // interval — a save then lands in the pause between two sentences instead of
  // halfway through a word, and a fast typist makes one request rather than one
  // every two seconds.
  //
  // Same ref trick as ⌘S below: the effect belongs up here with the others, and
  // the function it calls cannot be declared until after the early returns.
  const autoSaveRef = useRef(null);
  useEffect(() => {
    if (!autoSaveRef.current) return undefined;
    const timer = setTimeout(() => autoSaveRef.current?.(), AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
    // `draft` is a fresh object per keystroke, which is what restarts the
    // countdown; the rest are the conditions that decide whether there is
    // anything to save at all.
  }, [draft, dirty, locked, recovered, saving]);

  // ⌘S / Ctrl-S. Registered up here with the other effects, so it reads the
  // current `save` through a ref rather than needing to be declared after it.
  const saveRef = useRef(null);
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key !== "s" || !(event.metaKey || event.ctrlKey)) return;
      // Only claim the shortcut when there is something to save. Otherwise the
      // browser's own Save Page is the more useful thing to leave alone.
      if (!saveRef.current) return;
      event.preventDefault();
      saveRef.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // The one path local storage cannot cover: a reload or a close discards the
  // React tree before anything can offer the buffer back, so the browser's own
  // prompt is what gives the user the chance to stay.
  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event) => {
      event.preventDefault();
      // Browsers ignore custom text now, but returnValue must be set for the
      // prompt to appear at all in some of them.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (loading && !data) return <Skeleton rows={6} />;
  if (error) return <ErrorBanner message={error} onRetry={reload} />;
  if (!data || !draft) return null;

  const set = (key) => (event) =>
    setDraft((current) => ({ ...current, [key]: event.target.value }));

  // Recomputed per render rather than memoised: it is two passes over a string
  // that is already being re-rendered into a textarea on the same keystroke.
  const stats = editorStats(draft.body_markdown);

  const splitList = (value) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

  /**
   * Write the fields to the server.
   *
   * Shared by the button, ⌘S and the auto-save timer, so the three cannot
   * disagree about what gets sent or what becomes of the recovery buffer.
   * Throws on failure — how loudly to say so is the caller's decision, and the
   * two callers want opposite things.
   */
  async function persist() {
    const sent = draft;
    const updated = await api.updateContent(data.id, {
      ...sent,
      keywords: splitList(sent.keywords),
      tags: splitList(sent.tags),
      // The API rejects a relative path and reads "" as "no image".
      cover_image_url: sent.cover_image_url.trim() || null,
    });
    setData(updated);
    // Take the server's copy — which may have normalised a keyword list or a
    // trimmed URL — only if the fields still hold what was sent. Anything typed
    // while the request was in flight is newer than the response, and is what
    // the next save should carry.
    setDraft((current) => (differs(current, sent) ? current : draftFrom(updated)));
    // The server now holds this text, so the recovery buffer has nothing left
    // to recover. Dropping it here rather than waiting for the mirror effect
    // keeps a failed save from clearing it.
    draftStore.clear(data.id);
    setRecovered(null);
  }

  async function save() {
    setSaving(true);
    try {
      await persist();
      setAutoSave({ status: "saved", at: Date.now(), error: null });
      toast.success("Saved");
    } catch (err) {
      setAutoSave((current) => ({ ...current, status: "error", error: err.message }));
      toast.error(err.message);
    } finally {
      setSaving(false);
    }
  }

  /**
   * The timer's save: silent when it works, inline when it does not.
   *
   * No toast either way. A toast on every success would narrate something the
   * user never asked for, and one on every failure would fire again on the next
   * keystroke — for an offline laptop that is a stack of them. A failure leaves
   * the text in the fields, in the recovery buffer, and behind the beforeunload
   * prompt, so nothing is lost while it says so quietly.
   */
  async function autosave() {
    setSaving(true);
    setAutoSave((current) => ({ ...current, status: "saving", error: null }));
    try {
      await persist();
      setAutoSave({ status: "saved", at: Date.now(), error: null });
    } catch (err) {
      setAutoSave((current) => ({ ...current, status: "error", error: err.message }));
    } finally {
      setSaving(false);
    }
  }

  // Live, so the shortcut always saves the text on screen. Only wired up when
  // there is something to write: a published piece is read-only, and a clean
  // draft has nothing to send.
  saveRef.current = !locked && dirty && !saving ? save : null;
  // The same conditions, plus one: nothing writes to the server underneath a
  // recovery offer. The buffer it is pointing at is unsaved work, and a save
  // triggered before the user has answered would resolve the question for them
  // by overwriting one of the two answers.
  autoSaveRef.current = !locked && dirty && !saving && !recovered ? autosave : null;

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
      {recovered && (
        <RecoveryBanner
          at={recovered.at}
          onRestore={() => {
            setDraft(recovered.draft);
            setRecovered(null);
          }}
          onDiscard={() => {
            draftStore.clear(data.id);
            setRecovered(null);
          }}
        />
      )}

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
            {/* Counted from the textarea, not from `data` — the server's
                figures describe the last save, and a length that only caught up
                when you saved would be wrong for exactly as long as you were
                writing. Same arithmetic as the server's, so the two agree the
                moment it does save. */}
            <span>{formatCount(stats.words)} words</span>
            <span>{formatReadLength(stats.minutes)}</span>
            <span>updated {formatWhen(data.updated_at)}</span>
            {data.generated_by_model && (
              <span title="Which model wrote it">{data.generated_by_model}</span>
            )}
            <Confidence value={data.confidence} />
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={data.status} />
          {!locked && <AutoSaveStatus state={autoSave} dirty={dirty} />}
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
          {/* Fed the live draft, not `data`: the point is to see the clip
              while you are still editing the title that causes it. */}
          <SocialPreview
            draft={draft}
            url={data.canonical_url || publishedUrl(data)}
            contentId={data.id}
          />
          <LinksPanel contentId={data.id} />
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

/** Where the piece actually went live, for the domain line on the link card.
 *
 *  Only a published publication counts: a scheduled one has an intended URL but
 *  no page yet, and claiming it on the card would be a promise about something
 *  that does not exist. Mirrors the same choice on the server side. */
function publishedUrl(content) {
  return (
    content.publications?.find(
      (publication) =>
        publication.status === "published" && publication.external_url,
    )?.external_url ?? ""
  );
}

function escapeText(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function SeoPanel({ issues, draft, onChange, locked }) {
  const [coverBroken, setCoverBroken] = useState(false);
  const cover = draft.cover_image_url.trim();

  return (
    <div className="panel space-y-4 p-5">
      <h2 className="text-sm font-semibold text-ink-900">SEO</h2>

      <div>
        <label className="label" htmlFor="c-cover">
          Cover image
        </label>
        <input
          id="c-cover"
          className="input font-mono text-[11px]"
          placeholder="https://cdn.example.com/cover.png"
          value={draft.cover_image_url}
          onChange={(event) => {
            setCoverBroken(false);
            onChange("cover_image_url")(event);
          }}
          disabled={locked}
        />
        {cover ? (
          coverBroken ? (
            <p className="mt-1.5 rounded bg-bad-wash px-2 py-1 text-xs text-bad">
              That URL did not load an image.
            </p>
          ) : (
            // A live thumbnail is the only honest check: it is the same fetch the
            // platforms will make.
            <img
              src={cover}
              alt=""
              className="mt-2 aspect-[16/9] w-full rounded-lg border border-line object-cover"
              onError={() => setCoverBroken(true)}
            />
          )
        ) : (
          <p className="mt-1 text-xs text-ink-400">
            Shown in the Dev.to, Medium and Hashnode feeds, and used for the
            LinkedIn and Twitter link preview. Must be an absolute URL.
          </p>
        )}
      </div>

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

/** HEAD-check the links in the body, on demand.
 *
 *  Not loaded with the editor: it makes real outbound requests, and a draft you
 *  are still writing has links you have not finished typing. Three verdicts, and
 *  the distinction is the point — only "broken" (a 404 or 410) is a fact. */
function LinksPanel({ contentId }) {
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.checkLinks(contentId));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const TONE = {
    broken: "bg-bad-wash text-bad",
    unknown: "bg-warn-wash text-warn",
    ok: "bg-good-wash text-good",
  };

  return (
    <div className="panel space-y-3 p-5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink-900">Links</h2>
        <button className="btn-quiet -mr-2.5" onClick={run} disabled={busy}>
          {busy ? "Checking…" : result ? "Re-check" : "Check"}
        </button>
      </div>

      {error && <p className="text-xs text-bad">{error}</p>}

      {!result && !error && (
        <p className="text-xs text-ink-400">
          Free models invent plausible documentation URLs. A dead link blocks
          publishing until you fix it or override.
        </p>
      )}

      {result && result.checked === 0 && (
        <p className="text-xs text-ink-400">No links in the body.</p>
      )}

      {result && result.checked > 0 && (
        <>
          <p className="text-xs text-ink-500">
            {result.broken_count === 0
              ? `${result.checked} link${result.checked === 1 ? "" : "s"}, none dead.`
              : `${result.broken_count} of ${result.checked} dead.`}
          </p>
          <ul className="space-y-1.5">
            {result.links
              // Dead first, then unresolved, then the ones that are fine.
              .slice()
              .sort(
                (a, b) =>
                  ["broken", "unknown", "ok"].indexOf(a.status) -
                  ["broken", "unknown", "ok"].indexOf(b.status),
              )
              .map((link) => (
                <li
                  key={link.url}
                  className={`rounded-lg px-3 py-2 text-xs ${TONE[link.status]}`}
                >
                  <span className="block break-all font-mono text-[10px]">
                    {link.url}
                  </span>
                  <span className="mt-0.5 block">
                    {link.status === "ok"
                      ? `OK (${link.http_status})`
                      : link.detail}
                  </span>
                </li>
              ))}
          </ul>
        </>
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
  // Set when the server refuses over a dead link. Shown here rather than as a
  // toast because the override that answers it lives in this form.
  const [deadLinks, setDeadLinks] = useState(null);
  const [allowBroken, setAllowBroken] = useState(false);

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
        allow_broken_links: allowBroken,
      });
      onDone();
    } catch (err) {
      if (err.message.includes("allow_broken_links")) {
        setDeadLinks(err.message);
      } else {
        onError(err.message);
      }
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

        {deadLinks && (
          <div className="space-y-2.5 rounded-lg bg-bad-wash px-3 py-2.5">
            <p className="break-words text-xs text-bad">
              {/* The server's list, minus the hint about the flag this
                  checkbox now provides. */}
              {deadLinks.split(". Fix them,")[0]}.
            </p>
            <label className="flex items-start gap-2.5 text-sm text-ink-700">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={allowBroken}
                onChange={(e) => setAllowBroken(e.target.checked)}
              />
              <span>
                Publish anyway
                <span className="mt-0.5 block text-xs text-ink-400">
                  Sometimes the page is about to exist.
                </span>
              </span>
            </label>
          </div>
        )}

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

/**
 * What became of the last automatic save.
 *
 * Deliberately narrow. The Save button already says whether anything is
 * outstanding and whether a write is in flight, so repeating that here would be
 * two controls narrating one fact. What the button cannot say is *when* the text
 * last reached the server, and that saving has quietly stopped working — the two
 * things worth knowing after ten minutes of typing without touching anything.
 */
function AutoSaveStatus({ state, dirty }) {
  if (state.status === "error") {
    return (
      <span
        role="status"
        className="font-mono text-[11px] text-bad"
        // The server's own words, on hover. Too long for the header, and too
        // specific to throw away — "disk full" and "session expired" want very
        // different responses.
        title={state.error}
      >
        Auto-save failed
      </span>
    );
  }
  if (!dirty && state.at) {
    return (
      <span role="status" className="font-mono text-[11px] text-ink-400">
        Saved {formatWhen(state.at)}
      </span>
    );
  }
  return null;
}

/**
 * "You have unsaved work from earlier" — offered, never applied on its own.
 *
 * The server's copy is what the user last committed to. Silently replacing it
 * with something a crashed tab left behind is the kind of help that loses work
 * rather than saving it, so both outcomes are a deliberate click, and the
 * timestamp is there because "which one is newer?" is the only question that
 * decides it.
 */
function RecoveryBanner({ at, onRestore, onDiscard }) {
  return (
    <div
      role="status"
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warn/25 bg-warn-wash px-4 py-3 text-sm text-warn"
    >
      <span className="min-w-0">
        Unsaved edits from {formatWhen(at)} are still in this browser. They were
        never saved to Herald.
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <button className="btn-quiet text-warn" onClick={onDiscard}>
          Discard
        </button>
        <button className="btn-ghost" onClick={onRestore}>
          Restore them
        </button>
      </span>
    </div>
  );
}

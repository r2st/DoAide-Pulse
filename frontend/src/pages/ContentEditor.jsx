import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { SectionBoundary } from "../components/ErrorBoundary";
import SocialPreview from "../components/SocialPreview";
import { Confidence, ErrorBanner, Skeleton, StatusBadge } from "../components/ui/Bits";
import Dialog from "../components/ui/Dialog";
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
import {
  OPERATIONS,
  TONES,
  normalizeSelection,
  selectionProblem,
  spliceSelection,
} from "../lib/passageEdit";

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
    keywords: content.keywords.join(", "),
    tags: content.tags.join(", "),
    // The one field `ContentDetail` declares nullable; the two lists above
    // carry `[]` as their schema default and cannot arrive absent.
    cover_image_url: content.cover_image_url ?? "",
    marketing_images: content.marketing_images ?? [],
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

  const { data, error, loading, reload, setData, setError } = useApi(
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

  // The passage the body textarea currently holds selected, as
  // `{ start, end, text }` — see lib/passageEdit. Kept in state rather than
  // read off the element when a button is pressed, because clicking the button
  // is what takes the focus away from the textarea.
  const bodyRef = useRef(null);
  const draftRef = useRef(null);
  const [selection, setSelection] = useState(null);
  // Which passage operation is in flight, or null. The operation's own name
  // rather than a boolean, so the button that was pressed can say so.
  const [aiBusy, setAiBusy] = useState(null);
  // The body as it was before the last passage edit. A controlled textarea
  // updated by `setDraft` leaves nothing on the browser's undo stack — the
  // change never went through the input — so one level of undo has to be kept
  // here, and it is the level that matters: the model just replaced a
  // paragraph the author had written.
  const [undoBody, setUndoBody] = useState(null);

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
    // Both describe offsets into a body that has just been replaced. Carrying
    // them across would leave an Undo button that restores another piece.
    setSelection(null);
    setUndoBody(null);
  }, [data]);

  // Put the caret back over the replacement after a passage edit.
  //
  // The splice happens through `setDraft`, so React re-renders the textarea
  // with new text and a collapsed caret; without this the author has to find
  // and reselect the paragraph to run a second operation on it, which is the
  // common case (shorten, then read it, then proofread).
  const restoreRange = useRef(null);
  useEffect(() => {
    const range = restoreRange.current;
    if (!range || !bodyRef.current) return;
    restoreRange.current = null;
    bodyRef.current.focus();
    bodyRef.current.setSelectionRange(range.start, range.end);
  }, [draft]);

  const dirty = useMemo(
    () => Boolean(saved && draft && differs(draft, saved)),
    [draft, saved],
  );

  // A published piece is a record of what went out. Editing it here would
  // change nothing on the platforms, so the fields are read-only.
  const locked = data?.status === "published";

  // Look for a recovery buffer once per piece, on arrival, and take the chance
  // to drop everyone else's stale ones while we are here.
  //
  // "Once per piece" is the ref, not the dependency list. `saved` is derived
  // from `data`, so this effect re-runs on every background refresh — the one
  // Approve fires, the one a publication retry fires — and by then the buffer
  // holds what the author is *currently typing*. It differs from the server's
  // copy because it is meant to; that is what being mid-edit means. So the
  // banner appeared, unprompted, offering to restore the text already on
  // screen.
  //
  // Which would be merely baffling if the banner did not also stop the writing
  // being saved: an unanswered offer suspends the mirror and disables the
  // auto-save, both deliberately, because nothing may resolve the question on
  // the author's behalf. So an offer raised about live text turns the
  // auto-save off underneath someone who is still typing and has no idea they
  // have been asked anything.
  //
  // The suite could not see it. `mockResolvedValue` hands back one object for
  // every call, and React bails out of a `setData` with an unchanged identity,
  // so a reload in a test never actually changed `data`.
  const recoveryCheckedId = useRef(null);
  useEffect(() => {
    draftStore.prune();
    if (!saved || locked || recoveryCheckedId.current === contentId) return;
    recoveryCheckedId.current = contentId;
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
  // `!data` as well as `error`: the banner takes the whole page only when there
  // is no page to take it over from.
  //
  // Every reload after the first is a background refresh the author did not
  // ask for — the one Approve fires, the one a publication retry fires, the one
  // the publish dialog fires on the way out — and any of them can fail on a
  // blip while the textarea holds minutes of unsaved writing. Returning the
  // banner unconditionally unmounted the editor and took that text off the
  // screen, which is the same failure the null-response guard in `persist`
  // exists to prevent, arriving by the other door. It is also exactly what the
  // SectionBoundary around each panel below refuses to allow: a background
  // thing going wrong costs you that background thing, not the draft.
  //
  // With data on hand the error is rendered inline instead, above the header,
  // with the same Retry.
  if (error && !data) return <ErrorBanner message={error} onRetry={reload} />;
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
    const updated = await api.updateContent(
      data.id,
      {
        ...sent,
        keywords: splitList(sent.keywords),
        tags: splitList(sent.tags),
        // The API rejects a relative path and reads "" as "no image".
        cover_image_url: sent.cover_image_url.trim() || null,
        marketing_images: sent.marketing_images || [],
      },
      // The version of the piece these fields were edited from. `data` is only
      // replaced by a save of our own — the load effect above runs once per
      // piece — so this is genuinely "what this editor last saw", and a save
      // that would land on top of somebody else's is refused rather than
      // performed. Everything a failed save protects is already in place: the
      // text stays in the fields, the recovery buffer is untouched, and the
      // status line says what happened, so the author's paragraph survives the
      // 412 and can be reapplied after a reload.
      data.version,
    );
    // A 2xx is not proof there is a piece in the reply. `lib/api` reads a body
    // it cannot parse as "no structured body" and answers `null` — which is the
    // right call there, because a gateway timeout page and a proxy's error HTML
    // are not Pulse talking. Here it was committed into state regardless, and
    // the next render read `draftFrom(null).title` and threw: the editor
    // disappeared into its error boundary, taking the author's unsaved text off
    // the screen at the exact moment the save had failed to store it.
    //
    // Refusing it puts the failure on the path built for one. The text stays in
    // the fields, the recovery buffer is untouched, and the status line says the
    // save did not land — which is all true, and none of it was before.
    if (!updated || typeof updated !== "object") {
      throw new Error("The server did not return the saved piece — not saved.");
    }
    setData(updated);
    // The server has just answered with the current piece, so whatever a
    // failed background reload is still complaining about above is no longer
    // true. Leaving it there would sit a stale "Service Unavailable" over an
    // editor that has demonstrably just reached the server.
    setError(null);
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
  // The text on screen, readable from an async handler that has awaited a
  // round trip. Same reason as the two refs above: `draft` in a closure is the
  // text as it was when the request went out, and splicing into that would
  // silently discard anything typed while the model was thinking.
  draftRef.current = draft;

  /** Record what is selected in the body, so a toolbar button can act on it. */
  function rememberSelection(event) {
    const field = event.target;
    setSelection(
      normalizeSelection(field.value, field.selectionStart, field.selectionEnd),
    );
  }

  /**
   * Send the selected passage to the model and splice back what comes out.
   *
   * Saves first when there is anything to save. The server checks the passage
   * against the *stored* body and refuses one it cannot find — which is what
   * stops a stale editor pasting an edit over the wrong paragraph — so an
   * unsaved draft would otherwise turn every one of these into a 422 telling
   * the author to press Save and try again. Doing it for them is the whole
   * difference between a feature and an error message.
   */
  async function runPassageEdit(operation, tone) {
    if (aiBusy || locked || selectionProblem(selection)) return;
    // Nothing writes to the server underneath a recovery offer — the save
    // below would answer the banner's question by overwriting one of the two
    // answers before the user picked either. Same rule as the auto-save, said
    // out loud here because the user pressed a button and deserves a reason.
    if (recovered) {
      toast.error("Restore or discard the unsaved edits above first.");
      return;
    }
    const target = selection;
    setAiBusy(operation);
    try {
      if (dirty) await persist();
      const result = await api.editPassage(data.id, {
        selection: target.text,
        operation,
        // "" is the Project voice option: send nothing and let the server use
        // the project's own tone.
        tone: tone || null,
      });

      const before = draftRef.current.body_markdown;
      const next = spliceSelection(before, target, result.replacement);
      if (!next) {
        toast.error(
          "That passage has changed since you selected it — nothing was replaced.",
        );
        return;
      }
      setUndoBody(before);
      setDraft((current) => ({ ...current, body_markdown: next.body }));
      setSelection(next.selection);
      restoreRange.current = next.selection;
    } catch (err) {
      toast.error(err.message);
    } finally {
      setAiBusy(null);
    }
  }

  /** Put the body back as it was before the last passage edit. */
  function undoPassageEdit() {
    if (undoBody === null) return;
    setDraft((current) => ({ ...current, body_markdown: undoBody }));
    setUndoBody(null);
    // The offsets described the replacement, which is no longer there.
    setSelection(null);
  }

  async function approve() {
    try {
      // The response says what approving actually did. On a project set to
      // publish on its own, approving queues it — telling the user it is
      // "ready to publish" would be describing a button they no longer need to
      // press, on a piece that is already on its way out.
      const approved = await api.approveContent(data.id);
      reload();
      const queued = approved?.publications ?? [];
      toast.success(
        queued.length
          ? `Approved — publishing to ${queued.map((p) => p.platform).join(", ")}`
          : "Approved — ready to publish",
      );
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
      {/* A refresh that failed while the piece is on screen. Said here rather
          than in place of the editor — see the guard above. */}
      {error && <ErrorBanner message={error} onRetry={reload} />}

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
                <PassageTools
                  selection={selection}
                  dirty={dirty}
                  busy={aiBusy}
                  undoable={undoBody !== null}
                  disabled={locked}
                  onRun={runPassageEdit}
                  onUndo={undoPassageEdit}
                />
                <textarea
                  id="c-body"
                  ref={bodyRef}
                  className="input min-h-[520px] resize-y font-mono text-[13px] leading-relaxed"
                  value={draft.body_markdown}
                  onChange={set("body_markdown")}
                  onSelect={rememberSelection}
                  onBlur={rememberSelection}
                  disabled={locked}
                  spellCheck
                />
              </div>
            </div>
          ) : (
            <article
              className="prose-pulse panel px-7 py-6"
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

        {/* Every panel in here loads or derives something of its own, and the
            column sits beside a textarea that may hold minutes of unsaved
            writing. A boundary per panel is the difference between losing a
            link check and losing the draft. */}
        <aside className="space-y-4">
          <SectionBoundary name="editor:seo">
            <SeoPanel
              issues={data.seo_issues}
              draft={draft}
              onChange={set}
              setDraft={setDraft}
              locked={locked}
            />
          </SectionBoundary>
          {/* Fed the live draft, not `data`: the point is to see the clip
              while you are still editing the title that causes it. */}
          <SectionBoundary name="editor:social">
            <SocialPreview
              draft={draft}
              url={data.canonical_url || publishedUrl(data)}
              contentId={data.id}
            />
          </SectionBoundary>
          <SectionBoundary name="editor:links">
            <LinksPanel contentId={data.id} />
          </SectionBoundary>
          <SectionBoundary name="editor:preview-links">
            <PreviewLinksPanel contentId={data.id} />
          </SectionBoundary>
          <SectionBoundary name="editor:publications">
            <PublicationsPanel content={data} onChanged={reload} />
          </SectionBoundary>
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

/**
 * Run one model operation over the selected passage.
 *
 * Above the textarea rather than floating over the selection: a popover has to
 * be positioned against a caret inside a textarea, which cannot be measured
 * without cloning the whole field, and it covers the very text it is about.
 * The strip costs one line and is always in the same place.
 *
 * The buttons stay visible while they are unusable, with the reason spelled
 * out underneath — a toolbar that appears only once a valid selection exists
 * is a feature nobody discovers, because discovering it requires having
 * already done the thing it is waiting for.
 */
function PassageTools({ selection, dirty, busy, undoable, disabled, onRun, onUndo }) {
  const [tone, setTone] = useState("");

  // A published piece is a record of what went out; there is nothing here to
  // edit, so there is nothing to say about editing it.
  if (disabled) return null;

  const problem = selectionProblem(selection);
  const blocked = Boolean(problem) || Boolean(busy);

  return (
    <div className="mb-2 rounded-lg border border-line bg-canvas px-3 py-2">
      <div className="flex flex-wrap items-center gap-1">
        <span className="eyebrow mr-1.5">Edit passage</span>
        {OPERATIONS.map((operation) => (
          <button
            key={operation.value}
            type="button"
            className="btn-quiet"
            title={operation.title}
            disabled={blocked}
            onClick={() => onRun(operation.value, tone)}
          >
            {busy === operation.value ? "Working…" : operation.label}
          </button>
        ))}
        <label className="sr-only" htmlFor="c-tone">
          Tone
        </label>
        <select
          id="c-tone"
          className="rounded-md border border-line bg-paper px-1.5 py-1 text-xs text-ink-500"
          value={tone}
          onChange={(event) => setTone(event.target.value)}
          disabled={Boolean(busy)}
        >
          {TONES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        {undoable && (
          <button
            type="button"
            className="btn-quiet ml-auto text-ink-900"
            onClick={onUndo}
            disabled={Boolean(busy)}
          >
            Undo edit
          </button>
        )}
      </div>
      <p className="mt-1 text-xs text-ink-400">
        {problem ??
          `${formatCount(selection.text.length)} characters selected` +
            `${dirty ? " — the draft is saved first" : ""}. The replacement goes ` +
            `into the editor, where Undo puts it back.`}
      </p>
    </div>
  );
}

function SeoPanel({ issues, draft, onChange, setDraft, locked }) {
  const [coverBroken, setCoverBroken] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [galleryUploading, setGalleryUploading] = useState(false);
  const [aiRanFor, setAiRanFor] = useState("");
  const cover = draft.cover_image_url.trim();
  const fileInputRef = useRef(null);
  const galleryInputRef = useRef(null);
  const toast = useToast();

  const seoFieldsEmpty =
    !draft.meta_description && !draft.keywords && !draft.tags && !draft.excerpt;
  const hasContent = (draft.title?.length > 10) || (draft.body_markdown?.length > 50);
  const contentKey = `${draft.title}::${draft.body_markdown?.slice(0, 200)}`;

  useEffect(() => {
    if (locked || !hasContent || !seoFieldsEmpty || aiLoading) return;
    if (aiRanFor === contentKey) return;

    const timer = setTimeout(async () => {
      setAiLoading(true);
      try {
        const result = await api.generateFields({
          title: draft.title,
          body_markdown: draft.body_markdown,
          excerpt: draft.excerpt,
          fields: ["meta_description", "keywords", "tags", "excerpt"],
        });
        setDraft((d) => {
          if (d.meta_description || d.keywords || d.tags || d.excerpt) return d;
          return {
            ...d,
            ...(result.meta_description ? { meta_description: result.meta_description } : {}),
            ...(result.keywords ? { keywords: result.keywords.join(", ") } : {}),
            ...(result.tags ? { tags: result.tags.join(", ") } : {}),
            ...(result.excerpt ? { excerpt: result.excerpt } : {}),
          };
        });
        setAiRanFor(contentKey);
      } catch {
        // silent — auto-generation is best-effort
      } finally {
        setAiLoading(false);
      }
    }, 2000);
    return () => clearTimeout(timer);
  }, [contentKey, locked, hasContent, seoFieldsEmpty, aiLoading, aiRanFor]);

  async function handleCoverUpload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const result = await api.uploadImage(file);
      setDraft((d) => ({ ...d, cover_image_url: result.url }));
      setCoverBroken(false);
    } catch (err) {
      toast.error(`Upload failed: ${err.message}`);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleGalleryUpload(event) {
    const files = Array.from(event.target.files || []);
    if (!files.length) return;
    setGalleryUploading(true);
    try {
      const urls = [];
      for (const file of files) {
        const result = await api.uploadImage(file);
        urls.push(result.url);
      }
      setDraft((d) => ({
        ...d,
        marketing_images: [...(d.marketing_images || []), ...urls],
      }));
    } catch (err) {
      toast.error(`Upload failed: ${err.message}`);
    } finally {
      setGalleryUploading(false);
      if (galleryInputRef.current) galleryInputRef.current.value = "";
    }
  }

  function removeGalleryImage(index) {
    setDraft((d) => ({
      ...d,
      marketing_images: d.marketing_images.filter((_, i) => i !== index),
    }));
  }

  return (
    <div className="panel space-y-4 p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ink-900">SEO</h2>
        {aiLoading && (
          <span className="inline-flex items-center gap-1.5 text-[11px] text-accent">
            <span className="inline-block h-3 w-3 animate-spin rounded-full border border-accent/30 border-t-accent" />
            Auto-filling with AI…
          </span>
        )}
      </div>

      <div>
        <label className="label" htmlFor="c-cover">
          Cover image
        </label>
        <div className="flex gap-2">
          <input
            id="c-cover"
            className="input flex-1 font-mono text-[11px]"
            placeholder="https://cdn.example.com/cover.png"
            value={draft.cover_image_url}
            onChange={(event) => {
              setCoverBroken(false);
              onChange("cover_image_url")(event);
            }}
            disabled={locked}
          />
          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            className="hidden"
            onChange={handleCoverUpload}
          />
          <button
            type="button"
            className="shrink-0 rounded border border-line bg-surface px-2.5 py-1.5 text-xs font-medium text-ink-600 hover:bg-ink-50 disabled:opacity-50"
            onClick={() => fileInputRef.current?.click()}
            disabled={locked || uploading}
          >
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
        {cover ? (
          coverBroken ? (
            <p className="mt-1.5 rounded bg-bad-wash px-2 py-1 text-xs text-bad">
              That URL did not load an image.
            </p>
          ) : (
            <img
              src={cover}
              alt="Newsletter cover image preview"
              loading="lazy"
              className="mt-2 aspect-[16/9] w-full rounded-lg border border-line object-cover"
              onError={() => setCoverBroken(true)}
            />
          )
        ) : (
          <p className="mt-1 text-xs text-ink-400">
            Shown in the Dev.to, Medium and Hashnode feeds, and used for the
            LinkedIn and Twitter link preview.
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
          className={`input resize-y text-[13px] ${aiLoading && !draft.meta_description ? "animate-pulse bg-ink-50" : ""}`}
          placeholder={aiLoading ? "Generating…" : ""}
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
          className={`input text-[13px] ${aiLoading && !draft.keywords ? "animate-pulse bg-ink-50" : ""}`}
          placeholder={aiLoading ? "Generating…" : ""}
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
          className={`input text-[13px] ${aiLoading && !draft.tags ? "animate-pulse bg-ink-50" : ""}`}
          placeholder={aiLoading ? "Generating…" : ""}
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
          className={`input resize-y text-[13px] ${aiLoading && !draft.excerpt ? "animate-pulse bg-ink-50" : ""}`}
          placeholder={aiLoading ? "Generating…" : ""}
          value={draft.excerpt}
          onChange={onChange("excerpt")}
          disabled={locked}
        />
      </div>

      <div>
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold text-ink-700">Marketing images</h3>
          <input
            ref={galleryInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            multiple
            className="hidden"
            onChange={handleGalleryUpload}
          />
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded border border-line bg-surface px-2 py-1 text-[11px] font-medium text-ink-600 hover:bg-ink-50 disabled:opacity-50"
            onClick={() => galleryInputRef.current?.click()}
            disabled={locked || galleryUploading}
          >
            {galleryUploading ? "Uploading…" : "+ Add images"}
          </button>
        </div>
        {(draft.marketing_images?.length > 0) ? (
          <div className="mt-2 grid grid-cols-3 gap-2">
            {draft.marketing_images.map((url, index) => (
              <div key={index} className="group relative">
                <img
                  src={url}
                  alt={`Marketing image ${index + 1}`}
                  className="aspect-square w-full rounded-lg border border-line object-cover"
                />
                {!locked && (
                  <button
                    type="button"
                    className="absolute -right-1 -top-1 hidden rounded-full bg-bad p-0.5 text-white shadow group-hover:block"
                    onClick={() => removeGalleryImage(index)}
                    title="Remove image"
                  >
                    <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 3l6 6M9 3l-6 6"/></svg>
                  </button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-xs text-ink-400">
            Add images for social posts and marketing material.
          </p>
        )}
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

/**
 * Shareable, read-only links to this draft — a second pair of eyes without
 * giving out an account. The URL only ever appears once, in the response to
 * creating it: a link in the list below is shown as "issued" and "revoke",
 * never re-copyable, because the server only ever stored its hash.
 */
function PreviewLinksPanel({ contentId }) {
  const toast = useToast();
  const { data: links, loading, reload } = useApi(
    () => api.listPreviewLinks(contentId),
    [contentId],
  );
  const [creating, setCreating] = useState(false);
  const [justCreated, setJustCreated] = useState(null);
  const [busyId, setBusyId] = useState(null);

  async function create() {
    setCreating(true);
    try {
      const link = await api.createPreviewLink(contentId);
      setJustCreated(link);
      await reload();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setCreating(false);
    }
  }

  async function copy(url) {
    try {
      await navigator.clipboard.writeText(url);
      toast.success("Link copied");
    } catch {
      toast.error("Could not copy — select the link and copy it manually.");
    }
  }

  async function revoke(linkId) {
    setBusyId(linkId);
    try {
      await api.revokePreviewLink(contentId, linkId);
      if (justCreated?.id === linkId) setJustCreated(null);
      await reload();
    } catch (err) {
      toast.error(err.message);
    } finally {
      setBusyId(null);
    }
  }

  // The just-created link already has its own callout with the full URL;
  // listing it again below would say the same thing twice.
  const live = (links ?? []).filter(
    (link) => !link.revoked_at && link.id !== justCreated?.id,
  );

  return (
    <div className="panel space-y-3 p-5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink-900">Share preview</h2>
        <button className="btn-quiet -mr-2.5" onClick={create} disabled={creating}>
          {creating ? "Creating…" : "New link"}
        </button>
      </div>

      {!loading && live.length === 0 && !justCreated && (
        <p className="text-xs text-ink-400">
          Read-only link for external reviewers.
        </p>
      )}

      {justCreated && (
        <div className="rounded-lg border border-line bg-canvas px-3 py-2">
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 break-all font-mono text-[11px] text-ink-700">
              {justCreated.url}
            </code>
            <button className="btn-quiet shrink-0" onClick={() => copy(justCreated.url)}>
              Copy
            </button>
            {/* The list below deliberately excludes this link, so without a
                revoke here a URL created by mistake — pasted into the wrong
                chat, say — cannot be taken back until the page is reloaded.
                That is the one moment the user most wants it gone. */}
            <button
              className="btn-quiet shrink-0 text-bad"
              onClick={() => revoke(justCreated.id)}
              disabled={busyId === justCreated.id}
            >
              Revoke
            </button>
          </div>
          <p className="mt-1 text-xs text-ink-400">
            Shown once — only a hash is stored after this.
          </p>
        </div>
      )}

      {live.length > 0 && (
        <ul className="space-y-1.5">
          {live.map((link) => (
            <li
              key={link.id}
              className="flex items-center justify-between gap-2 rounded-lg bg-canvas px-3 py-2 text-xs text-ink-500"
            >
              <span>
                expires {formatWhen(link.expires_at)}
                {" · "}
                {link.view_count === 0
                  ? "never opened"
                  : `viewed ${link.view_count}× · last ${formatWhen(link.last_viewed_at)}`}
              </span>
              <button
                className="btn-quiet shrink-0 text-bad"
                onClick={() => revoke(link.id)}
                disabled={busyId === link.id}
              >
                Revoke
              </button>
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
    <Dialog
      label="Publish"
      onClose={onClose}
      closable={!busy}
      onSubmit={submit}
    >
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
    </Dialog>
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
        Unsaved edits from {formatWhen(at)} — browser-only, not saved to server.
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

import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { audit, previews } from "../lib/socialCards";

/**
 * What the post looks like when someone pastes its link into a feed.
 *
 * Renders the card rather than describing it. A character counter tells you a
 * title is 94 characters; it does not tell you the last four words are the ones
 * that carry the point, and that they are gone. Seeing the clip is the feature.
 *
 * Everything is computed locally from the *unsaved* draft (see lib/socialCards)
 * so it tracks what you are typing. The meta tags that actually ship come from
 * the API, on demand, because tags are something you paste once.
 */
export default function SocialPreview({ draft, url, contentId }) {
  const [network, setNetwork] = useState("x");

  // The whole panel is a pure function of the draft, so one memo covers it and
  // typing in the body does not re-clip four titles per keystroke.
  const { cards, issues } = useMemo(() => {
    const withUrl = { ...draft, url };
    return { cards: previews(withUrl), issues: audit(withUrl) };
  }, [draft, url]);

  const active = cards.find((card) => card.network === network) ?? cards[0];

  return (
    <div className="panel space-y-4 p-5">
      <div>
        <h2 className="text-sm font-semibold text-ink-900">Link preview</h2>
        <p className="mt-0.5 text-xs text-ink-400">
          How the card unfurls when the link is shared.
        </p>
      </div>

      <div
        className="flex flex-wrap gap-1"
        role="tablist"
        aria-label="Preview network"
      >
        {cards.map((card) => (
          <button
            key={card.network}
            role="tab"
            aria-selected={card.network === network}
            onClick={() => setNetwork(card.network)}
            className={[
              "rounded-full px-2.5 py-1 text-xs transition-colors",
              card.network === network
                ? "bg-ink-900 text-paper"
                : "text-ink-500 hover:bg-canvas hover:text-ink-900",
            ].join(" ")}
          >
            {card.label}
          </button>
        ))}
      </div>

      <Card card={active} />

      {(active.titleClipped || active.descriptionClipped) && (
        <p className="text-xs text-ink-400">
          {/* Named explicitly: the ellipsis in the card above is easy to read
              as a rendering artefact rather than as lost words. */}
          {active.titleClipped && active.descriptionClipped
            ? "Title and description are both cut here."
            : active.titleClipped
              ? "The title is cut here."
              : "The description is cut here."}
        </p>
      )}

      {issues.length === 0 ? (
        <p className="rounded-lg bg-good-wash px-3 py-2 text-xs text-good">
          The card is complete.
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

      {contentId != null && <MetaTags contentId={contentId} />}
    </div>
  );
}

/**
 * The tags themselves, fetched on demand.
 *
 * Not loaded with the editor and not computed locally: these reflect what is
 * *saved*, and handing someone tags built from an unsaved draft is how you get
 * a page whose head disagrees with its content. The button is the honest
 * boundary — you ask for them when you are ready to paste them.
 *
 * Git-published blogs need none of this; the publisher writes the equivalent
 * front matter itself. This is for a site Pulse does not deploy.
 */
function MetaTags({ contentId }) {
  const [tags, setTags] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      const result = await api.socialCards(contentId);
      setTags(result.meta_html);
    } catch (err) {
      setError(`Could not load social tags: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(tags);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be refused outright; the textarea below is
      // selectable, so there is still a way through.
      setError("Could not copy — select the tags and copy them manually.");
    }
  }

  return (
    <div className="space-y-2 border-t border-line pt-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-ink-900">Meta tags</h3>
        <button className="btn-quiet -mr-2.5" onClick={tags ? copy : load} disabled={busy}>
          {busy ? "Loading…" : copied ? "Copied" : tags ? "Copy" : "Show"}
        </button>
      </div>

      {error && <p className="text-xs text-bad">{error}</p>}

      {!tags && !error && (
        <p className="text-xs text-ink-400">
          For external sites. Git destinations get front matter automatically.
        </p>
      )}

      {tags && (
        <textarea
          readOnly
          rows={6}
          className="input resize-y font-mono text-[10px] leading-relaxed"
          value={tags}
          onFocus={(event) => event.target.select()}
          aria-label="Open Graph and Twitter Card meta tags"
        />
      )}
    </div>
  );
}

/**
 * One rendered card.
 *
 * Two layouts, because the networks have two: a wide image above the text when
 * there is a usable cover, and a small square placeholder beside it when there
 * is not. Which one you get is the single biggest visual difference between a
 * shared link that looks deliberate and one that looks broken, so the panel
 * shows the real consequence instead of a note about it.
 */
function Card({ card }) {
  const [broken, setBroken] = useState(false);
  const showImage = card.imageUrl && !broken;

  if (!showImage) {
    return (
      <div className="flex items-stretch gap-3 overflow-hidden rounded-xl border border-line-strong bg-paper">
        <div className="flex w-20 shrink-0 items-center justify-center bg-canvas text-ink-400">
          {/* Stands in for the grey box the network draws. Not an error state:
              this is genuinely what the card will look like. */}
          <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <circle cx="8.5" cy="9.5" r="1.5" />
            <path d="M21 16l-5-5-4 4-2-2-7 7" />
          </svg>
        </div>
        <div className="min-w-0 flex-1 py-2.5 pr-3">
          <CardText card={card} />
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-line-strong bg-paper">
      {/* 1.91:1 — the ratio every network crops to. Showing it here means a
          cover with the wrong aspect is visibly cropped in the panel, which is
          exactly what will happen in the feed. */}
      <img
        src={card.imageUrl}
        alt="Social media preview card"
        loading="lazy"
        className="aspect-[1.91/1] w-full border-b border-line object-cover"
        onError={() => setBroken(true)}
      />
      <div className="px-3 py-2.5">
        <CardText card={card} />
      </div>
    </div>
  );
}

function CardText({ card }) {
  return (
    <>
      {card.domain && (
        <p className="truncate font-mono text-[10px] uppercase tracking-wide text-ink-400">
          {card.domain}
        </p>
      )}
      <p className="mt-0.5 break-words text-sm font-semibold leading-snug text-ink-900">
        {card.title || (
          <span className="font-normal text-ink-400">
            Untitled — the card will show the URL
          </span>
        )}
      </p>
      {card.description && (
        <p className="mt-1 break-words text-xs leading-snug text-ink-500">
          {card.description}
        </p>
      )}
    </>
  );
}

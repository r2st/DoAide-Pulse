import { useState } from "react";

export default function ShareButtons({ url, title = "", text = "Check this out!", variant = "default" }) {
  const [copied, setCopied] = useState(false);
  const shareUrl =
    url || (typeof window !== "undefined" ? window.location.href : "");
  const encoded = encodeURIComponent(shareUrl);

  const whatsappText = title
    ? encodeURIComponent(`\u{1F4F0} ${title} — Read now → ${shareUrl} | Published with DoAide Pulse`)
    : encodeURIComponent(`${text} ${shareUrl}`);

  const tweetText = title
    ? encodeURIComponent(title)
    : encodeURIComponent(text);

  const linkedInUrl = `https://www.linkedin.com/sharing/share-offsite/?url=${encoded}`;

  function copyLink() {
    navigator.clipboard.writeText(shareUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  const btnClass = variant === "prominent"
    ? "inline-flex items-center gap-1.5 rounded-lg border border-line bg-paper px-4 py-2 text-sm font-medium text-ink-700 transition hover:border-brand-500 hover:text-brand-500"
    : "btn-ghost !py-1.5 !px-3 !text-xs";

  return (
    <div className="flex flex-wrap gap-3" aria-label="Share this article">
      <a
        href={`https://wa.me/?text=${whatsappText}`}
        target="_blank"
        rel="noopener noreferrer"
        className={btnClass}
        data-testid="share-whatsapp"
      >
        WhatsApp
      </a>
      <a
        href={`https://twitter.com/intent/tweet?text=${tweetText}&url=${encoded}`}
        target="_blank"
        rel="noopener noreferrer"
        className={btnClass}
        data-testid="share-twitter"
      >
        Twitter / X
      </a>
      <a
        href={linkedInUrl}
        target="_blank"
        rel="noopener noreferrer"
        className={btnClass}
        data-testid="share-linkedin"
      >
        LinkedIn
      </a>
      <button onClick={copyLink} className={btnClass} data-testid="share-copy">
        {copied ? "Copied!" : "Copy link"}
      </button>
    </div>
  );
}

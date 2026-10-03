import { useState } from "react";

export default function ShareButtons({ url, text = "Check this out!" }) {
  const [copied, setCopied] = useState(false);
  const shareUrl =
    url || (typeof window !== "undefined" ? window.location.href : "");
  const encoded = encodeURIComponent(shareUrl);
  const encodedText = encodeURIComponent(text);

  function copyLink() {
    navigator.clipboard.writeText(shareUrl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="flex flex-wrap gap-3" aria-label="Share">
      <a
        href={`https://wa.me/?text=${encodedText}%20${encoded}`}
        target="_blank"
        rel="noopener noreferrer"
        className="btn-ghost !py-1.5 !px-3 !text-xs"
      >
        WhatsApp
      </a>
      <a
        href={`https://twitter.com/intent/tweet?text=${encodedText}&url=${encoded}`}
        target="_blank"
        rel="noopener noreferrer"
        className="btn-ghost !py-1.5 !px-3 !text-xs"
      >
        Twitter / X
      </a>
      <button onClick={copyLink} className="btn-ghost !py-1.5 !px-3 !text-xs">
        {copied ? "Copied!" : "Copy link"}
      </button>
    </div>
  );
}

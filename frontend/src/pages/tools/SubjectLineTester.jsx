import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ShareButtons from "../../components/ShareButtons";

const POWER_WORDS = [
  "exclusive",
  "limited",
  "free",
  "new",
  "breaking",
  "urgent",
  "important",
  "alert",
  "secret",
  "proven",
  "insider",
  "instant",
];

function scoreSubjectLine(text) {
  if (!text.trim()) return { score: 0, tips: ["Type a subject line to get started."] };
  let score = 50;
  const tips = [];

  const len = text.length;
  if (len >= 30 && len <= 50) {
    score += 20;
  } else if (len >= 20 && len <= 60) {
    score += 10;
    tips.push("Good length, but 30-50 characters is the sweet spot for mobile.");
  } else if (len < 20) {
    score -= 10;
    tips.push("Too short. Aim for 30-50 characters to give readers enough context.");
  } else {
    score -= 10;
    tips.push("Too long. Subject lines over 60 characters get cut off on mobile.");
  }

  const lower = text.toLowerCase();
  let powerCount = 0;
  for (const w of POWER_WORDS) {
    if (lower.includes(w)) powerCount++;
  }
  score += Math.min(powerCount * 5, 15);
  if (powerCount > 0) tips.push(`Found ${powerCount} power word(s) — nice!`);
  else tips.push("Add a power word like 'free', 'exclusive', or 'proven' to boost urgency.");

  if (/\[.*?\]|\{.*?\}/.test(text)) {
    score += 10;
    tips.push("Personalization tokens detected — great for open rates.");
  } else {
    tips.push("Consider adding personalization like [Name] for a personal touch.");
  }

  const emojiMatch = text.match(
    /[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]/gu
  );
  const emojiCount = emojiMatch ? emojiMatch.length : 0;
  if (emojiCount === 1) {
    score += 5;
  } else if (emojiCount > 3) {
    score -= 5;
    tips.push("Too many emojis — one is effective, more than three looks spammy.");
  }

  const capsWords = text.split(/\s+/).filter((w) => w.length > 1 && w === w.toUpperCase() && /[A-Z]/.test(w));
  if (capsWords.length > 0) {
    score -= capsWords.length * 10;
    tips.push("Avoid ALL CAPS words — they trigger spam filters and feel aggressive.");
  }

  if (/\d/.test(text)) {
    score += 5;
    tips.push("Numbers add specificity — readers love concrete details.");
  }

  if (text.trim().endsWith("?")) {
    score += 5;
    tips.push("Questions engage readers by prompting them to think.");
  }

  return { score: Math.max(0, Math.min(100, score)), tips };
}

function scoreColor(s) {
  if (s >= 80) return "text-good";
  if (s >= 50) return "text-warn";
  return "text-bad";
}

function scoreLabel(s) {
  if (s >= 80) return "Excellent";
  if (s >= 50) return "Decent";
  return "Needs Work";
}

export default function SubjectLineTester() {
  const [text, setText] = useState("");
  const { score, tips } = scoreSubjectLine(text);

  useEffect(() => {
    document.title = "Subject Line Tester | DoAide Pulse";
  }, []);

  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <h1 className="page-title mb-2">Subject Line Tester</h1>
      <p className="mb-6 text-ink-500">
        Score your email subject line on open-rate predictors. Free, no signup.
      </p>

      <div className="panel p-5 space-y-4">
        <div>
          <label htmlFor="subject" className="label">
            Your subject line
          </label>
          <input
            id="subject"
            className="input"
            placeholder="e.g. 5 Growth Hacks You Haven't Tried"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>

        {text.trim() && (
          <div className="flex items-center gap-4">
            <div className={`text-4xl font-bold ${scoreColor(score)}`} data-testid="score">
              {score}
            </div>
            <div>
              <div className={`font-semibold ${scoreColor(score)}`} data-testid="label">
                {scoreLabel(score)}
              </div>
              <div className="text-xs text-ink-400">out of 100</div>
            </div>
          </div>
        )}
      </div>

      {tips.length > 0 && text.trim() && (
        <div className="mt-6 space-y-2" data-testid="tips">
          <h2 className="text-sm font-semibold text-ink-900">Tips</h2>
          <ul className="space-y-1 text-sm text-ink-700">
            {tips.map((t, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-brand-500">•</span> {t}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="panel mt-10 p-5 text-center">
        <p className="mb-3 text-ink-900">
          Ready to build newsletters with AI-powered subject lines?
        </p>
        <Link to="/" className="btn-primary">
          Try DoAide Pulse
        </Link>
      </div>

      <div className="mt-6">
        <ShareButtons text="Score your email subject lines for free — Subject Line Tester by DoAide Pulse" />
      </div>
    </div>
  );
}

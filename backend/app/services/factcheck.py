"""Product and feature names a piece claims that nothing about the project supports.

The third kind of corruption, and the one no character can reveal.
:mod:`app.services.ai` catches the sampler slipping a *character* from somewhere
else into a word — ``the front蛅 end``, ``démontrated latency``. This catches it
slipping a whole *noun* in, spelled in plain ASCII, in a sentence that parses::

    ## Integration with Wird
    The outreach module reads from the Teppil inbox …

"Wird" and "Teppil" are not products. They are not typos of products. They are
not in the project's brief, its stack, its keywords, its commits or its README —
they are two nouns a free-tier model needed in order to finish a sentence, and
it wrote them with the same confidence it wrote everything else. Every existing
gate passed the piece: the JSON parsed, the body was long enough, the SEO score
was fine, no link was dead, and every character in it is ASCII.

**The discriminator is grounding, not spelling.** A product name is a fact about
the world that Pulse either has on file or does not. So the gate builds a
vocabulary out of everything Pulse actually knows about the project — its
brief, its keywords and stack, the commits or feed entries that prompted the
piece, and (only when something looks unsupported, see
:func:`app.services.content_pipeline.generate_and_route`) the repo's README —
and reports the name-shaped tokens in the copy that appear in none of it.

**What counts as name-shaped** is deliberately narrow, because "capitalized word
Pulse has not seen before" is most of the English language. Three arms, and a
claim carries the name of the one that fired so a reviewer knows what they are
looking at:

``fused``
    An internal capital after a lowercase letter, in a token that itself
    *starts* lowercase — ``builtExamples``, ``aboutSPECIFIC``, ``forEditor``.
    Almost nothing in English is spelled this way, so the shape is two words
    welded together by a sampler that dropped the space.

    The lowercase head is what separates the splice from the shape's other
    inhabitant, which is most of the technology industry's brand names:
    ``SmartRecruiters``, ``ClearTax``, ``MagicBricks``, ``PreToolUse``. Those
    are proper nouns, so they are capitalized, and a project is not expected to
    have every third-party name it mentions in passing on file — an early
    version reported six such mentions across the production corpus against
    four real splices, all four of which began with an English word
    mid-sentence and therefore lowercase. A capitalized invention is not lost
    with them: ``framed`` reads it the moment the copy claims the product ships
    it, which is the case worth holding a piece back for.

``framed``
    A capitalized word in a phrase that asserts it is *part of the product*:
    "integration with X", "the X inbox", "X integration", "powered by X". The
    frame is what separates "the Teppil inbox" from every other capitalized word
    in a paragraph — a name Pulse has never heard of is unremarkable until the
    copy claims the product ships it.

``hyphen``
    A capitalized word joined to a short lowercase fragment that is not a word
    English hyphenates onto things — ``Integrate‑mar``. The narrowest and the
    noisiest arm; ``_HYPHEN_TAILS`` and a four-character ceiling are what keep
    "audit-ready" and "third-party" out of it.

**Biased toward review, like the splice gates.** A project that mentions a
competitor once, in a frame, will be held for a glance. That is one click,
against a post going out under the user's byline announcing an integration that
does not exist — which is what the unbiased version of this did twice, in copy
that is still sitting in the review queue because a human happened to read it.

Reported, never repaired. "Wird" cannot be corrected without knowing what the
model meant, and it usually meant nothing; deleting the heading would leave a
paragraph describing an integration with no name. A reviewer needs the sentence.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from app.models.project import Project
    from app.services.github_client import RepoActivity
    from app.services.signals import TriggerSignal


#: How much text around a name goes into :attr:`Claim.context`, each side.
CONTEXT_RADIUS = 60


@dataclass(frozen=True)
class Claim:
    """One name in the copy that nothing on file supports."""

    #: The token exactly as the model wrote it — ``Wird``, ``builtExamples``.
    name: str
    #: Which arm fired: ``fused``, ``framed`` or ``hyphen``.
    rule: str
    #: The surrounding sentence fragment. The name alone tells a reviewer what
    #: to search for; this tells them what the piece *claims*, which is the part
    #: they have to judge.
    context: str

    def __str__(self) -> str:  # pragma: no cover - display aid
        return f"{self.name} ({self.rule})"


# --------------------------------------------------------------------------
# Markdown → prose
# --------------------------------------------------------------------------

#: Fenced code. Stripped first and whole: a Python block is wall-to-wall
#: ``camelCase`` identifiers, and every one of them would fire the ``fused`` arm.
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
#: Link and image *targets* — ``](https://…)`` and bare URLs. A hostname is full
#: of name-shaped fragments that nobody wrote as prose.
_LINK_TARGET = re.compile(r"\]\([^)]*\)|<https?://[^>]*>|\bhttps?://\S+|\S+@\S+\.\w+")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
#: Markdown furniture that would otherwise glue itself to a token.
_MARKUP = re.compile(r"[*_~#>|\[\]]+")


def prose(markdown: str) -> str:
    """*markdown* with the parts that are not English removed.

    Code, link targets and HTML tags carry identifiers and hostnames that look
    exactly like the thing this module hunts for and mean nothing of the kind.
    Replaced with a space rather than deleted, so two words either side of a
    stripped span do not fuse into a third that never existed.
    """
    if not markdown:
        return ""
    text = _FENCE.sub(" ", markdown)
    text = _INLINE_CODE.sub(" ", text)
    text = _LINK_TARGET.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    return _MARKUP.sub(" ", text)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: A word for vocabulary purposes: letters and digits, held together by the
#: punctuation that lives *inside* names — ``Node.js``, ``add-on``, ``O'Reilly``.
_VOCAB_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9]*(?:[-‐‑'’.][A-Za-z0-9]+)*")

#: Trailing possessive, dropped before normalising so ``Pulse's`` grounds
#: against ``Pulse``.
_POSSESSIVE = re.compile(r"['’]s$", re.I)

_STRIP_FROM_KEY = re.compile(r"[-‐‑'’.\s]+")


def normalize(token: str) -> str:
    """The form two spellings of the same name have in common.

    Case, internal punctuation and the possessive all vary freely between a
    project's brief and the copy written from it — ``Node.js`` / ``nodejs``,
    ``add-on`` / ``addon``, ``Pulse's`` / ``Pulse``. Folding them away is what
    makes grounding a lookup rather than a guess.
    """
    folded = unicodedata.normalize("NFKC", token)
    folded = _POSSESSIVE.sub("", folded)
    return _STRIP_FROM_KEY.sub("", folded).casefold()


def vocabulary(*sources: Any) -> frozenset[str]:
    """Every word in *sources*, normalised, as the set to ground names against.

    Accepts strings and arbitrary nests of iterables of them, because the facts
    this is built from are a mix of both — ``project.name`` is a string,
    ``project.tech_stack`` is a list, and a signal's ``items`` is a tuple. A
    ``None`` anywhere in there is skipped rather than raising: the columns that
    feed this are nullable, and a project with no ``live_url`` is not an error.

    Multi-word entries contribute their words *and* their joined form, so
    ``"Monte Carlo"`` grounds both ``Monte`` and ``MonteCarlo``.
    """
    words: set[str] = set()

    def absorb(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            for match in _VOCAB_WORD.finditer(value):
                key = normalize(match.group())
                if key:
                    words.add(key)
            joined = normalize(value)
            if joined:
                words.add(joined)
            return
        if isinstance(value, Iterable):
            for item in value:
                absorb(item)

    for source in sources:
        absorb(source)
    return frozenset(words)


def project_vocabulary(
    project: Project,
    *,
    activity: RepoActivity | None = None,
    signal: TriggerSignal | None = None,
    extra: Any = None,
) -> frozenset[str]:
    """Everything Pulse knows about *project*, as a grounding set.

    The same facts the prompt was built from (:meth:`Project.brief`) plus the
    event that prompted it, which is the half that matters most: a piece written
    about a release names the things that release touched, and those names are
    in the commit subjects rather than in any column.

    The README is **not** fetched here. It is one HTTP call to GitHub per
    generated piece, and it is only ever needed for the pieces that have an
    unsupported name in them — so the caller checks against this set first and
    only pays for the README when something looks wrong. See
    :func:`app.services.content_pipeline.generate_and_route`.
    """
    brief = project.brief()
    sources: list[Any] = [
        brief["name"],
        project.slug,
        brief["description"],
        brief["tech_stack"],
        brief["target_audience"],
        brief["keywords"],
        brief["live_url"],
        brief["repo_url"],
        extra,
    ]
    if activity is not None:
        sources += [
            activity.full_name,
            activity.description,
            activity.topics,
            [commit.summary for commit in activity.new_commits],
        ]
        if activity.new_release is not None:
            sources += [
                activity.new_release.tag,
                activity.new_release.name,
                activity.new_release.body,
            ]
    if signal is not None:
        sources += [signal.source, signal.headline, signal.summary, signal.items]
    return vocabulary(*sources)


# --------------------------------------------------------------------------
# What the gate does not need told
# --------------------------------------------------------------------------

#: Names common enough that a project is not expected to declare them.
#:
#: Short on purpose, and the same trade as ``ai._LOANWORDS``: a name missing
#: from here costs one glance at a review queue, and a name wrongly *added* is a
#: hole in the gate for as long as it sits here. So it holds the things a
#: developer writes without thinking — languages, databases, the big platforms —
#: and nothing that a project could plausibly be claiming an integration with as
#: a *feature*, because that claim is exactly what this exists to check.
_WELL_KNOWN = frozenset(
    normalize(name)
    for name in (
        # Languages and runtimes
        "JavaScript", "TypeScript", "Node.js", "Python", "Ruby", "Rust", "Golang",
        "Java", "Kotlin", "Swift", "PHP", "Elixir", "Erlang", "Scala", "Haskell",
        "CPython", "PyPy", "Deno", "Bun",
        # Data stores and infrastructure
        "PostgreSQL", "Postgres", "MySQL", "MariaDB", "SQLite", "MongoDB", "Redis",
        "Memcached", "Elasticsearch", "OpenSearch", "ClickHouse", "CockroachDB",
        "DynamoDB", "Cassandra", "RabbitMQ", "Kafka", "Docker", "Kubernetes",
        "Terraform", "Ansible", "Nginx", "Apache", "Caddy", "HAProxy", "Envoy",
        # Clouds and hosts
        "AWS", "GCP", "Azure", "Heroku", "Vercel", "Netlify", "Cloudflare",
        "DigitalOcean", "Hetzner", "Fly.io", "Render", "Railway", "Supabase",
        "Firebase", "S3", "EC2", "Lambda", "RDS",
        # Frameworks and libraries
        "React", "Vue", "Svelte", "Angular", "Next.js", "Nuxt", "Remix", "Vite",
        "Webpack", "ESLint", "Prettier", "Tailwind", "Bootstrap", "jQuery",
        "Django", "Flask", "FastAPI", "Rails", "Laravel", "Spring", "Express",
        "SQLAlchemy", "Alembic", "Pydantic", "Celery", "Pytest", "Jest",
        "Playwright", "Cypress", "Selenium", "NumPy", "Pandas", "PyTorch",
        "TensorFlow", "Scikit-learn", "Matplotlib",
        # Protocols, formats and standards
        "HTTP", "HTTPS", "REST", "GraphQL", "gRPC", "WebSocket", "WebSockets",
        "OAuth", "OAuth2", "OpenID", "SAML", "JWT", "SSO", "SSL", "TLS", "DNS",
        "SMTP", "IMAP", "RSS", "Atom", "JSON", "YAML", "TOML", "XML", "CSV",
        "PDF", "HTML", "CSS", "SVG", "Markdown", "SQL", "URL", "URI", "UUID",
        "API", "APIs", "SDK", "CLI", "UI", "UX", "CI", "CD", "SEO", "CRUD",
        "CORS", "CSRF", "XSS", "SSRF", "RFC", "ISO", "UTC", "GDPR", "SOC",
        "HIPAA", "OpenAPI", "OpenTelemetry", "Prometheus", "Grafana", "Sentry",
        "Datadog",
        # Ecosystem names that carry internal capitals, which is the shape the
        # `fused` arm reads. Every one of these was a false positive against
        # real copy before it was listed.
        "PyPI", "npm", "NestJS", "Nest.js", "TypeORM", "Prisma", "Knex",
        "Sequelize", "Vitest", "Zod", "tRPC", "DevOps", "MLOps", "WYSIWYG",
        "OpenRouter", "Groq", "Cerebras", "Ollama", "LangChain", "LlamaIndex",
        "Hugging Face", "WebRTC", "WebGL", "WebAssembly", "WASM", "GraphiQL",
        "JavaScriptCore", "OpenSSL", "OpenSSH", "cURL", "jQuery", "DataFrame",
        "JupyterLab", "PyCharm", "IntelliJ", "VSCode", "Xcode", "Gradle",
        "Maven", "NuGet", "RubyGems", "Homebrew", "systemd", "launchd",
        # The brands that genuinely start lowercase and take a capital second,
        # which is the one shape `_is_splice` cannot tell from a dropped space.
        "iPhone", "iPad", "iPadOS", "iCloud", "iMac", "iTunes", "watchOS",
        "tvOS", "eBay", "eSIM", "eCommerce",
        # Platforms and companies developers name in passing
        "WhatsApp", "Telegram", "Signal", "iMessage", "WeChat", "TikTok",
        "Instagram", "Facebook", "Snapchat", "Pinterest", "Threads",
        "GitHub", "GitLab", "Bitbucket", "Git", "Linux", "Ubuntu", "Debian",
        "Windows", "macOS", "iOS", "Android", "Chrome", "Firefox", "Safari",
        "Google", "Microsoft", "Apple", "Amazon", "Meta", "OpenAI", "Anthropic",
        "Claude", "ChatGPT", "Gemini", "Slack", "Discord", "Notion", "Linear",
        "Jira", "Asana", "Trello", "Figma", "Zapier", "Stripe", "PayPal",
        "Twilio", "SendGrid", "Mailgun", "Postmark", "Zoom", "LinkedIn",
        "Twitter", "YouTube", "Reddit", "Medium", "Substack", "WordPress",
        "Shopify", "Salesforce", "HubSpot", "Zendesk", "Intercom", "Mastodon",
        "Bluesky", "Hashnode", "Buttondown", "Forem",
        # Calendar and units, which capitalize mid-sentence legitimately
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
        "Sunday", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
        "English", "Unicode", "UTF-8", "ASCII", "Latin",
    )
)

#: Lowercase fragments English genuinely hyphenates onto a capitalized word.
#:
#: Only consulted by the ``hyphen`` arm, and only for fragments of four
#: characters or fewer — anything longer is left alone on the grounds that a
#: real English suffix ("audit-ready", "event-driven", "third-party") is a word,
#: and the corruption this arm exists for is a *fragment* ("Integrate-mar").
_HYPHEN_TAILS = frozenset(
    {
        "a", "an", "and", "as", "at", "by", "de", "do", "en", "for", "in", "is",
        "it", "la", "le", "of", "off", "on", "or", "out", "per", "re", "the",
        "to", "up", "via", "vs", "with",
        "aid", "all", "app", "api", "art", "aware", "back", "base", "big",
        "bit", "box", "call", "car", "care", "case", "cat", "co", "code",
        "cold", "core", "cost", "cron", "cut", "day", "dev", "dir", "dive",
        "down", "drop", "dupe", "end", "env", "era", "eye",
        "fast", "few", "fed", "fee", "fill", "find", "fire", "first", "fit",
        "five", "flow", "form", "four", "free", "full", "fun", "gap", "go",
        "gray", "grey", "grid", "half", "hand", "hard", "have", "head",
        "heavy", "hoc", "hold", "host", "hot", "hour", "how", "hub", "id",
        "ing", "ish", "job",
        "key", "kind", "lab", "land", "last", "late", "law", "lead", "led",
        "left", "less", "let", "life", "like", "line", "link", "list", "live",
        "load", "lock", "log", "long", "look", "loop", "loss", "low", "made",
        "mail", "main", "make", "many", "map", "mark", "mid", "mind", "mode",
        "more", "most", "much", "net", "new", "next", "nine", "non", "note",
        "now", "num", "odd", "old", "one", "only", "op", "open", "ops", "over",
        "own", "pack", "page", "paid", "pair", "part", "pass", "past", "path",
        "pay", "peer", "pick", "plan", "play", "plus", "poll", "pool", "port",
        "post", "pre", "pro", "prod", "pull", "pure", "push", "put", "rate",
        "ray", "read", "real", "repo", "rich", "ride", "ring", "rise", "risk",
        "role", "roll", "room",
        "root", "rule", "run", "safe", "sale", "save", "seat", "self", "sell",
        "semi", "send", "set", "shot", "show", "shut", "side", "sign", "site",
        "size", "skip", "slot", "slow", "snap", "soft", "sold", "some", "soon", "sort",
        "spam", "spec", "spin", "spot", "src", "star", "stat", "stay", "step",
        "stop", "sub",
        "such", "sum", "sure", "swap", "sync", "tab", "tag", "take", "talk",
        "task", "team", "tech", "tell", "ten", "term", "test", "text", "than",
        "that", "them", "then", "they", "thin", "this", "thru", "tie", "tier",
        "time", "tiny", "tip", "tmp", "tool", "top", "tour", "town", "trip",
        "true", "try", "tune",
        "turn", "two", "type", "un", "unit", "ups", "use", "used", "user",
        "view",
        "vote", "wait", "wake", "walk", "wall", "want", "warm", "warn", "wash",
        "wave", "way", "weak", "wear", "web", "week", "well", "west", "what",
        "when", "wide", "wild", "will", "win", "wise", "wish", "word", "work",
        "wrap", "year", "yes", "zero", "zone", "zoom",
    }
)

#: Ordinary English, consulted only by the ``framed`` arm.
#:
#: The other two arms read *shape*, which is a property of the token. This one
#: reads a **frame**, and a frame is indifferent to what sits in it: "the
#: Summary tab" and "the Teppil inbox" are the same six characters of pattern.
#: What separates them is that "summary" is a word and "Teppil" is not — and
#: the capitalization cannot separate them either, because a Markdown heading
#: ("## The Killer Feature") and a UI label ("the Leads panel") are both title
#: case, where capitalizing a word says nothing about it being a name.
#:
#: So this is the discriminator, and it is a *word* list rather than a *name*
#: list: short, weighted toward the vocabulary of product copy, and wrong in
#: the safe direction. A word missing from here costs one glance at a review
#: queue. A hallucinated name that happens to also be an English word — a
#: feature called "the Ledger panel" that does not exist — is the miss this
#: buys, and it is the right trade: those read as plausible to a human reviewer
#: too, where "Teppil" does not.
_ORDINARY = frozenset(
    {
        # Determiners, pronouns and the connective tissue of a sentence, which
        # a frame will happily capture at the start of one.
        "a", "all", "an", "and", "another", "any", "both", "each", "either",
        "every", "few", "he", "her", "here", "his", "i", "it", "its", "many",
        "me", "more", "most", "much", "my", "neither", "no", "none", "not",
        "one", "other", "others", "our", "several", "she", "some", "that",
        "the", "their", "them", "there", "these", "they", "this", "those",
        "us", "we", "what", "when", "where", "which", "who", "whose", "why",
        "you", "your",
        # The nouns product copy hangs on the frames above.
        "access", "account", "action", "activity", "admin", "agent", "alert",
        "analysis", "analytics", "answer", "app", "approval", "archive",
        "article", "attachment", "audit", "author", "automation", "backend",
        "backup",
        "badge", "balance", "batch", "billing", "board", "body", "branch",
        "brief", "budget", "build", "bulk", "calendar", "call", "campaign",
        "card", "case", "catalog", "category", "chain", "change", "channel",
        "chart", "chat", "check", "client", "cluster", "column", "comment",
        "company", "compliance", "config", "contact", "content", "context",
        "control", "conversation", "cost", "count", "coverage", "credential",
        "customer", "cycle", "dashboard", "data", "database", "date", "deal",
        "default", "delivery", "demo", "deploy", "design", "detail", "digest",
        "directory", "document", "domain", "draft", "editor", "email",
        "employee", "engine", "entry", "error", "event", "example",
        "experience", "export", "feature", "feed", "field", "file", "filter",
        "finance", "flow", "folder", "form", "frontend", "fullstack",
        "function", "graph", "group",
        "growth", "guide", "header", "health", "history", "home", "hook",
        "identity", "image", "import", "inbox", "index", "insight",
        "integration", "interface", "invoice", "issue", "item", "job",
        "journey", "key", "label", "layer", "layout", "lead", "ledger",
        "level", "library", "license", "limit", "link", "list", "log",
        "login", "logic", "market", "member", "menu", "message", "method",
        "metric", "model", "module", "monitor", "name", "network", "node",
        "note", "notice", "notification", "number", "object", "offer",
        "onboarding", "operation", "option", "order", "output", "overview",
        "owner", "page", "panel", "parameter", "partner", "password", "path",
        "payment", "payroll", "performance", "permission", "phase", "picker",
        "pipeline", "plan", "platform", "policy", "portal", "post",
        "preference", "preview", "price", "pricing", "priority", "process",
        "product", "profile", "project", "prompt", "property", "provider",
        "query", "queue", "quote", "rate", "record", "reference", "refund",
        "region", "release", "reminder", "report", "request", "response",
        "result", "review", "risk", "role", "route", "row", "rule", "run",
        "sales", "scenario", "schedule", "scope", "score", "screen", "search",
        "section", "security", "server", "service", "session", "setting",
        "setup", "share", "sheet", "shortcut", "sidebar", "signal",
        "signature", "site", "slot", "snapshot", "source", "space", "stage",
        "state", "statement", "status", "step", "storage", "store", "story",
        "stream", "subscription", "summary", "support", "survey", "sync",
        "system", "tab", "table", "tag", "target", "task", "team",
        "template", "tenant", "test", "text", "theme", "thread", "ticket",
        "time", "timeline", "timer", "title", "token", "tool", "topic",
        "total", "trace", "track", "traffic", "transaction", "trend",
        "trigger", "type", "update", "upload", "usage", "user", "value",
        "version", "video", "view", "voice", "volume", "wallet", "warning",
        "webhook", "widget", "workflow", "workspace", "wizard", "zone",
        # The adjectives a title-cased heading or bullet lead starts with.
        "active", "advanced", "automated", "automatic", "available", "basic",
        "best", "better", "central", "clean", "clear", "common",
        "complete", "core", "current", "custom", "daily", "dedicated",
        "deep", "detailed", "direct", "dynamic", "easy", "effective",
        "efficient", "essential", "existing", "extra", "fast", "final",
        "first", "flexible", "free", "full", "general", "global", "good",
        "great", "high", "important", "improved", "instant", "intelligent",
        "internal", "killer", "large", "last", "latest", "light",
        "live", "local", "long", "low", "main", "major", "manual", "minor",
        "mobile", "modern", "monthly", "multiple", "native", "new", "next",
        "old", "only", "open", "personal", "popular", "powerful", "practical",
        "precise", "primary", "private", "proactive", "public", "quick",
        "rapid", "real", "recent", "reliable", "remote", "rich", "robust",
        "safe", "same", "scalable", "seamless", "secure", "shared", "short",
        "simple", "single", "small", "smart", "solid", "special", "specific",
        "standard", "static", "strong", "unified", "unique", "universal",
        "useful", "valid", "weekly", "whole", "wide", "yearly",
        # Verbs and gerunds that head a bullet or a heading.
        "adding", "building", "connecting", "creating", "getting", "keeping",
        "making", "managing", "moving", "reading", "running", "sending",
        "starting", "tracking", "using", "working", "writing",
    }
)

#: Longest hyphen fragment the ``hyphen`` arm will look at. Above this the
#: fragment is a word rather than the truncation the arm is for; see
#: :data:`_HYPHEN_TAILS`.
_HYPHEN_TAIL_MAX = 4


# --------------------------------------------------------------------------
# Candidate names
# --------------------------------------------------------------------------

#: A token that could be a name. Letters and digits, joined across the internal
#: punctuation names carry. Deliberately excludes a leading digit so that "2024"
#: and "409A" are not candidates on their own.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-‐‑'’.][A-Za-z0-9]+)*")

#: An internal capital following a lowercase letter, which is the ``fused``
#: shape. ``PostgreSQL`` matches (``eS``); ``API`` and ``AI`` do not.
#:
#: Matching the shape is not sufficient on its own — see :func:`_is_splice`,
#: which is what tells ``builtExamples`` from ``SmartRecruiters``.
_FUSED = re.compile(r"[a-z][A-Z]")

_HYPHEN_SPLIT = re.compile(r"[-‐‑]")

#: Component nouns — the words that turn a capitalized token in front of them
#: into a claim that the product ships a thing by that name.
_COMPONENT = (
    r"inbox|dashboard|panel|module|plugin|widget|engine|queue|worker|adapter|"
    r"integration|integrations|api|sdk|cli|endpoint|feature|tab|view|report|"
    r"reports|editor|composer|scheduler|pipeline|service|connector|extension|"
    r"add-on|toolkit|library|runtime|console|portal|workspace|assistant|agent|"
    r"sync|importer|exporter|webhook|webhooks|bridge|gateway|store|index|"
    r"backend|frontend|layer|driver|parser|resolver|checker|monitor|tracker"
)

#: The name being captured. Capitalized, and allowed to carry the same internal
#: punctuation as :data:`_TOKEN`.
_NAME = r"(?P<name>[A-Z][A-Za-z0-9]*(?:[-‐‑'’.][A-Za-z0-9]+)*)"

#: Phrases that assert the captured name is part of the product. Each is a
#: claim a reader would act on — "we integrate with X" is a reason to buy.
#:
#: The case-insensitivity is **scoped** to the framing words with ``(?i:…)``
#: rather than applied to the pattern. A file-level ``re.I`` reaches into
#: :data:`_NAME` and turns its leading ``[A-Z]`` into "any letter", which makes
#: every one of these match an ordinary lowercase noun: "the outreach module"
#: reports ``outreach``, and the arm that exists to find two hallucinations
#: instead reports most of the paragraph.
_FRAMES = (
    re.compile(rf"\b(?i:integrat(?:e|es|ed|ing|ion|ions))\s+(?i:with)\s+{_NAME}"),
    re.compile(rf"\b(?i:connect(?:s|ed|ing)?)\s+(?i:to|with)\s+{_NAME}"),
    re.compile(rf"\b(?i:powered|backed|driven)\s+(?i:by)\s+{_NAME}"),
    re.compile(rf"\b(?i:built)\s+(?i:on|with|atop)\s+{_NAME}"),
    re.compile(rf"\b(?i:the|our|its|their|a|an)\s+{_NAME}\s+(?i:{_COMPONENT})\b"),
    re.compile(rf"\b{_NAME}\s+(?i:integration)\b"),
    re.compile(rf"\b(?i:support)\s+(?i:for)\s+{_NAME}"),
)


@dataclass(frozen=True)
class _Part:
    """One hyphen-separated piece of a token, and where it sits inside it."""

    text: str
    start: int
    end: int


def _split_parts(token: str) -> list[_Part]:
    """*token* split on its hyphens, each part carrying its offset."""
    parts: list[_Part] = []
    cursor = 0
    for piece in _HYPHEN_SPLIT.split(token):
        start = token.index(piece, cursor) if piece else cursor
        parts.append(_Part(text=piece, start=start, end=start + len(piece)))
        cursor = start + len(piece) + 1
    return parts


def _context(text: str, start: int, end: int) -> str:
    """The fragment of *text* around ``[start:end]``, on one line."""
    left = max(0, start - CONTEXT_RADIUS)
    right = min(len(text), end + CONTEXT_RADIUS)
    snippet = " ".join(text[left:right].split())
    return f"{'…' if left else ''}{snippet}{'…' if right < len(text) else ''}"


def _is_known(token: str, known: Collection[str]) -> bool:
    """Whether *token* is grounded in *known* or common enough not to need it."""
    key = normalize(token)
    return not key or key in known or key in _WELL_KNOWN


def _is_ordinary(token: str) -> bool:
    """Whether *token* is an English word rather than a name. Framed arm only.

    Tries the plural off as well as on, because :data:`_ORDINARY` is written in
    the singular and copy is not: "the Leads panel" and "the Scenarios tab" are
    the same false positive as "the Lead panel" and would each need their own
    entry otherwise.
    """
    key = normalize(token)
    if key in _ORDINARY:
        return True
    if key.endswith("es") and key[:-2] in _ORDINARY:
        return True
    return key.endswith("s") and key[:-1] in _ORDINARY


def _accounted_for(name: str, known: Collection[str]) -> bool:
    """Whether the ``framed`` arm has any business reporting *name*.

    Grounded, ordinary, or — the case this exists for — a compound whose every
    *name-shaped* part is one of those. "A NestJS-based service" puts a known
    name and an ordinary adjective inside a frame, and judging the welded pair
    as one unknown token reported it; "the Wird-Sync bridge" still reports,
    because ``Wird`` is capitalized and accounted for by nothing.
    """
    if _is_known(name, known) or _is_ordinary(name):
        return True
    parts = _split_parts(name)
    if len(parts) < 2:
        return False
    return all(
        not part.text[:1].isupper()
        or _is_known(part.text, known)
        or _is_ordinary(part.text)
        for part in parts
    )


def _is_splice(token: str, known: Collection[str]) -> bool:
    """Whether *token* is two words welded together rather than a name.

    Both are spelled the same way — a capital in the middle of a word — and the
    head is what tells them apart. A brand is a proper noun and starts with a
    capital (``SmartRecruiters``, ``ClearTax``, ``PreToolUse``); a splice starts
    with whatever English word the sampler was mid-sentence in when it dropped
    the space, which is lowercase (``builtExamples``, ``forEditor``).

    That asymmetry is the whole discriminator, and it is worth stating why it is
    allowed to be: the vocabulary cannot carry every third-party product a piece
    mentions once in passing, so *unknown* and *capitalized* is the ordinary
    condition of a brand name rather than evidence against it. The claim arms
    are where a capitalized unknown gets read — this one only has to find the
    tokens that are not words at all.
    """
    return bool(
        token[:1].islower() and _FUSED.search(token) and not _is_known(token, known)
    )


def _hyphen_fragment(token: str, known: Collection[str]) -> bool:
    """Whether *token* is a capitalized word welded to a non-word fragment.

    ``Integrate-mar`` yes; ``Audit-ready``, ``Out-of-Office`` and ``add-on`` no.
    Requires a capitalized head so that ordinary lowercase compounds never reach
    the length test, and only judges fragments short enough to be a truncation
    rather than a word (:data:`_HYPHEN_TAIL_MAX`).
    """
    parts = _HYPHEN_SPLIT.split(token)
    if len(parts) < 2 or not parts[0][:1].isupper():
        return False
    for part in parts[1:]:
        if not part or not part[0].islower():
            continue
        if len(part) > _HYPHEN_TAIL_MAX:
            continue
        folded = part.casefold()
        if folded in _HYPHEN_TAILS or _is_known(part, known):
            continue
        return True
    return False


def unsupported_names(
    text: str, known: Collection[str], *, limit: int = 10
) -> list[Claim]:
    """Name-shaped tokens in *text* that *known* does not account for.

    *text* is Markdown; code and link targets are stripped before anything is
    read (:func:`prose`). *known* is a normalised vocabulary, from
    :func:`project_vocabulary` or :func:`vocabulary`.

    Deduplicated by normalised name, so a hallucination repeated in six
    paragraphs is one thing to review rather than six. *limit* caps the list for
    the same reason the splice gates cap theirs: a reviewer acts on the first
    few and the count, never on the hundredth.
    """
    if not text:
        return []

    body = prose(text)
    if not body:
        return []

    found: list[tuple[int, Claim]] = []
    seen: set[str] = set()
    spans: list[tuple[int, int]] = []

    def record(name: str, rule: str, start: int, end: int) -> None:
        key = normalize(name)
        if key in seen:
            return
        # Overlapping spans are one bad token seen by two arms, not two
        # problems. ``NestJSManchester‑poweredubar`` is caught whole by a frame
        # and again in part by the fused shape; reporting both makes a single
        # splice look like a piece with two hallucinations in it, and sends the
        # reviewer to the same word twice.
        if any(start < prior_end and prior_start < end for prior_start, prior_end in spans):
            return
        seen.add(key)
        spans.append((start, end))
        found.append(
            (start, Claim(name=name, rule=rule, context=_context(body, start, end)))
        )

    # Frames first, so that a name matching both arms is reported as the claim
    # it is rather than as a spelling curiosity: "integration with GoSumoX" is
    # more useful to a reviewer under `framed` than under `fused`.
    for frame in _FRAMES:
        for match in frame.finditer(body):
            name = match.group("name")
            if _accounted_for(name, known):
                continue
            record(name, "framed", match.start("name"), match.end("name"))

    for match in _TOKEN.finditer(body):
        token = match.group()
        if _is_known(token, known):
            continue
        # Per *part*, not per token. A hyphen in prose joins two words, and the
        # question this arm asks is about one of them: "WhatsApp-Native" and
        # "NestJS-based" are a known name next to an ordinary adjective, and
        # judging the welded pair as one unknown token reported both.
        offset = match.start()
        for part in _split_parts(token):
            if _is_splice(part.text, known):
                record(part.text, "fused", offset + part.start, offset + part.end)
                break
        else:
            if _hyphen_fragment(token, known):
                record(token, "hyphen", match.start(), match.end())

    # Document order, not arm order. `limit` cuts the tail of a long piece, and
    # a reviewer opening the editor reads from the top — a cap that kept the
    # frame matches and dropped everything before them would send them to the
    # wrong paragraph.
    found.sort(key=lambda pair: pair[0])
    return [claim for _, claim in found[:limit]]


__all__ = [
    "CONTEXT_RADIUS",
    "Claim",
    "normalize",
    "project_vocabulary",
    "prose",
    "unsupported_names",
    "vocabulary",
]

# Pulse — feature improvements

Written against the code as of `7b3879c`, as proposals. The **first pass** has
since been implemented in full — the items marked ✅ below are shipped, and the
"Where the product is now" section describes the state *before* them. Everything
unmarked is still a proposal.

| Shipped | Was |
|---|---|
| ✅ 5.1 auto-canonical + syndication order | `cb1f8c5` |
| ✅ 5.2 link and claim validation | `85f9260` |
| ✅ 1.6 cover images | `73a3fe6` |
| ✅ 3.1 UTM tagging + click attribution | `6cdc933` |
| ✅ 2.1 Git-based publishing | `fe618dc` |
| ✅ 2.2 Mastodon · 2.3 Bluesky | `fe618dc` |
| ✅ Hashnode + WordPress transports | `fe618dc` |

Two destinations became seven, canonicals are correct, and outbound traffic is
attributable. The **second pass** — the feedback loop, §3.2–3.4 and §4.1/4.4 —
is what comes next.

**Effort scale** (one developer, including tests and UI):
**S** ≤ 1 day · **M** 2–4 days · **L** 1–2 weeks · **XL** 3+ weeks

---

## Where the product is now

Worth stating plainly, because it sets the priorities below.

- One piece of content is one Markdown body, adapted to each platform at publish
  time by truncation and format conversion (`services/publishers/formatting.py`).
  There is no per-platform *writing*.
- Two platforms publish (Dev.to, Medium); one reports metrics (Dev.to). Medium's
  token tap is closed to new accounts, so in practice a new install has **one**
  working destination.
- `Content.generated_by_provider` / `generated_by_model` / `confidence` are
  recorded and never analysed. `canonical_url` exists on the model, is sent by
  every adapter, and is never set by anything but a hand-typed field.
- `ContentMetric` is an append-only series, but `analytics_service` only ever
  reads the latest row per publication — the series is stored and unused.
- `cadence.py` is a static table and says so, deferring learned timing to
  analytics. `max_per_week` is advice; nothing enforces it.
- No media handling of any kind — no cover images, which Dev.to, Medium and
  Hashnode all display and which materially affect click-through.
- Single user by construction: `Project.user_id`, registration closed, no roles,
  no comments, no audit trail.

The through-line: Pulse generates well and distributes narrowly, and the data
it already collects to close the loop is not yet read.

---

## Prioritized shortlist

| # | Feature | Category | Effort | Why it's here |
|---|---|---|---|---|
| 1 | Per-platform derivative copy (`ContentVariant`) | Generation | **M** | A 1200-word tutorial truncated to 280 chars is the weakest output in the product |
| 2 | Git-based publishing (commit Markdown to a repo) | Platforms | **M** | No OAuth, reuses `GITHUB_TOKEN`, targets the Astro/Hugo/Next blogs the audience actually runs |
| 3 | Auto-canonical + syndication order | SEO | **S** | Field and adapter plumbing already exist; today syndicated copies compete with the original |
| 4 | Mastodon + Bluesky adapters | Platforms | **S** each | Free write APIs, token auth, dev-dense audiences — cheapest reach per day of work |
| 5 | Cover images (upload + generated OG card) | Generation | **M** | The one missing field every blogging platform surfaces in its feed |
| 6 | Inline AI editing (rewrite / expand / shorten / retone selection) | Generation | **M** | Today the only iteration is regenerate-the-whole-thing |
| 7 | Link + claim validation before publish | SEO / quality | **S** | Free models invent URLs; a 404 in a published post is the most visible failure mode |
| 8 | UTM tagging + click attribution | Analytics | **S** | Turns "views on Dev.to" into "traffic to the product", the number that matters |
| 9 | "What actually works" insight report | Analytics | **M** | Reads `generated_by_model`, `content_type`, publish hour, length against outcomes — data already on disk |
| 10 | Learned optimal timing (replaces the static table) | Scheduling | **M** | `cadence.py` explicitly leaves this door open |
| 11 | Finish Hashnode + WordPress adapters | Platforms | **S** each | Formatting is done and tested; only the transport is missing |
| 12 | Notifications + approve-from-Slack/email | Automation | **M** | The review queue only exists if someone opens the app; `mailer.py` is already there |
| 13 | Shareable draft preview links | Collaboration | **S** | Solo-dev-friendly feedback without inventing multi-tenancy |
| 14 | GitHub App / webhooks instead of hourly polling | Integration | **M** | Kills the 60-req/hour ceiling and the up-to-an-hour lag |

Items 1–5 are the ones I'd do first: two fix distribution, three fix quality,
and none of them require a data-model rewrite.

---

## 1. Content generation

**1.1 Per-platform derivative copy — M**
Add `ContentVariant(content_id, platform, body, status)`. After a piece is
approved, generate a purpose-written LinkedIn post, tweet thread and newsletter
blurb from the canonical body rather than truncating it. Editable per variant;
falls back to today's `formatting.truncate_*` when generation fails, so nothing
regresses. Touches `content_generator.py`, `publishing_service.build_request`,
`ContentEditor.jsx`.

**1.2 Inline AI editing — M**
`POST /content/{id}/edit` taking a selection + an operation (rewrite, expand,
shorten, change tone, make more technical, add a code example). Small prompts,
low token cost, huge quality-of-life gain over full regeneration. Pairs with
**1.3**.

**1.3 Draft revision history — S/M**
`ContentRevision` rows on every save and every AI operation, with restore. Makes
inline editing safe to use and answers "what did the model change?".

**1.4 Project voice profile — S**
Tone is three enum values today. Add a per-project `voice_sample` (paste two
posts you like) and 2–3 few-shot excerpts into the prompt. Cheapest available
lever on output quality — no schema churn beyond one column.

**1.5 Richer repo grounding — M**
The prompt currently sees commit subject lines and release notes. Add README,
CHANGELOG, and (for tutorials) the diff of `docs/` and public API signatures.
Most invented-fact failures come from a thin brief, not a weak model.

**1.6 Cover images — M**
Upload, plus a generated OG card (project name, title, tech badges — SVG →
PNG, no image model needed). Store on disk or S3; send as `cover_image`
(Dev.to), `coverImage` (Hashnode), featured media (WordPress). Also fixes
LinkedIn and Twitter link previews.

**1.7 N-draft generation with a pick — S**
Generate 2–3 titles/openings, show them side by side, keep one. Free-tier
quota permitting; gate behind a setting.

**1.8 Content series / launch campaign — L**
One release → a planned sequence (announcement now, deep-dive in 4 days,
tutorial in 2 weeks), scheduled as a unit. Builds on `ContentIdea`, which
already exists and is under-used.

---

## 2. Publishing platforms

Finish what's scaffolded first — the formatting layers are written and tested,
so these are transport-only:

| Platform | Missing piece | Effort |
|---|---|---|
| Hashnode | One GraphQL mutation | **S** |
| WordPress | Basic auth + a live test | **S** |
| Twitter/X | OAuth 1.0a signing (and a paid tier for writes) | **M** |
| LinkedIn | Registered app + 3-legged OAuth — an auth flow, not an adapter | **M/L** |

New destinations, ranked by reach-per-effort:

**2.1 Git-based publishing — M**
Commit the Markdown (plus front matter) to a path in a repo and open a PR or
push to a branch. No OAuth, reuses `GITHUB_TOKEN`, and it is how most of the
target audience actually runs their blog. Should be the *primary* destination in
the canonical chain (see 5.1).

**2.2 Mastodon — S** · **2.3 Bluesky — S**
Both have free, simple, token-authenticated write APIs and dev-heavy audiences.
Bluesky needs facet handling for links; Mastodon is essentially a form POST.

**2.4 Ghost — S**
Admin API key, JWT-signed requests, Markdown accepted directly. Common among
self-hosting developers.

**2.5 Reddit — M**
High reach, high risk: subreddit rules make auto-posting a good way to get
banned. Ship as draft-and-copy or explicit-approval-only.

**2.6 Newsletter (Buttondown / Beehiiv / Substack) — M**
Buttondown has the cleanest API. Turns the archive into an owned channel rather
than six rented ones.

**2.7 Announcement webhooks (Discord / Slack / Telegram) — S**
Not "publishing" so much as broadcasting, but it's a webhook URL and a formatted
message.

**2.8 RSS/JSON feed out — S**
Pulse already holds every published piece; serving a feed costs one endpoint
and makes the archive syndicable.

---

## 3. Analytics and insights

**3.1 UTM tagging + click attribution — S**
Append per-platform UTM parameters to `project_url` / `canonical_url` in
`build_request`. Suddenly "which platform sends actual traffic" is answerable
without any platform's stats API — which matters when only Dev.to has one.

**3.2 Use the time series — S/M**
`analytics_service` reads only `MAX(id)` per publication. Add first-24h
velocity, growth curves and decay detection over the rows already stored. This
is query work, not collection work.

**3.3 "What actually works" report — M**
Correlate outcomes against `content_type`, tone, `generated_by_model`, word
count, publish weekday/hour and confidence. Render as a weekly digest with an
LLM-written summary over the computed numbers (compute first, narrate second —
never let the model do the arithmetic).

**3.4 GitHub stars/traffic correlation — M**
Pulse already knows the repo and the publish timestamp. Overlay stars and repo
traffic against publication dates: "this post moved 40 stars" is the metric a
developer-marketing tool should own, and nobody else can compute it.

**3.5 Google Search Console integration — M**
Impressions, clicks and average position per published URL. The only real source
of SEO truth, and `external_url` gives a clean join key.

**3.6 Plausible / GA4 read-only — M**
Server-side pageviews for self-hosted and WordPress destinations, where no
platform API exists.

**3.7 Underperformance alerts — S**
"This post is at 20% of your median 48-hour views" — cheap once **3.2** lands.

**3.8 Export — S**
CSV/JSON of content and metrics. Table stakes; also an escape hatch that makes
the product easier to trust.

---

## 4. Scheduling and automation

**4.1 Learned optimal timing — M**
Replace the static `CADENCES` table with per-platform posting hours derived from
the user's own metrics, falling back to the table until there's enough data.
`cadence.py` already names this as the intended successor.

**4.2 Queue/slot scheduling — M**
Define weekly slots per platform ("Tue and Thu 13:00"); approving a piece drops
it into the next free slot instead of prompting for a datetime. Turns scheduling
from a decision into a default.

**4.3 Enforce cadence — S**
`max_per_week` is currently advice. Warn (or block, configurably) when a queue
exceeds it — the daily autopilot cap protects against a burst, not against a
steady overdose.

**4.4 Evergreen re-share — S/M**
Re-share top performers to social channels after N days, with a cooldown. Best
ROI in the whole document per line of code, and it needs **3.2** to pick targets.

**4.5 Autopilot dry-run + daily digest — S**
"Here's what I would have written and published." The current auto mode asks for
a lot of trust up front; a dry-run week earns it.

**4.6 More triggers — M**
GitHub webhooks (see 6.1), GitLab, an RSS/changelog watcher, npm/PyPI release
feeds. The autopilot's logic is trigger-agnostic already; only
`github_client.fetch_activity` is not.

**4.7 Recurring content jobs — S**
"A monthly changelog roundup per project" — a beat task over `ContentIdea`.

---

## 5. SEO and growth

**5.1 Auto-canonical + syndication order — S**
Designate a primary destination per project. First publish sets
`content.canonical_url` from its `external_url`; every later platform receives
it. All the plumbing exists — nothing currently sets the field. This is the
highest value-to-effort item in the document.

**5.2 Link and claim validation — S**
HEAD-check every URL in a generated body before publish and surface dead ones in
the SEO panel alongside `seo.audit()` output. Free models fabricate plausible
documentation links; this catches them for the cost of a few HTTP requests.

**5.3 Internal linking suggestions — M**
Suggest links to your own previously published pieces by keyword overlap.
Compounds as the archive grows, and Pulse knows every URL it has published.

**5.4 Content refresh detection — M**
Flag posts whose traffic has decayed or whose tech stack has moved on, and
offer a regenerate-with-current-facts pass. Refreshing an existing ranked post
usually beats writing a new one.

**5.5 Keyword research — M/L**
Today keywords are typed by hand. Either a paid SERP API, or — better and
free — derive them from Search Console impressions (**3.5**): optimise for
what you *nearly* rank for.

**5.6 Structured data + readability — S**
JSON-LD `Article` markup for self-hosted and WordPress output; a readability
score and an alt-text check in the existing audit panel.

---

## 6. Integrations

**6.1 GitHub App / webhooks — M**
Replace hourly polling with push and release events. Removes the 60-req/hour
anonymous ceiling, removes the up-to-an-hour lag, and gives per-repo install
consent instead of one global PAT.

**6.2 Slack / Discord approval — M**
Post the draft to a channel with Approve / Edit / Skip buttons. Pairs with the
digest in **4.5**; makes the review queue reachable without opening the app.

**6.3 Outbound webhooks — S**
`content.published`, `publication.failed`, `review.pending` as user-configured
HTTP callbacks. One table, one dispatch helper, and it makes Pulse composable
with everything it will never natively integrate with.

**6.4 An MCP server for Pulse — M**
Expose projects, drafts, generation and publishing as MCP tools so Claude Code
can draft and queue a post from inside the repo being written about. Pulse's
whole premise is "you're already in the terminal"; this meets the user there.

**6.5 Bring-your-own-key / paid models — S**
Config supports four providers, all free-tier. Let a user paste a paid key and
select a model per project. Free tier stays the default; quality ceases to be
capped by it.

**6.6 Notion / Obsidian import — M**
Pull existing drafts in as `Content` rows so Pulse can publish what's already
written, not only what it wrote.

---

## 7. Collaboration

Currently single-user by construction (`Project.user_id`, registration closed,
no roles). Two paths, and I'd take the cheap one first.

**7.1 Shareable draft preview links — S**
A signed, expiring, read-only URL for one draft. Gets a second pair of eyes on a
post without building multi-tenancy. Do this one.

**7.2 Review comments and change requests — M**
Threaded comments anchored to a draft, a reviewer field, and a decision log.
Meaningful even solo — it's where "why did I reject this?" lives.

**7.3 Audit trail — S**
Who approved, published, retried or deleted what, and when. Small table, and
prerequisite for anything multi-user.

**7.4 Teams, roles and shared projects — XL**
Organisation → membership → role (owner / editor / reviewer), re-scoping every
`user_id` query and every ownership check, plus invitations and per-org platform
connections. A product decision, not a feature: it changes the security model,
the seed flow, and the deployment story. Don't start it until 7.1–7.3 have shown
that collaboration is actually wanted.

---

## Suggested sequencing

**First pass — distribution and quality (~3 weeks)**
5.1 auto-canonical → 2.1 Git publishing → 2.2/2.3 Mastodon + Bluesky →
Hashnode + WordPress transports → 5.2 link validation → 3.1 UTM tagging.
Ends with six working destinations instead of two, correct canonicals, and
traffic attribution.

**Second pass — the feedback loop (~3 weeks)**
3.2 time series → 3.3 what-works report → 3.4 stars correlation →
4.1 learned timing → 4.4 evergreen re-share.
Pulse starts recommending rather than only executing.

**Third pass — the writing itself (~3 weeks)**
1.1 per-platform variants → 1.6 cover images → 1.2/1.3 inline editing with
history → 1.4 voice profile.

**Ongoing, any time**
6.3 webhooks, 6.5 BYO key, 7.1 preview links, 3.8 export — each a day or less
and independent of everything above.

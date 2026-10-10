"""Read-only GitHub client: what has shipped in a project since we last looked.

Only three endpoints matter here — repo metadata, recent commits, and releases —
so this is plain httpx rather than a dependency on PyGithub.

Two things this module deliberately does *not* do:

* **Authenticate as the user.** A token is a global setting, not per project.
  Pulse reads public repos to write about them; it never pushes.
* **Fetch full history.** The autopilot cares about the delta since a stored
  watermark, so a single page of commits is enough. A repo that has moved more
  than a page since the last scan gets "100+ commits", which is the same
  editorial signal as the exact number.
"""
from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.config import settings
from app.services.errors import friendly_network_error
from app.models.project import COMMIT_SHA_MAX_LENGTH, RELEASE_TAG_MAX_LENGTH

logger = logging.getLogger(__name__)

#: One page of commits. Enough for any sane scan interval; see module docstring.
_COMMIT_PAGE_SIZE = 100


class GitHubError(RuntimeError):
    """A GitHub request failed, or the repo isn't reachable."""


class GitHubRateLimited(GitHubError):
    """The API refused us for rate-limit reasons.

    Separated from the generic error because the caller's response differs: a
    404 means "stop scanning this project", a rate limit means "come back
    later", and conflating them would make an unauthenticated Pulse quietly
    disable every project it watches.

    *retry_after* is the seconds GitHub asked us to wait, when it said. Only the
    secondary limit sends it; the primary one sends a reset timestamp instead.
    """

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GitHubNotFound(GitHubError):
    """GitHub has no such path.

    :func:`fetch_latest_release` needs to tell "this repo has releases disabled"
    (fine, return ``None``) from "this repo is gone" (not fine), and used to do
    it by searching the *message* of the generic error for ``"has no"``. The
    message is prose written for a human reading a 502 body; a reword would
    have silently turned every missing repo into a repo with no releases, and
    the phrase itself appears in any repo name containing it. A subclass says
    the same thing where it cannot be edited out from under the check.
    """


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str
    author: str
    committed_at: datetime | None
    url: str

    @property
    def summary(self) -> str:
        """First line of the commit message — the part worth showing a model."""
        return self.message.strip().splitlines()[0] if self.message.strip() else ""


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    body: str
    published_at: datetime | None
    url: str
    prerelease: bool


@dataclass(frozen=True)
class RepoActivity:
    """Everything the content engine needs to decide "is this worth a post?"."""

    full_name: str
    #: Commits newer than the watermark, newest first.
    new_commits: list[Commit] = field(default_factory=list)
    #: The latest release, if it is newer than the watermark.
    new_release: Release | None = None
    #: Repo description and topics — useful context even when nothing is new.
    description: str = ""
    topics: list[str] = field(default_factory=list)
    stars: int = 0
    #: The current HEAD sha and latest tag, for storing as the new watermark.
    head_sha: str | None = None
    latest_tag: str | None = None

    @property
    def has_news(self) -> bool:
        """Whether anything happened worth writing about since the watermark."""
        return bool(self.new_commits or self.new_release)


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Pulse/0.1 (+https://github.com/r2st/DoAide-Pulse)",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


#: What GitHub calls the secondary limit when it does not send ``Retry-After``.
#: Both spellings are live — the API still returns the older "abuse detection"
#: wording — and neither is a status code, so the body is the only signal left.
_SECONDARY_LIMIT_HINTS = ("secondary rate limit", "abuse detection")


def _retry_after(resp: httpx.Response) -> int | None:
    """The ``Retry-After`` delay in seconds, if GitHub sent a usable one.

    Only the numeric form is read. The HTTP-date form is legal and GitHub does
    not use it here; parsing it wrong would be worse than not having it, since
    the value's whole job is to be waited for.
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(float(raw.strip()))
    except (AttributeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _reset_at(resp: httpx.Response) -> str:
    """``X-RateLimit-Reset`` as a time a human can act on, else as it came."""
    raw = resp.headers.get("X-RateLimit-Reset")
    if raw is None:
        return "an unknown time"
    try:
        moment = datetime.fromtimestamp(int(float(raw)), tz=UTC)
    except (OSError, OverflowError, TypeError, ValueError):
        return str(raw)
    return moment.strftime("%H:%M UTC")


@dataclass(frozen=True)
class Throttle:
    """GitHub saying "not now": which of its two limits, and for how long.

    Deliberately carries no message. The two callers are talking to different
    people about different tokens — the autopilot about ``GITHUB_TOKEN``, the
    publisher about the user's own PAT — so the wording is theirs and only the
    facts are shared.
    """

    #: The burst limit rather than the hourly quota. It is the one that sends
    #: ``Retry-After``, and the one a busy sweep or a batch of publishes trips.
    secondary: bool
    #: Seconds GitHub asked us to wait, when it said. Only the secondary limit
    #: sends it.
    retry_after: int | None
    #: When the hourly quota comes back, phrased for a human.
    reset_at: str


def throttle_reason(resp: httpx.Response) -> Throttle | None:
    """Whether this 403/429 is GitHub throttling us, or a genuine refusal.

    Public because two unrelated callers need this discrimination and both got
    it wrong the same way. GitHub reports its *secondary* limit as a **403**,
    and a 403 reads as "forbidden" to anyone not looking for this — so a
    throttled request presents as an access failure and gets answered by
    telling somebody their credentials are bad. See :func:`_rate_limit` for
    what that cost the autopilot, and
    :meth:`app.services.publishers.git.GitAdapter._translate` for what it cost
    publishing.
    """
    retry_after = _retry_after(resp)
    if retry_after is not None:
        return Throttle(secondary=True, retry_after=retry_after, reset_at=_reset_at(resp))

    # No header. The body carries the same news, and a secondary limit without
    # ``Retry-After`` is common enough that treating it as an access failure
    # would leave the same hole half-open. Clipped: this is an error body being
    # scanned for a phrase, not a payload being read.
    body = (resp.text or "")[:1000].lower()
    if any(hint in body for hint in _SECONDARY_LIMIT_HINTS):
        return Throttle(secondary=True, retry_after=None, reset_at=_reset_at(resp))

    if resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0":
        return Throttle(secondary=False, retry_after=None, reset_at=_reset_at(resp))
    return None


def _rate_limit(resp: httpx.Response) -> GitHubRateLimited | None:
    """The rate-limit error this 403/429 is, or ``None`` if it is not one.

    GitHub has two limits and reports them differently, and only one of them was
    recognised here.

    The *primary* limit is the request quota: ``X-RateLimit-Remaining: 0`` and a
    reset timestamp. That was handled.

    The *secondary* limit is the one that catches a busy sweep — it fires on
    request concurrency and burst rate, and it fires while the quota is
    nearly untouched. GitHub reports it as a 403 with ``Retry-After`` and
    ``X-RateLimit-Remaining`` still in the hundreds, which is exactly the shape
    this function's caller used to read as "private repo, no token". That
    mis-read is not cosmetic: :class:`GitHubRateLimited` is the one error the
    autopilot answers by holding the watermark and *not* stamping
    ``last_scanned_at``, because it means we never looked. As a plain
    ``GitHubError`` a throttled scan instead marked the project unreachable and
    stamped it as scanned — and since the secondary limit is per-account, one
    burst did that to every project in the sweep at once, each of them logging
    a repo that was never private.

    "Private repo" was the wrong guess in any case: GitHub answers 404, not 403,
    for a private repo the caller cannot see, precisely so that a 403 does not
    confirm it exists.
    """
    throttle = throttle_reason(resp)
    if throttle is None:
        return None
    if throttle.secondary:
        if throttle.retry_after is not None:
            return GitHubRateLimited(
                f"GitHub is throttling this account (secondary rate limit); it "
                f"asked us to wait {throttle.retry_after}s.",
                retry_after=throttle.retry_after,
            )
        return GitHubRateLimited(
            "GitHub is throttling this account (secondary rate limit). "
            "Slow down and try again shortly."
        )
    return GitHubRateLimited(
        f"GitHub rate limit exhausted (resets at {throttle.reset_at}). "
        "Set GITHUB_TOKEN to raise the ceiling from 60 to 5000 req/hour."
    )


_MAX_RESPONSE_BYTES = 1_048_576


def _get(path: str, *, params: dict | None = None) -> httpx.Response:
    url = f"{settings.github_api_url.rstrip('/')}{path}"
    try:
        resp = httpx.get(
            url,
            headers=_headers(),
            params=params,
            timeout=settings.github_timeout_seconds,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub is not reachable: {friendly_network_error(exc)}") from exc
    if len(resp.content) > _MAX_RESPONSE_BYTES:
        resp._content = resp.content[:_MAX_RESPONSE_BYTES]

    # 403 and 429 are how GitHub says no, and it says no for two unrelated
    # reasons that have to be told apart — see :func:`_rate_limit`.
    if resp.status_code in (403, 429):
        limited = _rate_limit(resp)
        if limited is not None:
            raise limited
        raise GitHubError(
            f"GitHub denied access to {path} ({resp.status_code}). The token "
            "may be missing a scope, or this account may be blocked from the "
            "repo."
        )
    if resp.status_code == 404:
        raise GitHubNotFound(f"GitHub has no {path} — check the repo URL")
    if resp.status_code >= 400:
        raise GitHubError(
            f"GitHub returned {resp.status_code} for {path}. "
            "If this keeps happening, check that the repo URL is correct "
            "and that the token has read access to it."
        )
    return resp


def _json(resp: httpx.Response, path: str) -> object:
    """The response body decoded, as a :class:`GitHubError` if it will not.

    Every caller of this module catches ``GitHubError`` and nothing else — the
    scan route turns it into a 502, the autopilot into a "unreachable" status
    that still advances ``last_scanned_at``. A 200 carrying something that is
    not JSON (a proxy's HTML error page, a captive portal, a truncated body)
    raised ``json.JSONDecodeError`` straight through all of that: the route
    answered 500 "Internal server error" for an upstream fault that had a
    perfectly good 502 waiting for it, and the autopilot logged a crash
    traceback and left the project looking never-scanned.
    """
    try:
        return resp.json()
    except ValueError as exc:
        content_type = resp.headers.get("content-type") or "no content-type"
        logger.warning("GitHub non-JSON response for %s: %s", path, content_type)
        raise GitHubError(
            f"GitHub returned an unexpected response for {path}. "
            "If this keeps happening, GitHub may be having issues."
        ) from exc


def _int(value: object) -> int:
    """A count from the payload, or zero. Never an exception.

    ``stargazers_count`` is a number in every response GitHub documents, so an
    ``int()`` straight off the payload reads as safe — but it is parsing
    someone else's JSON, and the one thing this module must not do is fail in a
    way its callers do not catch.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _topics(value: object) -> list[str]:
    """The repo's topics, or an empty list.

    ``list(payload or [])`` looks equivalent and is not: a *string* where the
    list should be iterates into one topic per character, and those go into the
    generator's prompt.
    """
    if not isinstance(value, list):
        return []
    return [topic for topic in value if isinstance(topic, str)]


def _text(value: object, limit: int | None = None) -> str:
    """A string from the payload, optionally clipped. Never an exception.

    Every field on the two dataclasses below is annotated ``str`` and every one
    of them is read straight off somebody else's JSON. ``payload.get(x) or ""``
    defends against the key being absent and against nothing else: a number, a
    list or a nested object all pass it, and what they reach next is a
    ``[:120]`` that raises ``TypeError`` or a database column that will not take
    them. Same reasoning as :func:`_int` and :func:`_topics` — this module's one
    rule is that it does not fail in a way its callers do not catch, and
    ``GitHubError`` is the whole of what they catch.

    *limit* is for the two values that go on to be *stored*, and clipping them
    here rather than at the point of storage is deliberate: both are watermarks,
    compared on the next scan against the value that was stored last time, so
    cutting on the way out would compare a full value against a truncated one
    and never match. See :func:`_release_from_payload`, where that reasoning was
    written down first.
    """
    if not isinstance(value, str):
        return ""
    return value[:limit] if limit else value


def _mapping(value: object) -> dict:
    """A nested object from the payload, or an empty one.

    ``payload.get("commit") or {}`` reads as this and is not. A commits page
    whose entries carry a *string* under ``commit`` — a truncated body, a proxy
    rewriting the response, a future API version — hands the next ``.get()`` a
    ``str``, and ``AttributeError`` is not a ``GitHubError``. It escapes the
    scan route as a 500 rather than the 502 waiting for it, and the autopilot
    logs a crash for a repo that is merely unreadable.
    """
    return value if isinstance(value, dict) else {}


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _commit_from_payload(payload: dict) -> Commit:
    commit = _mapping(payload.get("commit"))
    commit_author = _mapping(commit.get("author"))
    author = _text(commit_author.get("name"))
    # The GitHub *account* is better attribution than the git author when both
    # exist — git configs lie, accounts don't.
    login = _text(_mapping(payload.get("author")).get("login"))
    return Commit(
        # Clipped, because this is the value that becomes
        # ``Project.last_seen_commit_sha`` — see COMMIT_SHA_MAX_LENGTH.
        sha=_text(payload.get("sha"), COMMIT_SHA_MAX_LENGTH),
        message=_text(commit.get("message")),
        author=login or author,
        committed_at=_parse_ts(commit_author.get("date")),
        url=_text(payload.get("html_url")),
    )


def _release_from_payload(payload: dict) -> Release:
    # Truncated here rather than at the two sites that store it as a watermark,
    # because the tag is not only stored — it is *compared* against the stored
    # one to decide whether a release is new. Cutting on the way out would
    # compare a full tag against a truncated watermark, never match, and
    # announce the same release on every scan for as long as the tag existed.
    # Cutting on the way in means both sides of that comparison are the same
    # string. A 120-character prefix collision between two real tags is not a
    # thing that happens.
    tag = _text(payload.get("tag_name"), RELEASE_TAG_MAX_LENGTH)
    return Release(
        tag=tag,
        name=_text(payload.get("name")) or tag,
        body=_text(payload.get("body")),
        published_at=_parse_ts(payload.get("published_at")),
        url=_text(payload.get("html_url")),
        prerelease=bool(payload.get("prerelease")),
    )


def _commits_page(full_name: str, *, per_page: int) -> list[dict]:
    """One page of the commits endpoint, as a list of raw objects.

    The endpoint is asked without a ``sha`` parameter, which is what makes this
    cost the same on a repo with one branch and a repo with four hundred:
    GitHub's default is the repo's *default branch*, and there is no per-branch
    fan-out anywhere in Pulse. That is deliberate rather than incidental — a
    scan that walked every branch would multiply both the request count and the
    editorial noise (a post about somebody's abandoned spike), against an API
    that rate-limits per account. ``tests/test_a_scan_does_not_walk_branches.py``
    pins it, because "no ``sha`` param" is a property that is one well-meant
    patch away from being lost.
    """
    path = f"/repos/{full_name}/commits"
    resp = _get(path, params={"per_page": per_page})
    payload = _json(resp, path)
    if not isinstance(payload, list):
        raise GitHubError(f"Unexpected commits payload for {full_name}")
    # A list whose entries are not objects is not a commits page. Dropping the
    # entry rather than raising keeps one malformed element from losing the
    # ninety-nine good ones either side of it.
    return [item for item in payload if isinstance(item, dict)]


def fetch_commits(full_name: str, *, since_sha: str | None = None) -> list[Commit]:
    """Commits newest-first, truncated at *since_sha* if it appears.

    When the watermark isn't in the page — the repo has moved more than
    ``_COMMIT_PAGE_SIZE`` commits, or the branch was rewritten — the whole page
    is returned. Over-reporting is the right failure here: it means the post
    says "a lot has changed", not that a change is missed.

    **The common case is answered with a one-commit page.** A poll loop asks
    this question every ``autopilot_scan_interval_seconds`` and the honest
    answer is almost always "nothing new" — the watermark *is* HEAD. That answer
    was costing a hundred fully-populated commit objects every time, per project,
    for the whole day: each entry carries the commit message, both author and
    committer blocks, the tree, and four URLs, so a no-op scan was transferring
    on the order of a hundred kilobytes to discover a single sha it already had.

    So the sha is checked first, with ``per_page=1``, and the full page is only
    fetched when the top of the branch has actually moved. The trade is one
    extra round-trip on the scans that *do* have news, against a page saved on
    the many that do not — and GitHub's rate limit counts requests, not bytes,
    so this is not free. It is worth it because the ratio is not close: an
    active repo moves a handful of times a day against a scan every hour, and
    the saved request on a no-news scan is the same 1 the extra one costs, so
    the request count is unchanged in the worst case and the bytes are not.

    Skipped entirely when there is no watermark — a first scan has nothing to
    compare against, and the pre-flight would be a wasted request.
    """
    if since_sha:
        head = _commits_page(full_name, per_page=1)
        # An empty page means an empty repo, which has no commits to report and
        # no HEAD to compare. Falling through to the full fetch would ask the
        # same endpoint the same question again for the same empty answer.
        if not head:
            return []
        if _commit_from_payload(head[0]).sha == since_sha:
            return []

    commits: list[Commit] = []
    for item in _commits_page(full_name, per_page=_COMMIT_PAGE_SIZE):
        commit = _commit_from_payload(item)
        if since_sha and commit.sha == since_sha:
            break
        commits.append(commit)
    return commits


def fetch_latest_release(full_name: str) -> Release | None:
    """The most recent published release, or ``None`` if the repo has none.

    Uses the releases list rather than ``/releases/latest`` so a repo whose
    newest release is a prerelease still reports it — a beta is exactly the kind
    of thing worth an announcement.
    """
    path = f"/repos/{full_name}/releases"
    try:
        resp = _get(path, params={"per_page": 1})
    except GitHubNotFound:
        # A repo with releases disabled 404s here. That is not a failure.
        return None
    payload = _json(resp, path)
    if not isinstance(payload, list) or not payload:
        return None
    if not isinstance(payload[0], dict):
        return None
    return _release_from_payload(payload[0])


#: How much of a README is read. Generous — the fact-check vocabulary wants the
#: feature list, which is usually well past the badges — and bounded, because
#: this is somebody else's file and a monorepo README runs to tens of thousands
#: of words that all end up in a set.
README_MAX_CHARS = 200_000


def fetch_readme(full_name: str) -> str:
    """The repo's README as text, or ``""`` if it has none.

    The one thing Pulse can read that says what a project actually *does* in
    the project's own words, which is why
    :mod:`app.services.factcheck` grounds product claims against it.

    Returns ``""`` rather than raising for the two ways this legitimately comes
    back empty — a repo with no README (404), and a payload in an encoding
    GitHub has not documented — because the caller is a *gate*, and a gate that
    raises where it meant to abstain fails the piece instead of passing it. A
    repo that cannot be read at all still raises :class:`GitHubError`, which is
    what every caller of this module already catches.
    """
    path = f"/repos/{full_name}/readme"
    try:
        payload = _json(_get(path), path)
    except GitHubNotFound:
        return ""
    if not isinstance(payload, dict):
        return ""

    content = _text(payload.get("content"))
    if _text(payload.get("encoding")) != "base64":
        # The API documents base64 and nothing else here, but it honours an
        # Accept header that returns raw text, and a proxy in front of it may
        # have done exactly that. Whatever came back as a string is closer to
        # the README than an empty vocabulary is.
        return content[:README_MAX_CHARS]
    try:
        raw = base64.b64decode(content)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", "replace")[:README_MAX_CHARS]


def fetch_activity(
    full_name: str,
    *,
    since_sha: str | None = None,
    since_tag: str | None = None,
) -> RepoActivity:
    """Everything new in *full_name* since the given watermarks.

    Raises :class:`GitHubError` if the repo can't be read at all;
    a missing releases endpoint is tolerated.
    """
    path = f"/repos/{full_name}"
    repo = _json(_get(path), path)
    if not isinstance(repo, dict):
        raise GitHubError(f"Unexpected repo payload for {full_name}")

    commits = fetch_commits(full_name, since_sha=since_sha)
    release = fetch_latest_release(full_name)
    new_release = release if (release and release.tag != since_tag) else None

    # HEAD is the first commit of the *unfiltered* page. When `since_sha` was
    # already HEAD the list comes back empty, so fall back to the watermark
    # rather than clearing it — clearing would make the next scan re-report
    # everything.
    head_sha = commits[0].sha if commits else since_sha

    activity = RepoActivity(
        full_name=full_name,
        new_commits=commits,
        new_release=new_release,
        description=str(repo.get("description") or ""),
        topics=_topics(repo.get("topics")),
        stars=_int(repo.get("stargazers_count")),
        head_sha=head_sha,
        latest_tag=release.tag if release else since_tag,
    )
    logger.info(
        "github scan %s: %d new commits, release=%s",
        full_name,
        len(activity.new_commits),
        new_release.tag if new_release else "none",
    )
    return activity


__all__ = [
    "Commit",
    "GitHubError",
    "GitHubNotFound",
    "GitHubRateLimited",
    "Release",
    "RepoActivity",
    "Throttle",
    "fetch_activity",
    "fetch_commits",
    "fetch_latest_release",
    "fetch_readme",
    "throttle_reason",
]

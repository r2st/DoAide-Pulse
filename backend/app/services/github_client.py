"""Read-only GitHub client: what has shipped in a project since we last looked.

Only three endpoints matter here — repo metadata, recent commits, and releases —
so this is plain httpx rather than a dependency on PyGithub.

Two things this module deliberately does *not* do:

* **Authenticate as the user.** A token is a global setting, not per project.
  Herald reads public repos to write about them; it never pushes.
* **Fetch full history.** The autopilot cares about the delta since a stored
  watermark, so a single page of commits is enough. A repo that has moved more
  than a page since the last scan gets "100+ commits", which is the same
  editorial signal as the exact number.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: One page of commits. Enough for any sane scan interval; see module docstring.
_COMMIT_PAGE_SIZE = 100


class GitHubError(RuntimeError):
    """A GitHub request failed, or the repo isn't reachable."""


class GitHubRateLimited(GitHubError):
    """The API refused us for rate-limit reasons.

    Separated from the generic error because the caller's response differs: a
    404 means "stop scanning this project", a rate limit means "come back
    later", and conflating them would make an unauthenticated Herald quietly
    disable every project it watches.
    """


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
        return bool(self.new_commits or self.new_release)


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Herald/0.1 (+https://github.com/r2st/Herald)",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


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
        raise GitHubError(f"GitHub request to {path} failed: {exc}") from exc

    # 403 and 429 both mean rate-limited; 403 also covers "private repo, no
    # token", which the remaining-quota header distinguishes.
    if resp.status_code in (403, 429):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0" or resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset", "?")
            raise GitHubRateLimited(
                f"GitHub rate limit exhausted (resets at {reset}). "
                "Set GITHUB_TOKEN to raise the ceiling from 60 to 5000 req/hour."
            )
        raise GitHubError(f"GitHub denied access to {path} (403) — private repo?")
    if resp.status_code == 404:
        raise GitHubNotFound(f"GitHub has no {path} — check the repo URL")
    if resp.status_code >= 400:
        raise GitHubError(f"GitHub returned {resp.status_code} for {path}")
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
        raise GitHubError(
            f"GitHub returned a non-JSON body for {path} ({content_type})"
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


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _commit_from_payload(payload: dict) -> Commit:
    commit = payload.get("commit") or {}
    author = (commit.get("author") or {}).get("name") or ""
    # The GitHub *account* is better attribution than the git author when both
    # exist — git configs lie, accounts don't.
    login = (payload.get("author") or {}).get("login")
    return Commit(
        sha=payload.get("sha") or "",
        message=commit.get("message") or "",
        author=login or author,
        committed_at=_parse_ts((commit.get("author") or {}).get("date")),
        url=payload.get("html_url") or "",
    )


def _release_from_payload(payload: dict) -> Release:
    return Release(
        tag=payload.get("tag_name") or "",
        name=payload.get("name") or payload.get("tag_name") or "",
        body=payload.get("body") or "",
        published_at=_parse_ts(payload.get("published_at")),
        url=payload.get("html_url") or "",
        prerelease=bool(payload.get("prerelease")),
    )


def fetch_commits(full_name: str, *, since_sha: str | None = None) -> list[Commit]:
    """Commits newest-first, truncated at *since_sha* if it appears.

    When the watermark isn't in the page — the repo has moved more than
    ``_COMMIT_PAGE_SIZE`` commits, or the branch was rewritten — the whole page
    is returned. Over-reporting is the right failure here: it means the post
    says "a lot has changed", not that a change is missed.
    """
    path = f"/repos/{full_name}/commits"
    resp = _get(path, params={"per_page": _COMMIT_PAGE_SIZE})
    payload = _json(resp, path)
    if not isinstance(payload, list):
        raise GitHubError(f"Unexpected commits payload for {full_name}")

    commits: list[Commit] = []
    for item in payload:
        # A list whose entries are not objects is not a commits page. Skipping
        # the entry rather than raising keeps one malformed element from losing
        # the ninety-nine good ones either side of it.
        if not isinstance(item, dict):
            continue
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
    "fetch_activity",
    "fetch_commits",
    "fetch_latest_release",
]

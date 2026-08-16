"""Git-based publishing — commit the post to a repository.

The destination most of Herald's audience actually runs. An Astro, Hugo, Eleventy
or Next blog is a directory of Markdown files with front matter; publishing to it
is a commit, not an API integration. That makes this the only destination with
no OAuth, no application review, no rate tier and no platform that can withdraw
access — and the only one where the published artefact is a file the user owns.

It should normally be the project's **canonical platform**: the copy on your own
domain is the original, and Dev.to and Medium are the syndication. Herald's
canonical machinery does the rest.

Three things worth knowing:

* **Drafts are honest here.** Every static-site generator understands
  ``draft: true`` in front matter, so ``as_draft`` writes the file with the flag
  set rather than refusing. The post lands in the repo and stays out of the
  build.
* **Committing is idempotent by path.** Re-publishing the same piece overwrites
  its own file rather than adding a second one, because the contents API is
  keyed on the path and Herald sends the existing blob's ``sha``.
* **The published URL is computed, not returned.** Git hands back a commit, not
  a page — the page appears when CI finishes. ``site_url`` plus the slug is the
  address that will exist; without it the commit URL is returned instead, which
  is honest about what actually happened.

API: https://docs.github.com/en/rest/repos/contents
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.models.publication import Platform
from app.services import github_client, seo, social_cards
from app.services.publishers import formatting
from app.services.publishers.base import (
    PREFLIGHT_ERROR,
    PREFLIGHT_WARNING,
    Adapter,
    CredentialError,
    CredentialField,
    PreflightFinding,
    PublishError,
    PublishRequest,
    PublishResult,
    RateLimited,
)

logger = logging.getLogger(__name__)

#: Where the file goes, unless the user says otherwise. Astro's content
#: collections and Eleventy both read this path; Hugo wants `content/posts`.
_DEFAULT_PATH = "src/content/blog/{slug}.md"

_API = "https://api.github.com"

#: `owner/repo`, the only form the contents API takes.
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

#: Path shapes that would escape the repo, the checkout, or the URL.
#:
#: The first three are the filesystem ones — a leading ``/`` or ``~`` anchors
#: somewhere other than the repo root, and ``..`` climbs out of it.
#:
#: The rest are there because this path is not only a path. It is interpolated
#: straight into ``{_API}/repos/{repo}/contents/{path}``, and httpx leaves every
#: one of these characters alone when it builds the URL — verified, not assumed:
#:
#: * ``?`` and ``#`` are not filename characters at that point, they are the
#:   query and fragment delimiters. ``blog/{slug}.md?draft`` does not commit to
#:   a file with a question mark in its name; it commits to ``blog/<slug>.md``
#:   and hands GitHub a parameter. The file lands somewhere the user did not
#:   ask for and nothing reports it, which is the same failure the ``..`` check
#:   above exists to prevent, reached through the URL instead of the tree.
#: * ``%`` starts a percent-escape that survives to GitHub intact, so ``%2e%2e``
#:   is a ``..`` this regex would never see. Whether the far end decodes it
#:   before resolving the path is GitHub's business and not something to bet a
#:   write on.
#: * ``\`` passes through unencoded too, and is a separator on the checkout
#:   that every ``/``-shaped rule here is blind to.
#: * Control characters, including the newline, have no business in a filename
#:   and every business in a smuggled request line.
#:
#: Rejecting rather than escaping: a legitimate post path contains none of
#: these, so the template that carries one is a mistake to report, not an input
#: to sanitise into something the user did not write either.
_UNSAFE_PATH = re.compile(r"(^/)|(\.\.)|(^~)|([?#%\\])|([\x00-\x1f\x7f])")

#: The per-entry fields carried through a sitemap rewrite. ``loc`` is renamed to
#: ``url`` on the way in because that is what ``seo.build_sitemap_xml`` reads.
_SITEMAP_FIELDS = ("loc", "lastmod", "changefreq", "priority")


def _localname(tag: object) -> str:
    """An element's name without its namespace.

    Sitemaps in the wild are inconsistently namespaced — the schema says to
    declare ``sitemaps.org/schemas/sitemap/0.9`` and plenty of hand-written and
    generator-written ones simply do not. Matching on a qualified name treats
    those as containing no URLs at all, which for a function that rewrites the
    file means silently replacing it with a single entry.
    """
    name = str(tag)
    return name.rsplit("}", 1)[-1] if name.startswith("{") else name


def _parse_urlset(raw: str) -> list[dict[str, str]] | None:
    """Existing sitemap entries, or ``None`` if this file must not be rewritten.

    ``None`` is the important return. A ``<sitemapindex>`` lists other sitemaps
    rather than pages, and its ``<loc>`` elements read as perfectly ordinary
    URLs — rewriting one as a ``<urlset>`` turns a working index into a page
    list pointing at sitemaps. Anything that is not a ``<urlset>``, including a
    file that does not parse, is left where it is.

    Entries keep ``lastmod``, ``changefreq`` and ``priority``. Reading only the
    ``<loc>`` and rebuilding from that loses every other field in the file, and
    ``lastmod`` is the one a crawler uses to decide what to re-fetch.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if _localname(root.tag) != "urlset":
        return None

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for url_el in root:
        if _localname(url_el.tag) != "url":
            continue
        entry: dict[str, str] = {}
        for child in url_el:
            field_name = _localname(child.tag)
            if field_name in _SITEMAP_FIELDS and child.text and child.text.strip():
                entry["url" if field_name == "loc" else field_name] = child.text.strip()
        loc = entry.get("url")
        # First occurrence wins, as it did when this was a set. Document order
        # is kept rather than sorted: the file belongs to the user, and
        # reordering it on every publish makes the diff unreadable.
        if loc and loc not in seen:
            seen.add(loc)
            entries.append(entry)
    return entries


class GitAdapter(Adapter):
    platform = Platform.GIT
    display_name = "Git repository"
    implemented = True
    # A repo has no engagement to report. Whatever analytics the built site runs
    # is the source of truth, which is exactly what the UTM tagging feeds.
    supports_metrics = False
    utm_medium = "owned"
    # The only destination on a domain the user controls, which is what makes it
    # the original when no canonical platform is named. See
    # `Adapter.owns_domain`.
    owns_domain = True
    caveat = (
        "Commits Markdown to a branch. Leave the token blank to use Herald's "
        "GITHUB_TOKEN, which needs write access to the repo — the one used for "
        "repo scanning is usually read-only."
    )
    credential_fields = (
        CredentialField(
            key="repo",
            label="Repository",
            help_text="owner/name — the repo your blog is built from.",
            secret=False,
        ),
        CredentialField(
            key="branch",
            label="Branch",
            help_text="Defaults to the repository's default branch.",
            secret=False,
            required=False,
        ),
        CredentialField(
            key="path_template",
            label="Path",
            help_text=f"Where posts live. Defaults to {_DEFAULT_PATH}",
            secret=False,
            required=False,
        ),
        CredentialField(
            key="site_url",
            label="Site URL",
            help_text=(
                "e.g. https://blog.example.com — used to work out the published "
                "address. Leave blank to record the commit instead."
            ),
            secret=False,
            required=False,
        ),
        CredentialField(
            key="token",
            label="Access token",
            help_text=(
                "A token with contents:write on this repo. Blank falls back to "
                "Herald's GITHUB_TOKEN."
            ),
            required=False,
        ),
    )

    # -- credentials -------------------------------------------------------- #

    def _repo(self, credentials: dict[str, Any]) -> str:
        (repo,) = self._require(credentials, "repo")
        repo = repo.strip().removesuffix(".git").strip("/")
        # Accept a pasted URL as well as owner/name — both are what people have
        # in the clipboard.
        if "github.com" in repo:
            repo = repo.split("github.com", 1)[1].lstrip(":/")
        if not _REPO_RE.match(repo):
            raise CredentialError(
                f"'{credentials.get('repo')}' is not an owner/name repository"
            )
        return repo

    def _token(self, credentials: dict[str, Any]) -> str:
        token = str(credentials.get("token") or "").strip() or settings.github_token
        if not token:
            raise CredentialError(
                "Git publishing needs a token with write access — set one on the "
                "connection, or configure GITHUB_TOKEN."
            )
        return token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    # -- the file ----------------------------------------------------------- #

    def path_for(self, request: PublishRequest, credentials: dict[str, Any]) -> str:
        """Where in the repo this piece is written.

        The template takes ``{slug}``, ``{year}``, ``{month}`` and ``{day}``,
        which covers the date-partitioned layouts (Jekyll's ``_posts/`` in
        particular) as well as the flat ones.
        """
        template = str(credentials.get("path_template") or "").strip() or _DEFAULT_PATH
        now = datetime.now(UTC)
        # Content always carries a slug; the fallback is for a request built by
        # hand, where writing to `post.md` beats writing to `.md`.
        slug = (request.slug or "").strip() or "post"
        try:
            path = template.format(
                slug=slug,
                year=f"{now:%Y}",
                month=f"{now:%m}",
                day=f"{now:%d}",
            )
        except (KeyError, IndexError) as exc:
            raise CredentialError(
                f"'{template}' uses a placeholder this adapter does not provide: {exc}"
            ) from exc

        # Check first, normalise second. Trimming leading "./" from a path that
        # begins "../" would turn a traversal into a plausible-looking relative
        # path and commit it somewhere the user did not ask for.
        #
        # Checked *after* formatting, so a slug carrying one of these is caught
        # as well as a template that does. Nothing should be able to produce one
        # — ``app.models.project.slugify`` reduces a title to ``[a-z0-9-]`` —
        # but the guard is one line either way and the slug is the half that
        # arrives from a user rather than from a settings form.
        path = path.strip()
        if _UNSAFE_PATH.search(path):
            raise CredentialError(
                f"'{template}' resolves to '{path}', which is not a path inside "
                "the repository. Paths are relative to the repo root and cannot "
                "contain '..', '?', '#', '%' or a backslash."
            )
        if path.endswith("/"):
            raise CredentialError(f"'{template}' resolves to a directory, not a file")
        return path.removeprefix("./")

    def preflight(self, request: PublishRequest) -> list[PreflightFinding]:
        """Whether this piece can become a file in somebody's repo.

        The Git destination writes Markdown with YAML front matter to a path
        built from the slug, so the two things that fail here are a piece with
        no slug — there is no filename to write — and a body that is not
        Markdown so much as nothing at all.

        The path itself is *not* checked, deliberately: it comes from the
        connection's ``path_template``, and this method has no credentials by
        contract. ``path_for`` validates it at publish time, where the template
        actually exists.
        """
        findings: list[PreflightFinding] = []

        if not (request.slug or "").strip():
            findings.append(
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    "A Git post is a file named after the slug, and this piece "
                    "has no slug.",
                )
            )
        if not request.body_markdown.strip():
            findings.append(
                PreflightFinding(
                    PREFLIGHT_ERROR,
                    "A Git post is its Markdown body, and this piece has none.",
                )
            )
        if not (request.meta_description or request.excerpt or "").strip():
            # Not an error: the file commits fine without one. But `description`
            # is the front-matter key every static-site generator reads for the
            # page's meta description and its card, so an empty one publishes a
            # post that looks broken in a search result and unfurls as a grey
            # rectangle.
            findings.append(
                PreflightFinding(
                    PREFLIGHT_WARNING,
                    "No meta description or excerpt, so the front matter's "
                    "`description` will be empty — most themes use it for the "
                    "page description and the social card.",
                )
            )
        return findings

    def build_file(self, request: PublishRequest) -> str:
        """Front matter plus the Markdown body, as it will land on disk.

        The field names are the ones the static-site generators agree on:
        ``title``, ``description``, ``date``, ``tags``, ``draft``, and
        ``canonicalURL``/``image`` for the two that differ only in casing
        between ecosystems. Extra keys are cheap and a missing one is not.
        """
        matter: dict[str, object] = {
            "title": request.title,
            "description": request.meta_description or request.excerpt,
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "tags": formatting.normalize_tags(request.tags, limit=8, allow_spaces=True),
        }
        if request.as_draft:
            matter["draft"] = True
        if request.cover_image_url:
            matter["image"] = request.cover_image_url
        # Only when the original is somewhere *else*: a canonical pointing at
        # the page it is written on is noise, and this destination is normally
        # the original.
        if request.canonical_url:
            matter["canonicalURL"] = request.canonical_url
        # Keywords help static-site generators (Astro, Hugo, Next.js) populate
        # <meta name="keywords"> and JSON-LD. The focus keyword is the primary
        # SEO target for this piece.
        if request.keywords:
            matter["keywords"] = request.keywords
        if request.focus_keyword:
            matter["focusKeyword"] = request.focus_keyword

        # Open Graph / Twitter Card keys, so the link unfurls as a real card
        # instead of a grey rectangle. These go in the front matter and not in
        # the body: a crawler reads <head>, and a body-level <meta> is ignored
        # by every one of them. The theme is what puts them in the head — which
        # is why this emits the camel-cased keys the common themes look for
        # rather than raw tags. See app.services.social_cards.
        matter.update(
            social_cards.front_matter_keys(
                social_cards.meta_tags(
                    title=request.title,
                    url=request.canonical_url or "",
                    meta_description=request.meta_description,
                    excerpt=request.excerpt,
                    body_markdown=request.body_markdown,
                    cover_image_url=request.cover_image_url,
                    site_name=request.project_name,
                    tags=request.tags,
                )
            )
        )

        # Reading time and word count — most blog themes display these.
        plain = seo.strip_markdown(request.body_markdown)
        word_count = len(plain.split())
        reading_time = max(1, round(word_count / 238))  # avg adult: ~238 wpm
        matter["readingTime"] = reading_time
        matter["wordCount"] = word_count

        body = request.body_markdown.strip()

        # Append JSON-LD structured data so static-site generators that don't
        # auto-generate it still get schema.org Article markup in the page.
        json_ld = seo.build_json_ld(
            title=request.title,
            body_markdown=body,
            meta_description=request.meta_description or request.excerpt,
            url=request.canonical_url or "",
            cover_image_url=request.cover_image_url,
            keywords=request.keywords or None,
            author_name=request.project_name,
            publisher_name=request.project_name,
        )
        body += seo.json_ld_script_tag(json_ld)

        return f"{formatting.front_matter(matter)}\n\n{body}\n"

    # -- the adapter -------------------------------------------------------- #

    def _classify(self, resp: httpx.Response) -> PublishError | None:
        """Tell GitHub throttling us apart from GitHub refusing us.

        Overrides the classification half of ``_translate`` rather than
        ``_translate`` itself, so whatever this returns still gets the status
        stamped on it by the base class — see
        :attr:`~app.services.publishers.base.PublishError.status_code`.

        The base class reads every 403 as a rejected credential, which is true
        of every other destination Herald publishes to and false of this one.
        GitHub reports its *secondary* rate limit — the burst limit, tripped by
        a sweep publishing several pieces at once — as a **403** carrying
        ``Retry-After``, with the hourly quota still nearly untouched.

        Left to the base class that 403 became a
        :class:`~app.services.publishers.base.CredentialError`, and a
        credential error is terminal by design: no retry, publication failed
        for good, and the piece marked ``failed`` with "GitHub rejected the
        credentials" against a token that was working a second earlier and
        would work a minute later. The user's repair for that message is to go
        and reissue a PAT that was never the problem.

        As a :class:`~app.services.publishers.base.RateLimited` it instead
        reaches :func:`app.services.publishing_service._defer`, which parks the
        row until the time GitHub actually named and lets it publish itself.

        ``github_client.throttle_reason`` is the same discrimination the
        autopilot's scan path already had to learn; the note there records the
        matching mis-read on that side.
        """
        if resp.status_code in (403, 429):
            throttle = github_client.throttle_reason(resp)
            if throttle is not None:
                return RateLimited(
                    self._throttle_message(throttle),
                    retry_after=throttle.retry_after,
                )
        return super()._classify(resp)

    @staticmethod
    def _throttle_message(throttle: github_client.Throttle) -> str:
        """What to tell the user, in terms of the token *they* control.

        ``github_client`` words the same facts for the autopilot, where the
        token is Herald's own ``GITHUB_TOKEN`` and "set one to raise the
        ceiling" is useful advice. Here the token is the user's PAT and that
        advice is noise: nothing they can set changes this limit, and the row
        is already parked, so the honest message says who is waiting and for
        how long.
        """
        if throttle.secondary:
            if throttle.retry_after is not None:
                return (
                    "GitHub is throttling this account (secondary rate limit); "
                    f"it asked us to wait {throttle.retry_after}s. The commit is "
                    "queued and will go out after that."
                )
            return (
                "GitHub is throttling this account (secondary rate limit). "
                "The commit is queued and will be retried shortly."
            )
        return (
            f"GitHub's hourly rate limit for this token is spent (resets at "
            f"{throttle.reset_at}). The commit is queued until then."
        )

    def verify(self, credentials: dict[str, Any]) -> str:
        """Return the repository, refusing a token that cannot write to it.

        Push permission is checked here because a read-only token reads a public
        repo perfectly well and then fails at the first commit — which is a bad
        time to find out.
        """
        repo = self._repo(credentials)
        resp = self._request(
            "GET", f"{_API}/repos/{repo}", headers=self._headers(self._token(credentials))
        )
        data = self._json_object(resp)
        # A read-only token reads a public repo perfectly well and then fails at
        # the first commit, which is a bad time to find out.
        if not (data.get("permissions") or {}).get("push"):
            raise CredentialError(
                f"The token can read {repo} but not write to it. It needs "
                "contents:write."
            )
        return data.get("full_name") or repo

    def _existing_sha(
        self, repo: str, path: str, branch: str, token: str
    ) -> str | None:
        """The blob sha of the file already at *path*, if there is one.

        The contents API needs it to overwrite rather than reject, and a 404
        here is the ordinary "this is a new post" case, not a failure.

        **A 404 and only a 404.** "I was not allowed to look" is not "there is
        nothing there", and the difference decides whether the commit below
        carries a ``sha``. Read as "new post", a failed lookup makes ``publish``
        send a create over a live path, which the contents API refuses — so a
        correction to a published piece fails as a 422 naming a file the user
        can see perfectly well in their own repo, and the retry that would have
        fixed it never happens because the lookup fails the same way each time.

        That argument was first made for a throttle, which is a ``RateLimited``,
        and it is not about throttling: a 500 from GitHub, a gateway timing out
        mid-read and a connect error are all the same statement. Each one used
        to take the swallow, because each one is a bare ``PublishError`` and the
        carve-outs were written as a list of the two types that had been seen.
        Asking the status instead makes the ordinary case the narrow one, which
        is the way round it should have been: a new destination for this failure
        does not need a new ``except``.
        """
        params = {"ref": branch} if branch else None
        try:
            resp = self._request(
                "GET",
                f"{_API}/repos/{repo}/contents/{path}",
                headers=self._headers(token),
                params=params,
            )
        except PublishError as exc:
            if exc.status_code == 404:
                return None
            # 401/403 (a rejected token), a throttle, a 5xx, a timeout, a
            # connect error — none of them say the file is not there.
            raise
        data = self._json(resp)
        # A directory comes back as a list, which means the path is unusable —
        # a more useful answer than the generic shape error below, so it is
        # checked first rather than left to ``_json_object``.
        if isinstance(data, list):
            raise PublishError(f"{path} is a directory in {repo}, not a file")
        return self._json_object(resp).get("sha")

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
        """Commit the rendered file, creating it or updating it in place.

        An existing path is updated rather than refused: re-publishing a piece is
        how a correction reaches a repo, and the blob sha is passed back so the
        commit is rejected if the file changed underneath.
        """
        repo = self._repo(credentials)
        token = self._token(credentials)
        branch = str(credentials.get("branch") or "").strip()
        path = self.path_for(request, credentials)
        contents = self.build_file(request)

        payload: dict[str, Any] = {
            "message": f"content: {request.title}",
            "content": base64.b64encode(contents.encode("utf-8")).decode("ascii"),
        }
        if branch:
            payload["branch"] = branch
        sha = self._existing_sha(repo, path, branch, token)
        if sha:
            payload["sha"] = sha

        resp = self._request(
            "PUT",
            f"{_API}/repos/{repo}/contents/{path}",
            headers=self._headers(token),
            json_body=payload,
        )
        data = self._json_object(resp)
        commit = data.get("commit") or {}
        commit_sha = commit.get("sha")
        if not commit_sha:
            raise PublishError(f"GitHub returned no commit for {path}: {data}")

        published_url = self._published_url(request, credentials, data, commit)

        # Best-effort sitemap update: add the new post's URL and re-commit
        # sitemap.xml.  Failures here must not block the publish result.
        #
        # Gated on the published URL actually being *on the site*, not merely on
        # a site being configured. `_published_url` falls back to the GitHub
        # commit address whenever it cannot compute a page address — a slugless
        # request is enough — and a sitemap is a list of pages on this domain.
        # A github.com URL in it is a crawl error the user did not write.
        site = str(credentials.get("site_url") or "").strip().rstrip("/")
        if site and not request.as_draft and published_url.startswith(f"{site}/"):
            try:
                self._update_sitemap(
                    repo=repo,
                    branch=branch,
                    token=token,
                    headers=self._headers(token),
                    post_url=published_url,
                )
            except Exception:
                logger.info("sitemap update skipped for %s", repo, exc_info=True)

        return PublishResult(
            external_id=commit_sha,
            external_url=published_url,
            extra={"path": path, "branch": branch or "default", "repo": repo},
        )

    def _update_sitemap(
        self,
        *,
        repo: str,
        branch: str,
        token: str,
        headers: dict[str, str],
        post_url: str,
    ) -> None:
        """Add *post_url* to ``sitemap.xml`` in the repo, creating it if absent.

        The sitemap is fetched, parsed, de-duplicated, and re-committed in a
        single PUT. If the file does not exist yet it is created with just
        this one entry. This is best-effort: the caller catches any exception.

        This rewrites a file in a repository the user owns, so the read has to
        be as careful as the write: anything that does not parse as a
        ``<urlset>`` is left exactly where it is rather than replaced by a
        one-entry sitemap. See :func:`_parse_urlset`.
        """
        sitemap_path = "public/sitemap.xml"
        params = {"ref": branch} if branch else None

        # Fetch existing sitemap (if any).
        existing_sha: str | None = None
        entries: list[dict[str, str]] = []
        try:
            resp = self._request(
                "GET",
                f"{_API}/repos/{repo}/contents/{sitemap_path}",
                headers=headers,
                params=params,
            )
            data = self._json(resp)
            if isinstance(data, dict) and data.get("content"):
                raw = base64.b64decode(data["content"]).decode("utf-8")
                parsed = _parse_urlset(raw)
                if parsed is None:
                    logger.info(
                        "%s: %s is not a <urlset> — left alone", repo, sitemap_path
                    )
                    return
                existing_sha = data.get("sha")
                entries = parsed
        except PublishError:
            pass  # 404 — sitemap does not exist yet.

        # Re-publishing a piece is exactly when `lastmod` earns its keep, so an
        # entry that is already here gets its date moved rather than skipped.
        # Only an entry already stamped today has genuinely nothing to say, and
        # that is the case worth avoiding a commit for.
        now_iso = datetime.now(UTC).strftime("%Y-%m-%d")
        for entry in entries:
            if entry["url"] == post_url:
                if entry.get("lastmod") == now_iso:
                    return
                entry["lastmod"] = now_iso
                break
        else:
            entries.append({"url": post_url, "lastmod": now_iso})

        sitemap_xml = seo.build_sitemap_xml(entries)

        payload: dict[str, Any] = {
            "message": "chore: update sitemap.xml",
            "content": base64.b64encode(sitemap_xml.encode("utf-8")).decode("ascii"),
        }
        if branch:
            payload["branch"] = branch
        if existing_sha:
            payload["sha"] = existing_sha

        self._request(
            "PUT",
            f"{_API}/repos/{repo}/contents/{sitemap_path}",
            headers=headers,
            json_body=payload,
        )
        logger.info("sitemap.xml updated in %s with %s", repo, post_url)

    def _published_url(
        self,
        request: PublishRequest,
        credentials: dict[str, Any],
        data: dict[str, Any],
        commit: dict[str, Any],
    ) -> str:
        """Where the post will be readable once the site rebuilds.

        A draft has no address and never will until the flag comes off, so it
        reports the file in the repo — which is also what happens when no site
        URL is configured. Both are "the commit landed", stated accurately
        rather than as a URL that would 404.
        """
        site = str(credentials.get("site_url") or "").strip().rstrip("/")
        if site and not request.as_draft and request.slug:
            return f"{site}/{request.slug}"
        return (data.get("content") or {}).get("html_url") or commit.get("html_url", "")


__all__ = ["GitAdapter"]

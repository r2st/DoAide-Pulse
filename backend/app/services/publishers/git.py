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

from app.config import settings
from app.models.publication import Platform
from app.services import seo, social_cards
from app.services.publishers import formatting
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    CredentialField,
    PublishError,
    PublishRequest,
    PublishResult,
)

logger = logging.getLogger(__name__)

#: Where the file goes, unless the user says otherwise. Astro's content
#: collections and Eleventy both read this path; Hugo wants `content/posts`.
_DEFAULT_PATH = "src/content/blog/{slug}.md"

_API = "https://api.github.com"

#: `owner/repo`, the only form the contents API takes.
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

#: Path components that would escape the repo or the checkout.
_UNSAFE_PATH = re.compile(r"(^/)|(\.\.)|(^~)")


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
        path = path.strip()
        if _UNSAFE_PATH.search(path):
            raise CredentialError(f"'{template}' resolves outside the repository")
        return path.removeprefix("./")

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

    def verify(self, credentials: dict[str, Any]) -> str:
        repo = self._repo(credentials)
        resp = self._request(
            "GET", f"{_API}/repos/{repo}", headers=self._headers(self._token(credentials))
        )
        data = resp.json() or {}
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
        """
        params = {"ref": branch} if branch else None
        try:
            resp = self._request(
                "GET",
                f"{_API}/repos/{repo}/contents/{path}",
                headers=self._headers(token),
                params=params,
            )
        except CredentialError:
            raise  # 401/403 must surface, not be silenced as "file not found"
        except PublishError:
            return None
        data = resp.json()
        # A directory comes back as a list, which means the path is unusable.
        if isinstance(data, list):
            raise PublishError(f"{path} is a directory in {repo}, not a file")
        return data.get("sha")

    def publish(self, request: PublishRequest, credentials: dict[str, Any]) -> PublishResult:
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
        data = resp.json() or {}
        commit = data.get("commit") or {}
        commit_sha = commit.get("sha")
        if not commit_sha:
            raise PublishError(f"GitHub returned no commit for {path}: {data}")

        published_url = self._published_url(request, credentials, data, commit)

        # Best-effort sitemap update: add the new post's URL and re-commit
        # sitemap.xml.  Failures here must not block the publish result.
        site = str(credentials.get("site_url") or "").strip().rstrip("/")
        if site and not request.as_draft:
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
        """
        sitemap_path = "public/sitemap.xml"
        params = {"ref": branch} if branch else None

        # Fetch existing sitemap (if any).
        existing_sha: str | None = None
        existing_urls: set[str] = set()
        try:
            resp = self._request(
                "GET",
                f"{_API}/repos/{repo}/contents/{sitemap_path}",
                headers=headers,
                params=params,
            )
            data = resp.json()
            if isinstance(data, dict) and data.get("content"):
                existing_sha = data.get("sha")
                raw = base64.b64decode(data["content"]).decode("utf-8")
                # Parse existing URLs out of the XML.
                import xml.etree.ElementTree as ET

                root = ET.fromstring(raw)
                ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                for loc in root.findall(".//sm:loc", ns):
                    if loc.text:
                        existing_urls.add(loc.text.strip())
        except PublishError:
            pass  # 404 — sitemap does not exist yet.

        if post_url in existing_urls:
            return  # Already present, nothing to do.

        # Build updated sitemap.
        now_iso = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = [{"url": u} for u in sorted(existing_urls)]
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

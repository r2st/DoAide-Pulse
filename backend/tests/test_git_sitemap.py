"""The sitemap the Git adapter re-commits into the user's own repository.

``GitAdapter.publish`` appends the new post to ``public/sitemap.xml`` and
commits it back. That makes this the one place Herald rewrites a file it did
not write, in a repository somebody else owns, so the read has to be as careful
as the write — and it was not:

* every existing entry was rebuilt from its ``<loc>`` alone, dropping the
  ``lastmod`` a crawler uses to decide what to re-fetch;
* re-publishing a piece left its ``lastmod`` at the original date, which is
  exactly the moment the field is supposed to move;
* a sitemap without the namespace declaration parsed as containing no URLs and
  was replaced wholesale by a one-entry file;
* a ``<sitemapindex>`` parsed as if its child-sitemap ``<loc>``\\s were pages,
  and was rewritten as a ``<urlset>`` listing them;
* a slugless publish has no page address, so the GitHub *commit* URL was
  written into the sitemap as though it were a page on the site.

The tests drive ``publish`` through a fake ``_request`` rather than calling the
private helper, because two of these are decided by the caller's guard and only
show up end to end.
"""
from __future__ import annotations

import base64
import logging
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.services.publishers.base import PublishError, PublishRequest
from app.services.publishers.git import GitAdapter

_SITEMAP_PATH = "public/sitemap.xml"
_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_CREDENTIALS = {
    "repo": "r2st/blog",
    "token": "ghp_test",
    "site_url": "https://blog.example.com",
}


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing",
        slug="automating",
        body_markdown="## Why\n\nHerald writes the posts.\n",
        excerpt="Herald writes the posts.",
        meta_description="Herald automates developer marketing end to end.",
        project_name="Herald",
    )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _github_error(status: int, message: str) -> PublishError:
    """A failure shaped the way :meth:`Adapter._translate` shapes one.

    The status is not decoration. ``_existing_sha`` decides "this is a new post"
    from a 404 and re-raises everything else, so a fake that raises a statusless
    ``PublishError`` for a missing file is not modelling the seam it replaces.
    """
    error = PublishError(message)
    error.status_code = status
    return error


class _FakeGitHub:
    """Just enough of the contents API to see what gets committed.

    ``files`` maps a repo path to its decoded text. A path that is absent 404s,
    which for :meth:`Adapter._request` means a ``PublishError`` carrying that
    status — the same signal the real one raises and the adapter's "no sitemap
    yet" branch reads.
    """

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.puts: list[tuple[str, dict]] = []

    def __call__(self, method, url, *, headers=None, json_body=None, params=None):
        path = url.split("/contents/", 1)[1] if "/contents/" in url else None

        if method == "GET" and path is None:  # the repo probe used by verify()
            return _FakeResponse({"full_name": "r2st/blog", "permissions": {"push": True}})

        if method == "GET":
            if path not in self.files:
                raise _github_error(404, "GitHub returned 404: Not Found")
            body = self.files[path].encode("utf-8")
            return _FakeResponse(
                {"sha": f"blob-{path}", "content": base64.b64encode(body).decode("ascii")}
            )

        if method == "PUT":
            decoded = base64.b64decode(json_body["content"]).decode("utf-8")
            self.puts.append((path, json_body))
            self.files[path] = decoded
            return _FakeResponse(
                {
                    "commit": {
                        "sha": "c0ffee",
                        "html_url": "https://github.com/r2st/blog/commit/c0ffee",
                    },
                    "content": {
                        "html_url": f"https://github.com/r2st/blog/blob/main/{path}"
                    },
                }
            )

        raise AssertionError(f"unexpected {method} {url}")

    # -- assertions ------------------------------------------------------- #

    @property
    def sitemap(self) -> str | None:
        return self.files.get(_SITEMAP_PATH)

    @property
    def sitemap_was_written(self) -> bool:
        return any(path == _SITEMAP_PATH for path, _ in self.puts)


def _publish(request_, github, credentials=None):
    adapter = GitAdapter()
    adapter._request = github
    return adapter.publish(request_, credentials or _CREDENTIALS)


def _entries(xml: str) -> list[dict[str, str]]:
    """``[{loc, lastmod, ...}]`` in document order, namespace-insensitively."""
    root = ET.fromstring(xml)
    out = []
    for url_el in root:
        entry = {}
        for child in url_el:
            name = str(child.tag).rsplit("}", 1)[-1]
            entry[name] = (child.text or "").strip()
        out.append(entry)
    return out


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _urlset(*entries: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="{_NS}">{"".join(entries)}</urlset>\n'
    )


def _url(loc: str, *, lastmod: str | None = None, priority: str | None = None) -> str:
    parts = [f"<loc>{loc}</loc>"]
    if lastmod:
        parts.append(f"<lastmod>{lastmod}</lastmod>")
    if priority:
        parts.append(f"<priority>{priority}</priority>")
    return f"<url>{''.join(parts)}</url>"


# -- the metadata that was being dropped ---------------------------------- #


def test_existing_lastmod_survives_a_republish(request_):
    """The core loss: entries were rebuilt from their <loc> and nothing else.

    A sitemap's whole job is telling a crawler what changed and when. Rewriting
    it on every publish with the dates stripped out leaves a file that is
    syntactically fine and informationally empty.
    """
    github = _FakeGitHub(
        {
            _SITEMAP_PATH: _urlset(
                _url("https://blog.example.com/older", lastmod="2024-01-05"),
                _url("https://blog.example.com/oldest", lastmod="2023-11-30"),
            )
        }
    )

    _publish(request_, github)

    by_loc = {e["loc"]: e for e in _entries(github.sitemap)}
    assert by_loc["https://blog.example.com/older"]["lastmod"] == "2024-01-05"
    assert by_loc["https://blog.example.com/oldest"]["lastmod"] == "2023-11-30"
    assert by_loc["https://blog.example.com/automating"]["lastmod"] == _today()


def test_other_per_entry_fields_survive_too(request_):
    """``priority`` was reset to the 0.7 default for every existing entry.

    Less consequential than ``lastmod`` and lost the same way, so it is fixed
    the same way — the rewrite carries the fields through rather than
    reconstructing them.
    """
    github = _FakeGitHub(
        {_SITEMAP_PATH: _urlset(_url("https://blog.example.com/", priority="1.0"))}
    )

    _publish(request_, github)

    by_loc = {e["loc"]: e for e in _entries(github.sitemap)}
    assert by_loc["https://blog.example.com/"]["priority"] == "1.0"


def test_document_order_is_preserved(request_):
    """The file belongs to the user; re-sorting it on every publish is churn."""
    github = _FakeGitHub(
        {
            _SITEMAP_PATH: _urlset(
                _url("https://blog.example.com/zebra"),
                _url("https://blog.example.com/alpha"),
            )
        }
    )

    _publish(request_, github)

    assert [e["loc"] for e in _entries(github.sitemap)] == [
        "https://blog.example.com/zebra",
        "https://blog.example.com/alpha",
        "https://blog.example.com/automating",
    ]


# -- re-publishing the same piece ------------------------------------------ #


def test_republishing_moves_the_lastmod_forward(request_):
    """An entry already present had its date left alone — the one case that matters.

    The old code returned early on a URL it had seen before, so the second
    publish of a piece told crawlers nothing had changed about it. Editing and
    re-publishing is the normal way a post changes.
    """
    github = _FakeGitHub(
        {
            _SITEMAP_PATH: _urlset(
                _url("https://blog.example.com/automating", lastmod="2024-01-05")
            )
        }
    )

    _publish(request_, github)

    entries = _entries(github.sitemap)
    assert len(entries) == 1, "the piece must be updated in place, not appended twice"
    assert entries[0]["lastmod"] == _today()


def test_a_second_publish_on_the_same_day_writes_nothing(request_):
    """No new information, so no commit — the early return still has a job.

    Narrowed rather than removed: it now fires only when the date it would
    write is the date already there.
    """
    github = _FakeGitHub(
        {
            _SITEMAP_PATH: _urlset(
                _url("https://blog.example.com/automating", lastmod=_today())
            )
        }
    )

    _publish(request_, github)

    assert not github.sitemap_was_written


# -- files that must not be rewritten -------------------------------------- #


def test_a_sitemap_without_the_namespace_is_not_wiped(request_):
    """The destructive one: no xmlns meant "no URLs found", so the file was replaced.

    Plenty of hand-written and generator-written sitemaps omit the declaration.
    Matching on the qualified name found nothing in them, and "nothing" fed
    straight into a rewrite — a repo full of posts came back as a one-entry
    file, committed to the user's main branch.
    """
    github = _FakeGitHub(
        {
            _SITEMAP_PATH: (
                '<?xml version="1.0" encoding="UTF-8"?>\n<urlset>'
                "<url><loc>https://blog.example.com/one</loc>"
                "<lastmod>2024-02-02</lastmod></url>"
                "<url><loc>https://blog.example.com/two</loc></url>"
                "</urlset>\n"
            )
        }
    )

    _publish(request_, github)

    locs = [e["loc"] for e in _entries(github.sitemap)]
    assert "https://blog.example.com/one" in locs
    assert "https://blog.example.com/two" in locs
    assert "https://blog.example.com/automating" in locs


def test_a_sitemap_index_is_left_alone(request_):
    """Its <loc>s point at sitemaps, not pages, and read identically.

    Parsing them as page URLs and re-emitting the file as a <urlset> turns a
    working index into a list of pages that are not pages. There is no correct
    edit to make here, so the right move is to make none.
    """
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<sitemapindex xmlns="{_NS}">'
        "<sitemap><loc>https://blog.example.com/posts-sitemap.xml</loc></sitemap>"
        "</sitemapindex>\n"
    )
    github = _FakeGitHub({_SITEMAP_PATH: index})

    _publish(request_, github)

    assert not github.sitemap_was_written
    assert github.sitemap == index


def test_an_unparseable_sitemap_is_left_alone(request_):
    """Not readable is not the same as empty."""
    junk = "<urlset><url><loc>https://blog.example.com/one</loc>"
    github = _FakeGitHub({_SITEMAP_PATH: junk})

    _publish(request_, github)

    assert not github.sitemap_was_written
    assert github.sitemap == junk


def test_a_missing_sitemap_is_created(request_):
    """The 404 branch: absent is genuinely empty, and one entry is correct."""
    github = _FakeGitHub()

    _publish(request_, github)

    entries = _entries(github.sitemap)
    assert [e["loc"] for e in entries] == ["https://blog.example.com/automating"]
    assert entries[0]["lastmod"] == _today()
    # Created, not replaced — no blob sha to overwrite.
    payload = next(body for path, body in github.puts if path == _SITEMAP_PATH)
    assert "sha" not in payload


def test_an_existing_sitemap_is_overwritten_with_its_blob_sha(request_):
    """The contents API rejects a write to an existing path without one."""
    github = _FakeGitHub({_SITEMAP_PATH: _urlset(_url("https://blog.example.com/one"))})

    _publish(request_, github)

    payload = next(body for path, body in github.puts if path == _SITEMAP_PATH)
    assert payload["sha"] == f"blob-{_SITEMAP_PATH}"


# -- what belongs in a sitemap at all -------------------------------------- #


def test_a_slugless_publish_does_not_put_a_commit_url_in_the_sitemap(request_):
    """The guard checked that a site was configured, not that the URL was on it.

    ``_published_url`` falls back to the GitHub commit address whenever it
    cannot compute a page address, and a slugless request is enough to get
    there. The sitemap then advertised github.com to crawlers as a page of the
    user's site.
    """
    github = _FakeGitHub()

    result = _publish(replace(request_, slug=""), github)

    assert result.external_url.startswith("https://github.com/")
    assert not github.sitemap_was_written


def test_a_draft_is_not_added_to_the_sitemap(request_):
    """It is committed with ``draft: true`` and stays out of the build."""
    github = _FakeGitHub()

    _publish(replace(request_, as_draft=True), github)

    assert not github.sitemap_was_written


def test_no_site_url_means_no_sitemap(request_):
    """Without one there is no address to list, and no site to list it on."""
    github = _FakeGitHub()

    _publish(request_, github, {"repo": "r2st/blog", "token": "ghp_test"})

    assert not github.sitemap_was_written


# -- the publish itself ----------------------------------------------------- #


def test_the_post_is_committed_and_the_sitemap_failure_cannot_block_it(request_):
    """Best-effort means best-effort: the result still describes the post."""

    class _SitemapRefuses(_FakeGitHub):
        def __call__(self, method, url, **kwargs):
            if method == "PUT" and _SITEMAP_PATH in url:
                raise _github_error(500, "GitHub returned 500: Server Error")
            return super().__call__(method, url, **kwargs)

    github = _SitemapRefuses()

    result = _publish(request_, github)

    assert result.external_id == "c0ffee"
    assert result.external_url == "https://blog.example.com/automating"
    assert result.extra["path"] == "src/content/blog/automating.md"
    committed = github.files["src/content/blog/automating.md"]
    assert 'title: "Automating developer marketing"' in committed
    assert "Herald writes the posts." in committed


# -- the read failure that used to read as "there is no sitemap" ------------ #


class _SitemapUnreadable(_FakeGitHub):
    """The sitemap is there; GitHub will not let us look at it.

    Every status except 404 goes through here in the tests below, because the
    point is that the *status* decides and not the exception type — all of them
    arrive as a bare ``PublishError`` from ``Adapter._translate``.
    """

    def __init__(self, status: int, files=None):
        super().__init__(files or {_SITEMAP_PATH: _urlset(_url("https://blog.example.com/one"))})
        self.status = status

    def __call__(self, method, url, **kwargs):
        if method == "GET" and _SITEMAP_PATH in url:
            raise _github_error(self.status, f"GitHub returned {self.status}")
        return super().__call__(method, url, **kwargs)


@pytest.mark.parametrize("status", [401, 403, 409, 429, 500, 502])
def test_a_sitemap_that_cannot_be_read_is_not_treated_as_absent(request_, status):
    """"I was not allowed to look" is not "there is nothing there".

    The same distinction ``_existing_sha`` makes, against the same API, missing
    from the sitemap path: the ``except`` took every ``PublishError`` while the
    comment beside it asserted 404. A rejected token, a throttle or a 5xx on the
    read therefore produced a create over a live path with no blob sha — which
    the contents API refuses — so the sitemap was never written *and* the only
    record of why was a 409 conflict, with the 403 that caused it nowhere at all.
    """
    github = _SitemapUnreadable(status)

    result = _publish(request_, github)

    # The post still lands: the sitemap is best-effort and always was.
    assert result.external_id == "c0ffee"
    # But nothing was written over a file we could not read.
    assert not github.sitemap_was_written


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_the_real_status_reaches_the_log_rather_than_a_downstream_conflict(
    request_, status, caplog
):
    """The caller's one line has to name the cause, not its consequence."""
    github = _SitemapUnreadable(status)

    with caplog.at_level(logging.INFO, logger="app.services.publishers.git"):
        _publish(request_, github)

    skipped = [
        r for r in caplog.records
        if r.name == "app.services.publishers.git" and "sitemap update skipped" in r.getMessage()
    ]
    assert len(skipped) == 1
    assert str(status) in str(skipped[0].exc_info[1])


def test_a_genuine_404_still_creates_the_file(request_):
    """The narrow case stays narrow: absent is absent, and one entry is right."""
    github = _FakeGitHub()

    _publish(request_, github)

    assert github.sitemap_was_written
    assert _entries(github.sitemap)[0]["loc"] == "https://blog.example.com/automating"

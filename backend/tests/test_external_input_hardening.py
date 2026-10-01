"""What Pulse does with malformed answers from the services it does not own.

Three sources, one rule: a bad answer from outside must degrade to a smaller
correct result, never to a corrupted one and never to a traceback.

* **A user's own ``sitemap.xml``.** The one file Pulse rewrites in a repository
  somebody else owns. Anything in it Pulse does not recognise has to survive
  untouched or be skipped — never silently reshaped, because the diff lands in
  the user's git history under their name.
* **GitHub's JSON.** Timestamps in particular: a field that does not parse is a
  missing date, not an exception halfway through building a scan result.
* **The Celery broker.** When it cannot be reached the publish still has to
  happen, inline, on the request thread.
"""
from __future__ import annotations

import base64

import pytest

from app.models.publication import Platform
from app.services import content_pipeline, github_client
from app.services.publishers.base import PublishError, PublishRequest
from app.services.publishers.git import GitAdapter, _parse_urlset

_SITEMAP_PATH = "public/sitemap.xml"
_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_CREDENTIALS = {
    "repo": "r2st/blog",
    "token": "ghp_test",
    "site_url": "https://blog.example.com",
}


# --------------------------------------------------------------------------- #
# Parsing somebody else's sitemap                                             #
# --------------------------------------------------------------------------- #


def _urlset(body: str) -> str:
    return f'<?xml version="1.0"?><urlset xmlns="{_NS}">{body}</urlset>'


def test_a_foreign_element_among_the_urls_is_skipped_not_read_as_one():
    """Sitemap generators emit comments, extensions and stray nodes.

    Reading one as a ``<url>`` would put an entry with no ``loc`` into the list
    and write it back out as an empty ``<url/>``.
    """
    parsed = _parse_urlset(
        _urlset(
            "<url><loc>https://blog.example.com/a</loc></url>"
            "<something-else><loc>https://blog.example.com/not-a-page</loc></something-else>"
            "<url><loc>https://blog.example.com/b</loc></url>"
        )
    )

    assert [entry["url"] for entry in parsed] == [
        "https://blog.example.com/a",
        "https://blog.example.com/b",
    ]


def test_fields_the_sitemap_spec_does_not_define_are_dropped():
    """Only the known per-entry fields are carried forward.

    Writing back an element Pulse does not understand risks putting it in the
    wrong place in the document, which is worse than losing it.
    """
    parsed = _parse_urlset(
        _urlset(
            "<url>"
            "<loc>https://blog.example.com/a</loc>"
            "<lastmod>2026-01-01</lastmod>"
            "<invented-field>nonsense</invented-field>"
            "<changefreq>   </changefreq>"
            "</url>"
        )
    )

    assert parsed == [{"url": "https://blog.example.com/a", "lastmod": "2026-01-01"}]


def test_an_entry_with_no_location_at_all_is_dropped():
    parsed = _parse_urlset(
        _urlset(
            "<url><lastmod>2026-01-01</lastmod></url>"
            "<url><loc>https://blog.example.com/a</loc></url>"
        )
    )

    assert [entry["url"] for entry in parsed] == ["https://blog.example.com/a"]


def test_a_url_listed_twice_keeps_only_its_first_appearance():
    """First occurrence wins, and document order is not disturbed to do it."""
    parsed = _parse_urlset(
        _urlset(
            "<url><loc>https://blog.example.com/a</loc><lastmod>2026-01-01</lastmod></url>"
            "<url><loc>https://blog.example.com/b</loc></url>"
            "<url><loc>https://blog.example.com/a</loc><lastmod>2026-06-01</lastmod></url>"
        )
    )

    assert [entry["url"] for entry in parsed] == [
        "https://blog.example.com/a",
        "https://blog.example.com/b",
    ]
    assert parsed[0]["lastmod"] == "2026-01-01"


def test_an_empty_urlset_parses_as_no_entries_rather_than_none():
    """Distinct from unparseable: an empty sitemap is a sitemap, and appending
    to it is correct. ``None`` would mean "leave the file alone"."""
    assert _parse_urlset(_urlset("")) == []


# --------------------------------------------------------------------------- #
# The contents API answering with something unexpected                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing",
        slug="automating",
        body_markdown="## Why\n\nPulse writes the posts.\n",
        excerpt="Pulse writes the posts.",
        meta_description="Pulse automates developer marketing end to end.",
        project_name="Pulse",
    )


def _not_found() -> PublishError:
    """What ``_request`` raises for a path that is not in the repo.

    The status is part of the signal, not decoration: ``_existing_sha`` reads
    "there is no file here" off a 404 specifically and re-raises everything
    else, because "I was not allowed to look" is not "there is nothing there".
    A fake that raises a statusless ``PublishError`` for a missing file is not
    modelling the seam it stands in for.
    """
    error = PublishError("GitHub returned 404: Not Found")
    error.status_code = 404
    return error


class _Contents:
    """A contents API that can be told to answer oddly for the sitemap."""

    def __init__(self, sitemap_payload):
        self.sitemap_payload = sitemap_payload
        self.written: dict[str, str] = {}

    def __call__(self, method, url, *, headers=None, json_body=None, params=None):
        path = url.split("/contents/", 1)[1] if "/contents/" in url else None

        if method == "GET" and path == _SITEMAP_PATH:
            if self.sitemap_payload is _MISSING:
                raise _not_found()
            return _Response(self.sitemap_payload)
        if method == "GET":
            raise _not_found()
        if method == "PUT":
            self.written[path] = base64.b64decode(json_body["content"]).decode("utf-8")
            return _Response(
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


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


_MISSING = object()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"sha": "blob", "content": ""}, id="empty-content"),
        pytest.param({"sha": "blob"}, id="no-content-key"),
        pytest.param(["not", "an", "object"], id="a-list"),
        pytest.param(None, id="null"),
    ],
)
def test_a_contents_response_without_a_file_in_it_starts_a_fresh_sitemap(
    request_, payload
):
    """A directory listing, a null, a shape that changed — none is a sitemap.

    The safe reading is "there isn't one yet", which writes a new single-entry
    file. The dangerous one would be treating the shape as parseable and
    committing whatever fell out.
    """
    github = _Contents(payload)
    adapter = GitAdapter()
    adapter._request = github

    adapter.publish(request_, _CREDENTIALS)

    written = github.written[_SITEMAP_PATH]
    assert "https://blog.example.com/automating" in written
    assert written.count("<url>") == 1


def test_a_sitemap_that_is_there_is_appended_to_rather_than_replaced(request_):
    body = _urlset("<url><loc>https://blog.example.com/older</loc></url>")
    github = _Contents(
        {"sha": "blob", "content": base64.b64encode(body.encode()).decode("ascii")}
    )
    adapter = GitAdapter()
    adapter._request = github

    adapter.publish(request_, _CREDENTIALS)

    written = github.written[_SITEMAP_PATH]
    assert "https://blog.example.com/older" in written
    assert "https://blog.example.com/automating" in written


# --------------------------------------------------------------------------- #
# GitHub's JSON                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value", ["not a date", "2026-13-45T00:00:00Z", "", None, "   "]
)
def test_a_timestamp_that_does_not_parse_becomes_no_timestamp(value):
    """A commit with an unreadable date is still a commit worth reporting."""
    assert github_client._parse_ts(value) is None


def test_a_timestamp_that_does_parse_keeps_its_zone():
    parsed = github_client._parse_ts("2026-07-01T12:00:00Z")

    assert parsed is not None
    assert parsed.utcoffset() is not None
    assert parsed.year == 2026


def test_the_token_is_sent_as_a_bearer_header_when_there_is_one(monkeypatch):
    """Unauthenticated GitHub is 60 requests an hour for the whole install."""
    monkeypatch.setattr("app.services.github_client.settings.github_token", "ghp_secret")

    headers = github_client._headers()

    assert headers["Authorization"] == "Bearer ghp_secret"


def test_no_token_means_no_authorization_header_rather_than_an_empty_one(monkeypatch):
    """An empty ``Authorization`` is a 401, which reads as bad credentials
    rather than as the anonymous request it actually is."""
    monkeypatch.setattr("app.services.github_client.settings.github_token", "")

    assert "Authorization" not in github_client._headers()


# --------------------------------------------------------------------------- #
# The broker                                                                  #
# --------------------------------------------------------------------------- #


def test_with_celery_switched_off_the_publish_happens_inline(monkeypatch):
    """A single-process install has no worker; the request thread does the work."""
    monkeypatch.setattr("app.services.content_pipeline.settings.celery_enabled", False)
    ran: list[int] = []
    monkeypatch.setattr(
        "app.tasks.publish_tasks.publish_one", lambda publication_id: ran.append(publication_id)
    )

    content_pipeline.publish_now(7)

    assert ran == [7]


def test_a_platform_named_twice_is_only_published_to_once(db, project, connect):
    """``autopilot_platforms`` is a plain JSON list — nothing dedupes it on read.

    Two entries would mean two publications, and the second one is a duplicate
    post on somebody's blog.
    """
    connect("devto", "mastodon")
    project.autopilot_platforms = ["devto", "devto", "mastodon"]
    db.commit()

    assert content_pipeline.publishable_destinations(project).usable == [
        Platform.DEVTO,
        Platform.MASTODON,
    ]

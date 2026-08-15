"""The Git adapter's credential checks and its write path.

``test_git_sitemap.py`` covers what gets committed. This covers whether the
commit happens at all: the token checks that run before anything is written, the
overwrite-vs-create decision, and the failures that must not be swallowed.

The distinction that matters most is in :meth:`_existing_sha`. A 404 there means
"this is a new post" and is the ordinary case; a 401 or 403 means the token is
wrong, and reading that as "new post" would turn a credential problem into a
create attempt that fails later with a worse message.
"""
from __future__ import annotations

import base64

import pytest

from app.services.publishers.base import CredentialError, PublishError, PublishRequest
from app.services.publishers.git import GitAdapter

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


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# Tokens                                                                       #
# --------------------------------------------------------------------------- #


def test_publishing_with_no_token_anywhere_says_what_to_do_about_it(monkeypatch):
    monkeypatch.setattr(
        "app.services.publishers.git.settings.github_token", "", raising=False
    )
    adapter = GitAdapter()

    with pytest.raises(CredentialError) as exc:
        adapter._token({"repo": "r2st/blog"})

    message = str(exc.value)
    assert "contents:write" in message or "GITHUB_TOKEN" in message


def test_a_blank_token_on_the_connection_falls_through_to_the_install_wide_one(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.publishers.git.settings.github_token", "ghp_install", raising=False
    )

    assert GitAdapter()._token({"token": "   "}) == "ghp_install"


def test_the_connection_s_own_token_wins_over_the_install_wide_one(monkeypatch):
    monkeypatch.setattr(
        "app.services.publishers.git.settings.github_token", "ghp_install", raising=False
    )

    assert GitAdapter()._token({"token": "ghp_mine"}) == "ghp_mine"


# --------------------------------------------------------------------------- #
# verify()                                                                     #
# --------------------------------------------------------------------------- #


def test_a_token_that_can_read_but_not_write_is_refused_at_connection_time(
    monkeypatch,
):
    """A read-only token reads a public repo perfectly well.

    Without this check the first sign of trouble is a failed publish, hours
    later, on a piece the user thought was scheduled.
    """
    adapter = GitAdapter()
    adapter._request = lambda *a, **k: _Response(
        {"full_name": "r2st/blog", "permissions": {"pull": True, "push": False}}
    )

    with pytest.raises(CredentialError, match="contents:write"):
        adapter.verify(_CREDENTIALS)


def test_a_repo_response_with_no_permissions_block_is_treated_as_no_write(
    monkeypatch,
):
    """Absent is not the same as true — assume the restrictive reading."""
    adapter = GitAdapter()
    adapter._request = lambda *a, **k: _Response({"full_name": "r2st/blog"})

    with pytest.raises(CredentialError, match="contents:write"):
        adapter.verify(_CREDENTIALS)


def test_a_writable_repo_verifies_and_answers_with_its_canonical_name():
    adapter = GitAdapter()
    adapter._request = lambda *a, **k: _Response(
        {"full_name": "r2st/blog", "permissions": {"push": True}}
    )

    assert adapter.verify(_CREDENTIALS) == "r2st/blog"


def test_a_repo_response_with_no_name_falls_back_to_what_was_configured():
    adapter = GitAdapter()
    adapter._request = lambda *a, **k: _Response({"permissions": {"push": True}})

    assert adapter.verify(_CREDENTIALS) == "r2st/blog"


# --------------------------------------------------------------------------- #
# _existing_sha: create, overwrite, or refuse                                  #
# --------------------------------------------------------------------------- #


def _not_found() -> PublishError:
    """What ``_request`` raises for a file that is not in the repo.

    The status matters as much as the type: ``_existing_sha`` reads "no file
    there" off a 404 specifically, because every *other* failure means it was
    not able to look — see ``test_a_lookup_that_fails_is_not_a_new_post``.
    """
    error = PublishError("GitHub returned 404: Not Found")
    error.status_code = 404
    return error


def test_a_missing_file_is_a_new_post_not_a_failure():
    adapter = GitAdapter()

    def not_found(*a, **k):
        raise _not_found()

    adapter._request = not_found

    assert adapter._existing_sha("r2st/blog", "posts/a.md", "", "t") is None


def test_a_rejected_token_surfaces_rather_than_reading_as_a_new_post():
    """Silencing this would turn "your token is wrong" into a create attempt."""
    adapter = GitAdapter()

    def forbidden(*a, **k):
        raise CredentialError("403 Forbidden")

    adapter._request = forbidden

    with pytest.raises(CredentialError):
        adapter._existing_sha("r2st/blog", "posts/a.md", "", "t")


def test_a_path_that_is_actually_a_directory_is_refused():
    """The contents API answers a directory with a list, and a PUT there would
    not do anything the user meant."""
    adapter = GitAdapter()
    adapter._request = lambda *a, **k: _Response([{"name": "a.md"}])

    with pytest.raises(PublishError, match="is a directory"):
        adapter._existing_sha("r2st/blog", "posts", "", "t")


def test_an_existing_file_yields_the_sha_the_overwrite_needs():
    adapter = GitAdapter()
    adapter._request = lambda *a, **k: _Response({"sha": "blob123"})

    assert adapter._existing_sha("r2st/blog", "posts/a.md", "", "t") == "blob123"


def test_the_branch_is_passed_as_a_ref_when_one_is_configured():
    adapter = GitAdapter()
    seen: dict = {}

    def record(method, url, *, headers=None, json_body=None, params=None):
        seen["params"] = params
        return _Response({"sha": "blob123"})

    adapter._request = record
    adapter._existing_sha("r2st/blog", "posts/a.md", "gh-pages", "t")

    assert seen["params"] == {"ref": "gh-pages"}


def test_no_branch_means_no_ref_rather_than_an_empty_one():
    adapter = GitAdapter()
    seen: dict = {}

    def record(method, url, *, headers=None, json_body=None, params=None):
        seen["params"] = params
        return _Response({"sha": "blob123"})

    adapter._request = record
    adapter._existing_sha("r2st/blog", "posts/a.md", "", "t")

    assert seen["params"] is None


# --------------------------------------------------------------------------- #
# publish()                                                                    #
# --------------------------------------------------------------------------- #


class _Repo:
    """A repo that answers GETs from ``files`` and records every PUT."""

    def __init__(self, files: dict[str, str] | None = None, commit_sha="c0ffee"):
        self.files = dict(files or {})
        self.puts: list[tuple[str, dict]] = []
        self._commit_sha = commit_sha

    def __call__(self, method, url, *, headers=None, json_body=None, params=None):
        path = url.split("/contents/", 1)[1] if "/contents/" in url else None
        if method == "GET":
            if path not in self.files:
                raise _not_found()
            body = self.files[path].encode("utf-8")
            return _Response(
                {"sha": f"blob-{path}", "content": base64.b64encode(body).decode("ascii")}
            )
        if method == "PUT":
            self.puts.append((path, json_body))
            self.files[path] = base64.b64decode(json_body["content"]).decode("utf-8")
            commit = {"sha": self._commit_sha} if self._commit_sha else {}
            return _Response(
                {
                    "commit": {
                        **commit,
                        "html_url": "https://github.com/r2st/blog/commit/c0ffee",
                    },
                    "content": {"html_url": f"https://github.com/r2st/blog/blob/{path}"},
                }
            )
        raise AssertionError(f"unexpected {method} {url}")

    def put_for(self, suffix: str) -> dict:
        return next(body for path, body in self.puts if path.endswith(suffix))


def test_a_new_post_is_committed_without_a_sha(request_):
    adapter = GitAdapter()
    repo = _Repo()
    adapter._request = repo

    adapter.publish(request_, _CREDENTIALS)

    body = repo.put_for("automating.md")
    assert "sha" not in body, "a create must not carry a blob sha"
    assert body["message"].startswith("content: ")


def test_re_publishing_carries_the_existing_sha_so_it_overwrites(request_):
    adapter = GitAdapter()
    path = adapter.path_for(request_, _CREDENTIALS)
    repo = _Repo({path: "old contents"})
    adapter._request = repo

    adapter.publish(request_, _CREDENTIALS)

    assert repo.put_for("automating.md")["sha"] == f"blob-{path}"


def test_a_configured_branch_reaches_every_commit(request_):
    adapter = GitAdapter()
    repo = _Repo()
    adapter._request = repo

    adapter.publish(request_, {**_CREDENTIALS, "branch": "gh-pages"})

    assert repo.puts, "nothing was committed"
    for _, body in repo.puts:
        assert body["branch"] == "gh-pages"


def test_a_put_that_comes_back_without_a_commit_is_a_failure_not_a_success(request_):
    """Answering 200 with no commit means nothing landed; reporting the publish
    as done would leave a piece marked live that is not in the repo."""
    adapter = GitAdapter()
    adapter._request = _Repo(commit_sha=None)

    with pytest.raises(PublishError, match="no commit"):
        adapter.publish(request_, _CREDENTIALS)


def test_the_committed_file_is_the_rendered_post(request_):
    adapter = GitAdapter()
    repo = _Repo()
    adapter._request = repo

    adapter.publish(request_, _CREDENTIALS)

    committed = repo.files[adapter.path_for(request_, _CREDENTIALS)]
    assert committed.startswith("---")
    assert "Herald writes the posts." in committed

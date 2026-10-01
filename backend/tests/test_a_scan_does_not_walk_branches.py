"""A scan reads the default branch, and nothing makes it read more than that.

The file :func:`app.services.github_client._commits_page` names, pinning the
property its docstring calls "one well-meant patch away from being lost": the
commits endpoint is asked **without a ``sha`` parameter**.

Why that one line is worth a test file. GitHub's ``/repos/{repo}/commits``
defaults to the repository's default branch. Passing ``sha`` selects a branch,
and the obvious-looking improvement — "scan every branch so nothing is missed" —
costs a request per branch against an API that rate-limits per *account*, and
produces posts about somebody's abandoned spike. Neither cost shows up in a
test that only asserts the commits came back, because with one branch in the
fixture every version of the code returns the same list. So the assertion has to
be about the *request*, and nothing else in the suite makes one.

Pulse has no per-branch fan-out anywhere. These tests fail if one appears.
"""
from __future__ import annotations

import pytest

from app.services import github_client


@pytest.fixture
def calls(monkeypatch):
    """Record every request ``github_client`` makes, and answer it.

    Patches :func:`github_client._get` rather than ``httpx.get``: the params
    are what these tests are about, and ``_get`` is the one place they are
    assembled. A stub further down would also have to reproduce the rate-limit
    classification to get the same code path.
    """
    recorded: list[dict] = []
    pages = {"value": []}

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self):
            return pages["value"]

    def fake_get(path, *, params=None):
        recorded.append({"path": path, "params": dict(params or {})})
        return _Resp()

    monkeypatch.setattr(github_client, "_get", fake_get)
    return {"recorded": recorded, "set": lambda v: pages.update(value=v)}


def _commit(sha: str) -> dict:
    return {
        "sha": sha,
        "commit": {
            "message": f"feat: {sha}",
            "author": {"name": "r2st", "date": "2026-07-01T00:00:00Z"},
        },
        "html_url": f"https://github.com/r2st/Herald/commit/{sha}",
    }


# --------------------------------------------------------------------------- #
# The property                                                                 #
# --------------------------------------------------------------------------- #


def test_a_commits_page_is_asked_for_without_a_branch(calls):
    """No ``sha`` param — the whole point of the file.

    With one, the request selects a branch and the cost of a scan stops being
    independent of how many branches the repo has.
    """
    calls["set"]([_commit("aaa1111")])

    github_client._commits_page("r2st/Herald", per_page=100)

    (request,) = calls["recorded"]
    assert request["path"] == "/repos/r2st/Herald/commits"
    assert "sha" not in request["params"], (
        "the commits endpoint was asked for a specific branch. GitHub's "
        "default is the repo's default branch, and selecting one is the first "
        "half of a per-branch fan-out — see the docstring on _commits_page."
    )
    assert set(request["params"]) == {"per_page"}


def test_a_full_fetch_names_no_branch_either(calls):
    """``fetch_commits`` is the caller that matters, and it makes two requests.

    The cheap ``per_page=1`` HEAD check and the full page. Both go through
    ``_commits_page``, so both are covered — but a future caller that built its
    own request would not be, which is why this asserts on every recorded call
    rather than on the first.
    """
    calls["set"]([_commit("aaa1111"), _commit("bbb2222")])

    github_client.fetch_commits("r2st/Herald", since_sha="bbb2222")

    assert calls["recorded"], "no request was made at all"
    for request in calls["recorded"]:
        assert "sha" not in request["params"], (
            f"{request['path']} was asked with {sorted(request['params'])}; "
            "a scan must not select a branch"
        )


def test_a_scan_with_no_watermark_still_names_no_branch(calls):
    """The first scan of a project takes the other path through the function."""
    calls["set"]([_commit("aaa1111")])

    github_client.fetch_commits("r2st/Herald")

    for request in calls["recorded"]:
        assert "sha" not in request["params"]


# --------------------------------------------------------------------------- #
# The cost the property buys                                                   #
# --------------------------------------------------------------------------- #


def test_the_request_count_does_not_depend_on_the_repo(calls):
    """A scan costs the same on a repo with one branch and one with hundreds.

    Stated as a request count because that is what GitHub's rate limit counts.
    The fixture cannot vary the branch list — nothing asks for it — which is
    precisely the property: there is no branch enumeration to grow.
    """
    calls["set"]([_commit("aaa1111")])
    github_client.fetch_commits("r2st/Herald", since_sha="aaa1111")
    unchanged = len(calls["recorded"])

    calls["recorded"].clear()
    calls["set"]([_commit("ccc3333"), _commit("aaa1111")])
    github_client.fetch_commits("r2st/Herald", since_sha="aaa1111")
    moved = len(calls["recorded"])

    # One request when the watermark is still HEAD, two when it has moved —
    # the trade `fetch_commits` documents. Neither number is per-branch.
    assert unchanged == 1
    assert moved == 2


def test_an_unmoved_head_is_answered_with_a_one_commit_page(calls):
    """The cheap check comes first, and it asks for exactly one commit.

    Not strictly about branches, but it is the other half of the same request:
    a no-op scan used to transfer a hundred fully-populated commit objects to
    discover a sha it already had. Pinned here because both properties live in
    the params of the same call, and a patch that adds ``sha`` is likely to
    rewrite this line too.
    """
    calls["set"]([_commit("aaa1111")])

    assert github_client.fetch_commits("r2st/Herald", since_sha="aaa1111") == []

    (request,) = calls["recorded"]
    assert request["params"]["per_page"] == 1


def test_no_module_level_caller_passes_a_sha_parameter():
    """A belt-and-braces read of the source itself.

    The tests above cover the paths they call. This one covers the ones nobody
    has written yet: it reads ``github_client`` and fails if a ``sha`` key
    appears in a params dict anywhere in it. Crude on purpose — the property is
    a negative, and a negative about code that does not exist yet cannot be
    asserted by calling it.
    """
    import inspect
    import re

    source = inspect.getsource(github_client)
    commit_requests = [
        line
        for line in source.splitlines()
        if re.search(r"""params\s*=\s*\{[^}]*["']sha["']""", line)
    ]
    assert commit_requests == [], (
        "something in github_client passes a `sha` parameter: "
        f"{commit_requests}. If that is a deliberate change, the scan now "
        "costs a request per branch — read _commits_page's docstring first."
    )

"""The GitHub reader, and what it does when GitHub answers with nonsense.

Every caller of this module catches `GitHubError` and nothing else — the scan
route turns it into a 502, the autopilot into an "unreachable" status that
still advances `last_scanned_at`. So the contract under test is not just "the
happy path parses"; it is that *nothing else escapes*, because anything that
does becomes a 500 on a route that had a 502 ready for it.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.project import COMMIT_SHA_MAX_LENGTH, RELEASE_TAG_MAX_LENGTH
from app.services import github_client


def _response(
    payload: object = None,
    *,
    status_code: int = 200,
    text: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    kwargs: dict = {"status_code": status_code, "headers": headers or {}}
    if text is not None:
        kwargs["text"] = text
    else:
        kwargs["json"] = payload
    return httpx.Response(request=httpx.Request("GET", "https://api.github.test"), **kwargs)


def _stub(monkeypatch, handler) -> list[str]:
    """Replace httpx.get, recording the paths asked for."""
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return handler(url, **kwargs)

    monkeypatch.setattr(github_client.httpx, "get", fake_get)
    return seen


REPO = {
    "description": "A repo",
    "topics": ["python", "fastapi"],
    "stargazers_count": 12,
}
COMMITS = [
    {
        "sha": "abc123",
        "html_url": "https://github.test/c/abc123",
        "commit": {
            "message": "feat: a thing\n\nbody",
            "author": {"name": "Ada", "date": "2026-01-02T03:04:05Z"},
        },
        "author": {"login": "ada"},
    }
]


# -- The happy path, so the failure tests below mean something --------------- #


def test_fetch_activity_reads_repo_commits_and_release(monkeypatch):
    def handler(url, **_):
        if url.endswith("/releases"):
            return _response(
                [{"tag_name": "v2", "name": "Two", "published_at": "2026-01-03T00:00:00Z"}]
            )
        if url.endswith("/commits"):
            return _response(COMMITS)
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo")

    assert activity.stars == 12
    assert activity.topics == ["python", "fastapi"]
    assert activity.head_sha == "abc123"
    assert activity.new_commits[0].author == "ada"
    assert activity.new_commits[0].summary == "feat: a thing"
    assert activity.new_release.tag == "v2"
    assert activity.has_news


def test_commits_stop_at_the_watermark(monkeypatch):
    page = [
        {"sha": "new", "commit": {"message": "newer"}},
        {"sha": "seen", "commit": {"message": "the watermark"}},
        {"sha": "old", "commit": {"message": "older"}},
    ]
    _stub(monkeypatch, lambda url, **_: _response(page))

    commits = github_client.fetch_commits("owner/repo", since_sha="seen")
    assert [c.sha for c in commits] == ["new"]


# -- Malformed upstream responses ------------------------------------------ #


def test_a_non_json_body_is_a_github_error_not_a_json_decode_error(monkeypatch):
    """A proxy's HTML error page under a 200 used to escape every handler.

    `resp.json()` raises `json.JSONDecodeError`, which is not a `GitHubError`,
    so it went past the scan route's 502 handler and out through the app's
    catch-all as a 500 — Pulse reporting someone else's fault as its own.
    """
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            text="<html>502 Bad Gateway</html>",
            headers={"content-type": "text/html"},
        ),
    )

    with pytest.raises(github_client.GitHubError) as exc:
        github_client.fetch_activity("owner/repo")
    assert "non-JSON" in str(exc.value)
    assert "text/html" in str(exc.value)


def test_a_repo_payload_that_is_not_an_object_is_a_github_error(monkeypatch):
    """`fetch_commits` checked its payload shape; `fetch_activity` did not.

    A list where the repo object belongs reached `.get()` and raised
    `AttributeError` — again past every caller's `except GitHubError`.
    """
    def handler(url, **_):
        if url.endswith("/commits"):
            return _response(COMMITS)
        if url.endswith("/releases"):
            return _response([])
        return _response(["not", "an", "object"])

    _stub(monkeypatch, handler)
    with pytest.raises(github_client.GitHubError):
        github_client.fetch_activity("owner/repo")


def test_a_commits_payload_that_is_not_a_list_is_a_github_error(monkeypatch):
    _stub(monkeypatch, lambda url, **_: _response({"message": "Not Found"}))
    with pytest.raises(github_client.GitHubError):
        github_client.fetch_commits("owner/repo")


def test_a_malformed_commit_entry_does_not_lose_the_rest_of_the_page(monkeypatch):
    page = ["nonsense", *COMMITS]
    _stub(monkeypatch, lambda url, **_: _response(page))

    commits = github_client.fetch_commits("owner/repo")
    assert [c.sha for c in commits] == ["abc123"]


def test_topics_that_arrive_as_a_string_do_not_become_one_topic_per_character(monkeypatch):
    """`list("python")` is eleven topics, and they go into the model's prompt."""
    def handler(url, **_):
        if url.endswith("/commits"):
            return _response(COMMITS)
        if url.endswith("/releases"):
            return _response([])
        return _response({**REPO, "topics": "python"})

    _stub(monkeypatch, handler)
    assert github_client.fetch_activity("owner/repo").topics == []


def test_a_non_numeric_star_count_reads_as_zero(monkeypatch):
    def handler(url, **_):
        if url.endswith("/commits"):
            return _response(COMMITS)
        if url.endswith("/releases"):
            return _response([])
        return _response({**REPO, "stargazers_count": "lots"})

    _stub(monkeypatch, handler)
    assert github_client.fetch_activity("owner/repo").stars == 0


def test_a_release_entry_that_is_not_an_object_reads_as_no_release(monkeypatch):
    _stub(monkeypatch, lambda url, **_: _response(["nonsense"]))
    assert github_client.fetch_latest_release("owner/repo") is None


# -- Status codes ---------------------------------------------------------- #


def test_a_404_on_releases_means_releases_are_disabled_not_a_failure(monkeypatch):
    """Distinguished by exception type now, not by grepping the message.

    The old check searched the error's prose for "has no". A reword of that
    sentence would have turned every missing *repo* into a repo with no
    releases, silently.
    """
    _stub(monkeypatch, lambda url, **_: _response({}, status_code=404))
    assert github_client.fetch_latest_release("owner/repo") is None


def test_a_404_is_a_not_found_which_is_still_a_github_error(monkeypatch):
    _stub(monkeypatch, lambda url, **_: _response({}, status_code=404))

    with pytest.raises(github_client.GitHubNotFound):
        github_client.fetch_commits("owner/repo")
    # Callers catch the base class; the subclass must not slip past them.
    assert issubclass(github_client.GitHubNotFound, github_client.GitHubError)


def test_a_rate_limit_is_not_swallowed_by_the_releases_fallback(monkeypatch):
    """429 on /releases must propagate — "come back later", not "no releases".

    `GitHubRateLimited` is a `GitHubError`, so the old blanket `except
    GitHubError` in this function caught it; only the message check let it back
    out. Now the fallback is narrowed to the one status it is for.
    """
    _stub(monkeypatch, lambda url, **_: _response({}, status_code=429))

    with pytest.raises(github_client.GitHubRateLimited):
        github_client.fetch_latest_release("owner/repo")


def test_an_exhausted_quota_under_a_403_reads_as_rate_limited(monkeypatch):
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            {}, status_code=403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999"}
        ),
    )
    with pytest.raises(github_client.GitHubRateLimited) as exc:
        github_client.fetch_commits("owner/repo")
    # The epoch as a time somebody can wait for, rather than as an epoch.
    assert "00:16 UTC" in str(exc.value)


def test_an_unparseable_reset_is_reported_as_it_arrived(monkeypatch):
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            {},
            status_code=429,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "soon"},
        ),
    )
    with pytest.raises(github_client.GitHubRateLimited) as exc:
        github_client.fetch_commits("owner/repo")
    assert "soon" in str(exc.value)


# -- The secondary limit --------------------------------------------------- #
#
# GitHub has two rate limits and reports them differently. The primary one is
# the request quota: `X-RateLimit-Remaining: 0`, tested above. The secondary
# one fires on burst rate and concurrency, *while the quota is nearly
# untouched* — a 403 with `Retry-After` and hundreds of requests left. That is
# the shape this module used to call "private repo, no token", and the mis-read
# reached much further than the message: `GitHubRateLimited` is the one error
# the autopilot answers by holding the watermark and not stamping
# `last_scanned_at`, because it means we never looked. As a plain `GitHubError`
# a throttled scan marked the project unreachable and stamped it scanned, and
# because the secondary limit is per-account it did that to every project in
# the sweep at once.


def test_a_retry_after_on_a_403_reads_as_rate_limited_with_quota_to_spare(monkeypatch):
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            {},
            status_code=403,
            headers={"Retry-After": "47", "X-RateLimit-Remaining": "4831"},
        ),
    )
    with pytest.raises(github_client.GitHubRateLimited) as exc:
        github_client.fetch_commits("owner/repo")
    assert exc.value.retry_after == 47
    assert "47s" in str(exc.value)


def test_the_secondary_limit_is_recognised_from_the_body_without_a_header(monkeypatch):
    """GitHub often omits ``Retry-After``; the body still says what happened."""
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            status_code=403,
            text='{"message": "You have exceeded a secondary rate limit."}',
            headers={"X-RateLimit-Remaining": "4831"},
        ),
    )
    with pytest.raises(github_client.GitHubRateLimited) as exc:
        github_client.fetch_commits("owner/repo")
    assert exc.value.retry_after is None


def test_the_older_abuse_detection_wording_counts_too(monkeypatch):
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            status_code=403,
            text='{"message": "You have triggered an abuse detection mechanism."}',
            headers={"X-RateLimit-Remaining": "4831"},
        ),
    )
    with pytest.raises(github_client.GitHubRateLimited):
        github_client.fetch_commits("owner/repo")


def test_a_nonsense_retry_after_does_not_become_a_negative_wait(monkeypatch):
    """The value's whole job is to be waited for. Better absent than wrong."""
    _stub(
        monkeypatch,
        lambda url, **_: _response(
            {},
            status_code=403,
            headers={"Retry-After": "not a number", "X-RateLimit-Remaining": "4831"},
        ),
    )
    with pytest.raises(github_client.GitHubError) as exc:
        github_client.fetch_commits("owner/repo")
    assert not isinstance(exc.value, github_client.GitHubRateLimited)


def test_a_plain_403_is_still_an_access_failure_not_a_rate_limit(monkeypatch):
    """A blocked token, a missing scope. Not a private repo — those 404."""
    _stub(
        monkeypatch,
        lambda url, **_: _response({}, status_code=403, headers={"X-RateLimit-Remaining": "57"}),
    )
    with pytest.raises(github_client.GitHubError) as exc:
        github_client.fetch_commits("owner/repo")
    assert not isinstance(exc.value, github_client.GitHubRateLimited)


def test_a_transport_failure_is_a_github_error(monkeypatch):
    def handler(url, **_):
        raise httpx.ConnectError("no route to host")

    _stub(monkeypatch, handler)
    with pytest.raises(github_client.GitHubError):
        github_client.fetch_commits("owner/repo")


def test_a_500_is_a_github_error(monkeypatch):
    _stub(monkeypatch, lambda url, **_: _response({}, status_code=500))
    with pytest.raises(github_client.GitHubError):
        github_client.fetch_commits("owner/repo")


# -- Watermarks ------------------------------------------------------------ #


def test_an_unchanged_repo_keeps_its_watermark_rather_than_clearing_it(monkeypatch):
    """No new commits means HEAD is still the watermark, not None.

    Clearing it would make the next scan re-report the whole page.
    """
    def handler(url, **_):
        if url.endswith("/commits"):
            return _response([{"sha": "seen", "commit": {"message": "the watermark"}}])
        if url.endswith("/releases"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo", since_sha="seen", since_tag="v1")

    assert activity.new_commits == []
    assert activity.head_sha == "seen"
    assert activity.latest_tag == "v1"
    assert not activity.has_news


def test_a_release_already_seen_is_not_reported_as_new(monkeypatch):
    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([{"tag_name": "v1"}])
        if url.endswith("/commits"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo", since_tag="v1")

    assert activity.new_release is None
    assert activity.latest_tag == "v1"


# -- A tag longer than the watermark column ---------------------------------- #
#
# `Project.last_seen_release_tag` is `String(120)` and nothing between GitHub
# and that column is a request schema, so the tag is bounded here or nowhere.
# Git allows a ref name of up to 255 bytes.


def test_a_tag_longer_than_the_watermark_column_is_cut_on_the_way_in(monkeypatch):
    long_tag = "v" + "9" * 254

    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([{"tag_name": long_tag}])
        if url.endswith("/commits"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo")

    assert len(activity.latest_tag) == RELEASE_TAG_MAX_LENGTH
    assert len(activity.new_release.tag) == RELEASE_TAG_MAX_LENGTH


def test_the_cut_tag_still_matches_the_watermark_it_was_stored_as(monkeypatch):
    """The reason the cut is here and not at the two sites that store it.

    The stored watermark is compared against the next scan's tag to decide
    whether a release is new. Truncating on the way out would compare a full
    tag against a truncated watermark, never match, and announce the same
    release every hour for as long as the tag existed.
    """
    long_tag = "v" + "9" * 254
    stored = long_tag[:RELEASE_TAG_MAX_LENGTH]

    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([{"tag_name": long_tag}])
        if url.endswith("/commits"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo", since_tag=stored)

    assert activity.new_release is None


# -- The sha, which is the other value that becomes a fixed-width column ---- #
#
# `Project.last_seen_commit_sha` is `String(40)` and, like the tag above,
# nothing between GitHub and that column is a request schema. Unlike the tag, it
# had no cut at all — forty hex characters is a SHA-1 object name and that is
# what GitHub serves, so the bound held by convention rather than by anything in
# the code. Git's own SHA-256 transition doubles it.


def test_a_sha_wider_than_the_watermark_column_is_cut_on_the_way_in(monkeypatch):
    """Otherwise the *storing* commit is what fails, which lands badly.

    On PostgreSQL an over-wide value is `StringDataRightTruncation` raised from
    the commit that records the watermark: a 500 on a manual scan whose upstream
    call had already succeeded, and in the autopilot a watermark that never
    advances — so the same commits are re-read and written about again on every
    scan, for ever.
    """
    sha256 = "a" * 64
    _stub(monkeypatch, lambda url, **_: _response([{"sha": sha256, "commit": {}}]))

    commits = github_client.fetch_commits("owner/repo")

    assert len(commits[0].sha) == COMMIT_SHA_MAX_LENGTH


def test_the_cut_sha_still_matches_the_watermark_it_was_stored_as(monkeypatch):
    """Same reasoning as the tag: both sides of the comparison must be cut."""
    sha256 = "a" * 64
    stored = sha256[:COMMIT_SHA_MAX_LENGTH]
    page = [{"sha": sha256, "commit": {"message": "the watermark"}}]
    _stub(monkeypatch, lambda url, **_: _response(page))

    assert github_client.fetch_commits("owner/repo", since_sha=stored) == []


# -- Fields that are the wrong *type* rather than absent -------------------- #
#
# `payload.get(x) or ""` defends against a missing key and against nothing else.
# Every one of these reaches a `[:120]` that raises `TypeError`, a `.replace()`
# that raises `AttributeError`, or a database column that will not take the
# value — and none of those is a `GitHubError`.


def test_a_commit_whose_nested_object_is_a_string_does_not_raise(monkeypatch):
    """`payload["commit"]` as a string used to be `AttributeError` on `.get`."""
    page = [{"sha": "abc123", "commit": "not an object", "author": "nor this"}]
    _stub(monkeypatch, lambda url, **_: _response(page))

    commits = github_client.fetch_commits("owner/repo")

    assert commits[0].sha == "abc123"
    assert commits[0].message == ""
    assert commits[0].author == ""
    assert commits[0].committed_at is None


def test_a_non_string_sha_reads_as_empty_rather_than_reaching_the_column(monkeypatch):
    page = [{"sha": {"oid": "abc"}, "commit": {"message": "m"}}]
    _stub(monkeypatch, lambda url, **_: _response(page))

    assert github_client.fetch_commits("owner/repo")[0].sha == ""


def test_a_non_string_commit_date_is_not_a_timestamp(monkeypatch):
    """`_parse_ts` called `.replace` on it, which an int does not have."""
    page = [{"sha": "abc", "commit": {"message": "m", "author": {"date": 1700000000}}}]
    _stub(monkeypatch, lambda url, **_: _response(page))

    assert github_client.fetch_commits("owner/repo")[0].committed_at is None


def test_a_non_string_release_tag_reads_as_no_tag(monkeypatch):
    """It reached `[:120]`, and slicing an int is a `TypeError`."""
    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([{"tag_name": 2026, "body": ["a", "list"]}])
        if url.endswith("/commits"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo")

    assert activity.new_release.tag == ""
    assert activity.new_release.body == ""


def test_a_release_name_falls_back_to_the_cut_tag_not_the_raw_one(monkeypatch):
    """The fallback used to reach past the truncation to `tag_name` itself."""
    long_tag = "v" + "9" * 254

    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([{"tag_name": long_tag}])
        if url.endswith("/commits"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo")

    assert activity.new_release.name == long_tag[:RELEASE_TAG_MAX_LENGTH]


# -- Repos that moved under us --------------------------------------------- #


def test_a_force_pushed_branch_reports_the_whole_page_rather_than_nothing(monkeypatch):
    """The watermark is gone from history, so there is nothing to stop at.

    Over-reporting is the right failure: the post says "a lot has changed",
    which is true, rather than the scan silently deciding nothing has.
    """
    page = [{"sha": f"new{i}", "commit": {"message": f"m{i}"}} for i in range(100)]
    _stub(monkeypatch, lambda url, **_: _response(page))

    commits = github_client.fetch_commits("owner/repo", since_sha="rewritten-away")

    assert len(commits) == 100


def test_a_repo_that_has_moved_more_than_a_page_still_advances_its_watermark(
    monkeypatch,
):
    """The head of the page is HEAD, so the next scan starts from there.

    Without it the watermark would stay put and every scan would re-report the
    same hundred commits until the repo went quiet.
    """
    page = [{"sha": f"c{i}", "commit": {"message": f"m{i}"}} for i in range(100)]

    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([])
        if url.endswith("/commits"):
            return _response(page)
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo", since_sha="long-gone")

    assert activity.head_sha == "c0"
    assert len(activity.new_commits) == 100


def test_a_deleted_repo_is_a_not_found_rather_than_no_releases(monkeypatch):
    """The repo call 404s first, so the releases fallback never gets a say."""
    _stub(monkeypatch, lambda url, **_: _response({"message": "Not Found"}, status_code=404))

    with pytest.raises(github_client.GitHubNotFound):
        github_client.fetch_activity("owner/deleted")


def test_an_empty_repo_keeps_a_null_watermark_rather_than_inventing_one(monkeypatch):
    """No commits and no prior watermark. `head_sha` must stay None."""
    def handler(url, **_):
        if url.endswith("/releases"):
            return _response([])
        if url.endswith("/commits"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/empty")

    assert activity.head_sha is None
    assert not activity.has_news


# -- A branch that was rewritten under us ------------------------------------ #
#
# `fetch_commits` truncates the page at the watermark. When the watermark is not
# *in* the page the whole page comes back, and the module docstring calls that
# the right failure: over-reporting means the post says "a lot has changed",
# never that a change was missed. What makes it a recoverable failure rather
# than a permanent one is `head_sha` — it has to advance to the page's newest
# commit. Falling back to the vanished watermark would re-report the same page
# on every scan for as long as the branch stayed rewritten.


def test_a_force_push_that_orphans_the_watermark_reports_the_page_and_moves_on(
    monkeypatch,
):
    page = [
        {"sha": f"new{i}", "commit": {"message": f"rewritten {i}"}} for i in range(5)
    ]

    def handler(url, **_):
        if url.endswith("/commits"):
            return _response(page)
        if url.endswith("/releases"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo", since_sha="orphaned")

    assert len(activity.new_commits) == 5
    # The new HEAD, not "orphaned" — otherwise every later scan repeats this one.
    assert activity.head_sha == "new0"


def test_a_repo_that_moved_more_than_a_page_reports_the_page_and_moves_on(monkeypatch):
    """Same shape, different cause: the watermark is real but too far back."""
    page = [{"sha": f"c{i}", "commit": {"message": f"commit {i}"}} for i in range(100)]

    def handler(url, **_):
        if url.endswith("/commits"):
            return _response(page)
        if url.endswith("/releases"):
            return _response([])
        return _response(REPO)

    _stub(monkeypatch, handler)
    activity = github_client.fetch_activity("owner/repo", since_sha="a-thousand-back")

    assert len(activity.new_commits) == 100
    assert activity.head_sha == "c0"

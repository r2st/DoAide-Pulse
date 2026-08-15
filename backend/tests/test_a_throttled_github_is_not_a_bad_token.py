"""GitHub says "slow down" with the same status code it says "no" with.

Every other destination Herald publishes to uses 403 for one thing: this
credential is not allowed. GitHub uses it for two. Its *secondary* rate limit —
the burst limit, the one a sweep publishing several pieces at once actually
trips — comes back as a **403** with ``Retry-After``, while the hourly quota
sits nearly untouched.

:meth:`publishers.base.Adapter._translate` reads 403 as a rejected credential,
which is right everywhere else and wrong here, and the cost is not cosmetic. A
``CredentialError`` is terminal on purpose: no retry, the publication is failed
for good, and the piece goes to ``failed`` carrying "GitHub rejected the
credentials" — about a token that worked a second earlier and would work a
minute later. The only repair that message suggests is reissuing a PAT that was
never the problem.

``services.github_client`` already had to learn this distinction on the
autopilot's scan path; see the note on :func:`github_client._rate_limit` for the
matching mis-read there. This is the same discrimination on the publish path,
where the answer is to park the row until the time GitHub named.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.publishers.base import CredentialError, PublishError, RateLimited
from app.services.publishers.git import GitAdapter


def _response(
    status: int, *, headers: dict[str, str] | None = None, text: str = ""
) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, text=text)


@pytest.fixture
def adapter() -> GitAdapter:
    return GitAdapter()


def _raising(response: httpx.Response, calls: list[int]):
    """A ``_send`` that answers with *response* and counts how often it is asked."""

    def _send(*args, **kwargs):
        calls.append(1)
        return response

    return _send


# --------------------------------------------------------------------------- #
# The discrimination                                                           #
# --------------------------------------------------------------------------- #


def test_a_secondary_limit_403_parks_the_commit_instead_of_condemning_the_token(adapter):
    """The shape GitHub actually sends: 403, ``Retry-After``, quota untouched."""
    error = adapter._translate(
        _response(403, headers={"Retry-After": "45", "X-RateLimit-Remaining": "4831"})
    )

    assert isinstance(error, RateLimited), (
        f"a throttled publish came back as {type(error).__name__}, which is "
        "terminal — the publication would fail for good against a good token"
    )
    assert error.retry_after == 45


def test_a_throttle_403_with_no_retry_after_header_is_still_a_throttle(adapter):
    """GitHub does not always send the header; the body still says so.

    Both spellings are live — the API still returns the older "abuse detection"
    wording — so the body is the only signal left when the header is absent.
    """
    for wording in ("You have exceeded a secondary rate limit", "abuse detection"):
        error = adapter._translate(_response(403, text=wording))
        assert isinstance(error, RateLimited), wording
        assert error.retry_after is None


def test_a_spent_hourly_quota_is_a_throttle_not_a_refusal(adapter):
    """The primary limit: no ``Retry-After``, but ``Remaining`` is 0."""
    error = adapter._translate(
        _response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1786000000"})
    )

    assert isinstance(error, RateLimited)


def test_a_genuinely_bad_token_is_still_a_credential_error(adapter):
    """The half of 403 that was always read correctly, and must stay that way.

    No ``Retry-After``, quota untouched, nothing in the body about limits — a
    token that really is not allowed to write here. Answering this one with a
    deferral would park a publication that is never going to succeed and retry
    it until the attempts run out.
    """
    error = adapter._translate(
        _response(
            403,
            headers={"X-RateLimit-Remaining": "4900"},
            text='{"message": "Resource not accessible by personal access token"}',
        )
    )

    assert isinstance(error, CredentialError)
    assert not isinstance(error, RateLimited)


def test_a_401_is_left_exactly_where_it_was(adapter):
    """Only 403 and 429 are ambiguous. 401 means one thing."""
    assert isinstance(adapter._translate(_response(401)), CredentialError)


def test_a_429_still_carries_the_platforms_own_delay(adapter):
    error = adapter._translate(_response(429, headers={"Retry-After": "90"}))

    assert isinstance(error, RateLimited)
    assert error.retry_after == 90


def test_a_success_is_not_an_error(adapter):
    assert adapter._translate(_response(200)) is None


# --------------------------------------------------------------------------- #
# What the user is told                                                        #
# --------------------------------------------------------------------------- #


def test_the_message_does_not_send_the_user_after_the_wrong_token(adapter):
    """``github_client`` words this for the autopilot, where the advice differs.

    There the token is Herald's own ``GITHUB_TOKEN`` and "set one to raise the
    ceiling from 60 to 5000" is the fix. Here it is the user's PAT, nothing
    they can set moves this limit, and the row is already parked — so repeating
    that advice would be telling them to go and change an unrelated setting.
    """
    message = str(
        adapter._translate(_response(403, headers={"Retry-After": "45"}))
    )

    assert "GITHUB_TOKEN" not in message
    assert "45s" in message
    assert "credential" not in message.lower()


# --------------------------------------------------------------------------- #
# Through the request path                                                     #
# --------------------------------------------------------------------------- #


def test_the_throttle_is_raised_rather_than_hammered(adapter, monkeypatch):
    """403 is not in ``_TRANSIENT_STATUSES``, and should not become so.

    The in-process retry loop can only hold a worker for its backoff ceiling.
    Replaying a burst limit inside that window is how a soft limit becomes a
    ban, so this must leave the adapter on the first answer and let
    ``publishing_service._defer`` park it for the full delay GitHub asked for.
    """
    calls: list[int] = []
    monkeypatch.setattr(
        GitAdapter,
        "_send",
        _raising(_response(403, headers={"Retry-After": "60"}), calls),
    )

    with pytest.raises(RateLimited):
        adapter._request("PUT", "https://api.github.com/x", headers={})

    assert calls == [1], f"the burst limit was replayed {len(calls)} times in-process"


def test_a_throttled_lookup_does_not_report_the_post_as_new(adapter, monkeypatch):
    """``_existing_sha`` swallows ``PublishError`` as "no file there".

    That is right for a 404 and wrong for a throttle: "I was not allowed to
    look" is not "there is nothing there", and the difference decides whether
    the commit that follows carries a ``sha``. Swallowed, a re-publish of a
    live post is sent as a create, which the contents API refuses — so a
    correction to a published piece would fail as a 422 naming a file the user
    can see perfectly well in their own repo.

    ``RateLimited`` is a subclass of ``PublishError``, so this is the exact
    hole that opens if the classification above lands without it.
    """
    monkeypatch.setattr(
        GitAdapter,
        "_send",
        _raising(_response(403, headers={"Retry-After": "30"}), []),
    )

    with pytest.raises(RateLimited):
        adapter._existing_sha("r2st/blog", "src/content/blog/x.md", "", "ghp_test")


def test_a_missing_file_is_still_just_a_new_post(adapter, monkeypatch):
    """The ordinary case the swallow exists for, unchanged."""
    monkeypatch.setattr(GitAdapter, "_send", _raising(_response(404), []))

    assert adapter._existing_sha("r2st/blog", "src/content/blog/x.md", "", "t") is None


def test_a_bad_token_on_the_lookup_still_surfaces(adapter, monkeypatch):
    """The other thing ``_existing_sha`` refuses to silence."""
    monkeypatch.setattr(
        GitAdapter,
        "_send",
        _raising(_response(403, headers={"X-RateLimit-Remaining": "4900"}), []),
    )

    with pytest.raises(CredentialError):
        adapter._existing_sha("r2st/blog", "src/content/blog/x.md", "", "t")


def test_every_throttle_is_a_publish_error_so_nothing_upstream_leaks_it(adapter):
    """``RateLimited`` stays inside the hierarchy ``publishing_service`` catches."""
    error = adapter._translate(_response(403, headers={"Retry-After": "5"}))

    assert isinstance(error, PublishError)

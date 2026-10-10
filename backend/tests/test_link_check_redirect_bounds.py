"""The link checker's remaining arms: no host, a redirect loop, the real pool.

:mod:`tests.test_link_check` drives :func:`check_url` directly with a mock
transport. These cover the two shapes it cannot reach that way — a URL with
nothing to resolve, and a server that keeps redirecting — plus :func:`check`
itself, whose thread pool the single-URL tests step around entirely.

The redirect budget matters for the same reason the per-hop SSRF check does: a
hostile or merely broken server controls how many times Pulse is willing to go
round, and "until something gives" is not an answer inside a publish request.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import link_check


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


# --------------------------------------------------------------------------- #
# Nothing to resolve                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", ["https://", "http:///just/a/path"])
def test_a_url_with_no_hostname_is_broken_without_a_request(url):
    """No request could settle it, so there is no point making one."""
    verdict = link_check.check_url(url, client=_client(lambda request: httpx.Response(200)))

    assert verdict.status == link_check.BROKEN
    assert "cannot resolve" in verdict.detail
    assert verdict.http_status is None


def test_the_same_answer_is_given_to_the_webhook_pre_flight():
    assert "cannot resolve" in (link_check.unreachable_reason("https://") or "")


# --------------------------------------------------------------------------- #
# Redirect budget                                                              #
# --------------------------------------------------------------------------- #


def test_a_redirect_loop_is_unknown_rather_than_an_endless_walk(monkeypatch):
    """Ten hops and Pulse stops. The verdict is ``unknown``, not ``broken``.

    A server that loops is misconfigured, which is not evidence that the page
    the author linked to is gone — and only ``broken`` is allowed to stop a
    publish.
    """
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)
    hops = 0

    def _always_redirect(request):
        nonlocal hops
        hops += 1
        return httpx.Response(302, headers={"location": f"https://example.com/{hops}"})

    with _client(_always_redirect) as client:
        verdict = link_check.check_url("https://example.com/start", client=client)

    assert verdict.status == link_check.UNKNOWN
    assert "redirect" in verdict.detail.lower()
    # Bounded, and bounded where the constant says.
    assert hops == link_check._MAX_REDIRECTS


def test_a_redirect_with_no_location_header_settles_on_that_response(monkeypatch):
    """A 301 that does not say where is not a redirect anyone can follow.

    Answered as the 3xx it is — ``ok`` — rather than chased into the redirect
    budget or reported as broken.
    """
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)

    with _client(lambda request: httpx.Response(301)) as client:
        verdict = link_check.check_url("https://example.com/x", client=client)

    assert verdict.status == link_check.OK
    assert verdict.http_status == 301


# --------------------------------------------------------------------------- #
# The pool                                                                     #
# --------------------------------------------------------------------------- #


def test_check_returns_a_verdict_per_url_in_the_order_given(monkeypatch):
    """The results must line up with the input — ``pool.map``, not ``submit``.

    Out-of-order results would attach one link's verdict to another link's URL,
    which is the one way a link checker can be worse than none.
    """
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)

    codes = {"/a": 200, "/b": 404, "/c": 500}

    def _by_path(request):
        return httpx.Response(codes[request.url.path])

    # Captured before the patch: ``link_check.httpx`` is the module itself, so
    # the replacement would otherwise call itself.
    real_client = httpx.Client
    monkeypatch.setattr(
        link_check.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(_by_path), follow_redirects=False
        ),
    )

    verdicts = link_check.check(
        ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    )

    assert [v.url.rsplit("/", 1)[-1] for v in verdicts] == ["a", "b", "c"]
    assert [v.status for v in verdicts] == [
        link_check.OK,
        link_check.BROKEN,
        link_check.UNKNOWN,
    ]
    assert [v.url for v in link_check.broken(verdicts)] == ["https://example.com/b"]


def test_check_of_an_empty_list_never_builds_a_client(monkeypatch):
    def _explode(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("no client should be built for no urls")

    monkeypatch.setattr(link_check.httpx, "Client", _explode)

    assert link_check.check([]) == []

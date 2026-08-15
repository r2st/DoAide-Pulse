"""What the link checker fetches, which should be nothing.

:func:`app.services.link_check.check_url` decides a verdict from a status code
and, on a redirect, a ``Location`` header. It has never wanted a body. But
``httpx.Client.request`` reads one before it returns — that is what makes
``.text`` available on the object it hands back — so every check was
downloading a response in full and discarding it.

On the HEAD that starts every check, that costs nothing: there is no body. The
GET is the one that mattered, and the reason it exists is what makes it sharp:
it is a retry for hosts that answer 403/405/501 to HEAD, so the responses
Herald actually ends up downloading are exactly the ones it did not choose.
Everything about the size is somebody else's decision — the URL comes out of a
user's document, the host decides what to send, and :func:`check` runs
``link_check_max_urls`` of them concurrently inside a request a person is
waiting on. There was no cap anywhere on that path.

``app.services.feeds`` already had this problem and solved it by streaming to a
byte cap, because a feed body is wanted. Here the cap is zero, so the fix is
smaller: stream the response and close it unread.

These tests assert the absence of a read, which needs a stream that can tell.
``_TattleStream`` records whether anything iterated it; ``_HugeStream`` would
hang the suite for a minute if anything did.
"""
from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from app.services import link_check

URL = "https://example.com/report.pdf"


class _TattleStream(httpx.SyncByteStream):
    """A body that remembers being read."""

    def __init__(self, payload: bytes = b"x" * 2048) -> None:
        self.payload = payload
        self.read = False

    def __iter__(self) -> Iterator[bytes]:
        self.read = True
        yield self.payload


class _HugeStream(httpx.SyncByteStream):
    """The 8 GB link, stopped one chunk in.

    A stream that really yielded 8 GB would make a regression here an
    out-of-memory kill somewhere in the suite rather than a failing test, which
    is a worse way to learn the same thing. So it announces the size it would
    have been and refuses to be the one that proves it.
    """

    WOULD_BE_BYTES = 8 * 1024 * 1024 * 1024

    def __init__(self) -> None:
        self.chunks_read = 0

    def __iter__(self) -> Iterator[bytes]:
        self.chunks_read += 1
        yield b"x" * (1024 * 1024)
        raise AssertionError(
            "the link checker read the body — this response is "
            f"{self.WOULD_BE_BYTES // (1024**3)} GB and there are "
            "`link_check_max_urls` of it in flight at once"
        )


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


# --------------------------------------------------------------------------- #
# The ordinary path                                                            #
# --------------------------------------------------------------------------- #


def test_a_link_that_answers_head_is_never_downloaded():
    stream = _TattleStream()

    def _handler(request):
        assert request.method == "HEAD"
        return httpx.Response(200, stream=stream)

    with _client(_handler) as client:
        status = link_check.check_url(URL, client=client)

    assert status.status == link_check.OK
    assert status.http_status == 200
    assert stream.read is False


def test_the_get_retry_is_not_a_download_either():
    """The retry for hosts that refuse HEAD — the one that was downloading.

    405 to HEAD is not unusual and not hostile; it is how a large share of the
    web answers. Which is why this is the path that decides whether a document
    full of links to PDFs is a memory problem.
    """
    stream = _TattleStream()
    seen: list[str] = []

    def _handler(request):
        seen.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, stream=stream)

    with _client(_handler) as client:
        status = link_check.check_url(URL, client=client)

    assert seen == ["HEAD", "GET"]
    assert status.status == link_check.OK
    assert stream.read is False


@pytest.mark.parametrize("code", [403, 405, 501])
def test_every_head_refusal_retries_without_downloading(code):
    stream = _TattleStream()

    def _handler(request):
        if request.method == "HEAD":
            return httpx.Response(code)
        return httpx.Response(200, stream=stream)

    with _client(_handler) as client:
        assert link_check.check_url(URL, client=client).status == link_check.OK
    assert stream.read is False


def test_a_huge_body_does_not_have_to_be_read_to_be_judged():
    """The whole point, in the shape it would actually arrive in.

    Eight gigabytes behind a link in a draft. If the body is ever consumed this
    test stops being fast and starts being a hang, which is the failure mode
    worth having for a regression that is otherwise invisible until production
    runs out of memory.
    """
    body = _HugeStream()

    def _handler(request):
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, stream=body)

    with _client(_handler) as client:
        status = link_check.check_url(URL, client=client)

    assert status.status == link_check.OK
    assert body.chunks_read == 0


# --------------------------------------------------------------------------- #
# The paths that still have to work                                            #
# --------------------------------------------------------------------------- #


def test_a_redirect_chain_is_followed_without_reading_a_hop():
    """Each hop is still checked for SSRF, and no hop's body is read.

    The redirect walk is hand-rolled precisely so every ``Location`` is
    validated before the client connects (see ``_follow_safely``), and it reads
    the header off a response — so it is the code most likely to be broken by
    closing that response early.
    """
    streams = [_TattleStream(), _TattleStream(), _TattleStream()]

    def _handler(request):
        path = request.url.path
        if path == "/one":
            return httpx.Response(
                302, headers={"location": "https://example.com/two"}, stream=streams[0]
            )
        if path == "/two":
            return httpx.Response(
                301, headers={"location": "https://example.com/three"}, stream=streams[1]
            )
        return httpx.Response(200, stream=streams[2])

    with _client(_handler) as client:
        status = link_check.check_url("https://example.com/one", client=client)

    assert status.status == link_check.OK
    assert [s.read for s in streams] == [False, False, False]


def test_a_redirect_to_a_private_address_is_still_caught():
    """The SSRF guard is the reason the walk is hand-rolled. Unchanged by this."""

    def _handler(request):
        if request.url.path == "/one":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200)  # pragma: no cover — must never be reached

    with _client(_handler) as client:
        status = link_check.check_url("https://example.com/one", client=client)

    assert status.status == link_check.BROKEN
    assert "private address" in status.detail


def test_a_404_is_still_broken_and_a_500_is_still_unknown():
    """The verdicts themselves, so "reads nothing" cannot pass by doing nothing."""

    def _handler(request):
        code = int(request.url.path.strip("/"))
        return httpx.Response(code, stream=_TattleStream())

    with _client(_handler) as client:
        assert link_check.check_url("https://example.com/404", client=client).status == (
            link_check.BROKEN
        )
        assert link_check.check_url("https://example.com/500", client=client).status == (
            link_check.UNKNOWN
        )


def test_the_connection_is_released_after_each_check():
    """Closing a stream early must not leak the connection it was on.

    Fifty checks through one client is more than the pool holds, so a response
    left open would surface here as a hang rather than in production as a
    checker that works until the day somebody has a lot of links.
    """

    def _handler(request):
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, stream=_TattleStream())

    with _client(_handler) as client:
        for i in range(50):
            assert link_check.check_url(f"{URL}?{i}", client=client).status == (
                link_check.OK
            )

"""A body cap that only reads ``Content-Length`` is not a cap.

HTTP/1.1 lets a client send ``Transfer-Encoding: chunked`` and no
``Content-Length`` at all. The guard in :mod:`app.main` read the header, found
nothing, and waved the request through — after which the route reached for
``await request.json()`` and the server buffered whatever was on the way. One
anonymous request, unbounded memory, no login required: the header check was
the whole cap, and the header is the client's to omit.

So the cap has two halves now, and this file holds both to the same number:

* the declared body, refused before it is read, which is the polite half and
  the one that lets the client find out *why*;
* the undeclared one, counted as it is consumed and cut off at the same
  boundary, which is the half that has to hold when the client is not being
  polite.

The counting half is asserted three ways — that it stops an oversized stream,
that it does not stop a legitimate one, and that it stops it having read a
bounded amount rather than after swallowing the lot — because a cap that fires
only once the whole body is in memory has already lost.
"""
from __future__ import annotations

from collections.abc import Iterator

import anyio
import httpx

from app.main import MAX_BODY_BYTES, BodySizeLimitMiddleware

#: Chunk size for the streamed bodies below. Big enough that a body is a
#: handful of chunks rather than thousands, small enough that the cap lands
#: partway through one.
_CHUNK = 64 * 1024


def _chunks(total: int) -> Iterator[bytes]:
    """A generator body: httpx sends this ``chunked``, with no Content-Length."""
    sent = 0
    while sent < total:
        size = min(_CHUNK, total - sent)
        sent += size
        yield b"x" * size


def test_a_generator_body_really_does_arrive_undeclared():
    """The premise, asserted rather than assumed.

    Every test below sends its body as a generator on the understanding that
    httpx turns that into a chunked request with no ``Content-Length``. If that
    ever stopped being true they would all still pass, while exercising the
    header check they were written to get past.
    """
    built = httpx.Request(
        "POST",
        "http://testserver/api/v1/auth/register",
        content=_chunks(_CHUNK * 2),
    )

    assert "content-length" not in built.headers
    assert built.headers.get("transfer-encoding") == "chunked"


def test_an_undeclared_oversized_body_is_refused(client):
    """The hole: no ``Content-Length``, and a body well past the cap."""
    resp = client.post(
        "/api/v1/auth/register",
        content=_chunks(MAX_BODY_BYTES * 3),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


# ---- Driving the middleware directly -------------------------------------- #
#
# The end-to-end tests above answer "is it refused". They cannot answer "how
# much was read first": Starlette's TestClient drains the whole generator into
# a request before any of it reaches the app, so a byte counter wrapped around
# the client body measures the client. The reads that matter happen on the
# server's side of that boundary, so the tests below stand the middleware up on
# its own and count what it pulls from the receive channel.


def _drive(
    chunk_sizes: list[int], *, headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[list[int], list[dict]]:
    """Run the middleware over a body-reading stub app.

    Returns the chunk sizes it actually asked the channel for, and the ASGI
    messages it let out.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/anything",
        "raw_path": b"/anything",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json"), *(headers or [])],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }
    handed: list[int] = []
    sent: list[dict] = []
    remaining = list(chunk_sizes)

    async def receive():
        if not remaining:
            return {"type": "http.request", "body": b"", "more_body": False}
        size = remaining.pop(0)
        handed.append(size)
        return {
            "type": "http.request",
            "body": b"x" * size,
            "more_body": bool(remaining),
        }

    async def send(message):
        sent.append(message)

    async def _reads_the_whole_body(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    anyio.run(BodySizeLimitMiddleware(_reads_the_whole_body), scope, receive, send)
    return handed, sent


def _status(sent: list[dict]) -> int:
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    return start["status"]


def test_the_stream_is_cut_off_rather_than_swallowed():
    """It has to stop *reading*, not just stop answering.

    A guard that consumes the whole body and then returns 413 has already paid
    the memory it exists to protect. A hundred times the cap is offered, and no
    more than one chunk past it may be taken.
    """
    handed, sent = _drive([_CHUNK] * (MAX_BODY_BYTES * 100 // _CHUNK))

    assert _status(sent) == 413
    assert sum(handed) <= MAX_BODY_BYTES + _CHUNK


def test_a_declared_oversize_is_refused_without_reading_a_byte():
    """The cheap half earns its place by not touching the channel at all."""
    handed, sent = _drive(
        [_CHUNK] * 32,
        headers=[(b"content-length", str(MAX_BODY_BYTES + 1).encode())],
    )

    assert _status(sent) == 413
    assert handed == []


def test_the_app_s_own_answer_is_dropped_once_the_cap_is_hit():
    """The stub app answers 200 to a truncated body. That must not be the reply.

    This is the failure the flag exists to prevent: the app sees a short body,
    makes what it can of it, and answers as if that were the request. Only one
    response goes out, and it is the 413.
    """
    _, sent = _drive([_CHUNK] * (MAX_BODY_BYTES * 2 // _CHUNK))

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert _status(sent) == 413


def test_a_body_under_the_cap_reaches_the_app_untouched():
    """Every chunk handed over, and the app's own answer let through."""
    handed, sent = _drive([_CHUNK] * 4)

    assert sum(handed) == _CHUNK * 4
    assert _status(sent) == 200


def test_an_undeclared_body_under_the_cap_is_let_through(client):
    """The cap must not become a ban on streamed requests.

    A well-formed registration sent chunked reaches the route and is answered on
    its merits — here a 422, because the body is not a registration.
    """
    resp = client.post(
        "/api/v1/auth/register",
        content=_chunks(1024),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 422, resp.text


def test_a_body_exactly_at_the_cap_is_allowed(client):
    """The boundary is inclusive, and the same one on both paths.

    Off by one here is a legitimate request refused, which is the more expensive
    direction of the two to get wrong.
    """
    declared = client.post(
        "/api/v1/auth/register",
        content=b"x" * MAX_BODY_BYTES,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(MAX_BODY_BYTES),
        },
    )
    streamed = client.post(
        "/api/v1/auth/register",
        content=_chunks(MAX_BODY_BYTES),
        headers={"Content-Type": "application/json"},
    )

    # Not 413: both reach the route, which rejects a megabyte of `x` as invalid
    # JSON. What matters is that the cap was not the thing that stopped them.
    assert declared.status_code != 413
    assert streamed.status_code != 413
    assert declared.status_code == streamed.status_code


def test_the_refusal_still_carries_the_security_headers(client):
    """The 413 is written by middleware, not by a route.

    Security headers are applied by a middleware sitting outside this one, so a
    response produced *here* rather than by the router still has to come out
    dressed like every other response. A hand-rolled ASGI response is exactly
    the kind that quietly stops doing that.
    """
    resp = client.post(
        "/api/v1/auth/register",
        content=_chunks(MAX_BODY_BYTES * 3),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"


def test_a_websocket_scope_is_not_touched():
    """The middleware only understands HTTP and must pass everything else on.

    There are no WebSocket routes today; the guard is that adding one does not
    mean discovering this middleware crashes on a scope with no headers.
    """
    seen: list[str] = []

    async def _app(scope, receive, send):
        seen.append(scope["type"])

    anyio.run(
        BodySizeLimitMiddleware(_app),
        {"type": "websocket"},
        _unused_receive,
        _unused_send,
    )

    assert seen == ["websocket"]


async def _unused_receive():  # pragma: no cover - never called
    raise AssertionError("the passthrough must not read")


async def _unused_send(message):  # pragma: no cover - never called
    raise AssertionError("the passthrough must not write")

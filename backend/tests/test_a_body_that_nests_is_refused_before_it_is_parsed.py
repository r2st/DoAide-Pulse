"""A byte cap does not constrain shape, and shape is what the parser pays for.

``MAX_BODY_BYTES`` stops a body that is too *big*. It says nothing about a body
that is small and pathological: ``[`` repeated forty thousand times is 40 KB,
sails through a 1 MB cap, and costs the JSON parser one C-stack frame per
character on the way down.

This is not a crash. Python's parser stops itself at the recursion limit and
FastAPI turns the resulting ``RecursionError`` into a 400, which is a
respectable outcome and the reason this is a hardening test rather than an
incident. What it is instead is a bad trade: the 400 arrives only *after* the
descent has been paid for, the route it happens on needs no login, and the
attacker's side of the exchange is forty thousand identical bytes. Counting
brackets as the body streams past costs a comparison per byte and refuses the
same request before the parser is handed it.

The interesting part is not the counter, it is the streaming. ASGI hands a body
over in chunks split at arbitrary byte offsets, and the split lands inside a
string literal as readily as between two tokens. A scanner that reset per chunk,
or that decoded each chunk as text, would reject perfectly ordinary bodies — so
the state that has to survive a chunk boundary is pinned here one boundary at a
time: an open string, a pending backslash, and a multi-byte character sawn in
half.

The status is a 400 rather than the 413 its neighbour returns, deliberately. The
body is a perfectly acceptable size; it is the shape that is refused, and
"payload too large" would send whoever reads it to look at the wrong limit.
"""
from __future__ import annotations

import anyio

from app.main import MAX_JSON_DEPTH, BodySizeLimitMiddleware, _is_json, _JSONDepthScanner

# --------------------------------------------------------------------------- #
# End to end, through the real app                                             #
# --------------------------------------------------------------------------- #


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def test_a_deeply_nested_body_is_refused(client):
    resp = client.post(
        "/api/v1/auth/register",
        content=_nested(20_000),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "nested too deeply" in resp.json()["detail"]


def test_the_refusal_happens_without_a_login(client):
    """The guard has to sit in front of authentication to be worth having.

    If the cost were only payable by an authenticated caller it would be a
    quota problem. It is payable by anyone who can open a socket, which is what
    makes it a limit rather than a policy.
    """
    resp = client.post(
        "/api/v1/content",
        content=_nested(20_000),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "nested too deeply" in resp.json()["detail"]


def test_a_body_at_the_limit_is_allowed_through(client):
    """The cap must not clip a body that merely reaches it.

    A 422 here is the *route* rejecting the payload on its schema, which is
    proof the middleware let it past — the middleware's own refusal is the 400
    asserted above.
    """
    resp = client.post(
        "/api/v1/auth/register",
        content=_nested(MAX_JSON_DEPTH),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_one_level_past_the_limit_is_refused(client):
    resp = client.post(
        "/api/v1/auth/register",
        content=_nested(MAX_JSON_DEPTH + 1),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_an_ordinary_registration_is_untouched(client):
    """The shape a real caller sends is nowhere near the cap.

    The 201 is the *route* answering, which is the point: the response came
    from the application rather than from the middleware, which would have
    refused with a 400 before the route ever ran.
    """
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "nested@example.com",
            "password": "correct-horse-battery",
            "full_name": "Nested Tester",
        },
    )
    assert resp.status_code == 201


def test_brackets_inside_a_string_are_not_structure(client):
    """``{`` in a title is a character, not a level.

    Herald bodies carry code samples, and a fenced block full of braces is the
    most ordinary thing a post about software contains. A counter that did not
    track string literals would refuse them.
    """
    body = '{"a":"' + "{" * (MAX_JSON_DEPTH * 20) + '"}'
    resp = client.post(
        "/api/v1/auth/register",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_a_form_encoded_body_is_not_scanned_as_json(client):
    """Login is form-encoded, and a form is not parsed by the JSON parser.

    The scanner is gated on the declared content type because that is what
    decides whether the recursive descent it guards ever runs. Scanning a form
    body would be counting brackets that cost nothing.
    """
    resp = client.post(
        "/api/v1/auth/login",
        content="username=" + "[" * 50_000 + "&password=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# The scanner, one chunk boundary at a time                                    #
# --------------------------------------------------------------------------- #


def test_the_scanner_counts_nesting():
    assert _JSONDepthScanner().feed(b'{"a":{"b":[1]}}') == 3


def test_the_scanner_reports_the_deepest_point_not_the_final_one():
    """Depth returns to zero at the end of every valid body.

    A scanner reporting its *current* depth would read a complete document as
    depth zero and never fire at all.
    """
    assert _JSONDepthScanner().feed(b"[[[]]] [] [[]]") == 3


def test_depth_survives_a_chunk_boundary():
    scanner = _JSONDepthScanner()
    scanner.feed(b"[[[")
    assert scanner.feed(b"[[") == 5


def test_a_string_left_open_across_a_boundary_stays_open():
    """The boundary falls mid-string; the brackets after it are still text."""
    scanner = _JSONDepthScanner()
    scanner.feed(b'{"a":"[[[')
    assert scanner.feed(b'[[["}') == 1


def test_an_escaped_quote_does_not_close_the_string():
    assert _JSONDepthScanner().feed(b'{"a":"x\\"[[[["}') == 1


def test_a_backslash_split_across_a_boundary_still_escapes():
    """The chunk ends on the backslash and the quote it escapes is in the next.

    A scanner that dropped its pending-escape flag at the boundary would read
    that quote as closing the string and start counting the following brackets
    as structure.
    """
    scanner = _JSONDepthScanner()
    scanner.feed(b'{"a":"x\\')
    assert scanner.feed(b'"[[[["}') == 1


def test_an_escaped_backslash_does_not_escape_the_next_quote():
    """``"a\\\\"`` ends the string; the brackets after it are real structure."""
    assert _JSONDepthScanner().feed(b'{"a":"x\\\\"}') == 1
    assert _JSONDepthScanner().feed(b'["x\\\\",[[]]]') == 3


def test_a_multibyte_character_split_across_a_boundary_does_not_raise():
    """A chunk can end mid-UTF-8, which is why the scanner reads bytes.

    Decoding half of an em dash raises ``UnicodeDecodeError``; scanning its
    bytes finds no brackets, because every continuation byte is >= 0x80 and no
    ASCII bracket can hide in one.
    """
    text = '{"a":"—"}'.encode()
    scanner = _JSONDepthScanner()
    scanner.feed(text[:7])
    assert scanner.feed(text[7:]) == 1


def test_unbalanced_closers_do_not_buy_extra_headroom():
    """Depth is clamped at zero on the way up.

    Without the clamp, a body could open with a run of ``]]]]`` to drive the
    counter negative and then nest that many levels for free.
    """
    scanner = _JSONDepthScanner()
    scanner.feed(b"]" * 100)
    assert scanner.feed(b"[[[") == 3


def test_an_empty_chunk_changes_nothing():
    scanner = _JSONDepthScanner()
    scanner.feed(b"[[")
    assert scanner.feed(b"") == 2


# --------------------------------------------------------------------------- #
# Which bodies get scanned at all                                              #
# --------------------------------------------------------------------------- #


def _scope(content_type: str | None) -> dict:
    headers = [] if content_type is None else [(b"content-type", content_type.encode())]
    return {"type": "http", "headers": headers}


def test_content_types_that_are_scanned():
    for declared in (
        "application/json",
        "application/json; charset=utf-8",
        "APPLICATION/JSON",
        "  application/json  ",
        "application/merge-patch+json",
        "application/vnd.api+json",
    ):
        assert _is_json(_scope(declared)), declared


def test_content_types_that_are_not():
    for declared in (
        None,
        "",
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/jsonp",
    ):
        assert not _is_json(_scope(declared)), declared


# --------------------------------------------------------------------------- #
# The cut-off, at the ASGI seam                                                #
# --------------------------------------------------------------------------- #


async def _reads_everything(scope, receive, send):
    while True:
        message = await receive()
        if message["type"] == "http.disconnect" or not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


def _drive(chunks: list[bytes]) -> tuple[list[bytes], list[dict]]:
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
        "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }
    handed: list[bytes] = []
    sent: list[dict] = []
    remaining = list(chunks)

    async def receive():
        if not remaining:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = remaining.pop(0)
        handed.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(remaining)}

    async def send(message):
        sent.append(message)

    anyio.run(BodySizeLimitMiddleware(_reads_everything), scope, receive, send)
    return handed, sent


def test_the_stream_is_cut_off_at_the_offending_chunk():
    """The rest of the body is never asked for.

    Reading to the end and *then* refusing would pay the bandwidth the cap
    exists to refuse. Ten chunks are offered; the second one crosses the limit,
    and no further chunk is taken.
    """
    handed, sent = _drive([b"[" * (MAX_JSON_DEPTH - 1)] + [b"[" * 1000] * 9)

    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    assert start["status"] == 400
    assert len(handed) == 2


def test_the_apps_answer_to_a_truncated_body_is_dropped():
    """The stub answers 200 to what it was given. Only the 400 goes out."""
    _, sent = _drive([b"[" * (MAX_JSON_DEPTH + 1)])

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert [m for m in sent if m["type"] == "http.response.start"][0]["status"] == 400


def test_a_shallow_streamed_body_reaches_the_app():
    handed, sent = _drive([b'{"a":', b'[1,2,3]', b"}"])

    assert len(handed) == 3
    assert [m for m in sent if m["type"] == "http.response.start"][0]["status"] == 200

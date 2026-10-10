"""Response body capping prevents oversized responses from exhausting memory.

Three integration points now cap response bodies:
- webhooks.py streams and caps at 4 KB (only needs status + 200-char excerpt)
- publishers/base.py truncates at 1 MB (needs status + short error text)
- github_client.py truncates at 1 MB (JSON payloads, bounded by GitHub)
"""
from __future__ import annotations

import httpx
import pytest

from app.services import webhooks
from app.services.publishers import base


class _OversizedTransport(httpx.BaseTransport):
    """Returns a response whose body exceeds the cap."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self._status = status

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status, content=self._body, request=request)


# -- webhooks ---------------------------------------------------------------- #


def test_webhook_response_body_is_capped():
    """A malicious webhook endpoint returning a huge body is truncated."""
    huge_body = b"X" * (webhooks._MAX_WEBHOOK_RESPONSE_BYTES * 3)
    transport = _OversizedTransport(huge_body, status=500)
    client = httpx.Client(transport=transport, timeout=10)

    with client.stream("POST", "https://example.test/hook", content=b"{}") as resp:
        text = webhooks._read_capped(resp)

    assert len(text) <= webhooks._MAX_WEBHOOK_RESPONSE_BYTES


def test_webhook_small_response_is_fully_read():
    """A normal-sized response is read completely."""
    body = b"error details here"
    transport = _OversizedTransport(body, status=400)
    client = httpx.Client(transport=transport, timeout=10)

    with client.stream("POST", "https://example.test/hook", content=b"{}") as resp:
        text = webhooks._read_capped(resp)

    assert text == "error details here"


# -- publishers/base -------------------------------------------------------- #


def test_publisher_response_body_is_capped():
    """An oversized platform response is truncated after read."""
    huge = b"Z" * (base._MAX_RESPONSE_BYTES * 2)
    resp = httpx.Response(502, content=huge, request=httpx.Request("GET", "https://api.test"))
    capped = base._capped(resp)
    assert len(capped.content) == base._MAX_RESPONSE_BYTES


def test_publisher_normal_response_is_untouched():
    """A normal-sized response is returned as-is."""
    body = b'{"ok": true}'
    resp = httpx.Response(200, content=body, request=httpx.Request("GET", "https://api.test"))
    capped = base._capped(resp)
    assert capped.content == body
    assert capped.json() == {"ok": True}

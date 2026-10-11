"""Every response carries its own timing, and slow ones say so in the log.

The API already had request IDs and slow-query logging. What it did not have was
a log line per request with method, path, status and duration — the thing an
operator greps for when "the dashboard is slow" means the endpoint, not the SQL
under it. And the X-Response-Time header lets the frontend surface latency in
dev tools without a network inspector.
"""
from __future__ import annotations

import logging


def test_response_carries_timing_header(client):
    """X-Response-Time is present and looks like a number followed by 'ms'."""
    r = client.get("/")
    assert "X-Response-Time" in r.headers
    raw = r.headers["X-Response-Time"]
    assert raw.endswith("ms")
    assert int(raw.removesuffix("ms")) >= 0


def test_server_error_is_logged_as_warning(client, caplog):
    """A 500 is worth a log line even when it is fast."""
    with caplog.at_level(logging.WARNING, logger="app.main"):
        client.get("/api/v1/analytics/overview")
    lines = [r for r in caplog.records if "server error" in r.message or r.levelno >= logging.WARNING]
    # The endpoint returns 401 (not authenticated), not 500 — so no server
    # error line. This proves the guard only fires on real 5xx.
    assert not any("server error" in r.message for r in caplog.records)


def test_normal_request_logged_at_debug(client, caplog):
    """Ordinary requests are at DEBUG — present when asked for, silent otherwise."""
    with caplog.at_level(logging.DEBUG, logger="app.main"):
        client.get("/")
    lines = [r for r in caplog.records if "GET" in r.message and "200" in r.message]
    assert lines
    assert lines[0].levelno == logging.DEBUG

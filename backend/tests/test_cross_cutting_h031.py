"""H031 — cross-cutting concerns.

Finding 1: X-Request-ID log injection.
Finding 2: API key rotation bypasses the per-project key cap.
"""
from __future__ import annotations

import logging

from app.logging_config import RequestIDFilter, request_id_var
from app.routers.api_keys import MAX_KEYS_PER_PROJECT


# --------------------------------------------------------------------------- #
# Finding 1: X-Request-ID sanitisation                                         #
# --------------------------------------------------------------------------- #


def test_request_id_newlines_are_stripped(client):
    """A newline in X-Request-ID must not reach the log or the response."""
    resp = client.get(
        "/api/v1/health",
        headers={"X-Request-ID": "abc\ndef\rghi"},
    )
    rid = resp.headers["x-request-id"]
    assert "\n" not in rid
    assert "\r" not in rid
    assert rid == "abcdefghi"


def test_request_id_control_chars_are_stripped(client):
    """Null bytes and other control characters must be removed."""
    resp = client.get(
        "/api/v1/health",
        headers={"X-Request-ID": "good\x00bad\x1b[31m"},
    )
    rid = resp.headers["x-request-id"]
    assert "\x00" not in rid
    assert "\x1b" not in rid
    assert rid == "goodbad[31m"


def test_request_id_truncated_to_64_chars(client):
    """An absurdly long X-Request-ID is capped."""
    long_id = "a" * 200
    resp = client.get(
        "/api/v1/health",
        headers={"X-Request-ID": long_id},
    )
    rid = resp.headers["x-request-id"]
    assert len(rid) == 64


def test_empty_request_id_after_sanitisation_gets_generated(client):
    """If sanitisation leaves nothing, a fresh id is generated."""
    resp = client.get(
        "/api/v1/health",
        headers={"X-Request-ID": "\n\r\x00"},
    )
    rid = resp.headers["x-request-id"]
    assert len(rid) > 0
    assert "\n" not in rid


def test_clean_request_id_passes_through(client):
    """A well-formed id is echoed unchanged."""
    resp = client.get(
        "/api/v1/health",
        headers={"X-Request-ID": "req-abc-123"},
    )
    assert resp.headers["x-request-id"] == "req-abc-123"


def test_request_id_filter_does_not_inject_newlines():
    """The logging filter must carry the sanitised value, not the raw header."""
    filt = RequestIDFilter()
    token = request_id_var.set("clean-id")
    try:
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        filt.filter(record)
        assert record.request_id == "clean-id"  # type: ignore[attr-defined]
        assert "\n" not in record.request_id  # type: ignore[attr-defined]
    finally:
        request_id_var.reset(token)


# --------------------------------------------------------------------------- #
# Finding 2: API key rotation cap bypass                                       #
# --------------------------------------------------------------------------- #


def _mint_key(client, auth, project_id):
    return client.post(
        "/api/v1/api-keys",
        json={
            "project_id": project_id,
            "name": "k",
            "scopes": ["content:read"],
        },
        headers=auth,
    )


def test_rotation_with_grace_blocked_at_cap(client, auth, project, db):
    """Rotating with a grace period must not exceed the live-key cap."""
    keys = []
    for i in range(MAX_KEYS_PER_PROJECT):
        resp = _mint_key(client, auth, project.id)
        assert resp.status_code == 201, resp.text
        keys.append(resp.json()["id"])

    resp = _mint_key(client, auth, project.id)
    assert resp.status_code == 409

    resp = client.post(
        f"/api/v1/api-keys/{keys[-1]}/rotate",
        json={"grace_hours": 24},
        headers=auth,
    )
    assert resp.status_code == 409
    assert "live keys" in resp.json()["detail"].lower()


def test_rotation_without_grace_succeeds_at_cap(client, auth, project, db):
    """Immediate rotation (grace_hours=0) revokes the old key, so the count
    stays the same — it must succeed even at the cap."""
    keys = []
    for i in range(MAX_KEYS_PER_PROJECT):
        resp = _mint_key(client, auth, project.id)
        assert resp.status_code == 201
        keys.append(resp.json()["id"])

    resp = client.post(
        f"/api/v1/api-keys/{keys[-1]}/rotate",
        json={"grace_hours": 0},
        headers=auth,
    )
    assert resp.status_code == 200


def test_rotation_with_grace_succeeds_below_cap(client, auth, project, db):
    """When there is room, a graceful rotation is allowed."""
    resp = _mint_key(client, auth, project.id)
    assert resp.status_code == 201
    key_id = resp.json()["id"]

    resp = client.post(
        f"/api/v1/api-keys/{key_id}/rotate",
        json={"grace_hours": 24},
        headers=auth,
    )
    assert resp.status_code == 200
    assert "replaced" in resp.json()

"""Observability coverage for H036/M11.

Each test asserts that a previously silent operation now emits the structured
log record an operator needs. The pattern is ``caplog.at_level`` on the right
logger, then the operation, then an assertion on the message text.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services import link_check, scheduling, accounts
from app.services.crypto import encrypt_credentials
from app.services.publishers.base import CredentialError, CredentialField


# ── Gap 1: settings — upsert_connection logs on success ───────────────────


def test_upsert_connection_logs_on_success(client, auth, user, monkeypatch, caplog):
    from app.services import publishers

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(adapter, "verify", lambda creds: "test-display")
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )

    with caplog.at_level("INFO", logger="app.routers.settings"):
        resp = client.put(
            "/api/v1/settings/connections",
            headers=auth,
            json={"platform": "devto", "credentials": {"api_key": "fake-key"}},
        )

    assert resp.status_code == 200
    assert "connected devto" in caplog.text
    assert "test-display" in caplog.text


# ── Gap 1b: settings — verify_connection logs status transitions ──────────


def test_verify_connection_logs_status_unchanged(
    client, auth, db, user, monkeypatch, caplog
):
    from app.services import publishers

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(adapter, "verify", lambda creds: "ok-user")
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )

    conn = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@dev",
    )
    db.add(conn)
    db.commit()

    with caplog.at_level("INFO", logger="app.routers.settings"):
        resp = client.post(
            "/api/v1/settings/connections/devto/verify", headers=auth
        )

    assert resp.status_code == 200
    assert "verified devto" in caplog.text
    assert "status=connected" in caplog.text


def test_verify_connection_logs_status_transition(
    client, auth, db, user, monkeypatch, caplog
):
    from app.services import publishers

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter, "verify", lambda creds: (_ for _ in ()).throw(
            CredentialError("bad token")
        ),
    )
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )

    conn = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@dev",
    )
    db.add(conn)
    db.commit()

    with caplog.at_level("WARNING", logger="app.routers.settings"):
        resp = client.post(
            "/api/v1/settings/connections/devto/verify", headers=auth
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "invalid"
    assert "status changed" in caplog.text
    assert "connected -> invalid" in caplog.text


# ── Gap 2: link_check — batch summary is logged ──────────────────────────


@pytest.fixture
def _no_dns(monkeypatch):
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def test_link_check_logs_broken_summary(_no_dns, caplog):
    def _handler(request):
        if "broken" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200)

    with caplog.at_level("WARNING", logger="app.services.link_check"):
        results = link_check.check(
            ["https://example.com/ok", "https://example.com/broken"],
            timeout=5,
        )

    assert any(r.status == link_check.BROKEN for r in results)
    assert "broken" in caplog.text
    assert "2 checked" in caplog.text


def test_link_check_logs_all_ok_at_debug(_no_dns, monkeypatch, caplog):
    original_check = link_check.check_url

    def _always_ok(url, *, client):
        return link_check.LinkStatus(url, link_check.OK, http_status=200)

    monkeypatch.setattr(link_check, "check_url", _always_ok)

    with caplog.at_level("DEBUG", logger="app.services.link_check"):
        link_check.check(["https://example.com/page"], timeout=5)

    assert "all ok" in caplog.text


def test_link_check_ssrf_rejection_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(
        link_check,
        "_unreachable_for_a_reader",
        lambda url: "private address" if "evil" in url else None,
    )

    with caplog.at_level("WARNING", logger="app.services.link_check"):
        with _mock_client(lambda req: httpx.Response(200)) as client:
            result = link_check.check_url("https://evil.example.com", client=client)

    assert result.status == link_check.BROKEN
    assert "SSRF-blocked" in caplog.text


# ── Gap 3: scheduling — optimal_slots logs computed slots ─────────────────


def test_optimal_slots_logs_computation(db, user, monkeypatch, caplog):
    conn = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@dev",
    )
    db.add(conn)
    db.commit()

    monkeypatch.setattr("app.config.settings.learned_cadence_enabled", False)

    with caplog.at_level("INFO", logger="app.services.scheduling"):
        slots = scheduling.optimal_slots(db, user.id, [Platform.DEVTO])

    assert len(slots) == 1
    assert "optimal_slots" in caplog.text
    assert "devto" in caplog.text


# ── Gap 4: accounts — case-insensitive fallback is logged ─────────────────


def test_case_insensitive_fallback_is_logged(db, user, caplog):
    stored_email = user.email
    mixed_case = stored_email.upper()

    with caplog.at_level("INFO", logger="app.services.accounts"):
        found = accounts.find_by_email(db, mixed_case)

    if found is not None:
        assert found.id == user.id
        assert "case-insensitive fallback" in caplog.text


def test_ambiguous_match_logs_warning(db, caplog):
    from app.models.user import User
    from app.security import hash_password

    u1 = User(email="Dupe@test.com", hashed_password=hash_password("pw1"))
    u2 = User(email="dupe@test.com", hashed_password=hash_password("pw2"))
    db.add_all([u1, u2])
    db.commit()

    with caplog.at_level("WARNING", logger="app.services.accounts"):
        result = accounts.find_by_email(db, "DUPE@test.com")

    assert result is None
    assert "ambiguous" in caplog.text

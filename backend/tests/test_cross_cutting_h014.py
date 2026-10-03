"""H014 cross-cutting concern audit: no internal details in error responses.

Three ``CredentialEncryptionError`` paths forwarded the exception's message
straight into the HTTP ``detail`` field. Those messages name environment
variables (``TOKEN_ENCRYPTION_KEY``), encryption technology (Fernet), and
operational hints — none of which belong in a response an API consumer sees.

The fix logs the raw error server-side and returns a generic message that
says what happened without saying how the server is configured.

The digest service also logged ``user.email`` — PII that belongs in a
mailbox, not in a log aggregator. Replaced with ``user.id``.
"""
from __future__ import annotations

import logging

import pytest

from app.models.project import Project, Tone
from app.services.crypto import CredentialEncryptionError

V1 = "/api/v1"

_CONFIG_TERMS = ("TOKEN_ENCRYPTION_KEY", "Fernet", "ENVIRONMENT")


@pytest.fixture
def project(db, user) -> Project:
    row = Project(
        user_id=user.id,
        name="H014",
        slug="h014",
        description="Cross-cutting test project",
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _raise_encryption_error(msg):
    def _raiser(_):
        raise CredentialEncryptionError(msg)
    return _raiser


def test_settings_encryption_error_hides_config_details(
    client, auth, db, user, monkeypatch
):
    """PUT /settings/connections must not expose TOKEN_ENCRYPTION_KEY in 500."""
    from app.models.publication import Platform
    from app.services import publishers
    from app.services.publishers.base import CredentialField

    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )
    monkeypatch.setattr(adapter, "verify", lambda credentials: "test-user")
    monkeypatch.setattr(
        "app.routers.settings.encrypt_credentials",
        _raise_encryption_error(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key"
        ),
    )

    resp = client.put(
        f"{V1}/settings/connections",
        json={"platform": "devto", "credentials": {"api_key": "test"}},
        headers=auth,
    )

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    for term in _CONFIG_TERMS:
        assert term not in detail, f"{term!r} leaked into error response"
    assert "encrypt" in detail.lower()


def test_trigger_encryption_error_hides_config_details(
    client, auth, db, user, project, monkeypatch
):
    """POST /triggers with a broken encryption key must not leak config."""
    monkeypatch.setattr(
        "app.services.triggers.store_secret",
        _raise_encryption_error(
            "Refusing to store unencrypted in production — set TOKEN_ENCRYPTION_KEY."
        ),
    )

    resp = client.post(
        f"{V1}/triggers",
        json={
            "project_id": project.id,
            "kind": "webhook",
            "name": "h014-trigger",
        },
        headers=auth,
    )

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    for term in _CONFIG_TERMS:
        assert term not in detail, f"{term!r} leaked into error response"


def test_webhook_encryption_error_hides_config_details(
    client, auth, db, user, monkeypatch
):
    """POST /webhooks with a broken encryption key must not leak config."""
    monkeypatch.setattr(
        "app.services.webhooks.store_secret",
        _raise_encryption_error(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key"
        ),
    )

    resp = client.post(
        f"{V1}/webhooks",
        json={
            "url": "https://example.com/hook",
            "events": ["content.published"],
        },
        headers=auth,
    )

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    for term in _CONFIG_TERMS:
        assert term not in detail, f"{term!r} leaked into error response"


def test_digest_log_uses_user_id_not_email(db, user, caplog):
    """The digest skip message must reference user.id, not user.email."""
    from app.services import digest

    with caplog.at_level(logging.INFO, logger="app.services.digest"):
        digest.send(db, user)

    log_text = caplog.text
    assert user.email not in log_text
    assert str(user.id) in log_text

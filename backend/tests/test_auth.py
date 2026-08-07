from __future__ import annotations

from app.config import settings


def test_register_and_login(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "new@example.com", "password": "longenough123", "full_name": "New"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "new@example.com"

    resp = client.post(
        "/api/v1/auth/login",
        data={"username": "new@example.com", "password": "longenough123"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["connected_platforms"] == []


def test_duplicate_email_rejected(client, user):
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": user.email, "password": "longenough123"},
    )
    assert resp.status_code == 409


def test_wrong_password_rejected(client, user):
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "wrongwrongwrong"},
    )
    assert resp.status_code == 401


def test_protected_route_requires_token(client):
    assert client.get("/api/v1/projects").status_code == 401


def test_login_rate_limit_kicks_in(client, user):
    """Hammering the login endpoint should eventually get a 429.

    The decorator captures settings.rate_limit_login at import time (default
    "10/minute;100/hour"), so we fire 11 requests to exceed the per-minute
    window.
    """
    for _ in range(10):
        client.post(
            "/api/v1/auth/login",
            data={"username": user.email, "password": "wrongwrongwrong"},
        )
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "wrongwrongwrong"},
    )
    assert resp.status_code == 429
    assert "Too many requests" in resp.json()["detail"]


def test_register_rate_limit(client, user):
    """Registration is also rate-limited (default "5/hour")."""
    for i in range(5):
        client.post(
            "/api/v1/auth/register",
            json={"email": f"rl{i}@example.com", "password": "longenough123"},
        )
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "rl99@example.com", "password": "longenough123"},
    )
    assert resp.status_code == 429


def test_password_too_short_rejected(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "abc"},
    )
    assert resp.status_code == 422


def test_invalid_email_rejected(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "longenough123"},
    )
    assert resp.status_code == 422


def test_inactive_user_cannot_login(client, db, user):
    user.is_active = False
    db.commit()
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "hunter2hunter2"},
    )
    assert resp.status_code == 403


def test_invalid_token_returns_401(client):
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer totally-invalid-jwt"},
    )
    assert resp.status_code == 401


def test_registration_closed_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", False)
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "new@example.com", "password": "longenough123"},
    )
    assert resp.status_code == 403
    assert "closed" in resp.json()["detail"].lower()


def test_registration_with_invite_token(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "registration_invite_token", "secret-invite")

    # Wrong token
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "invited@example.com",
            "password": "longenough123",
            "invite_token": "wrong",
        },
    )
    assert resp.status_code == 403

    # Right token
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "invited@example.com",
            "password": "longenough123",
            "invite_token": "secret-invite",
        },
    )
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Account preferences                                                          #
# --------------------------------------------------------------------------- #


def test_a_display_name_can_be_cleared(client, auth, db, user):
    """PATCH semantics: a field sent as null is an instruction, not an omission.

    Dropping every null meant a name, once set, could be changed but never
    removed — the account page offered an empty box that silently did nothing.
    """
    user.full_name = "Given Name"
    db.commit()

    resp = client.patch("/api/v1/auth/me", headers=auth, json={"full_name": None})
    assert resp.status_code == 200
    assert resp.json()["full_name"] is None

    db.refresh(user)
    assert user.full_name is None


def test_an_omitted_field_is_still_left_alone(client, auth, db, user):
    """The other half of PATCH: not sending a field must not clear it."""
    user.full_name = "Keep Me"
    db.commit()

    resp = client.patch(
        "/api/v1/auth/me", headers=auth, json={"weekly_digest_enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Keep Me"
    assert resp.json()["weekly_digest_enabled"] is False


def test_the_digest_flag_can_be_turned_off_and_back_on(client, auth, db, user):
    for wanted in (False, True):
        resp = client.patch(
            "/api/v1/auth/me", headers=auth, json={"weekly_digest_enabled": wanted}
        )
        assert resp.status_code == 200
        assert resp.json()["weekly_digest_enabled"] is wanted


def test_a_null_digest_flag_is_refused_rather_than_written(client, auth, db, user):
    """The column is NOT NULL — writing the null would be a 500, not a 422."""
    resp = client.patch(
        "/api/v1/auth/me", headers=auth, json={"weekly_digest_enabled": None}
    )
    assert resp.status_code == 422

    db.refresh(user)
    assert user.weekly_digest_enabled is True

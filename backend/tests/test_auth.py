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

from __future__ import annotations


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


def test_health_reports_no_providers_configured(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    # conftest blanks every key, so the chain is empty in tests.
    assert body["llm_providers"] == []
    assert set(body["implemented_platforms"]) == {"devto", "medium"}

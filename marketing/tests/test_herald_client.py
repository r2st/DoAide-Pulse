"""The HTTP client — the written-down contract for the endpoints the runner uses.

Herald turns ``/openapi.json`` off in production, so if this module drifts from
the API there is nothing to catch it but these tests. What they assert is the
wire detail: the form encoding on login that a JSON body silently fails, the
bearer header, the query parameters, and which error text reaches the operator.
"""
from __future__ import annotations

import httpx
import pytest
from herald_client import DEFAULT_BASE_URL, HeraldClient, HeraldError

BASE = "https://herald.example.com/api/v1"


def client_for(handler, **kwargs) -> HeraldClient:
    return HeraldClient(BASE, transport=httpx.MockTransport(handler), **kwargs)


def logged_in(handler) -> HeraldClient:
    """A client past the handshake, so a test can get to the endpoint it means."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
        return handler(request)

    client = client_for(route)
    client.login("someone@example.com", "hunter2")
    return client


# -- auth -------------------------------------------------------------------- #


def test_login_posts_form_encoded_credentials_not_json():
    """A JSON body returns 422 "Field required", which reads like a bad password."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "tok"})

    client_for(handler).login("someone@example.com", "hunter2")

    assert seen["content_type"] == "application/x-www-form-urlencoded"
    # `username`, not `email` — the OAuth2 password-flow field name.
    assert "username=someone%40example.com" in seen["body"]
    assert "password=hunter2" in seen["body"]


def test_a_rejected_login_carries_the_detail_herald_sent():
    handler = lambda r: httpx.Response(401, json={"detail": "Incorrect email or password"})
    with pytest.raises(HeraldError) as exc:
        client_for(handler).login("someone@example.com", "wrong")

    assert exc.value.status_code == 401
    assert exc.value.detail == "Incorrect email or password"


def test_calling_an_endpoint_before_logging_in_fails_without_a_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    with pytest.raises(HeraldError, match="not logged in"):
        client_for(handler).list_projects()
    assert not called


def test_login_from_env_says_which_variables_to_set(monkeypatch):
    monkeypatch.delenv("HERALD_EMAIL", raising=False)
    monkeypatch.delenv("HERALD_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="HERALD_EMAIL and HERALD_PASSWORD"):
        HeraldClient(BASE).login_from_env()


def test_login_from_env_uses_the_environment(monkeypatch):
    monkeypatch.setenv("HERALD_EMAIL", "someone@example.com")
    monkeypatch.setenv("HERALD_PASSWORD", "hunter2")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "tok"})

    client_for(handler).login_from_env()
    assert "username=someone%40example.com" in seen["body"]


# -- request plumbing -------------------------------------------------------- #


def test_every_authenticated_call_carries_the_bearer_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    logged_in(handler).list_projects()
    assert seen["auth"] == "Bearer tok"


def test_a_204_is_not_parsed_as_json():
    """DELETE /content/{id}/schedule answers 204 — ``.json()`` would raise."""
    handler = lambda r: httpx.Response(204)
    assert logged_in(handler).unschedule_content(7) is None


def test_an_empty_200_body_is_not_parsed_as_json():
    handler = lambda r: httpx.Response(200, content=b"")
    assert logged_in(handler).unschedule_content(7) is None


def test_an_error_with_no_json_body_still_reaches_the_operator():
    """A 502 from the reverse proxy is HTML, not Herald's JSON."""
    handler = lambda r: httpx.Response(502, text="<html>Bad Gateway</html>")
    with pytest.raises(HeraldError) as exc:
        logged_in(handler).list_projects()

    assert exc.value.status_code == 502
    assert "Bad Gateway" in exc.value.detail


def test_a_validation_error_shows_the_field_that_failed():
    body = {"detail": [{"loc": ["body", "title"], "msg": "Field required"}]}
    handler = lambda r: httpx.Response(422, json=body)
    with pytest.raises(HeraldError) as exc:
        logged_in(handler).create_content({"project_id": 1})

    assert "Field required" in exc.value.detail


def test_the_base_url_loses_a_trailing_slash():
    """Otherwise every path is built with a double slash in it."""
    assert HeraldClient("https://herald.example.com/api/v1/").base_url == BASE


def test_the_default_base_url_is_the_public_host():
    assert DEFAULT_BASE_URL.startswith("https://")


def test_the_client_closes_its_connection_pool():
    client = HeraldClient(BASE, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with client:
        pass
    assert client._http.is_closed


# -- endpoints --------------------------------------------------------------- #


def test_list_content_filters_by_project_and_caps_the_page():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json=[])

    logged_in(handler).list_content(project_id=4)

    assert seen["path"].endswith("/content")
    assert seen["params"] == {"limit": "500", "project_id": "4"}


def test_list_content_stays_inside_the_limit_the_api_allows():
    """``GET /content`` declares ``le=500``; asking for more is a 422."""
    import inspect

    default = inspect.signature(HeraldClient.list_content).parameters["limit"].default
    assert default <= 500


def test_list_content_without_a_project_sends_no_filter():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    logged_in(handler).list_content()
    assert "project_id" not in seen["params"]


def test_schedule_sends_optimize_and_scheduled_for_separately():
    """Herald rejects both together, so the client must not send both."""
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    client = logged_in(handler)
    client.schedule_content(1, platforms=["devto"], optimize=True)
    client.schedule_content(1, platforms=["devto"], scheduled_for="2026-12-01T09:00:00")

    assert "scheduled_for" not in bodies[0]
    assert bodies[0]["optimize"] is True
    assert bodies[1]["scheduled_for"] == "2026-12-01T09:00:00"
    assert bodies[1]["optimize"] is False


def test_connected_platforms_returns_only_the_live_ones():
    payload = [
        {"platform": "devto", "connection": {"status": "connected"}},
        {"platform": "bluesky", "connection": {"status": "invalid"}},
        {"platform": "medium", "connection": None},
        {"platform": "twitter"},
    ]
    handler = lambda r: httpx.Response(200, json=payload)
    assert logged_in(handler).connected_platforms() == ["devto"]


def test_check_links_reads_the_content_scoped_route():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"links": [], "checked": 0, "broken_count": 0})

    logged_in(handler).check_links(12)
    assert seen["path"].endswith("/content/12/links")


def test_patching_content_uses_patch_not_put():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        return httpx.Response(200, json={"id": 3})

    logged_in(handler).update_content(3, {"status": "review"})
    assert seen["method"] == "PATCH"

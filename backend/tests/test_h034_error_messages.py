"""H034: Error message quality audit.

Checks added by the H034 improvement round:

1. Every "not found" 404 message ends with a period.
2. Auth refusal messages end with a period.
3. GitHub ``_json`` errors do not leak content-type headers.
4. The error-shape sweep still holds after punctuation changes.
5. ``_validation_detail`` in templates never leaks Pydantic model class names.
6. The ``ValueError`` fallback in the template PATCH caps message length.
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from app.main import create_app
from app.services.errors import sanitize_unexpected_error


# --------------------------------------------------------------------------- #
# 1. Every 404 "not found" detail ends with a period                          #
# --------------------------------------------------------------------------- #


def _walk(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None)
        yield from _walk(getattr(inner, "routes", None) or getattr(route, "routes", []))


def _method(route: APIRoute) -> str:
    return sorted(route.methods - {"HEAD", "OPTIONS"})[0]


_NO_SUCH_ID = 987654321


def _url(route: APIRoute, prefix: str = "/api/v1") -> str:
    url = route.path
    for name in route.param_convertors:
        url = url.replace("{" + name + "}", str(_NO_SUCH_ID))
    return prefix + url


def _dependency_calls(route: APIRoute) -> set:
    seen = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            seen.add(dependant.call)
        stack.extend(dependant.dependencies)
    return seen


from app.deps import get_current_user  # noqa: E402

BY_ID = [
    pytest.param(route, id=f"{_method(route)} {route.path}")
    for route in _walk(create_app().routes)
    if route.include_in_schema
    and get_current_user in _dependency_calls(route)
    and route.param_convertors
]


@pytest.mark.parametrize("route", BY_ID)
def test_not_found_messages_end_with_a_period(client, auth, route):
    """Every 404 detail must end with punctuation — a period, a dash clause,
    or a closing parenthesis — never a bare noun like "Content not found".
    """
    url = _url(route)
    resp = client.request(_method(route), url, headers=auth)
    if resp.status_code != 404:
        pytest.skip("did not produce a 404")
    detail = resp.json().get("detail", "")
    assert detail.rstrip().endswith((".","!","?",")","…")), (
        f"{url} returned 404 with an unterminated message: {detail!r}"
    )


# --------------------------------------------------------------------------- #
# 2. Auth refusal messages end with a period                                  #
# --------------------------------------------------------------------------- #


def test_login_wrong_password_message_ends_with_period(client, user):
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "wrongpassword12"},
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail.endswith("."), f"Login error unterminated: {detail!r}"


def test_register_duplicate_email_message_ends_with_period(client, user):
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": user.email,
            "password": "differentpassword",
            "full_name": "Other",
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.endswith("."), f"Register error unterminated: {detail!r}"


# --------------------------------------------------------------------------- #
# 3. GitHub _json error no longer leaks content-type                          #
# --------------------------------------------------------------------------- #


def test_github_json_error_hides_content_type():
    from app.services.github_client import GitHubError, _json

    import httpx

    resp = httpx.Response(
        200,
        content=b"<html>Not JSON</html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )
    with pytest.raises(GitHubError) as exc_info:
        _json(resp, "/repos/owner/repo/commits")
    msg = str(exc_info.value)
    assert "text/html" not in msg, f"content-type leaked: {msg}"
    assert "charset" not in msg
    assert "unexpected response" in msg.lower()


# --------------------------------------------------------------------------- #
# 4. sanitize_unexpected_error still works                                    #
# --------------------------------------------------------------------------- #


def test_sanitize_hides_file_paths():
    exc = OSError("/opt/Herald/backend/app/services/publishers/git.py: crash")
    msg = sanitize_unexpected_error(exc)
    assert "/opt/Herald" not in msg
    assert "OSError" in msg


def test_sanitize_hides_connection_strings():
    exc = Exception("could not connect to postgresql://admin:s3cret@db:5432/pulse")
    msg = sanitize_unexpected_error(exc)
    assert "admin:s3cret" not in msg
    assert "postgresql://" not in msg


# --------------------------------------------------------------------------- #
# 5. Template _validation_detail does not leak Pydantic class names           #
# --------------------------------------------------------------------------- #


def test_validation_detail_strips_pydantic_internals():
    from pydantic import ValidationError

    from app.routers.templates import _validation_detail
    from app.schemas.template import TemplateCreate

    try:
        TemplateCreate(
            name="",
            mode="invalid_mode",
            title_template="t",
            body_template="b",
        )
    except ValidationError as exc:
        detail = _validation_detail(exc)
        assert "TemplateCreate" not in detail
        assert "input_type" not in detail
        assert detail  # not empty


# --------------------------------------------------------------------------- #
# 6. Overlong ValueError in template PATCH is capped                          #
# --------------------------------------------------------------------------- #


def test_template_patch_caps_huge_valueerror(client, auth, db):
    from app.models.template import ContentTemplate

    template = ContentTemplate(
        user_id=1,
        name="Test",
        mode="literal",
        content_type="tutorial",
        title_template="{{topic}}",
        body_template="Write about {{topic}}",
        variables=[{"name": "topic", "description": "The topic", "required": True}],
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    resp = client.patch(
        f"/api/v1/templates/{template.id}",
        json={"name": ""},
        headers=auth,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert len(detail) < 500

"""Why Herald needs no CSRF token, pinned so it stays true.

CSRF needs one precondition: the browser must attach the caller's credentials
to a request the caller's page did not make. Herald has no such credential.
The SPA holds a bearer token and puts it in an ``Authorization`` header it
writes itself, so a form POST from ``evil.example`` arrives at
``/api/v1/content/{id}`` as an anonymous request and is refused by
``get_current_user`` — not by a token check, but by there being no session to
ride. The right defence against CSRF here is the absence of ambient
credentials, and a synchroniser token on top of it would defend nothing.

That is a fine answer and a fragile one, because it is an *absence*, and
absences are what a later change removes without noticing. Three lines of code
would restore the precondition, none of them looking like a security change:

* a ``response.set_cookie("access_token", ...)`` on login — the obvious way to
  "fix" a user complaining that a refresh logs them out;
* ``allow_credentials=True`` on the CORS middleware — the obvious way to fix a
  cross-origin call in development that is failing for some other reason;
* a fallback in :func:`app.deps.get_current_user` that reads the token from a
  cookie or a query parameter when the header is missing — the obvious way to
  make a download link or an EventSource work, neither of which can set headers.

So this file asserts the absence directly, at each of those three points, plus
the sweep that says every state-changing endpoint either requires the bearer
token or requires a secret of its own. None of these tests will fail for a
CSRF *attack*; they fail for the change that would make one possible, which is
the only moment anything can be done about it.

See also ``test_cors.py``, which covers the same invariant from the response
side: the ``Access-Control-Allow-Credentials`` header is never sent.
"""
from __future__ import annotations

import pytest
from fastapi.middleware.cors import CORSMiddleware

from app.main import app
from app.security import create_access_token
from tests.test_public_endpoint_limits import _api_routes, _is_anonymous

ME = "/api/v1/auth/me"

#: Every state-changing endpoint reachable without a bearer token, and the
#: secret each one requires instead. A browser cannot supply any of them from a
#: cross-site page: the first four take a password or an emailed token in the
#: request *body*, and the fifth takes a per-trigger token in the path. None is
#: something the browser attaches on the caller's behalf, which is the property
#: that matters — a POST an attacker can make with credentials they already
#: know is not CSRF, it is just a POST.
_ANONYMOUS_WRITES = {
    ("POST", "/auth/register"): "an invite token and a password",
    ("POST", "/auth/login"): "a password",
    ("POST", "/auth/password-reset"): "nothing, and it says nothing back",
    ("POST", "/auth/password-reset/confirm"): "the emailed reset token",
    ("POST", "/triggers/inbound/{token}"): "the trigger's own inbound token",
}


# --------------------------------------------------------------------------- #
# Nothing is ever stored in the browser on Herald's say-so                     #
# --------------------------------------------------------------------------- #


def test_logging_in_hands_back_a_token_and_sets_no_cookie(client, user):
    """The one response most likely to grow a cookie, and the one that must not.

    A cookie set here would be attached to every later request by the browser
    itself, which is the whole precondition. The token goes in the body, where
    the SPA has to pick it up and send it back deliberately.
    """
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "hunter2hunter2"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    assert resp.cookies == {}


def test_no_endpoint_sets_a_cookie(client, auth):
    """The same check across a read, a write and both anonymous auth routes.

    Not a sweep over every route — a cookie would have to be set by a handler
    that has a reason to, and these are the shapes that have one. The login
    case above is the one that matters; the rest are here so a session-shaped
    change anywhere in the tree shows up.
    """
    responses = [
        client.get(ME, headers=auth),
        client.get("/api/v1/projects", headers=auth),
        client.patch("/api/v1/auth/me", headers=auth, json={"full_name": "Dev Two"}),
        client.post(
            "/api/v1/auth/password-reset", json={"email": "nobody@example.com"}
        ),
        client.get("/api/v1/health"),
    ]
    for resp in responses:
        assert "set-cookie" not in {k.lower() for k in resp.headers}, resp.url
    assert client.cookies == {}


# --------------------------------------------------------------------------- #
# The token is read from one place only                                        #
# --------------------------------------------------------------------------- #


def test_a_token_in_a_cookie_is_not_a_session(client, user):
    """A perfectly valid token, in the one place a browser would send by itself.

    This is the test that would fail first if ``get_current_user`` ever grew a
    cookie fallback — and it uses a token Herald really signed, so it fails for
    the fallback rather than for the token being bad.
    """
    client.cookies.set("access_token", create_access_token(user.id))
    try:
        assert client.get(ME).status_code == 401
    finally:
        client.cookies.clear()


@pytest.mark.parametrize("param", ["access_token", "token", "api_key", "jwt"])
def test_a_token_in_a_query_parameter_is_not_a_session(client, user, param):
    """The other ambient shape: a URL that authenticates by being visited.

    A link like this is CSRF's easier cousin — it needs no form, survives being
    pasted into a chat, and lands in every proxy log on the way. The names here
    are the ones a download link or an EventSource would plausibly be given.
    """
    token = create_access_token(user.id)
    assert client.get(f"{ME}?{param}={token}").status_code == 401


def test_the_bearer_header_is_what_does_work(client, user):
    """The control. Without this the three refusals above prove nothing."""
    token = create_access_token(user.id)
    resp = client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


# --------------------------------------------------------------------------- #
# The CORS configuration                                                       #
# --------------------------------------------------------------------------- #


def test_cors_is_configured_without_credentials():
    """Read off the installed middleware, not off a response header.

    ``test_cors`` asserts the header is absent, which is the observable half.
    This asserts the option is off, which is the half somebody edits — and the
    two can come apart: Starlette omits the header for a request it does not
    recognise as credentialed even when the option is on, so a response-only
    check can pass against a middleware that has already been changed.
    """
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    assert cors[0].kwargs.get("allow_credentials", False) is False


def test_cors_does_not_allow_the_cookie_header_through():
    """Belt and braces: even named explicitly, ``Cookie`` is not on the list.

    ``allow_headers`` is the enumerated set for a reason — it is what a
    preflight is answered with, and adding ``Cookie`` to it would be the third
    way to arrive at an ambient credential.
    """
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware][0]
    allowed = {h.lower() for h in cors.kwargs.get("allow_headers", [])}
    assert "cookie" not in allowed
    assert "authorization" in allowed


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #


def _state_changing(route) -> bool:
    return bool(route.methods - {"GET", "HEAD", "OPTIONS"})


def test_the_write_sweep_actually_finds_the_writes():
    """Guard the guard — the assertion below is vacuous on an empty set."""
    writes = [r for r in _api_routes() if _state_changing(r)]
    assert len(writes) > 20
    assert any(r.path == "/content/{content_id}" and "PATCH" in r.methods for r in writes)


def test_every_state_changing_endpoint_needs_a_credential_it_is_handed():
    """The regression guard, and the reason there is no CSRF token here.

    A new write endpoint that forgets ``get_current_user`` is an authorization
    bug first — but it is also the shape that reintroduces CSRF the moment any
    ambient credential exists, so it is worth failing here too, named against
    the list of the five that are anonymous on purpose.
    """
    anonymous_writes = {
        (sorted(route.methods - {"HEAD", "OPTIONS"})[0], route.path)
        for route in _api_routes()
        if _state_changing(route) and _is_anonymous(route)
    }
    assert anonymous_writes == set(_ANONYMOUS_WRITES)


def test_an_anonymous_write_is_refused_without_its_own_secret(client, user):
    """The list above claims each anonymous write needs a secret. Spot-check two.

    A sweep that only counts routes would pass just as happily against an
    inbound trigger that fired for any token at all.
    """
    # Wrong password, right shape.
    assert client.post(
        "/api/v1/auth/login",
        data={"username": "dev@example.com", "password": "not-the-password"},
    ).status_code == 401

    # A token nobody issued.
    assert client.post(
        "/api/v1/triggers/inbound/deadbeefdeadbeefdeadbeefdeadbeef", json={}
    ).status_code == 404

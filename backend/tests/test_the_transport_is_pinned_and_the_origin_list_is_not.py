"""Two headers whose comments promised more than the code delivered.

``test_cors.py`` already pins the headers Pulse has always sent. This file
covers the two that were missing or wrong, and the CORS allow-list, which had
nothing stopping a development value from being deployed.

**HSTS.** The middleware's own comment read "Caddy already sets HSTS and some of
these, but defence-in-depth means the app should not rely on that" — and then
set every header in that list *except* HSTS. So the one header the comment named
was the one the app did not send, and the whole policy rested on a ``header``
block in a shared Caddyfile that Pulse does not own.

It cannot simply be sent unconditionally, which is presumably how it came to be
left out. A browser ignores HSTS on a plain-HTTP response, so emitting it always
would be merely useless in most places — but not on localhost, where a developer
who once runs an HTTPS dev server pins their own machine to TLS for a year and
has no way to undo it but to clear it by hand. So it is sent when, and only
when, the request actually arrived over TLS, which behind Caddy means the
forwarded header is the only honest signal.

**CSP.** The comment claimed the policy was "tightened to 'self' when docs are
disabled". It was not: one string was built with ``'unsafe-inline'`` and a
jsdelivr origin and sent everywhere, so production — where the docs are off and
every response is JSON — was advertising a script policy for a Swagger UI it
does not serve.

**The origin list.** ``cors_origins`` split a comma-separated string and handed
it to Starlette unread. A ``*`` left in an env file, or an ``http://`` origin
kept from staging, would have been honoured on a live box. It is now filtered in
production — filtered rather than refused, because this property is read while
the app is being built and raising would turn a too-broad entry into a box that
will not boot.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.main import create_app

#: Settings every case here starts from.
#:
#: ``_env_file=None`` matters: a developer checkout has a ``.env`` at the repo
#: root, ``Settings`` reads ``../.env`` by default, and the one in this repo
#: carries ``DEBUG=true``. Without this, a ``Settings(environment="production")``
#: built for a test comes back with ``debug`` set — which re-opens the docs, and
#: so quietly asserts the *development* CSP while claiming to check production.
_BASE = {
    "environment": "production",
    "debug": False,
    "jwt_secret": "x" * 48,
    "token_encryption_key": Fernet.generate_key().decode(),
    "database_url": "sqlite://",
    "_env_file": None,
}

# --------------------------------------------------------------------------- #
# HSTS                                                                         #
# --------------------------------------------------------------------------- #

_HSTS = "strict-transport-security"


def test_no_hsts_on_a_plain_http_request(client):
    """The dev server is http://localhost, and pinning it would be permanent."""
    assert _HSTS not in client.get("/api/v1/health").headers


def test_hsts_when_caddy_says_the_hop_was_tls(client):
    """In production the app sees plain HTTP from Caddy over the docker bridge.

    ``X-Forwarded-Proto`` is the only thing that knows TLS was terminated, and
    it is trustworthy here because nothing but Caddy can reach the port — the
    services bind the bridge address (see deploy/Caddyfile.pulse).
    """
    resp = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers[_HSTS] == "max-age=31536000; includeSubDomains"


def test_hsts_reads_only_the_first_forwarded_proto(client):
    """A chain of proxies appends, and the client's own hop is the first entry.

    Reading the whole header, or the last element, would let a caller append
    ``, https`` to a plaintext request and collect a pin for it.
    """
    resp = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https, http"})
    assert _HSTS in resp.headers

    resp = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "http, https"})
    assert _HSTS not in resp.headers


@pytest.mark.parametrize("declared", ["HTTPS", " https ", "hTTps"])
def test_forwarded_proto_is_matched_case_and_space_insensitively(client, declared):
    assert _HSTS in client.get("/api/v1/health", headers={"X-Forwarded-Proto": declared}).headers


@pytest.mark.parametrize("declared", ["http", "", "ws", "httpsx"])
def test_anything_that_is_not_https_gets_no_pin(client, declared):
    resp = client.get("/api/v1/health", headers={"X-Forwarded-Proto": declared})
    assert _HSTS not in resp.headers


def test_a_max_age_worth_having(client):
    """A short max-age is a pin that expires before it protects anybody."""
    resp = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"})
    max_age = int(resp.headers[_HSTS].split("max-age=")[1].split(";")[0])
    assert max_age >= 31_536_000


# --------------------------------------------------------------------------- #
# CSP                                                                          #
# --------------------------------------------------------------------------- #


def _csp_of(**overrides) -> str:
    """Build an app under the given settings and read back its CSP."""
    from fastapi.testclient import TestClient

    from app import main

    settings = Settings(**{**_BASE, **overrides})
    original = main.settings
    main.settings = settings
    try:
        with TestClient(create_app()) as client:
            return client.get("/api/v1/health").headers["content-security-policy"]
    finally:
        main.settings = original


def test_production_serves_a_locked_down_policy():
    """Nothing to load, because a JSON response has nothing to load."""
    csp = _csp_of()
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "jsdelivr" not in csp


def test_the_docs_loosening_applies_only_where_the_docs_are_served():
    """Swagger UI is the one thing here that runs script, and it needs both."""
    csp = _csp_of(environment="development")
    assert "unsafe-inline" in csp
    assert "cdn.jsdelivr.net" in csp


@pytest.mark.parametrize(
    "directive",
    ["frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'"],
)
def test_the_directives_that_hold_either_way(directive):
    """Framing, base-tag rewriting and plugins are refused in both policies."""
    assert directive in _csp_of()
    assert directive in _csp_of(environment="development")


def test_every_response_carries_the_policy(client):
    """Including the ones nobody thinks of as pages."""
    for path in ("/api/v1/health", "/", "/api/v1/does-not-exist"):
        assert "content-security-policy" in client.get(path).headers, path


# --------------------------------------------------------------------------- #
# The production CORS filter                                                   #
# --------------------------------------------------------------------------- #


def _origins(value: str, environment: str = "production") -> list[str]:
    return Settings(
        **{**_BASE, "backend_cors_origins": value, "environment": environment}
    ).cors_origins


def test_a_wildcard_is_dropped_in_production():
    assert _origins("*,https://pulse.doaide.com") == ["https://pulse.doaide.com"]


def test_a_plaintext_origin_is_dropped_in_production():
    assert _origins("http://staging.example.com,https://pulse.doaide.com") == [
        "https://pulse.doaide.com"
    ]


def test_loopback_is_left_alone():
    """An operator on an SSH tunnel to a live box is a real thing to do."""
    assert _origins("http://localhost:5173") == ["http://localhost:5173"]
    assert _origins("http://127.0.0.1:3000") == ["http://127.0.0.1:3000"]


def test_a_hostname_that_merely_starts_with_localhost_is_not_loopback():
    """``http://localhost.evil.test`` resolves to whatever its owner says.

    This is why the check parses the origin instead of matching a prefix.
    """
    assert _origins("http://localhost.evil.test") == []


def test_development_is_not_filtered():
    """The dev default is two plaintext origins, and it has to keep working."""
    assert _origins("*,http://anything.example.com", environment="development") == [
        "*",
        "http://anything.example.com",
    ]


def test_the_drop_is_logged_loudly_enough_to_act_on(caplog):
    """A silently narrowed allow-list is a support ticket about CORS errors."""
    with caplog.at_level("WARNING"):
        _origins("*,http://staging.example.com,https://ok.example.com")
    message = caplog.text
    assert "*" in message
    assert "http://staging.example.com" in message
    assert "BACKEND_CORS_ORIGINS" in message


def test_filtering_everything_is_allowed_to_leave_nothing():
    """An empty allow-list is the correct outcome, not a reason to fall back.

    Production is same-origin behind Caddy, so no origin at all is the normal
    state and refusing every cross-origin request is the right answer.
    """
    assert _origins("*") == []


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #


def test_pulse_sets_no_cookies_at_all(client):
    """There is no cookie to add flags to, and that is the property to keep.

    Pulse authenticates with a bearer token the SPA holds and sends
    explicitly. No cookie means no ambient credential, which is what makes the
    CORS policy's missing ``allow-credentials`` sufficient rather than merely
    tidy — and it is why there is no CSRF token anywhere in the tree.

    If a future change introduces a session cookie, this test is where that
    decision has to be made deliberately: Secure, HttpOnly and SameSite all
    become load-bearing the moment the first one is set.
    """
    for method, path, kwargs in (
        ("get", "/api/v1/health", {}),
        ("get", "/", {}),
        ("post", "/api/v1/auth/login", {"data": {"username": "a@b.com", "password": "x"}}),
    ):
        resp = getattr(client, method)(path, **kwargs)
        assert "set-cookie" not in resp.headers, f"{method} {path} set a cookie"


def test_authenticated_responses_are_not_cached(client, auth):
    """A shared cache holding somebody's drafts is the failure this prevents."""
    resp = client.get("/api/v1/projects", headers=auth)
    assert resp.headers["cache-control"] == "no-store"

"""Where an adapter is allowed to send a request.

Three platforms are "tell me where your server is": WordPress, Mastodon and a
self-hosted Bluesky PDS. Their base URL arrives from a settings form, and
Pulse's own process is what opens it — so ``http://169.254.169.254/`` typed
into the Site URL box is a request for the cloud metadata endpoint made by
something that can reach it, and the reply comes back to the caller inside the
error message. The other seven adapters point at a constant in the source and
are not part of this.

The rules pinned down here:

* the host is resolved and refused if *any* address is loopback, private or
  link-local, at verify time and again at publish time;
* every redirect hop is checked too, because a host that passes the pre-flight
  can answer ``302 → 127.0.0.1``;
* a name that does not resolve is *not* refused — DNS failure is not proof of
  anything, and refusing on it would break every offline test and every site
  behind a resolver having a bad minute;
* adapters with a constant host keep following redirects the ordinary way.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.publication import Platform
from app.services import link_check
from app.services.publishers import base
from app.services.publishers.base import (
    Adapter,
    PublishRequest,
    PublishResult,
    RefusedHost,
)
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.mastodon import MastodonAdapter
from app.services.publishers.wordpress import WordPressAdapter

_ARTICLE = PublishRequest(
    title="A post",
    body_markdown="Some words about the thing that shipped.",
    excerpt="Some words.",
    meta_description="Some words about the thing that shipped.",
)


@pytest.fixture
def resolves_private(monkeypatch):
    """Make every hostname look like it resolves to a private address."""
    monkeypatch.setattr(
        link_check,
        "_unreachable_for_a_reader",
        lambda url: "Resolves to a private or loopback address (127.0.0.1).",
    )


@pytest.fixture
def resolves_public(monkeypatch):
    """Make every hostname look like it resolves somewhere on the internet."""
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)


@pytest.fixture
def requests(monkeypatch):
    """Record every URL httpx is asked for; answer with queued responses."""
    seen: list[tuple[str, str]] = []

    def install(*responses: httpx.Response):
        queued = list(responses)

        def fake_request(method, url, **kwargs):
            seen.append((method, str(url)))
            if not queued:
                raise AssertionError(f"unexpected extra request to {url}")
            return queued.pop(0)

        monkeypatch.setattr(base.httpx, "request", fake_request)
        return seen

    return install


def _response(status: int, body: dict | None = None, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or None,
        json=body if body is not None else {"ok": True},
        request=httpx.Request("GET", "https://example.test/"),
    )


# --------------------------------------------------------------------------- #
# The adapters that take an address                                           #
# --------------------------------------------------------------------------- #


def test_wordpress_will_not_verify_against_a_private_site_url(
    resolves_private, requests
):
    calls = requests()  # no response queued: any request at all is the failure

    with pytest.raises(RefusedHost, match="private or loopback"):
        WordPressAdapter().verify(
            {
                "site_url": "http://169.254.169.254",
                "username": "admin",
                "application_password": "abcd efgh ijkl",
            }
        )
    assert calls == []


def test_wordpress_will_not_publish_to_a_private_site_url(resolves_private, requests):
    calls = requests()

    with pytest.raises(RefusedHost):
        WordPressAdapter().publish(
            _ARTICLE,
            {
                "site_url": "http://10.0.0.5",
                "username": "admin",
                "application_password": "abcd efgh ijkl",
            },
        )
    # Verify-time validation is not enough on its own: DNS can change between
    # connecting an account and publishing to it.
    assert calls == []


def test_mastodon_will_not_verify_against_a_private_instance(
    resolves_private, requests
):
    calls = requests()

    with pytest.raises(RefusedHost):
        MastodonAdapter().verify(
            {"instance_url": "localhost:3000", "access_token": "tok"}
        )
    assert calls == []


def test_bluesky_will_not_talk_to_a_private_pds(resolves_private, requests):
    calls = requests()

    with pytest.raises(RefusedHost):
        BlueskyAdapter().verify(
            {
                "handle": "me.bsky.social",
                "app_password": "pass-word-here",
                "service_url": "http://127.0.0.1:2583",
            }
        )
    assert calls == []


def test_a_public_instance_is_reached_as_normal(resolves_public, requests):
    calls = requests(_response(200, {"acct": "me"}))

    assert MastodonAdapter().verify(
        {"instance_url": "https://fosstodon.org", "access_token": "tok"}
    ) == "@me@fosstodon.org"
    assert calls == [
        ("GET", "https://fosstodon.org/api/v1/accounts/verify_credentials")
    ]


def test_a_hostname_that_does_not_resolve_is_not_refused(requests, monkeypatch):
    """DNS failure is not evidence. ``link_check`` returns None and we proceed.

    This is also what keeps the rest of the suite — and any site behind a
    resolver having a bad minute — working: the guard refuses addresses it can
    see are internal, not names it cannot see at all.
    """
    import socket

    def no_such_host(host, port):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(link_check.socket, "getaddrinfo", no_such_host)
    calls = requests(_response(200, {"acct": "me"}))

    MastodonAdapter().verify(
        {"instance_url": "https://nowhere.invalid", "access_token": "tok"}
    )
    assert len(calls) == 1


@pytest.mark.parametrize("scheme_url", ["file:///etc/passwd", "gopher://x/", "//x/y"])
def test_a_non_http_address_is_refused_before_anything_resolves(
    scheme_url, requests, monkeypatch
):
    calls = requests()
    # Nothing should even get as far as asking the resolver.
    monkeypatch.setattr(
        link_check, "_unreachable_for_a_reader", lambda url: pytest.fail("resolved")
    )

    with pytest.raises(RefusedHost, match="http"):
        WordPressAdapter().verify(
            {
                "site_url": scheme_url,
                "username": "admin",
                "application_password": "pw",
            }
        )
    assert calls == []


# --------------------------------------------------------------------------- #
# Redirects                                                                   #
# --------------------------------------------------------------------------- #


def test_a_redirect_into_the_network_is_refused(monkeypatch, requests):
    """The pre-flight checked a host that then handed the request somewhere else."""
    verdicts = {
        "https://blog.example.com": None,
        "http://169.254.169.254": "Resolves to a link-local address.",
    }
    monkeypatch.setattr(
        link_check,
        "_unreachable_for_a_reader",
        lambda url: next(
            (reason for prefix, reason in verdicts.items() if url.startswith(prefix)),
            None,
        ),
    )
    calls = requests(
        _response(302, location="http://169.254.169.254/latest/meta-data/")
    )

    with pytest.raises(RefusedHost, match="link-local"):
        WordPressAdapter().verify(
            {
                "site_url": "https://blog.example.com",
                "username": "admin",
                "application_password": "pw",
            }
        )
    # The first hop was made; the second never was.
    assert len(calls) == 1


def test_an_ordinary_redirect_is_still_followed(resolves_public, requests):
    calls = requests(
        _response(301, location="https://www.blog.example.com/wp-json/wp/v2/users/me"),
        _response(200, {"id": 7, "name": "Ada"}),
    )

    name = WordPressAdapter().verify(
        {
            "site_url": "https://blog.example.com",
            "username": "admin",
            "application_password": "pw",
        }
    )
    assert name == "Ada"
    assert [url for _, url in calls] == [
        "https://blog.example.com/wp-json/wp/v2/users/me",
        "https://www.blog.example.com/wp-json/wp/v2/users/me",
    ]


def test_a_redirect_loop_stops_rather_than_spinning(resolves_public, requests):
    hop = _response(302, location="https://blog.example.com/again")
    requests(*[hop] * (base._MAX_REDIRECTS + 1))

    with pytest.raises(base.PublishError, match="redirected more than"):
        WordPressAdapter().verify(
            {
                "site_url": "https://blog.example.com",
                "username": "admin",
                "application_password": "pw",
            }
        )


# --------------------------------------------------------------------------- #
# The adapters that do not take an address                                    #
# --------------------------------------------------------------------------- #


def test_only_the_three_address_taking_adapters_are_guarded():
    """A whole-registry check, so a new adapter with a URL field is noticed.

    The list is the point: every adapter carrying a credential field whose key
    ends in ``_url`` and that is *fetched* must be guarded. Git's ``site_url`` is
    the exception and stays out — it is used to build the published link, never
    to make a request.
    """
    from app.services import publishers

    guarded = {
        adapter.platform
        for adapter in publishers.all_adapters()
        if adapter.user_supplied_host
    }
    assert guarded == {Platform.WORDPRESS, Platform.MASTODON, Platform.BLUESKY}


def test_a_constant_host_adapter_lets_httpx_follow_redirects(monkeypatch):
    """Dev.to is allowed to move its own endpoints without a per-hop check."""
    seen: list[bool] = []

    def fake_request(method, url, **kwargs):
        seen.append(kwargs["follow_redirects"])
        return _response(200, {"username": "ada"})

    monkeypatch.setattr(base.httpx, "request", fake_request)
    monkeypatch.setattr(
        link_check, "_unreachable_for_a_reader", lambda url: pytest.fail("resolved")
    )

    assert DevToAdapter().verify({"api_key": "k"}) == "@ada"
    assert seen == [True]


def test_the_guard_is_off_by_default_for_a_new_adapter():
    class _Fresh(Adapter):
        platform = Platform.DEVTO
        display_name = "Fresh"

        def publish(self, request, credentials) -> PublishResult:  # pragma: no cover
            raise NotImplementedError

    assert _Fresh().user_supplied_host is False


# --------------------------------------------------------------------------- #
# Through the API                                                             #
# --------------------------------------------------------------------------- #


def test_connecting_a_private_wordpress_is_refused_and_stores_nothing(
    client, auth, db, resolves_private
):
    from app.models.platform_connection import PlatformConnection

    resp = client.put(
        "/api/v1/settings/connections",
        headers=auth,
        json={
            "platform": "wordpress",
            "credentials": {
                "site_url": "http://169.254.169.254",
                "username": "admin",
                "application_password": "abcd efgh ijkl",
            },
        },
    )

    assert resp.status_code == 400, resp.text
    assert "will not send" in resp.json()["detail"]
    assert db.query(PlatformConnection).count() == 0

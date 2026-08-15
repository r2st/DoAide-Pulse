"""What an adapter does when a platform answers 200 with something that isn't JSON.

The severity here is not "a confusing error message". `publishing_service.execute`
translates exceptions into publication outcomes by type: a `PublishError` spends
one of the retry budget, and anything that is *not* a `PublishError` falls to the
`except Exception` catch-all, which fails the publication **terminally**.

`response.json()` raises `json.JSONDecodeError` — a `ValueError`. So a CDN's HTML
error page, a captive portal, or a body cut short mid-stream burned the post
permanently with its retry budget untouched, and left a piece needing a hand
retry nobody knew to do. These tests pin the type, because the type is the bug.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.publication import Platform
from app.services.publishers import base
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    PublishError,
    PublishRequest,
    PublishResult,
)
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.buttondown import ButtondownAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.mastodon import MastodonAdapter
from app.services.publishers.medium import MediumAdapter
from app.services.publishers.wordpress import WordPressAdapter

_URL = "https://platform.test/api"


class _Probe(Adapter):
    platform = Platform.DEVTO
    display_name = "Probe"
    implemented = True

    def publish(  # pragma: no cover
        self, request: PublishRequest, credentials: dict
    ) -> PublishResult:
        raise NotImplementedError


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _: None)


def _html(status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        text="<html><body>502 Bad Gateway</body></html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", _URL),
    )


@pytest.fixture
def transport(monkeypatch):
    """Answer every request with the given response."""

    def install(response):
        monkeypatch.setattr(
            base.httpx, "request", lambda method, url, **kwargs: response
        )

    return install


# -- The base helper ------------------------------------------------------- #


def test_a_non_json_body_is_a_publish_error_not_a_value_error(probe, transport):
    transport(_html())
    resp = probe._request("GET", _URL)

    with pytest.raises(PublishError) as exc:
        probe._json(resp)
    assert "non-JSON" in str(exc.value)


def test_the_error_says_what_came_back_without_quoting_the_body(probe, transport):
    """The content type and status are the diagnosis; the body is someone else's.

    It is also unbounded, and this message is persisted on the publication row
    and rendered in the UI.
    """
    transport(_html())
    resp = probe._request("GET", _URL)

    with pytest.raises(PublishError) as exc:
        probe._json(resp)
    message = str(exc.value)
    assert "text/html" in message
    assert "200" in message
    assert "Bad Gateway" not in message


def test_it_is_not_a_credential_error_so_the_connection_is_not_marked_invalid(
    probe, transport
):
    """A gateway in the way is not a rejected token.

    A `CredentialError` would mark the platform connection invalid and ask the
    user to reconnect an account that was never the problem.
    """
    transport(_html())
    resp = probe._request("GET", _URL)

    with pytest.raises(PublishError) as exc:
        probe._json(resp)
    assert not isinstance(exc.value, CredentialError)


def test_a_valid_json_body_still_decodes(probe, transport):
    transport(
        httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", _URL))
    )
    assert probe._json(probe._request("GET", _URL)) == {"ok": True}


# -- The body that decodes into the wrong thing ---------------------------- #
#
# `_json` covers the body that will not decode. These cover the one that
# decodes into something that is not an object — which every caller then
# reached into with `.get`, raising AttributeError. Same harm, one layer in:
# not a PublishError, so `execute` fails the publication terminally.


@pytest.mark.parametrize(
    "body", [["maintenance"], "maintenance", 3, True], ids=["list", "str", "int", "bool"]
)
def test_a_json_body_that_is_not_an_object_is_a_publish_error(probe, transport, body):
    transport(httpx.Response(200, json=body, request=httpx.Request("GET", _URL)))
    resp = probe._request("GET", _URL)

    with pytest.raises(PublishError) as exc:
        probe._json_object(resp)
    assert not isinstance(exc.value, CredentialError)
    assert "object was expected" in str(exc.value)


def test_the_shape_error_names_what_came_back(probe, transport):
    """The type is the diagnosis. The body is someone else's and unbounded."""
    transport(
        httpx.Response(
            200, json=["maintenance"], request=httpx.Request("GET", _URL)
        )
    )
    with pytest.raises(PublishError) as exc:
        probe._json_object(probe._request("GET", _URL))
    assert "list" in str(exc.value)
    assert "maintenance" not in str(exc.value)


def test_an_object_body_comes_back_unchanged(probe, transport):
    transport(
        httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", _URL))
    )
    assert probe._json_object(probe._request("GET", _URL)) == {"ok": True}


def test_a_null_body_reads_as_an_empty_object(probe, transport):
    """What every call site meant by the ``or {}`` this replaced.

    The adapter's own next line — "returned no post id" — is a better message
    than anything the helper could invent for a body that decoded fine and
    simply said nothing.
    """
    transport(
        httpx.Response(
            200,
            content=b"null",
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", _URL),
        )
    )
    assert probe._json_object(probe._request("GET", _URL)) == {}


@pytest.mark.parametrize(
    "adapter, call",
    [
        (DevToAdapter(), lambda a: a.verify({"api_key": "k"})),
        (HashnodeAdapter(), lambda a: a.verify({"api_key": "k"})),
        (
            MastodonAdapter(),
            # `instance_url`, not `base_url`: the wrong key is missing-required,
            # so the adapter raises CredentialError — itself a PublishError —
            # before it makes a request, and this arm passes having swept
            # nothing. See `_reaches_the_transport` in
            # test_every_adapter_answers_a_bad_day_the_same_way.py.
            lambda a: a.verify(
                {"access_token": "t", "instance_url": "https://mastodon.test"}
            ),
        ),
        (
            BlueskyAdapter(),
            lambda a: a.verify({"handle": "me.bsky.social", "app_password": "p"}),
        ),
        (MediumAdapter(), lambda a: a.verify({"integration_token": "t"})),
        (ButtondownAdapter(), lambda a: a.verify({"api_key": "k"})),
        (WordPressAdapter(), lambda a: a.verify(
            {"site_url": "https://wp.test", "username": "u", "application_password": "p"}
        )),
    ],
    ids=["devto", "hashnode", "mastodon", "bluesky", "medium", "buttondown", "wordpress"],
)
def test_no_adapter_turns_a_wrong_shaped_body_into_a_terminal_failure(
    adapter, call, transport, monkeypatch
):
    """Swept, for the same reason the non-JSON case is: one adapter left out is
    one platform that still burns a post on a gateway having a bad minute."""
    monkeypatch.setattr(base.link_check, "unreachable_reason", lambda url: None)
    transport(
        httpx.Response(200, json=["maintenance"], request=httpx.Request("GET", _URL))
    )

    with pytest.raises(PublishError):
        call(adapter)


def test_medium_verifies_an_account_with_no_name_at_all(transport):
    """``_me`` guarantees an id and nothing else.

    Indexing ``name`` for the fallback raised ``KeyError`` on an account with
    neither name — a 500 from the settings page on a token it could have
    reported on.
    """
    transport(
        httpx.Response(
            200, json={"data": {"id": "1a2b"}}, request=httpx.Request("GET", _URL)
        )
    )
    assert MediumAdapter().verify({"integration_token": "t"}) == "1a2b"


# -- The adapters that read a body ----------------------------------------- #


@pytest.mark.parametrize(
    "adapter, call",
    [
        (DevToAdapter(), lambda a: a.verify({"api_key": "k"})),
        (HashnodeAdapter(), lambda a: a.verify({"api_key": "k"})),
        (
            MastodonAdapter(),
            # `instance_url`, not `base_url`: the wrong key is missing-required,
            # so the adapter raises CredentialError — itself a PublishError —
            # before it makes a request, and this arm passes having swept
            # nothing. See `_reaches_the_transport` in
            # test_every_adapter_answers_a_bad_day_the_same_way.py.
            lambda a: a.verify(
                {"access_token": "t", "instance_url": "https://mastodon.test"}
            ),
        ),
        (
            BlueskyAdapter(),
            lambda a: a.verify({"handle": "me.bsky.social", "app_password": "p"}),
        ),
    ],
    ids=["devto", "hashnode", "mastodon", "bluesky"],
)
def test_every_adapter_reports_a_non_json_body_as_a_publish_error(
    adapter, call, transport, monkeypatch
):
    """Swept across adapters rather than pinned at one, because the fix was.

    Each of these called `resp.json()` bare. One left unconverted is one
    platform that still fails its publications terminally on a transient blip.
    """
    # Mastodon and Bluesky point at a host the user typed, so `_request`
    # resolves it first and refuses private space. The test host does not
    # resolve at all; the SSRF guard is not what is under test here.
    monkeypatch.setattr(base.link_check, "unreachable_reason", lambda url: None)
    transport(_html())

    with pytest.raises(PublishError):
        call(adapter)


# -- Hashnode's GraphQL envelope ------------------------------------------- #
#
# GraphQL reports failure inside a 200 body, so this adapter reads further into
# the payload than the REST ones and has more shapes to be wrong about.


def test_a_graphql_body_that_is_not_an_object_is_a_publish_error(transport):
    """`.get` on a list raises AttributeError — again not a PublishError."""
    transport(
        httpx.Response(200, json=["not", "an", "envelope"], request=httpx.Request("POST", _URL))
    )
    with pytest.raises(PublishError) as exc:
        HashnodeAdapter().verify({"api_key": "k"})
    assert "GraphQL" in str(exc.value)


def test_a_malformed_error_entry_does_not_break_the_error_path(transport):
    """The errors array is what is read when something has *already* gone wrong.

    An entry that is a bare string rather than an object made reading the
    failure fail, replacing a clear "Hashnode said no" with an AttributeError
    and a terminal outcome.
    """
    transport(
        httpx.Response(
            200,
            json={"errors": ["something went wrong"]},
            request=httpx.Request("POST", _URL),
        )
    )
    with pytest.raises(PublishError) as exc:
        HashnodeAdapter().verify({"api_key": "k"})
    assert "something went wrong" in str(exc.value)


def test_an_auth_code_among_the_errors_is_still_a_credential_error(transport):
    """The classification must survive the hardening — retrying a dead token is
    three wasted attempts and a user who is never told to reconnect."""
    transport(
        httpx.Response(
            200,
            json={
                "errors": [
                    {"message": "token expired", "extensions": {"code": "UNAUTHENTICATED"}}
                ]
            },
            request=httpx.Request("POST", _URL),
        )
    )
    with pytest.raises(CredentialError):
        HashnodeAdapter().verify({"api_key": "k"})


def test_an_error_without_extensions_is_still_a_plain_publish_error(transport):
    transport(
        httpx.Response(
            200,
            json={"errors": [{"message": "upstream hiccup"}]},
            request=httpx.Request("POST", _URL),
        )
    )
    with pytest.raises(PublishError) as exc:
        HashnodeAdapter().verify({"api_key": "k"})
    assert not isinstance(exc.value, CredentialError)
    assert "upstream hiccup" in str(exc.value)

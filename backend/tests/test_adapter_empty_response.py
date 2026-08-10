"""A platform answers 200 with valid JSON that does not contain the answer.

Distinct from ``test_adapter_malformed_response.py``, which is about bodies that
are not JSON at all. Here the body parses fine — it is simply missing the one
field the call existed to get: the account on a ``verify``, the post id on a
``publish``. Every adapter has a guard for it, and each guard is on the branch
nothing walked.

Two properties are worth pinning, and they are not the same property:

* **The exception type**, because ``publishing_service.execute`` routes on it.
  ``PublishError`` spends one retry; ``CredentialError`` marks the connection
  invalid and asks the user to reconnect; anything else falls to the catch-all
  and fails the publication **terminally**. A post that is missing an id is a
  bad answer, not a bad token — and a ``verify`` that comes back without an
  account *is* about the credentials.
* **That it raises at all.** A guard that returned ``PublishResult`` with an
  empty ``external_id`` would record a publication Herald can never poll
  metrics for or link to, and mark it published.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.publishers import base
from app.services.publishers.base import (
    CredentialError,
    PublishError,
    PublishRequest,
)
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.mastodon import MastodonAdapter
from app.services.publishers.wordpress import WordPressAdapter


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _: None)


@pytest.fixture(autouse=True)
def no_ssrf_guard(monkeypatch):
    """Several of these point at a host the user typed, which ``_request``
    resolves before calling. The test host does not resolve at all, and the
    SSRF guard has its own file — see ``test_adapter_ssrf.py``.
    """
    monkeypatch.setattr(base.link_check, "unreachable_reason", lambda url: None)


@pytest.fixture
def transport(monkeypatch):
    """Answer requests with the given JSON bodies, in order.

    A list rather than a single response because two of these adapters make
    more than one call: Bluesky opens a session before it posts, so the empty
    body under test has to be the *second* answer, not the first.
    """

    def install(*bodies):
        queue = list(bodies)

        def _respond(method, url, **kwargs):
            body = queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(
                200, json=body, request=httpx.Request(method, url)
            )

        monkeypatch.setattr(base.httpx, "request", _respond)

    return install


def _request() -> PublishRequest:
    return PublishRequest(
        title="Retry logic that does not double-post",
        body_markdown="## It works\n\n" + ("word " * 200),
        excerpt="How Herald avoids double-posting.",
        meta_description="How Herald avoids double-posting.",
        tags=["python"],
        slug="retry-logic",
    )


_SESSION = {"accessJwt": "jwt", "did": "did:plc:abc", "handle": "me.bsky.social"}


# --------------------------------------------------------------------------- #
# verify: 200, but no account behind the credential                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "adapter, call, body",
    [
        (DevToAdapter(), lambda a: a.verify({"api_key": "k"}), {"type_of": "user"}),
        (
            MastodonAdapter(),
            lambda a: a.verify(
                {"instance_url": "https://mastodon.test", "access_token": "t"}
            ),
            {"id": "1"},
        ),
        (
            BlueskyAdapter(),
            lambda a: a.verify({"handle": "me.bsky.social", "app_password": "p"}),
            {"did": "did:plc:abc"},
        ),
        (
            WordPressAdapter(),
            lambda a: a.verify(
                {
                    "site_url": "https://blog.test",
                    "username": "me",
                    "application_password": "p",
                }
            ),
            {"name": "Me"},
        ),
        (
            HashnodeAdapter(),
            lambda a: a.verify({"api_key": "k"}),
            {"data": {"me": {}}},
        ),
    ],
    ids=["devto", "mastodon", "bluesky", "wordpress", "hashnode"],
)
def test_a_verify_with_no_account_in_it_is_a_credential_error(
    adapter, call, body, transport
):
    transport(body)

    with pytest.raises(CredentialError):
        call(adapter)


def test_a_verify_failure_names_the_platform(transport):
    """The message lands in ``connection.last_error`` on the settings page,
    where "returned no account" without a platform in front of it is useless.
    """
    transport({"type_of": "user"})

    with pytest.raises(CredentialError) as exc:
        DevToAdapter().verify({"api_key": "k"})
    assert "Dev.to" in str(exc.value)


# --------------------------------------------------------------------------- #
# publish: 200, but nothing that identifies the post                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "adapter, credentials, bodies",
    [
        (DevToAdapter(), {"api_key": "k"}, ({"url": "https://dev.to/x"},)),
        (
            MastodonAdapter(),
            {"instance_url": "https://mastodon.test", "access_token": "t"},
            ({"url": "https://mastodon.test/@me/1"},),
        ),
        (
            WordPressAdapter(),
            {
                "site_url": "https://blog.test",
                "username": "me",
                "application_password": "p",
            },
            ({"link": "https://blog.test/x"},),
        ),
        (
            BlueskyAdapter(),
            {"handle": "me.bsky.social", "app_password": "p"},
            (_SESSION, {"cid": "bafy"}),
        ),
        (
            HashnodeAdapter(),
            {"api_key": "k", "publication_id": "pub"},
            ({"data": {"publishPost": {"post": {}}}},),
        ),
    ],
    ids=["devto", "mastodon", "wordpress", "bluesky", "hashnode"],
)
def test_a_publish_with_no_id_in_it_is_a_publish_error(
    adapter, credentials, bodies, transport
):
    """Not a ``CredentialError``: the token clearly worked — the platform
    answered. Marking the connection invalid here would send the user off to
    re-enter a working token for a problem at the other end.
    """
    transport(*bodies)

    with pytest.raises(PublishError) as exc:
        adapter.publish(_request(), credentials)
    assert not isinstance(exc.value, CredentialError)


def test_a_hashnode_draft_with_no_id_in_it_is_a_publish_error(transport):
    """The draft path is a different GraphQL mutation with its own guard."""
    transport({"data": {"createDraft": {"draft": {}}}})
    request = PublishRequest(
        title="Draft",
        body_markdown="word " * 200,
        excerpt="x",
        meta_description="x",
        slug="draft",
        as_draft=True,
    )

    with pytest.raises(PublishError) as exc:
        HashnodeAdapter().publish(request, {"api_key": "k", "publication_id": "pub"})
    assert "draft" in str(exc.value).lower()


def test_a_graphql_envelope_with_no_data_is_a_publish_error(transport):
    """GraphQL's 200-for-everything means an empty envelope is the shape a
    failure arrives in, and ``data`` missing is not something to read past.
    """
    transport({"extensions": {}})

    with pytest.raises(PublishError) as exc:
        HashnodeAdapter().verify({"api_key": "k"})
    assert "no data" in str(exc.value)


def test_a_bluesky_session_without_a_jwt_is_a_credential_error(transport):
    """The handshake is the credential check, so a session that comes back
    without one is the token being wrong — even though the call was a publish.
    """
    transport({"did": "did:plc:abc", "handle": "me.bsky.social"})

    with pytest.raises(CredentialError):
        BlueskyAdapter().publish(_request(), {"handle": "me", "app_password": "p"})


def test_the_failure_carries_the_body_so_the_user_can_see_what_came_back(transport):
    """Quoted on purpose — an id that is missing is usually a payload the
    platform rejected quietly, and the body is the only evidence of which. It
    is also why every one of these is clipped before it reaches a column; see
    ``test_error_message_bounds.py``.
    """
    transport({"error": "post rejected: title too long"})

    with pytest.raises(PublishError) as exc:
        DevToAdapter().publish(_request(), {"api_key": "k"})
    assert "title too long" in str(exc.value)

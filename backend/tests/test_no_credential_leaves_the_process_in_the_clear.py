"""A platform credential may go out in a header or a body, and nowhere else.

Herald holds, per user, a Dev.to key, a GitHub PAT with write access to their
blog repo, a WordPress application password, a Mastodon and a Bluesky token.
They are encrypted at rest (``test_credentials_are_encrypted_at_rest.py``) and
re-encryptable without an outage (``test_credential_key_rotation.py``). This
file covers the part in between: the moment one is decrypted and used, which is
the only moment it exists in the clear.

Three places it must not end up, each a different kind of durable:

* **A URL.** A query string is logged by every proxy and reverse proxy on the
  path, lands in the platform's own access logs, and is the one part of an
  HTTPS request that intermediaries routinely retain. Plenty of APIs accept
  ``?api_key=`` and it is always the wrong option.
* **A log line.** Herald's own logs are shipped off the box and kept, and the
  publish path logs on every retry — the exact moment things are going wrong
  and somebody will be reading.
* **An exception message.** Failures are stored on the publication row in
  ``last_error`` and rendered in the UI, so an adapter that quotes what it sent
  writes the token into the database in plaintext, next to the column that was
  encrypted to keep it out.

Swept from ``credential_fields`` rather than a list here, so an adapter that
grows a new secret is covered by having declared it — and asserted to have been
reached, so an arm cannot pass by never making the call.

This was audited by reading, twice, and came back clean both times. Reading is
what the audit had: nothing failed if a later change put a token in a query
string, because nothing was looking. This is that audit, mechanised.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.models.content import Content, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform
from app.services import publishers, publishing_service
from app.services.crypto import encrypt_credentials
from app.services.errors import REDACTED, redact
from app.services.publishers import all_adapters, base, get_adapter
from app.services.publishers.base import (
    CredentialError,
    PublishError,
    PublishRequest,
    RateLimited,
)

_CREDENTIALS: dict[Platform, dict[str, str]] = {
    Platform.DEVTO: {"api_key": "devto-secret-8f21ac"},
    Platform.MEDIUM: {"integration_token": "medium-secret-3b90de"},
    Platform.HASHNODE: {"api_key": "hashnode-secret-77c4e1", "publication_id": "pub-1"},
    Platform.WORDPRESS: {
        "site_url": "https://blog.example.test",
        "username": "author",
        "application_password": "wordpress-secret-a41f0b",
    },
    Platform.MASTODON: {
        "instance_url": "https://mastodon.example.test",
        "access_token": "mastodon-secret-c209fe",
    },
    Platform.BLUESKY: {
        "handle": "author.bsky.social",
        "app_password": "bluesky-secret-6d17aa",
    },
    Platform.GIT: {
        "repo": "author/blog",
        "token": "ghp_gitsecret5e83b2",
        "path_template": "posts/{slug}.md",
    },
    Platform.BUTTONDOWN: {"api_key": "buttondown-secret-1c74fd"},
}

#: What each adapter that reports metrics is asked about. Shaped like the real
#: identifier, because Bluesky parses it.
_EXTERNAL_IDS: dict[Platform, str] = {
    Platform.DEVTO: "441029",
    Platform.MASTODON: "109876543210987654",
    Platform.BLUESKY: "at://did:plc:abc123/app.bsky.feed.post/3kabcd",
}

_FLEET = [a for a in all_adapters() if a.implemented]
_IDS = [a.platform.value for a in _FLEET]

_ARTICLE = PublishRequest(
    title="Shipping the scheduler rewrite",
    body_markdown="A paragraph with enough words in it to look like a real post.",
    excerpt="What changed and why.",
    meta_description="What changed in the scheduler and why.",
    tags=["python"],
    slug="shipping-the-scheduler-rewrite",
    canonical_url="https://blog.example.test/shipping-the-scheduler-rewrite",
    project_url="https://blog.example.test",
    project_name="Example",
)


def _secrets(adapter) -> list[str]:
    """The values this adapter declared secret, as they were handed to it."""
    credentials = _CREDENTIALS[adapter.platform]
    return [
        credentials[field.key]
        for field in adapter.credential_fields
        if field.secret and credentials.get(field.key)
    ]


def test_every_implemented_adapter_declares_at_least_one_secret():
    """Otherwise ``_secrets`` is empty and every sweep below asserts nothing.

    The failure mode this guards is quiet: a credential field that loses
    ``secret=True`` in a refactor stops being checked here *and* starts being
    echoed by the settings API, and nothing else would notice.
    """
    for adapter in _FLEET:
        assert _secrets(adapter), f"{adapter.display_name} has no secret to protect"


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Herald 1.0",
        slug="herald-1-0",
        body_markdown="## It's out\n\n" + ("word " * 200),
        excerpt="Herald 1.0 is out.",
        meta_description="Herald 1.0 is out.",
        tags=["python"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def connected(db, user) -> PlatformConnection:
    row = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.CONNECTED,
        encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        display_name="@r2st",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _: None)


@pytest.fixture(autouse=True)
def resolves_public(monkeypatch):
    """Not what is under test — see ``test_adapter_ssrf.py``."""
    monkeypatch.setattr(base.link_check, "unreachable_reason", lambda url: None)


@pytest.fixture
def recorder(monkeypatch):
    """Record every outbound call, answering each with *outcome*."""
    calls: list[dict] = []

    def install(outcome):
        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(base.httpx, "request", fake_request)
        return calls

    return install


def _ok() -> httpx.Response:
    """A 200 carrying nothing an adapter can use.

    Deliberately not a valid payload: the adapters will raise on it, and what
    this file inspects is what went *out* plus the failure that came back.
    Making each response valid per platform would test the same property with
    ten times the fixture.
    """
    return httpx.Response(
        200, json={}, request=httpx.Request("POST", "https://platform.test/api")
    )


def _drive(adapter, calls, call) -> BaseException | None:
    """Run *call*, tolerating the failure an empty 200 provokes."""
    error: BaseException | None = None
    try:
        call()
    except PublishError as exc:
        error = exc
    assert calls, (
        f"{adapter.display_name} never reached the transport — this arm "
        "inspected nothing"
    )
    return error


# --------------------------------------------------------------------------- #
# Not in the URL                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_no_secret_reaches_the_url_when_publishing(adapter, recorder):
    """The URL is the one part of the request that intermediaries keep."""
    calls = recorder(_ok())

    _drive(
        adapter,
        calls,
        lambda: adapter.publish(_ARTICLE, _CREDENTIALS[adapter.platform]),
    )

    for call in calls:
        for secret in _secrets(adapter):
            assert secret not in call["url"], (
                f"{adapter.display_name} put a credential in {call['url']}"
            )
            assert secret not in str(call.get("params") or {}), (
                f"{adapter.display_name} put a credential in a query parameter"
            )


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_no_secret_reaches_the_url_when_verifying(adapter, recorder):
    """``verify`` runs from the settings page, on a credential just typed in."""
    calls = recorder(_ok())

    _drive(
        adapter,
        calls,
        lambda: adapter.verify(_CREDENTIALS[adapter.platform]),
    )

    for call in calls:
        for secret in _secrets(adapter):
            assert secret not in call["url"]
            assert secret not in str(call.get("params") or {})


@pytest.mark.parametrize(
    "adapter",
    [a for a in _FLEET if a.supports_metrics],
    ids=[a.platform.value for a in _FLEET if a.supports_metrics],
)
def test_no_secret_reaches_the_url_when_polling_metrics(adapter, recorder):
    """The metrics sweep runs unattended, on a schedule, for every live post.

    Which makes it the highest-volume outbound path Herald has, and the one
    whose URLs would fill a proxy log fastest.
    """
    calls = recorder(_ok())

    _drive(
        adapter,
        calls,
        lambda: adapter.fetch_metrics(
            _EXTERNAL_IDS[adapter.platform], _CREDENTIALS[adapter.platform]
        ),
    )

    for call in calls:
        for secret in _secrets(adapter):
            assert secret not in call["url"]
            assert secret not in str(call.get("params") or {})


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_the_secret_does_go_out_somewhere(adapter, recorder):
    """The negative sweeps above pass trivially if nothing is ever sent.

    A publish that authenticated with nothing would satisfy every assertion in
    this file, so one arm has to check the credential actually left — in a
    header or a body, which are the two places it is allowed to be.

    WordPress is the exception and not a leak: it sends Basic auth, so what goes
    out is base64 of ``user:password`` rather than the password itself.
    """
    calls = recorder(_ok())

    _drive(
        adapter,
        calls,
        lambda: adapter.publish(_ARTICLE, _CREDENTIALS[adapter.platform]),
    )

    # ``json``, not ``json_body``: this records what reaches ``httpx.request``,
    # and ``_send`` passes the adapter's ``json_body`` along under httpx's name.
    # Bluesky is the arm that catches the difference — it is the one adapter
    # that authenticates in a body rather than a header.
    sent = " ".join(f"{call.get('headers')}{call.get('json')}" for call in calls)
    if adapter.platform is Platform.WORDPRESS:
        import base64 as b64

        credentials = _CREDENTIALS[Platform.WORDPRESS]
        pair = f"{credentials['username']}:{credentials['application_password']}"
        assert b64.b64encode(pair.encode()).decode() in sent
        return

    assert any(secret in sent for secret in _secrets(adapter)), (
        f"{adapter.display_name} authenticated with nothing — the sweeps in "
        "this file would pass on a request that cannot work"
    )


# --------------------------------------------------------------------------- #
# Not in the logs, not in the error                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
@pytest.mark.parametrize("status", [401, 429, 500])
def test_no_adapter_writes_the_credential_into_its_own_failure(
    adapter, status, recorder, caplog
):
    """Herald must never be the one that puts the token in the message.

    ``_request`` logs each retry and ``publishing_service`` stores what was
    raised on the row, so an adapter that quotes its own *request* into a
    failure copies the token into the database in plaintext and onto a screen —
    undoing, on the unhappy path, exactly what the encryption at rest is for.

    The platform's response body here says nothing secret, which is what
    isolates this claim to Herald's own formatting. The other half — a platform
    that echoes the credential back at us — is not something an adapter can
    prevent, and is covered at the layer that can: see
    ``test_a_platform_that_echoes_the_token_does_not_get_it_stored``.

    Swept over three statuses because they take three different arms of
    ``_translate``, and the arm that retries is the one that also logs.
    """
    calls = recorder(
        httpx.Response(
            status,
            json={"error": "rejected"},
            request=httpx.Request("POST", "https://platform.test/api"),
        )
    )

    with caplog.at_level(logging.DEBUG):
        error = _drive(
            adapter,
            calls,
            lambda: adapter.publish(_ARTICLE, _CREDENTIALS[adapter.platform]),
        )

    assert error is not None
    for secret in _secrets(adapter):
        assert secret not in str(error), (
            f"{adapter.display_name} quoted a credential into its failure, "
            "which is stored on the publication row and rendered in the UI"
        )
        assert secret not in caplog.text, (
            f"{adapter.display_name} logged a credential"
        )


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_the_secret_is_stripped_before_a_failure_becomes_a_row(adapter):
    """The half no adapter can defend against: the platform echoing it back.

    Some APIs validate by quoting what you sent — *"invalid api_key: ghp_…"* —
    and ``_translate`` puts the response body in the message deliberately,
    because "WordPress returned 400" with nothing after it is not something a
    user can act on. So the body is worth keeping and the token in it is not,
    and the only place that knows which substring is which is the caller that
    holds the credentials.

    Checked per adapter rather than once, because the input is
    ``credential_fields``: an adapter whose secret is not declared secret is
    invisible to this, and this is the test that says so.
    """
    credentials = _CREDENTIALS[adapter.platform]
    secrets = publishers.secret_values(adapter, credentials)
    assert secrets, f"{adapter.display_name} declares no secret to strip"

    error = PublishError(
        f"{adapter.display_name} returned 400: "
        f'{{"error": "invalid credential {secrets[0]}"}}'
    )
    publishing_service._redact_credentials(error, adapter, credentials)

    assert secrets[0] not in str(error)
    assert REDACTED in str(error)
    # The rest of the platform's message is the useful part and stays.
    assert "returned 400" in str(error)


def test_redaction_keeps_the_type_and_everything_hanging_off_it():
    """The failure is re-raised after redaction, into arms that match on type.

    ``RateLimited.retry_after`` decides how long the row is parked and
    ``status_code`` decides whether a Git lookup reads as "new post". Rebuilding
    the exception to change its message would drop both, which is why the args
    are rewritten in place.
    """
    adapter = get_adapter(Platform.DEVTO)
    credentials = _CREDENTIALS[Platform.DEVTO]
    secret = credentials["api_key"]
    error = RateLimited(f"Dev.to rate-limited {secret}", retry_after=900)
    error.status_code = 429

    publishing_service._redact_credentials(error, adapter, credentials)

    assert isinstance(error, RateLimited)
    assert error.retry_after == 900
    assert error.status_code == 429
    assert secret not in str(error)


def test_a_platform_that_echoes_the_token_does_not_get_it_stored(
    db, content, connected, monkeypatch
):
    """End to end: the row and the connection, after a platform echoes it back.

    ``publication.error`` and ``connection.last_error`` are both plaintext
    ``Text`` columns that the UI renders, and they sit in the same database as
    ``encrypted_credentials``. This is the assertion that the encryption is not
    quietly undone by the failure path beside it.
    """
    secret = "devto-secret-8f21ac"
    connected.encrypted_credentials = encrypt_credentials({"api_key": secret})
    db.commit()

    def echoes(self, request, credentials):
        raise CredentialError(f'Dev.to rejected the credentials (401): {{"key": "{secret}"}}')

    # The class, not ``get_adapter(...)``. ``_ADAPTERS`` holds one shared
    # instance per platform, and ``monkeypatch`` restores an instance attribute
    # by *setting it back* — so patching the singleton leaves a permanent
    # instance attribute shadowing the class for every test that runs after
    # this one, and their own class-level patches then silently do nothing.
    monkeypatch.setattr(type(get_adapter(Platform.DEVTO)), "publish", echoes)

    publication = publishing_service.queue(db, content, ["devto"])[0]
    publishing_service.execute(db, publication)
    db.refresh(publication)
    db.refresh(connected)

    assert publication.error and secret not in publication.error
    assert connected.last_error and secret not in connected.last_error
    assert REDACTED in publication.error
    # Still says what went wrong, which is the whole reason the body is kept.
    assert "401" in publication.error


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(lambda s: PublishError(f'Dev.to said 400: {{"key": "{s}"}}'), id="error"),
        pytest.param(
            lambda s: RateLimited(f"Dev.to throttled key {s}", retry_after=30),
            id="rate-limited",
        ),
    ],
)
def test_a_platform_that_echoes_the_token_on_a_metrics_poll_is_not_logged_in_the_clear(
    db, content, connected, monkeypatch, caplog, raised
):
    """The poll is the third caller that shows a platform the credential.

    ``execute`` and the headline sync both strip the connection's secrets out
    of a platform's answer before it is written or logged. ``collect_metrics``
    logged the answer as it came — and the metrics sweep is the path that runs
    most often, unattended, for every live post, so a platform that echoes
    its token would have put it in the shipped log once per poll.
    """
    secret = "devto-secret-8f21ac"
    connected.encrypted_credentials = encrypt_credentials({"api_key": secret})
    db.commit()
    from app.models.publication import Publication, PublicationStatus

    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="12345",
    )
    db.add(publication)
    db.commit()

    def echoes(self, external_id, credentials):
        raise raised(secret)

    monkeypatch.setattr(type(get_adapter(Platform.DEVTO)), "fetch_metrics", echoes)

    with caplog.at_level(logging.DEBUG):
        metric = publishing_service.collect_metrics(db, publication)

    assert metric is None
    assert "Dev.to" in caplog.text  # still says what happened
    assert secret not in caplog.text
    assert REDACTED in caplog.text


def test_a_credential_too_short_to_be_one_is_left_alone():
    """Redaction must not turn an ordinary message into holes.

    The fixture connection in ``conftest`` stores ``{"api_key": "k"}``, and a
    one-character secret appears inside every other word. Removing it would
    corrupt every failure message on a test box and, worse, on any real
    connection whose credential is short enough to collide — the message would
    silently lose text that was never a secret.
    """
    assert redact("a key that is not a key", ["k"]) == "a key that is not a key"
    assert redact("rejected: shortish", ["short"]) == "rejected: shortish"
    assert redact("rejected: abcdefgh", ["abcdefgh"]) == f"rejected: {REDACTED}"


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_a_dead_socket_says_nothing_that_contains_the_credential(
    adapter, recorder, caplog
):
    """The transport's own exception is interpolated into the message.

    ``_request`` builds "…request failed: {exc}" from whatever httpx raised, and
    httpx exceptions carry the request they were raised for. A URL with a
    credential in it would arrive in the error by that route even if no adapter
    ever formatted one deliberately — which is the failure the URL sweeps above
    would not catch on their own.
    """
    calls = recorder(httpx.ConnectError("connection refused"))

    with caplog.at_level(logging.DEBUG):
        error = _drive(
            adapter,
            calls,
            lambda: adapter.publish(_ARTICLE, _CREDENTIALS[adapter.platform]),
        )

    assert error is not None
    for secret in _secrets(adapter):
        assert secret not in str(error)
        assert secret not in caplog.text

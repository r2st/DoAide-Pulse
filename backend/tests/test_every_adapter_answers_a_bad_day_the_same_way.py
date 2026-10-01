"""Every adapter's ``publish()`` must answer a bad day with the same vocabulary.

``publishing_service.execute`` decides what happens to a publication row purely
from the *type* of what the adapter raised:

* ``CredentialError`` — terminal. The row fails, the connection is marked
  invalid, and the user is asked to reconnect.
* ``RateLimited`` — the row is parked until the time the platform named.
* any other ``PublishError`` — one of the retry budget, then backoff.
* anything else — the ``except Exception`` catch-all, which fails the
  publication **terminally** with its retry budget untouched.

So the mapping from "what went wrong out there" to "what type comes back" is
the whole contract, and it is worth exactly nothing if only one adapter honours
it.

Until this file, it was only ever checked against ``_Probe`` — a two-line
``Adapter`` subclass in ``test_publisher_retry.py`` that exists to call
``_request`` directly and has no ``publish()`` at all. Every real adapter was
covered for its *success* path (``test_publisher_success_paths.py``), for a
body that will not decode (``test_adapter_malformed_response.py``) and for a
body with nothing in it (``test_adapter_empty_response.py``), and not one of
them was ever handed a 401, a 429, a maintenance window or a dead socket
through the method the publish task actually calls.

That gap is invisible from the base class, because the base class is right. It
is a gap about *reachability*: an adapter that catches too broadly, that opens
a socket of its own, or that swallows a failure on a lookup it makes before the
real call, breaks the contract without ``_request`` ever changing. The last of
those was real — see ``test_a_lookup_that_could_not_look_is_not_a_new_post``.

Each sweep answers every HTTP call the adapter makes with the same bad outcome,
which keeps the test ignorant of how many calls an adapter makes and in what
order — the property under test is the type that comes out, not the shape of
the conversation. Every case asserts the transport was actually reached, so an
arm cannot pass by tripping over its own credential dict before it gets there.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.publication import Platform
from app.services.publishers import all_adapters, base, get_adapter
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishError,
    PublishRequest,
    RateLimited,
    UnsupportedOption,
)

# --------------------------------------------------------------------------- #
# The fleet                                                                    #
# --------------------------------------------------------------------------- #

#: A credential dict that gets each adapter as far as its first HTTP call.
#: Values are shaped like the real thing but are not secrets; what matters is
#: that every *required* field is present, or the adapter refuses before it
#: reaches the transport and the sweep tests nothing. ``_reaches_the_transport``
#: is the guard that keeps that honest.
_CREDENTIALS: dict[Platform, dict[str, str]] = {
    Platform.DEVTO: {"api_key": "devto-key"},
    Platform.MEDIUM: {"integration_token": "medium-token"},
    Platform.HASHNODE: {"api_key": "hashnode-key", "publication_id": "pub-1"},
    Platform.WORDPRESS: {
        "site_url": "https://blog.example.test",
        "username": "author",
        "application_password": "wp-app-password",
    },
    Platform.MASTODON: {
        "instance_url": "https://mastodon.example.test",
        "access_token": "masto-token",
    },
    Platform.BLUESKY: {
        "handle": "author.bsky.social",
        "app_password": "bsky-app-password",
    },
    Platform.GIT: {
        "repo": "author/blog",
        "token": "ghp_gittoken",
        "path_template": "posts/{slug}.md",
    },
    Platform.BUTTONDOWN: {"api_key": "buttondown-key"},
}

#: Every platform Pulse can actually publish to, as (platform, adapter) pairs.
#: Read off the registry rather than listed here: an adapter that becomes
#: implemented joins these sweeps by doing so, which is the point — the failure
#: this file exists to catch is a *new* destination quietly not honouring the
#: contract.
_FLEET = [a for a in all_adapters() if a.implemented]

_IDS = [a.platform.value for a in _FLEET]

_ARTICLE = PublishRequest(
    title="Shipping the scheduler rewrite",
    body_markdown="A paragraph with enough words in it to look like a real post.",
    excerpt="What changed and why.",
    meta_description="What changed in the scheduler and why it was worth doing.",
    tags=["python", "scheduling"],
    keywords=["scheduler"],
    focus_keyword="scheduler",
    slug="shipping-the-scheduler-rewrite",
    canonical_url="https://blog.example.test/shipping-the-scheduler-rewrite",
    project_url="https://blog.example.test",
    project_name="Example",
)


def test_the_fleet_is_every_implemented_platform():
    """The sweeps below are only as good as the list they walk.

    Guards the two ways this file goes quietly out of date: a new adapter with
    no credentials here (which would fail loudly, so it is the safe direction),
    and — the dangerous one — an adapter that has credentials and is silently
    dropped from ``_FLEET`` because it stopped reporting itself implemented.
    """
    assert {a.platform for a in _FLEET} == set(_CREDENTIALS)
    assert len(_FLEET) == 8, "an adapter joined or left; give it credentials above"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Let the in-process retry loop run without spending the wall clock on it."""
    slept: list[float] = []
    monkeypatch.setattr(base, "_sleep", slept.append)
    return slept


@pytest.fixture(autouse=True)
def resolves_public(monkeypatch):
    """WordPress, Mastodon and Bluesky resolve their host before connecting.

    The test hosts do not exist, and SSRF refusal is not what is under test
    here — ``test_adapter_ssrf.py`` owns that. Without this the three
    address-taking adapters would answer every case below with ``RefusedHost``,
    which is a ``CredentialError``, and three arms of the rate-limit sweep would
    fail for a reason that has nothing to do with rate limiting.
    """
    monkeypatch.setattr(base.link_check, "unreachable_reason", lambda url: None)


@pytest.fixture
def transport(monkeypatch):
    """Answer every outbound call with *outcome*; return the calls made.

    An ``Exception`` is raised, anything else is returned. One outcome for the
    whole conversation: an adapter that looks something up before it writes gets
    the same bad day for both calls, so the sweep does not have to know that it
    does.
    """
    calls: list[tuple[str, str]] = []

    def install(outcome):
        def fake_request(method, url, **kwargs):
            calls.append((method, url))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(base.httpx, "request", fake_request)
        return calls

    return install


def _response(status: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        json={"message": "no"},
        request=httpx.Request("POST", "https://platform.test/api"),
    )


def _publish(adapter, calls) -> BaseException:
    """Publish, and return what came back out. Fails if nothing was raised."""
    with pytest.raises(PublishError) as caught:
        adapter.publish(_ARTICLE, _CREDENTIALS[adapter.platform])
    assert calls, (
        f"{adapter.display_name} never reached the transport — it refused on "
        "something else first, so this case tested nothing"
    )
    return caught.value


# --------------------------------------------------------------------------- #
# A rejected token is terminal, everywhere                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_terminal_from_every_adapter(adapter, status, transport):
    """401/403 must be a ``CredentialError``, or the row retries a dead token.

    Retrying a rejected credential is not merely useless — it spends the
    publication's whole budget re-asking a question already answered, and ends
    at the same failure several minutes later with the user told nothing more
    useful than they would have been told immediately.
    """
    calls = transport(_response(status))

    error = _publish(adapter, calls)

    assert isinstance(error, CredentialError)
    assert adapter.display_name.split("/")[0] in str(error)


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_not_replayed_by_any_adapter(
    adapter, status, transport, no_waiting
):
    """The in-process loop must not soften a terminal answer into three of them."""
    calls = transport(_response(status))

    _publish(adapter, calls)

    assert not no_waiting, f"{adapter.display_name} slept before giving up on a {status}"
    # One call per distinct request the adapter makes, none of them a replay.
    assert len(calls) == len(set(calls))


# --------------------------------------------------------------------------- #
# A rate limit carries the platform's own answer to "when?"                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_a_rate_limit_is_carried_up_with_the_platforms_own_wait(adapter, transport):
    """429 + ``Retry-After`` must reach the caller as ``RateLimited``.

    The wait is the whole value of the type. Downgraded to a plain
    ``PublishError`` the row comes back on Pulse's own backoff, which is how a
    soft limit becomes a hard ban; upgraded to a ``CredentialError`` the piece
    fails for good against a token that was never the problem.

    900 seconds is deliberately longer than ``publish_retry_max_backoff_seconds``
    so it must be handed *upwards* rather than slept through — a wait the
    in-process loop cannot honour in full is one it must not honour in part.
    """
    calls = transport(_response(429, headers={"Retry-After": "900"}))

    error = _publish(adapter, calls)

    assert isinstance(error, RateLimited)
    assert error.retry_after == 900


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_a_long_rate_limit_is_not_slept_through_by_any_adapter(
    adapter, transport, no_waiting
):
    calls = transport(_response(429, headers={"Retry-After": "900"}))

    _publish(adapter, calls)

    assert not no_waiting, (
        f"{adapter.display_name} slept on a wait it was told to hand upwards"
    )


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_an_announced_maintenance_window_is_honoured_by_every_adapter(
    adapter, transport
):
    """RFC 9110 §10.2.3 puts ``Retry-After`` on 503 too, and it means the same.

    A platform saying "back in fifteen minutes" has given us the one number
    worth having. Answering it with Pulse's own one-second backoff spends the
    whole in-process budget inside the first blink of the outage.
    """
    calls = transport(_response(503, headers={"Retry-After": "900"}))

    error = _publish(adapter, calls)

    assert isinstance(error, RateLimited)
    assert error.retry_after == 900


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_a_bare_503_stays_retryable_rather_than_terminal(adapter, transport):
    """No header, nothing to honour — but still not the user's fault."""
    calls = transport(_response(503))

    error = _publish(adapter, calls)

    assert not isinstance(error, CredentialError | RateLimited)


# --------------------------------------------------------------------------- #
# The network, rather than the platform, having the bad day                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out connecting"),
        httpx.ReadTimeout("timed out reading"),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=["connect-refused", "connect-timeout", "read-timeout", "write", "protocol"],
)
def test_a_transport_failure_is_retryable_never_terminal(adapter, failure, transport):
    """A dead socket must not fail the piece for good.

    All five are ``httpx.HTTPError``, which ``_request`` turns into a plain
    ``PublishError``. The assertion that matters is the negative one: none of
    them may come back as a ``CredentialError`` (the user's token is fine) and
    none may escape as a non-``PublishError`` (which reaches
    ``publishing_service``'s catch-all and burns the row terminally with its
    retry budget untouched).
    """
    calls = transport(failure)

    error = _publish(adapter, calls)

    assert not isinstance(error, CredentialError)
    assert adapter.display_name.split("/")[0] in str(error)


@pytest.mark.parametrize("adapter", _FLEET, ids=_IDS)
def test_a_read_timeout_on_a_write_is_never_replayed(adapter, transport, no_waiting):
    """The one retry that must not happen: a POST that may already have landed.

    A read timeout means the request was sent and nobody knows whether it was
    processed. Replaying it risks a second published article, which is a worse
    failure than a missing one — so the in-process loop must not, for any
    adapter, replay the write. Lookups an adapter makes *before* the write are
    GETs and may be replayed freely; what is pinned here is that no adapter
    sends the same write twice.
    """
    calls = transport(httpx.ReadTimeout("timed out reading"))

    _publish(adapter, calls)

    writes = [call for call in calls if call[0] in {"POST", "PUT", "PATCH"}]
    assert len(writes) == len(set(writes)), (
        f"{adapter.display_name} replayed a write after a read timeout: {writes}"
    )


# --------------------------------------------------------------------------- #
# The lookup that could not look                                               #
# --------------------------------------------------------------------------- #


def test_a_lookup_that_could_not_look_is_not_a_new_post(transport, no_waiting):
    """The Git adapter asks whether the file exists before committing it.

    A 404 there is the ordinary "this is a new post" case. Every other failure
    means the lookup did not happen, and reading *that* as "new post" sends the
    commit without a blob sha — which the contents API refuses over a live path.
    A correction to a published piece then fails as a 422 naming a file the user
    can see perfectly well in their own repo, and the retry that would have
    fixed it repeats the same swallow.

    That argument was made once for a throttle and left as a list of the two
    exception types that had been seen (``CredentialError``, ``RateLimited``).
    A 500 and a timed-out socket are the same statement and took the swallow.
    """
    adapter = get_adapter(Platform.GIT)
    calls = transport(httpx.ReadTimeout("timed out reading"))

    error = _publish(adapter, calls)

    assert isinstance(error, PublishError)
    assert not any(call[0] == "PUT" for call in calls), (
        "the commit went out after a lookup that never answered"
    )


def test_a_lookup_that_404s_is_still_just_a_new_post(transport):
    """The ordinary case the swallow exists for, unchanged.

    The 404 must still read as "nothing there" and let the commit go out, or
    narrowing the swallow has broken every first publish to a repo.
    """
    adapter = get_adapter(Platform.GIT)
    calls = transport(_response(404))

    _publish(adapter, calls)

    assert any(call[0] == "PUT" for call in calls), (
        "a new post was never committed — the 404 stopped reading as 'new'"
    )


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_no_other_lookup_failure_reads_as_a_new_post(status, transport, no_waiting):
    """Swept over the statuses rather than pinned at one.

    The bug this replaces was a list of the failures somebody had already met.
    A list of statuses is the same mistake one layer along, so the assertion is
    the general one: only 404 lets the commit through.
    """
    adapter = get_adapter(Platform.GIT)
    calls = transport(_response(status))

    _publish(adapter, calls)

    assert not any(call[0] == "PUT" for call in calls)


# --------------------------------------------------------------------------- #
# The structural half: nothing may go around _request                          #
# --------------------------------------------------------------------------- #


def test_no_adapter_opens_a_socket_of_its_own():
    """Every sweep above is worth exactly as much as this guard.

    All the translation lives in ``Adapter._request``: a 401 is only a
    ``CredentialError`` because that method says so. An adapter that calls
    ``httpx`` directly gets none of it — no error translation, no retry rules,
    no SSRF check on a user-supplied host, and no ``publish_timeout_seconds``,
    so a hung platform holds a worker until something else gives up.

    Checked against the source rather than by calling anything, because the
    failure is a call that the sweeps above would never reach: a metrics poll,
    an error path, a branch that needs a credential no test has.
    """
    import ast
    import pathlib

    package = pathlib.Path(base.__file__).parent
    offenders: list[str] = []

    for path in sorted(package.glob("*.py")):
        if path.name == "base.py":
            continue  # the one place allowed to make the call
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "httpx"
            ):
                offenders.append(f"{path.name}:{node.lineno} httpx.{func.attr}(...)")

    assert not offenders, (
        "these bypass Adapter._request and get none of its error translation, "
        f"retries, SSRF guard or timeout: {offenders}"
    )


def test_the_unfinished_adapters_refuse_before_they_reach_a_socket(transport):
    """LinkedIn and Twitter are scaffolded, not connected.

    ``NotImplementedAdapter`` is terminal by design — it is how the API answers
    "why can't I publish here?" — and it has to be raised *without* a request,
    or an unfinished destination becomes a hung publication rather than an
    immediate, explicable failure.
    """
    calls = transport(_response(200))

    for platform in (Platform.LINKEDIN, Platform.TWITTER):
        adapter = get_adapter(platform)
        with pytest.raises(NotImplementedAdapter):
            adapter.publish(_ARTICLE, {"access_token": "t", "author_urn": "urn:li:1"})

    assert not calls, "an unfinished adapter went to the network anyway"


def test_the_draftless_platforms_refuse_before_they_reach_a_socket(transport):
    """Mastodon and Bluesky have no draft state.

    ``UnsupportedOption`` is terminal for the same reason ``CredentialError``
    is: retrying does not give a platform a feature it does not have. The part
    worth pinning is that it happens *before* the request — somebody who ticked
    a box to avoid going live must not have the post go live anyway.
    """
    calls = transport(_response(200))
    draft = PublishRequest(
        title=_ARTICLE.title,
        body_markdown=_ARTICLE.body_markdown,
        excerpt=_ARTICLE.excerpt,
        meta_description=_ARTICLE.meta_description,
        as_draft=True,
    )

    for platform in (Platform.MASTODON, Platform.BLUESKY):
        adapter = get_adapter(platform)
        with pytest.raises(UnsupportedOption):
            adapter.publish(draft, _CREDENTIALS[platform])

    assert not calls, "a draft reached a platform that would have published it live"

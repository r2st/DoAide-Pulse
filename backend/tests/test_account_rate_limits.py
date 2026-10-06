"""Rate limits on the authenticated endpoints that spend a shared resource.

The other limit sweep — ``test_public_endpoint_limits`` — is about *audience*:
anything reachable without a bearer token carries a limit. This one is about
*cost*, which is a different question with a different answer, and the two were
conflated for as long as "authenticated" was treated as "trusted enough to ask
as often as you like".

It is not, for six endpoints. Writing a piece, refreshing ideas, repurposing,
rewriting a passage and drafting headlines each turn into an LLM call against a
free-tier quota that is metered *per day* and shared by every account on the
install; a repo scan spends the install's single GitHub token; a link check
fans one request out to ``link_check_max_urls`` outbound requests from Pulse's
own address. None of those budgets belong to the caller, which is the whole
reason a caller cannot be left to decide how much of one to use.

Two properties matter more than any individual number, and both are here:

* the bucket is the **account**, not the address, so an office behind one NAT
  does not share a budget it never agreed to share
  (:func:`test_one_account_hitting_its_limit_does_not_touch_another`), and
* the account cannot **choose** its bucket, so rotating ``X-Forwarded-For``
  does not buy a second helping
  (:func:`test_the_same_account_cannot_dodge_its_budget_with_a_new_address`).

As in ``test_ratelimit`` and ``test_public_endpoint_limits``, the limits are the
real configured ones — slowapi reads the limit string when the decorator is
applied at import time, so these spend the actual budget rather than a lowered
one. ``/content/{id}/links`` is what the integration tests spend it on: it is
the cheapest of the six to call sixty times, because a draft with no links in
it makes no outbound requests at all.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.user import User
from app.ratelimit import account_key, client_key
from app.routers.projects import _not_refreshing
from app.security import create_access_token, hash_password
from tests.test_public_endpoint_limits import _api_routes, _is_limited


class _FakeRequest:
    """The two things :func:`account_key` reads, and nothing else."""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        client_host="198.51.100.9",
        query_params: dict[str, str] | None = None,
    ):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.query_params = query_params or {}

        class _Client:
            host = client_host

        self.client = _Client()
        self.scope = {"client": (client_host, 0)}


def _bearer(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


# --------------------------------------------------------------------------- #
# The key function                                                             #
# --------------------------------------------------------------------------- #


def test_a_valid_token_buckets_by_account():
    request = _FakeRequest(_bearer(42))
    assert account_key(request) == "user:42"


def test_the_account_bucket_cannot_collide_with_an_address_bucket():
    """``user:1`` and ``1`` must never be the same counter.

    A user id is a small integer and an address is not, so today they could not
    collide — which is exactly the kind of thing that stops being true quietly.
    """
    assert account_key(_FakeRequest(_bearer(1))).startswith("user:")
    assert not client_key(_FakeRequest()).startswith("user:")


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic aGVsbG86d29ybGQ="},
        # A well-formed JWT signed with the wrong key.
        {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OSJ9.bad"},
    ],
)
def test_an_unusable_token_falls_back_to_the_address(header):
    """Falling back is safe: the endpoint's own dependency refuses it anyway.

    What matters is that an unverified token cannot *name* a bucket — otherwise
    the limit would be per-claim rather than per-account, and a claim is free.
    """
    request = _FakeRequest(header, client_host="203.0.113.5")
    assert account_key(request) == "203.0.113.5"


def test_a_forged_token_cannot_spend_another_accounts_budget():
    """The bucket is only ever named by a token Pulse actually signed."""
    forged = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI3In0.nope"}
    assert account_key(_FakeRequest(forged)) != "user:7"


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #

#: What "expensive" means, spelled as the calls that make it so. An endpoint
#: whose source reaches one of these spends something it did not pay for.
_COSTLY_CALLS = (
    "content_generator.generate",
    "content_generator.suggest_ideas",
    "repurpose.generate",
    "inline_edit.edit(",
    "headlines.generate_variants",
    "github_client.fetch_activity",
    "_check_content_links",
    # Mail goes out through the install's single SMTP identity: one budget and
    # one sending reputation, shared by every account here.
    "digest.send(",
    # Both of these make a synchronous outbound request from Pulse's own
    # address because the caller asked them to — the link checker's problem in
    # a different shape. `triggers.check` is the sharper one: against a GitHub
    # trigger it spends the install's single GITHUB_TOKEN, so leaving it
    # unlimited was a way around `rate_limit_repo_scan`.
    "webhooks.deliver(",
    "trigger_service.check(",
    # Connecting or re-checking a platform verifies the credentials against the
    # platform, synchronously, before answering — so it is the same outbound
    # request the ping is, reached from the settings page. Two things make it
    # the sharper of the pair. `PUT /settings/connections` verifies credentials
    # taken *from the request body*, so unlimited it answers "is this token
    # good?" for any token at all, at Pulse's address rather than the
    # caller's. And `GitAdapter._token` falls back to the install's shared
    # GITHUB_TOKEN when the connection carries none, which is the
    # `rate_limit_repo_scan` budget again — the second way around it, after
    # `trigger_service.check`.
    "adapter.verify(",
)

#: Endpoints that touch a costly call but must not carry an account limit, with
#: the reason. Empty today; anything added needs an argument made for it.
_COSTLY_BUT_UNLIMITED: set[tuple[str, str]] = set()


def _is_costly(route: APIRoute) -> bool:
    try:
        source = inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover — every endpoint has source
        return False
    return any(call in source for call in _COSTLY_CALLS)


def _is_account_keyed(route: APIRoute) -> bool:
    try:
        source = inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover
        return False
    return "key_func=account_key" in source


def test_the_costly_sweep_actually_finds_something():
    """Guard the guard: the assertions below pass trivially on an empty set."""
    costly = [r.path for r in _api_routes() if _is_costly(r)]
    assert len(costly) >= 6
    assert "/content/generate" in costly


def test_every_costly_endpoint_carries_a_rate_limit():
    """The regression guard for model spend.

    A new endpoint that calls the writer and forgets this is silent in exactly
    the way the public-surface gap was: it works perfectly, for one caller, for
    as long as it takes to exhaust a quota everybody else is sharing.
    """
    unlimited = {
        (sorted(route.methods)[0], route.path)
        for route in _api_routes()
        if _is_costly(route) and not _is_limited(route)
    }
    assert unlimited == _COSTLY_BUT_UNLIMITED


def test_every_costly_endpoint_is_bucketed_by_account_not_address():
    """A limit keyed by IP on an authenticated endpoint is the wrong limit.

    It punishes a shared office and exempts anyone with a second address, so
    getting the decorator on is only half of it.
    """
    wrongly_keyed = {
        (sorted(route.methods)[0], route.path)
        for route in _api_routes()
        if _is_costly(route) and _is_limited(route) and not _is_account_keyed(route)
    }
    assert wrongly_keyed == _COSTLY_BUT_UNLIMITED


def test_the_costly_endpoints_are_the_ones_expected():
    """Names the six, so deleting a limit shows up as a diff here too."""
    paths = {r.path for r in _api_routes() if _is_costly(r)}
    assert paths == {
        "/content/generate",
        "/content/ideas/{idea_id}/write",
        "/content/{content_id}/repurpose",
        "/content/{content_id}/edit",
        "/content/{content_id}/headlines",
        "/content/{content_id}/links",
        "/projects/{project_id}/scan",
        "/projects/{project_id}/ideas",
        # Found by the sweep above rather than by reading the routers: in
        # `prompt` mode this is `/content/generate` reached by another route,
        # and it was the one costly endpoint missed on the first pass.
        "/templates/{template_id}/use",
        # Not model spend — the other two shared budgets. Mailing the digest
        # goes out through the install's one SMTP identity; the ping and the
        # trigger check are synchronous outbound requests from Pulse's own
        # address, and a GitHub trigger check spends the same single token
        # `/projects/{id}/scan` is limited to protect.
        "/analytics/digest/send",
        "/webhooks/{webhook_id}/ping",
        "/triggers/{trigger_id}/check",
        # The sweep's find, again — `redeliver` fires the same outbound request
        # as the ping and was not on the list written by reading the routers.
        # It is the one of the four a retry loop reaches most naturally, since
        # it is the button beside a delivery that just failed.
        "/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver",
        # Verifying credentials is an outbound request too, and these two were
        # making it unlimited. They are the settings page's Connect and
        # Re-check buttons — see `_COSTLY_CALLS` on why the first is the worse
        # of them. Both were found by reading the routers *for this*, not by
        # the sweep: `adapter.verify` was not on the list of costly calls, so
        # the sweep agreed with itself that there was nothing to find.
        "/settings/connections",
        "/settings/connections/{platform}/verify",
    }


# --------------------------------------------------------------------------- #
# The ideas endpoint: one route, two costs                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on", "t", "y", "TRUE"])
def test_a_refreshing_ideas_call_is_not_exempt(raw):
    """Every spelling FastAPI parses as ``True`` must also spend the budget.

    A limit that exempts ``?refresh=on`` while the endpoint honours it is not a
    limit — it is a documented bypass.
    """
    request = _FakeRequest(query_params={"refresh": raw})
    assert _not_refreshing(request) is False


@pytest.mark.parametrize("raw", ["", "false", "0", "no", "off", "nonsense"])
def test_a_non_refreshing_ideas_call_is_exempt(raw):
    """The projects page reads this on every visit; it must stay free."""
    request = _FakeRequest(query_params={"refresh": raw})
    assert _not_refreshing(request) is True


def test_ideas_without_refresh_is_exempt_when_the_param_is_absent():
    assert _not_refreshing(_FakeRequest()) is True


def test_reading_ideas_is_never_rate_limited(client, auth, project):
    """Past the refreshing budget, and still answering.

    The cheap half of this endpoint is a page load. Limiting it at the model
    call's budget would have turned "open the projects page" into a 429.
    """
    url = f"/api/v1/projects/{project.id}/ideas"
    for _ in range(70):
        assert client.get(url, headers=auth).status_code == 200


# --------------------------------------------------------------------------- #
# Spending a real budget                                                       #
# --------------------------------------------------------------------------- #


@pytest.fixture
def linkless_draft(db, project) -> Content:
    """A draft with no URLs in it, so the check makes no outbound requests."""
    row = Content(
        project_id=project.id,
        title="No links here",
        slug="no-links-here",
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.DRAFT,
        body_markdown="Just prose, with nothing to check.",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_the_link_checker_budget_is_spent_and_then_refused(client, auth, linkless_draft):
    """60/hour. The 61st is refused rather than served."""
    assert settings.rate_limit_link_check.startswith("60/hour")
    url = f"/api/v1/content/{linkless_draft.id}/links"

    for _ in range(60):
        assert client.get(url, headers=auth).status_code == 200

    resp = client.get(url, headers=auth)
    assert resp.status_code == 429
    assert "Too many requests" in resp.json()["detail"]
    assert resp.headers.get("retry-after")


def test_the_digest_send_budget_is_spent_and_then_refused(client, auth):
    """10/hour. The 11th is refused rather than mailed.

    The account has nothing to report, so every one of these answers
    ``sent: false`` without touching SMTP — the limit is charged for asking,
    which is the only way it can stop a loop that would otherwise have to reach
    the mail server to be counted.
    """
    assert settings.rate_limit_digest_send.startswith("10/hour")
    url = "/api/v1/analytics/digest/send"

    for _ in range(10):
        resp = client.post(url, headers=auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["sent"] is False

    refused = client.post(url, headers=auth)
    assert refused.status_code == 429
    assert "Too many requests" in refused.json()["detail"]
    assert refused.headers.get("retry-after")


def test_one_account_hitting_its_limit_does_not_touch_another(
    client, db, auth, linkless_draft
):
    """The property that made this per-account rather than per-address.

    Both callers arrive from the same address — which is what a shared office,
    a VPN or a single reverse proxy looks like — and the second is unaffected.
    """
    url = f"/api/v1/content/{linkless_draft.id}/links"
    for _ in range(60):
        client.get(url, headers=auth)
    assert client.get(url, headers=auth).status_code == 429

    other = User(
        email="second@example.com",
        full_name="Second",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    db.refresh(other)

    # Not a 429: a different account, from the same address. (404 because the
    # draft is not theirs — which is the *other* guard doing its job, and is
    # what this endpoint should say to them.)
    resp = client.get(url, headers=_bearer(other.id))
    assert resp.status_code == 404


def test_the_same_account_cannot_dodge_its_budget_with_a_new_address(
    client, auth, linkless_draft
):
    """The mirror of the test above, and the reason the key is the token.

    Had the bucket stayed the address, spending the budget would cost one
    header to reset.
    """
    url = f"/api/v1/content/{linkless_draft.id}/links"
    for _ in range(60):
        client.get(url, headers={**auth, "X-Forwarded-For": "203.0.113.10"})

    resp = client.get(url, headers={**auth, "X-Forwarded-For": "198.51.100.7"})
    assert resp.status_code == 429


def test_a_limited_endpoint_still_returns_its_body(client, auth, linkless_draft):
    """The `request`/`response` parameters slowapi needs must not leak out.

    Adding them is what the limiter costs, and getting it wrong is a 500 on
    every call rather than only a limited one — see
    ``test_public_endpoint_limits`` for the same trap on the anonymous surface.
    """
    resp = client.get(f"/api/v1/content/{linkless_draft.id}/links", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert "request" not in body and "response" not in body
    assert body["checked"] == 0
    assert body["broken_count"] == 0
    assert body["links"] == []
    assert resp.headers.get("x-ratelimit-limit")


def test_an_anonymous_call_to_a_costly_endpoint_is_still_401(client, linkless_draft):
    """The limit is a second line, not a replacement for the first."""
    assert client.get(f"/api/v1/content/{linkless_draft.id}/links").status_code == 401


# --------------------------------------------------------------------------- #
# Bulk and headline endpoints carry rate limits                                #
# --------------------------------------------------------------------------- #


_BULK_CONTENT_ENDPOINTS = [
    "/api/v1/content/bulk/approve",
    "/api/v1/content/bulk/reject",
    "/api/v1/content/bulk/retry",
]

_BULK_PUBLISH_ENDPOINT = "/api/v1/content/bulk/publish"
_ARCHIVE_OLD_ENDPOINT = "/api/v1/content/bulk/archive-old"


@pytest.mark.parametrize("url", _BULK_CONTENT_ENDPOINTS)
def test_bulk_content_endpoints_carry_rate_limits(url, client, auth):
    resp = client.post(url, headers=auth, json={"content_ids": [999999]})
    assert resp.headers.get("x-ratelimit-limit"), f"{url} missing rate-limit header"


def test_bulk_publish_carries_rate_limit(client, auth):
    resp = client.post(
        _BULK_PUBLISH_ENDPOINT,
        headers=auth,
        json={"content_ids": [999999], "platforms": ["hashnode"]},
    )
    assert resp.headers.get("x-ratelimit-limit"), "bulk publish missing rate-limit header"


def test_archive_old_carries_rate_limit(client, auth):
    resp = client.post(
        _ARCHIVE_OLD_ENDPOINT,
        headers=auth,
        json={"older_than_days": 365},
    )
    assert resp.headers.get("x-ratelimit-limit"), "archive-old missing rate-limit header"


@pytest.mark.parametrize("path", [
    "/content/bulk/approve",
    "/content/bulk/reject",
    "/content/bulk/publish",
    "/content/bulk/retry",
    "/content/bulk/archive-old",
    "/content/{content_id}/headlines/apply",
    "/content/{content_id}/headlines/auto-select",
])
def test_bulk_and_headline_endpoints_are_keyed_by_account(path):
    routes = _api_routes()
    for route in routes:
        if route.path == path:
            assert _is_account_keyed(route), f"{path} not keyed by account"
            return
    pytest.fail(f"route {path} not found")


def test_headline_apply_carries_rate_limit(client, auth, linkless_draft):
    url = f"/api/v1/content/{linkless_draft.id}/headlines/apply"
    resp = client.post(url, headers=auth, json={"title": "Test"})
    assert resp.headers.get("x-ratelimit-limit"), "headline apply missing rate-limit header"


def test_headline_auto_select_carries_rate_limit(client, auth, linkless_draft):
    url = f"/api/v1/content/{linkless_draft.id}/headlines/auto-select"
    resp = client.post(url, headers=auth)
    assert resp.headers.get("x-ratelimit-limit"), "headline auto-select missing rate-limit header"

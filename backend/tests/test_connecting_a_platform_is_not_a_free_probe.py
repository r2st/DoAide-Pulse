"""The settings page's two buttons, and the outbound request behind each.

``PUT /settings/connections`` and ``POST /settings/connections/{platform}/verify``
both call ``adapter.verify``, which is a synchronous HTTP request to the
platform made from Pulse's address while the caller waits. That is the same
thing ``/webhooks/{id}/ping`` and ``/triggers/{id}/check`` do, and those have
carried ``rate_limit_outbound_probe`` since it was added — these two did not,
because the sweep in ``test_account_rate_limits`` looked for a list of costly
calls that ``adapter.verify`` was not on. The sweep agreed with itself.

Connect is the sharper of the two, and worth naming separately from "it makes a
request":

* It verifies the credentials **in the request body**, not the stored ones. So
  unlimited it answers "is this token valid?" for any token at all — a
  credential-stuffing oracle against Dev.to, Hashnode, WordPress and the rest,
  run from Pulse's address and its reputation rather than the caller's.
* :meth:`app.services.publishers.git.GitAdapter._token` falls back to the
  install's shared ``GITHUB_TOKEN`` when the connection carries no token of its
  own. That is one budget for every account here, and 403 for all of them once
  it is spent — the same budget ``rate_limit_repo_scan`` exists to protect,
  reached by a second route.

The sweep in ``test_account_rate_limits`` is what keeps the decorators on. This
file is the behaviour: the budget is really spent, really refused, really per
account, and the endpoints still work up to the point they stop.

The limits are the real configured ones — slowapi reads the limit string at
import time, so a test cannot lower one and has to spend it.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.publication import Platform
from app.security import create_access_token, hash_password
from app.services.publishers.base import CredentialField

CONNECT = "/api/v1/settings/connections"
VERIFY = "/api/v1/settings/connections/devto/verify"

#: One under the hourly budget, so a loop can spend it and leave one call over.
BUDGET = 60


@pytest.fixture
def stub_devto(monkeypatch):
    """A Dev.to adapter that verifies without touching the network.

    Counts the calls, because "the limit refused the request" and "the request
    was made and refused later" look identical from the status code, and only
    one of them protects the platform.
    """
    from app.services import publishers

    calls: list[dict] = []
    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(
        adapter, "verify", lambda creds: (calls.append(dict(creds)), "test-user")[1]
    )
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )
    return calls


def _connect_body(key: str = "fake-key") -> dict:
    return {"platform": "devto", "credentials": {"api_key": key}}


# --------------------------------------------------------------------------- #
# The configured budget                                                        #
# --------------------------------------------------------------------------- #


def test_the_probe_budget_is_the_one_the_other_outbound_endpoints_use():
    """These two are limited *because* they are the ping in another shape.

    Pinned so the connection endpoints cannot drift onto a budget of their own
    without somebody deciding to give them one.
    """
    assert settings.rate_limit_outbound_probe.startswith("60/hour")


# --------------------------------------------------------------------------- #
# Connecting                                                                   #
# --------------------------------------------------------------------------- #


def test_connecting_spends_the_budget_and_is_then_refused(client, auth, stub_devto):
    """The stuffing oracle, closed.

    Sixty attempts is far more than a person connecting a platform makes — the
    real number is one, or two after a typo — and far fewer than a loop needs
    to be worth running.
    """
    for i in range(BUDGET):
        resp = client.put(CONNECT, headers=auth, json=_connect_body(f"key-{i}"))
        assert resp.status_code == 200, resp.text

    refused = client.put(CONNECT, headers=auth, json=_connect_body("key-61"))
    assert refused.status_code == 429
    assert "Too many requests" in refused.json()["detail"]
    assert refused.headers.get("retry-after")

    # And the platform was never asked about the 61st credential. This is the
    # assertion that distinguishes a limit from a late error.
    assert len(stub_devto) == BUDGET
    assert "key-61" not in [c["api_key"] for c in stub_devto]


def test_a_refused_connect_stores_nothing(client, auth, stub_devto, db, user):
    """A 429 must not be a half-write.

    The limiter runs before the endpoint body, so there is no partial state to
    leave — asserted rather than reasoned about, because "the decorator runs
    first" is a property of slowapi's wrapper rather than of this code.
    """
    from app.models.platform_connection import PlatformConnection

    for i in range(BUDGET):
        assert client.put(
            CONNECT, headers=auth, json=_connect_body(f"key-{i}")
        ).status_code == 200

    assert client.put(
        CONNECT, headers=auth, json={"platform": "hashnode", "credentials": {"x": "y"}}
    ).status_code == 429

    rows = db.query(PlatformConnection).filter_by(user_id=user.id).all()
    assert [r.platform for r in rows] == [Platform.DEVTO]


# --------------------------------------------------------------------------- #
# Re-checking                                                                  #
# --------------------------------------------------------------------------- #


def test_verifying_spends_its_budget_and_is_then_refused(
    client, auth, connect, stub_devto
):
    connect(Platform.DEVTO)

    for _ in range(BUDGET):
        resp = client.post(VERIFY, headers=auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "connected"

    refused = client.post(VERIFY, headers=auth)
    assert refused.status_code == 429
    assert len(stub_devto) == BUDGET


def test_a_missing_connection_is_still_a_404_not_a_429(client, auth, stub_devto):
    """The limit does not change what an unconnected platform answers.

    Worth pinning because a limiter that fired first for everyone would turn
    the 404 into a 429 and make "not connected" indistinguishable from "asked
    too often" — and the settings page shows the detail string to the user.
    """
    resp = client.post(VERIFY, headers=auth)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not connected"
    assert stub_devto == []


def test_the_two_buttons_do_not_share_one_bucket(client, auth, connect, stub_devto):
    """slowapi scopes a limit per endpoint, so these are two budgets of sixty.

    Which is the behaviour to want — they are different actions with different
    reasons to be repeated — but it means the account's real ceiling on
    outbound verification is the sum, not the number in the config. Asserted so
    that stays a decision rather than a surprise.
    """
    connect(Platform.DEVTO)

    for _ in range(BUDGET):
        assert client.post(VERIFY, headers=auth).status_code == 200
    assert client.post(VERIFY, headers=auth).status_code == 429

    # Connect is untouched by the verify budget being gone.
    assert client.put(CONNECT, headers=auth, json=_connect_body()).status_code == 200


# --------------------------------------------------------------------------- #
# Whose budget                                                                 #
# --------------------------------------------------------------------------- #


def test_the_budget_belongs_to_the_account_not_the_address(
    client, auth, db, stub_devto
):
    """One account out of budget does not stop another connecting.

    The point of ``key_func=account_key`` on an authenticated endpoint: an
    office behind one NAT is many accounts, and bucketing by address would make
    the first of them to connect a platform spend everybody's turn.
    """
    from app.models.user import User

    for i in range(BUDGET):
        assert client.put(
            CONNECT, headers=auth, json=_connect_body(f"key-{i}")
        ).status_code == 200
    assert client.put(CONNECT, headers=auth, json=_connect_body()).status_code == 429

    other = User(
        email="other@example.com",
        full_name="Other",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(other)
    db.commit()
    db.refresh(other)

    resp = client.put(
        CONNECT,
        headers={"Authorization": f"Bearer {create_access_token(other.id)}"},
        json=_connect_body(),
    )
    assert resp.status_code == 200, resp.text


def test_a_new_address_does_not_buy_a_second_helping(client, auth, stub_devto):
    """The other half: the same account cannot rotate out of its own budget.

    ``account_key`` reads the subject from the verified bearer token, so
    ``X-Forwarded-For`` has nothing to say about which bucket this counts
    against.
    """
    for i in range(BUDGET):
        assert client.put(
            CONNECT, headers=auth, json=_connect_body(f"key-{i}")
        ).status_code == 200

    resp = client.put(
        CONNECT,
        headers={**auth, "X-Forwarded-For": "203.0.113.7"},
        json=_connect_body(),
    )
    assert resp.status_code == 429


def test_an_anonymous_connect_is_still_401(client):
    """The limit is not the authorization check and must not stand in for it."""
    assert client.put(CONNECT, json=_connect_body()).status_code == 401
    assert client.post(VERIFY).status_code == 401


# --------------------------------------------------------------------------- #
# The trap that comes with the limiter                                         #
# --------------------------------------------------------------------------- #


def test_a_limited_connect_still_returns_its_body(client, auth, stub_devto):
    """``headers_enabled=True`` needs somewhere to write the counters.

    An endpoint that returns a model must declare ``response: Response`` or
    slowapi raises on *every* call — see
    ``test_every_limited_endpoint_can_receive_the_rate_limit_headers``. That
    sweep catches the signature; this catches the shape of what comes back, and
    that neither injected parameter leaked into the response model.
    """
    resp = client.put(CONNECT, headers=auth, json=_connect_body())
    assert resp.status_code == 200

    body = resp.json()
    assert body["platform"] == "devto"
    assert body["status"] == "connected"
    assert body["display_name"] == "test-user"
    assert "request" not in body and "response" not in body
    assert resp.headers.get("x-ratelimit-limit")

    # And the credentials still do not come back out.
    assert "credentials" not in body
    assert "fake-key" not in resp.text

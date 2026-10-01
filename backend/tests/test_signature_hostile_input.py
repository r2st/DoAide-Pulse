"""``webhooks.verify`` against input chosen by whoever is calling.

The function has two callers with very different threat models. Outbound, it is
documentation-as-code: Pulse never receives its own webhooks, and the header it
checks is one Pulse just produced. Inbound, it is the *only* thing standing in
front of ``POST /triggers/inbound/{token}`` — an unauthenticated endpoint whose
signature header is attacker-controlled, byte for byte.

So the contract is stronger than "returns True for a good signature": it must
return ``False``, never raise, for every header a caller can send. The case that
broke it is worth naming, because it is invisible from the type signature —
``hmac.compare_digest`` raises ``TypeError`` when either string has a non-ASCII
character in it, so ``v1=café`` turned a 401 into a 500 and handed an
unauthenticated caller a choice of which error the server returns.
"""
from __future__ import annotations

import json

import pytest

from app.models.content import Content
from app.models.mixins import utcnow
from app.models.project import AutopilotMode
from app.models.trigger import Trigger
from app.services import webhooks

API = "/api/v1/triggers"

#: Every one of these is a header a caller can put on the wire. They are all
#: latin-1 representable because that is the encoding HTTP headers arrive in —
#: which is exactly how a non-ASCII character reaches Python in the first place.
HOSTILE_HEADERS = [
    pytest.param("t={now},v1=café", id="non-ascii-signature"),
    pytest.param("t={now},v1=" + "ÿ" * 64, id="high-bytes-right-length"),
    pytest.param("t={now},v1=" + "ff" * 31, id="hex-but-too-short"),
    pytest.param("t={now},v1=" + "ff" * 40, id="hex-but-too-long"),
    pytest.param("t={now},v1=not-hex-at-all-" + "z" * 48, id="right-length-not-hex"),
    pytest.param("t={now},v1=", id="empty-signature"),
    pytest.param("t={now}", id="no-signature-field"),
    pytest.param("v1=" + "ff" * 32, id="no-timestamp"),
    pytest.param("t=,v1=" + "ff" * 32, id="empty-timestamp"),
    pytest.param("t=nine,v1=" + "ff" * 32, id="unparseable-timestamp"),
    pytest.param("", id="empty-header"),
    pytest.param("garbage", id="no-fields-at-all"),
    pytest.param("t={now},v1=" + "ff" * 32 + ",v1=" + "ee" * 32, id="repeated-field"),
]


def _fill(template: str) -> str:
    return template.format(now=int(utcnow().timestamp()))


@pytest.mark.parametrize("template", HOSTILE_HEADERS)
def test_a_hostile_signature_is_false_and_never_an_exception(template):
    assert webhooks.verify("secret", _fill(template), "the body") is False


def test_a_genuine_signature_still_verifies():
    """The guard rejects shapes, not correct signatures."""
    body = '{"event":"content.published"}'
    header = webhooks.sign("secret", int(utcnow().timestamp()), body)

    assert webhooks.verify("secret", header, body) is True


def test_an_uppercase_digest_is_parsed_and_then_rejected_on_its_merits():
    """The shape check accepts either case; the comparison is case-sensitive.

    Worth pinning because the two halves disagree on purpose. Uppercase hex gets
    past the shape check — so a sender who wrote ``.hexdigest().upper()`` reaches
    the comparison rather than being turned away by a regex, which is where a
    signature mismatch belongs — and then fails there, because
    ``hmac.compare_digest`` is a byte comparison and every library in use emits
    lowercase.
    """
    body = "payload"
    timestamp, digest = webhooks.sign("secret", int(utcnow().timestamp()), body).split(
        ",v1="
    )
    shouted = f"{timestamp},v1={digest.upper()}"

    assert webhooks.verify("secret", shouted, body) is False
    assert webhooks.verify("secret", f"{timestamp},v1={digest}", body) is True


# --------------------------------------------------------------------------- #
# Through the one endpoint an outsider can reach                              #
# --------------------------------------------------------------------------- #


def _signed_trigger(client, auth, project, db):
    created = client.post(
        API,
        json={
            "project_id": project.id,
            "kind": "webhook",
            "name": "webhook trigger",
            "config": {"require_signature": True},
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    return created.json(), db.get(Trigger, created.json()["id"]).token


@pytest.mark.parametrize("template", HOSTILE_HEADERS)
def test_the_inbound_endpoint_answers_401_not_500(
    template, client, auth, project, db
):
    _, token = _signed_trigger(client, auth, project, db)

    resp = client.post(
        f"{API}/inbound/{token}",
        content=b'{"title":"hi"}',
        # Bytes, not str: header values travel as latin-1 octets, and the test
        # client refuses to encode a non-ASCII str for us. Sending the octets is
        # what a caller does, and what Starlette hands back to the endpoint is
        # the latin-1 decoding of them.
        headers={
            b"Content-Type": b"application/json",
            webhooks.SIGNATURE_HEADER.encode(): _fill(template).encode("latin-1"),
        },
    )

    assert resp.status_code == 401, resp.text
    assert db.query(Content).count() == 0


def test_a_correctly_signed_post_still_gets_through(client, auth, project, db):
    """The endpoint is still usable — the guard cost nothing a sender pays."""
    project.autopilot_mode = AutopilotMode.DRAFT
    db.commit()
    created, token = _signed_trigger(client, auth, project, db)

    body = json.dumps({"title": "Signed and sealed"})
    resp = client.post(
        f"{API}/inbound/{token}",
        content=body.encode(),
        headers={
            "Content-Type": "application/json",
            webhooks.SIGNATURE_HEADER: webhooks.sign(
                created["secret"], int(utcnow().timestamp()), body
            ),
        },
    )

    assert resp.status_code == 202, resp.text
    assert db.query(Content).count() == 1

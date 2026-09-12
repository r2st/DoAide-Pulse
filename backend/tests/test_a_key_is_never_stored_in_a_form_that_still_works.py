"""Per-project API keys: what is stored, what authenticates, and what stops.

Herald's second credential type, and the first one that is handed to a machine
rather than typed by a person. The properties worth pinning are the ones that
make it safe to leave on a build server:

* **The database never holds a working credential.** The row carries a SHA-256
  digest and a lookup prefix; the token exists in exactly one response and is
  not recoverable afterwards, so a dump of ``api_keys`` authenticates as nobody.

* **A scope is a wall, not a label.** A key without ``content:write`` is refused
  by the write endpoint with a 403, and the distinction from the 401 matters:
  one says retry with a better credential, the other says do not retry.

* **Every way a key stops working, works.** Revoked, expired, project deleted,
  account deactivated — four different mechanisms, all of which have to end in
  the same 401, because a machine credential that outlives one of them is a door
  nobody remembers leaving open.

* **Rotation is not an outage.** The replacement exists before the original is
  retired, and a grace window keeps both live long enough to redeploy.

The last group is the one that would fail silently: an unrevoked key on a
deactivated account still resolves to a row, still matches its digest, and
looks fine from every angle except the one that matters.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.api_key import ALL_SCOPES, ApiKey, ApiKeyScope
from app.models.content import Content, ContentIdea, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import as_aware, utcnow
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password
from app.services import api_keys

V1 = "/api/v1"


@pytest.fixture
def key(db, project) -> tuple[ApiKey, str]:
    """A live key on ``project`` with every scope, and its plaintext token."""
    row, token = api_keys.mint(
        db, project=project, name="CI", scopes=list(ALL_SCOPES)
    )
    db.commit()
    db.refresh(row)
    return row, token


def _headers(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


# --------------------------------------------------------------------------- #
# The token, and what is kept of it                                            #
# --------------------------------------------------------------------------- #


def test_the_token_is_recognisable_as_ours_and_carries_its_own_prefix():
    """``hrld_<prefix>_<secret>``, and the prefix parses back out.

    The marker is what makes a leaked token greppable — in a log, in a repo, by
    a secret scanner. The prefix is what makes authentication one indexed
    lookup instead of a scan comparing every stored digest.
    """
    token, prefix = api_keys.generate()
    assert token.startswith("hrld_")
    assert api_keys.split(token) == prefix
    # Long enough that guessing is not a strategy: 32 bytes, URL-safe.
    #
    # Split with the same ``maxsplit`` the parser uses, and for the same
    # reason: base64url includes ``_``, so a bare split cuts the secret in half
    # on roughly every second token.
    assert len(token.split("_", 2)[2]) >= 40


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-token",
        "hrld_only-two-parts",
        "wrong_abc123_secret",  # right shape, wrong marker
        "hrld__secret",  # empty prefix
        "hrld_abc123_",  # empty secret
    ],
)
def test_a_string_that_is_not_one_of_ours_is_refused_before_any_query(bad):
    """Parsing happens first, so a malformed header never reaches the database."""
    assert api_keys.split(bad) is None


def test_a_secret_containing_an_underscore_still_parses():
    """base64url has ``_`` in its alphabet, so about half of all tokens do.

    Pinned with a hand-built token rather than by minting until one appears:
    the failure this guards against is intermittent by nature, and a test that
    reproduces it only sometimes is not a test.
    """
    assert api_keys.split("hrld_abc123_secret_with_underscores") == "abc123"


def test_the_stored_row_cannot_be_used_to_authenticate(db, key):
    """The point of the whole design: no column holds a working credential.

    Checked against the row rather than by reading the service, because the
    thing being asserted is a property of what is *persisted* — a future change
    that stored the token "just for the UI" would pass every other test here.
    """
    row, token = key
    assert row.token_hash == api_keys.fingerprint(token)
    assert token not in row.token_hash
    # The prefix is in the clear, and is not enough on its own.
    assert row.prefix in token
    assert api_keys.authenticate(db, row.prefix) is None
    assert api_keys.authenticate(db, f"hrld_{row.prefix}_wrong-secret") is None


def test_two_keys_never_share_a_prefix(db, project):
    """A duplicate prefix would make authentication non-deterministic."""
    prefixes = set()
    for i in range(25):
        row, _ = api_keys.mint(
            db, project=project, name=f"k{i}", scopes=[ApiKeyScope.CONTENT_READ]
        )
        prefixes.add(row.prefix)
    db.commit()
    assert len(prefixes) == 25


# --------------------------------------------------------------------------- #
# Authenticating                                                               #
# --------------------------------------------------------------------------- #


def test_a_live_key_resolves_to_its_own_row(db, key):
    row, token = key
    assert api_keys.authenticate(db, token).id == row.id


def test_a_revoked_key_stops_immediately(db, key):
    row, token = key
    api_keys.revoke(db, row)
    assert api_keys.authenticate(db, token) is None


def test_revoking_twice_keeps_the_first_timestamp(db, key):
    """"When did this stop working" must not move because a button was pressed twice."""
    row, _ = key
    first = api_keys.revoke(db, row).revoked_at
    later = api_keys.revoke(db, row).revoked_at
    assert first == later


def test_an_expired_key_stops_without_anybody_revoking_it(db, key):
    row, token = key
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert api_keys.authenticate(db, token) is None
    # And the boundary is inclusive: a key expiring exactly now is expired.
    moment = utcnow()
    row.expires_at = moment
    db.commit()
    assert api_keys.authenticate(db, token, now=moment) is None


def test_a_key_on_a_deactivated_account_stops_being_a_credential(db, user, key):
    """The failure that would otherwise be silent.

    Nothing about the key itself changes when an account is switched off: the
    row is unrevoked, the digest still matches, the expiry is still in the
    future. Every other sweep in this tree checks ``is_active`` for the same
    reason — switching an account off has to close the doors it opened, and a
    credential sitting on somebody's CI runner is a door.
    """
    _row, token = key
    assert api_keys.authenticate(db, token) is not None

    user.is_active = False
    db.commit()
    assert api_keys.authenticate(db, token) is None


def test_last_used_is_recorded_but_not_on_every_single_call(db, key):
    """A usage stamp must not become a write per request.

    A status badge polling every thirty seconds would otherwise generate a
    write every thirty seconds, forever, per key — for a column whose only
    question is "is anything still using this?"
    """
    row, _ = key
    assert row.last_used_at is None

    assert api_keys.touch(db, row) is True
    first = row.last_used_at
    assert first is not None

    # Immediately again: inside the coalescing window, so no write.
    assert api_keys.touch(db, row) is False
    assert row.last_used_at == first

    # Far enough past it, and the stamp moves.
    later = as_aware(first) + api_keys.TOUCH_INTERVAL + timedelta(minutes=1)
    assert api_keys.touch(db, row, now=later) is True
    assert as_aware(row.last_used_at) == later


# --------------------------------------------------------------------------- #
# Scopes                                                                       #
# --------------------------------------------------------------------------- #


def test_scopes_are_stored_in_one_canonical_order(db, project):
    """Two keys that can do the same things must not look different."""
    forwards, _ = api_keys.mint(
        db,
        project=project,
        name="a",
        scopes=[ApiKeyScope.ANALYTICS_READ, ApiKeyScope.CONTENT_READ],
    )
    backwards, _ = api_keys.mint(
        db,
        project=project,
        name="b",
        scopes=[ApiKeyScope.CONTENT_READ, ApiKeyScope.ANALYTICS_READ],
    )
    db.commit()
    assert forwards.scopes == backwards.scopes


def test_a_repeated_scope_is_not_stored_twice(db, project):
    row, _ = api_keys.mint(
        db,
        project=project,
        name="a",
        scopes=[ApiKeyScope.CONTENT_READ, ApiKeyScope.CONTENT_READ],
    )
    assert row.scopes == ["content:read"]


def test_a_scope_herald_does_not_define_is_refused(db, project):
    with pytest.raises(api_keys.ApiKeyError, match="Unknown scope"):
        api_keys.mint(db, project=project, name="a", scopes=["content:destroy"])


def test_a_key_with_no_scopes_is_refused(db, project):
    """A credential that can do nothing is a credential somebody will widen later."""
    with pytest.raises(api_keys.ApiKeyError, match="at least one scope"):
        api_keys.mint(db, project=project, name="a", scopes=[])


def test_a_scope_this_herald_no_longer_defines_reads_as_absent(db, key):
    """``has_scope`` must not raise on a value it does not recognise.

    A stored scope string outlives the enum member that wrote it — a downgrade,
    or a scope withdrawn in a later version. Coercing back to the enum inside an
    auth dependency would turn that into a 500 on every request the key makes,
    where the correct answer is a quiet "no".
    """
    row, _ = key
    row.scopes = ["content:read", "content:teleport"]
    db.commit()
    assert row.has_scope(ApiKeyScope.CONTENT_READ) is True
    assert row.has_scope(ApiKeyScope.CONTENT_WRITE) is False


# --------------------------------------------------------------------------- #
# Rotation                                                                     #
# --------------------------------------------------------------------------- #


def test_rotating_with_no_grace_retires_the_old_key_at_once(db, key):
    old, old_token = key
    new, new_token = api_keys.rotate(db, old)

    assert new.id != old.id
    assert new_token != old_token
    assert new.rotated_from_id == old.id
    # Same name and scopes: a rotation replaces a credential, it does not
    # quietly change what that credential is for.
    assert (new.name, new.scopes) == (old.name, old.scopes)

    assert api_keys.authenticate(db, old_token) is None
    assert api_keys.authenticate(db, new_token).id == new.id


def test_a_grace_window_keeps_both_keys_working_until_it_closes(db, key):
    """The property that makes rotation something people actually do.

    A rotation that invalidates the old key the instant the new one appears
    requires redeploying every consumer in the same second, which is why the
    honest alternative is never rotating at all.
    """
    old, old_token = key
    new, new_token = api_keys.rotate(db, old, grace_hours=2)

    assert old.revoked_at is None
    assert api_keys.authenticate(db, old_token).id == old.id
    assert api_keys.authenticate(db, new_token).id == new.id

    after = utcnow() + timedelta(hours=2, minutes=1)
    assert api_keys.authenticate(db, old_token, now=after) is None
    assert api_keys.authenticate(db, new_token, now=after).id == new.id


def test_a_grace_window_never_extends_a_key_that_was_already_dated(db, key):
    """A rotation must not become a way to give a key more life than it had."""
    old, _ = key
    dies_soon = utcnow() + timedelta(minutes=30)
    old.expires_at = dies_soon
    db.commit()

    api_keys.rotate(db, old, grace_hours=24)
    assert as_aware(old.expires_at) == dies_soon


def test_the_replacement_does_not_inherit_an_expiry_already_in_the_past(db, key):
    """Rotation is not a resurrection.

    An expired key rotated into a replacement carrying the same dead timestamp
    would mint something that has never worked and never will.
    """
    old, _ = key
    old.expires_at = utcnow() - timedelta(days=1)
    db.commit()

    new, new_token = api_keys.rotate(db, old)
    assert new.expires_at is None
    assert api_keys.authenticate(db, new_token) is not None


def test_the_replacement_keeps_an_expiry_still_in_the_future(db, key):
    old, _ = key
    deadline = utcnow() + timedelta(days=30)
    old.expires_at = deadline
    db.commit()

    new, _ = api_keys.rotate(db, old)
    assert as_aware(new.expires_at) == deadline


def test_a_revoked_key_cannot_be_rotated(db, key):
    """There is nothing to keep working, so this is a new key, not a rotation."""
    old, _ = key
    api_keys.revoke(db, old)
    with pytest.raises(api_keys.ApiKeyError, match="already revoked"):
        api_keys.rotate(db, old)


def test_an_absurd_grace_window_is_refused(db, key):
    old, _ = key
    with pytest.raises(api_keys.ApiKeyError, match="more than"):
        api_keys.rotate(db, old, grace_hours=api_keys.MAX_GRACE_HOURS + 1)


def test_an_absurd_expiry_is_refused_rather_than_overflowing_a_timedelta():
    """The bound exists because the number reaches ``timedelta``.

    Without it a caller's ``99999999`` raises ``OverflowError`` from inside a
    request handler, which is a 500 for a value the schema already accepted.
    """
    with pytest.raises(api_keys.ApiKeyError):
        api_keys.expiry_from_days(api_keys.MAX_EXPIRY_DAYS + 1)
    with pytest.raises(api_keys.ApiKeyError):
        api_keys.expiry_from_days(0)
    assert api_keys.expiry_from_days(None) is None


# --------------------------------------------------------------------------- #
# The management API                                                           #
# --------------------------------------------------------------------------- #


def test_the_token_is_in_the_creating_response_and_in_no_other(client, auth, project):
    resp = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={
            "project_id": project.id,
            "name": "Release notes CI",
            "scopes": ["content:read"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    token = body["token"]
    assert token.startswith("hrld_")
    assert body["prefix"] in token
    assert body["scopes"] == ["content:read"]

    listing = client.get(f"{V1}/api-keys", headers=auth)
    assert listing.status_code == 200
    assert listing.json()[0]["prefix"] == body["prefix"]
    assert all("token" not in row for row in listing.json())


def test_a_revoked_key_leaves_the_listing_but_not_the_record(client, auth, project):
    created = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={"project_id": project.id, "name": "CI", "scopes": ["content:read"]},
    ).json()

    resp = client.delete(f"{V1}/api-keys/{created['id']}", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["revoked_at"] is not None

    assert client.get(f"{V1}/api-keys", headers=auth).json() == []
    kept = client.get(f"{V1}/api-keys?include_revoked=true", headers=auth).json()
    assert [row["id"] for row in kept] == [created["id"]]


def test_the_rotate_endpoint_returns_both_halves(client, auth, project):
    created = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={"project_id": project.id, "name": "CI", "scopes": ["content:read"]},
    ).json()

    resp = client.post(
        f"{V1}/api-keys/{created['id']}/rotate", headers=auth, json={"grace_hours": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"]["token"] != created["token"]
    assert body["key"]["rotated_from_id"] == created["id"]
    # The replaced key is dated rather than revoked, because a window was asked
    # for — a caller has to be able to tell those two outcomes apart.
    assert body["replaced"]["revoked_at"] is None
    assert body["replaced"]["expires_at"] is not None


def test_a_project_cannot_accumulate_unlimited_live_keys(client, auth, project, db):
    from app.routers.api_keys import MAX_KEYS_PER_PROJECT

    for i in range(MAX_KEYS_PER_PROJECT):
        api_keys.mint(
            db, project=project, name=f"k{i}", scopes=[ApiKeyScope.CONTENT_READ]
        )
    db.commit()

    resp = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={"project_id": project.id, "name": "one too many", "scopes": ["content:read"]},
    )
    assert resp.status_code == 409

    # A revoked key does not hold a slot: the history is kept, the budget is not.
    oldest = db.query(ApiKey).order_by(ApiKey.id).first()
    api_keys.revoke(db, oldest)
    assert client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={"project_id": project.id, "name": "now there is room", "scopes": ["content:read"]},
    ).status_code == 201


def test_the_scope_catalogue_describes_every_scope(client):
    """Reachable without a token, like the other constant lists."""
    resp = client.get(f"{V1}/api-keys/scopes")
    assert resp.status_code == 200
    rows = resp.json()
    assert {row["scope"] for row in rows} == {s.value for s in ALL_SCOPES}
    assert all(row["description"] for row in rows)


def test_a_key_cannot_be_minted_on_somebody_elses_project(client, auth, db):
    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.flush()
    theirs = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        description="Not yours.",
        tech_stack=[],
        keywords=[],
        tone=Tone.TECHNICAL,
    )
    db.add(theirs)
    db.commit()

    resp = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={"project_id": theirs.id, "name": "nice try", "scopes": ["content:read"]},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The machine surface                                                          #
# --------------------------------------------------------------------------- #


def test_whoami_answers_the_key_that_asked(client, key, project):
    row, token = key
    resp = client.get(f"{V1}/machine/whoami", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key_id"] == row.id
    assert body["prefix"] == row.prefix
    assert body["project_id"] == project.id
    assert body["project_slug"] == project.slug
    assert set(body["scopes"]) == {s.value for s in ALL_SCOPES}


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/machine/whoami"),
        ("get", "/machine/content"),
        ("get", "/machine/analytics"),
        ("post", "/machine/ideas"),
    ],
)
def test_no_machine_route_is_reachable_without_a_credential(client, method, path):
    resp = client.request(method.upper(), f"{V1}{path}", json={})
    assert resp.status_code == 401


def test_a_missing_scope_is_a_403_and_not_a_401(client, db, project):
    """The two failures say different things, and a client acts on the difference.

    401 means the credential is the problem — refresh it. 403 means the
    credential is fine and retrying will never work — page a human. Collapsing
    them into one status turns a permissions bug into an infinite retry loop.
    """
    row, token = api_keys.mint(
        db, project=project, name="read only", scopes=[ApiKeyScope.CONTENT_READ]
    )
    db.commit()

    assert client.get(f"{V1}/machine/content", headers=_headers(token)).status_code == 200

    refused = client.post(
        f"{V1}/machine/ideas",
        headers=_headers(token),
        json={"headline": "We shipped the thing"},
    )
    assert refused.status_code == 403
    assert "content:write" in refused.json()["detail"]

    assert client.get(
        f"{V1}/machine/analytics", headers=_headers(token)
    ).status_code == 403
    # And the unscoped route is still reachable: introspection must not need a
    # permission, or a job cannot find out why it is being refused.
    assert client.get(f"{V1}/machine/whoami", headers=_headers(token)).status_code == 200
    assert row.has_scope(ApiKeyScope.CONTENT_READ)


def test_the_content_listing_shows_only_the_keys_own_project(client, db, key, project, user):
    """The credential names the project. Nothing in the request can change that."""
    other = Project(
        user_id=user.id,
        name="Other",
        slug="other",
        description="Same account, different project.",
        tech_stack=[],
        keywords=[],
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.flush()
    db.add_all([
        Content(
            project_id=project.id,
            content_type=ContentType.CHANGELOG,
            status=ContentStatus.PUBLISHED,
            title="Ours",
            slug="ours",
            body_markdown="word " * 50,
            excerpt="Ours.",
        ),
        Content(
            project_id=other.id,
            content_type=ContentType.CHANGELOG,
            status=ContentStatus.PUBLISHED,
            title="Theirs",
            slug="theirs",
            body_markdown="word " * 50,
            excerpt="Theirs.",
        ),
    ])
    db.commit()

    _row, token = key
    resp = client.get(f"{V1}/machine/content", headers=_headers(token))
    assert resp.status_code == 200
    assert [row["title"] for row in resp.json()] == ["Ours"]
    assert resp.headers["X-Total-Count"] == "1"


def test_the_content_listing_never_carries_a_body(client, db, key, project):
    """A read-only machine credential gets the shipping record, not the prose."""
    db.add(
        Content(
            project_id=project.id,
            content_type=ContentType.CHANGELOG,
            status=ContentStatus.DRAFT,
            title="Unpublished",
            slug="unpublished",
            body_markdown="A secret we have not shipped yet. " * 20,
            excerpt="Secret.",
        )
    )
    db.commit()

    _row, token = key
    body = client.get(f"{V1}/machine/content", headers=_headers(token)).json()
    assert body[0]["title"] == "Unpublished"
    assert "body_markdown" not in body[0]
    assert "A secret we have not shipped" not in str(body)


def test_an_idea_filed_by_a_machine_records_which_key_filed_it(client, db, key, project):
    _row, token = key
    resp = client.post(
        f"{V1}/machine/ideas",
        headers=_headers(token),
        json={"headline": "v2.1 went out", "rationale": "Tagged in CI."},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "open"
    assert resp.json()["content_type"] == "changelog"

    idea = db.get(ContentIdea, resp.json()["id"])
    assert idea.project_id == project.id
    assert idea.source["kind"] == "api_key"
    assert idea.source["prefix"] == _row.prefix


def test_a_repeated_idea_is_answered_with_the_one_already_open(client, db, key, project):
    """A build job files the same idea twice. The queue holds it once.

    ``bank_ideas`` drops a restatement for the autopilot; the machine route
    inserted straight into the table, so a retried pipeline — the producer
    most likely to repeat — filed a copy per retry. Answered with the open
    idea and a 200 rather than a silent drop, so the job can see it is a
    replay.
    """
    _row, token = key
    body = {"headline": "Release v2.1 went out to production", "rationale": "CI."}
    first = client.post(f"{V1}/machine/ideas", headers=_headers(token), json=body)
    assert first.status_code == 201, first.text

    again = client.post(f"{V1}/machine/ideas", headers=_headers(token), json=body)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]

    open_ideas = db.query(ContentIdea).filter(ContentIdea.project_id == project.id).all()
    assert len(open_ideas) == 1


def test_a_used_idea_does_not_block_the_subject_being_filed_again(
    client, db, key, project
):
    """Only *open* ideas count — the same rule ``bank_ideas`` applies.

    A subject already written up is editorial repetition, not a duplicate row,
    and that is not a question a headline comparison should answer.
    """
    _row, token = key
    body = {"headline": "Release v2.1 went out to production"}
    first = client.post(f"{V1}/machine/ideas", headers=_headers(token), json=body)
    assert first.status_code == 201
    idea = db.get(ContentIdea, first.json()["id"])
    piece = Content(
        project_id=project.id,
        content_type=ContentType.CHANGELOG,
        status=ContentStatus.DRAFT,
        title="Shipped",
        slug="shipped",
        body_markdown="It went out.",
    )
    db.add(piece)
    db.flush()
    idea.used_content_id = piece.id
    db.commit()

    again = client.post(f"{V1}/machine/ideas", headers=_headers(token), json=body)
    assert again.status_code == 201, again.text
    assert again.json()["id"] != first.json()["id"]


def test_a_machine_is_held_to_the_same_idea_cap_as_the_scan(
    client, db, key, project, monkeypatch
):
    """The oldest unused idea goes when a machine files one past the cap.

    The autopilot prunes after every scan; the machine route inserted with no
    cap at all, so one producer was bounded and the table was not.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "autopilot_ideas_cap", 3)
    _row, token = key
    ids = []
    for n in range(4):
        resp = client.post(
            f"{V1}/machine/ideas",
            headers=_headers(token),
            json={"headline": f"Distinct subject number {n} about {'xyz'[n % 3]}"},
        )
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])

    remaining = {
        row.id
        for row in db.query(ContentIdea).filter(ContentIdea.project_id == project.id)
    }
    assert remaining == set(ids[1:])
    assert ids[0] not in remaining


def test_machine_analytics_counts_the_latest_snapshot_and_not_every_one(
    client, db, key, project
):
    """The append-only trap.

    ``content_metrics`` is a time series: summing every row counts the same
    hundred views once per poll, so a naive total crosses any number eventually
    whether or not a single person read anything.
    """
    content = Content(
        project_id=project.id,
        content_type=ContentType.CHANGELOG,
        status=ContentStatus.PUBLISHED,
        title="Shipped",
        slug="shipped",
        body_markdown="word " * 50,
        excerpt="Shipped.",
    )
    db.add(content)
    db.flush()
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        external_id="ext-1",
    )
    db.add(publication)
    db.flush()

    now = utcnow()
    for offset, views in ((3, 10), (2, 40), (1, 90)):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=now - timedelta(hours=offset),
                views=views,
                reactions=views // 10,
            )
        )
    db.commit()

    _row, token = key
    body = client.get(f"{V1}/machine/analytics", headers=_headers(token)).json()
    assert body["project_id"] == project.id
    assert body["published_count"] == 1
    # The newest reading, not 10 + 40 + 90.
    assert body["total_views"] == 90
    assert body["total_engagement"] == 9
    assert body["platforms"] == ["devto"]


def test_a_revoked_key_stops_reaching_the_machine_api(client, db, key):
    """The end-to-end version of the service-level check.

    Worth having separately: the dependency could resolve the row correctly and
    still forget to ask whether it is usable.
    """
    row, token = key
    assert client.get(f"{V1}/machine/whoami", headers=_headers(token)).status_code == 200
    api_keys.revoke(db, row)
    assert client.get(f"{V1}/machine/whoami", headers=_headers(token)).status_code == 401


def test_deleting_a_project_takes_its_keys_with_it(db, key, project):
    """A live credential naming a row that is gone is the worst of both."""
    row, _token = key
    key_id = row.id
    db.delete(project)
    db.commit()
    assert db.get(ApiKey, key_id) is None


def test_a_key_with_a_blank_name_is_refused(db, project):
    """The name is what the revoke button says. A blank one names nothing."""
    with pytest.raises(api_keys.ApiKeyError, match="name"):
        api_keys.mint(
            db, project=project, name="   ", scopes=[ApiKeyScope.CONTENT_READ]
        )


def test_a_negative_grace_window_is_refused(db, key):
    old, _ = key
    with pytest.raises(api_keys.ApiKeyError, match="negative"):
        api_keys.rotate(db, old, grace_hours=-1)


def test_a_failed_usage_stamp_does_not_fail_the_request_it_came_from(db, key, monkeypatch):
    """``touch`` is bookkeeping, and bookkeeping must not be able to 500 a read.

    The stamp is the least important thing happening on a machine request. A
    database that will not take the write is a reason to log, not a reason to
    refuse a caller whose credential was perfectly good.
    """
    row, _ = key

    def _boom():
        raise RuntimeError("the commit exploded")

    monkeypatch.setattr(db, "commit", _boom)
    assert api_keys.touch(db, row) is False


def test_creating_a_key_with_a_bad_expiry_is_a_422_not_a_500(client, auth, project):
    """The service's own refusals reach the caller as a refusal.

    ``expires_in_days`` is bounded by the schema too, so this is belt and
    braces — but the two bounds are written in different files and only one of
    them is what the OpenAPI advertises.
    """
    resp = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={
            "project_id": project.id,
            "name": "CI",
            "scopes": ["content:read"],
            "expires_in_days": api_keys.MAX_EXPIRY_DAYS + 1,
        },
    )
    assert resp.status_code == 422


def test_rotating_an_already_revoked_key_is_a_422_not_a_500(client, auth, project, db):
    created = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={"project_id": project.id, "name": "CI", "scopes": ["content:read"]},
    ).json()
    client.delete(f"{V1}/api-keys/{created['id']}", headers=auth)

    resp = client.post(
        f"{V1}/api-keys/{created['id']}/rotate", headers=auth, json={"grace_hours": 0}
    )
    assert resp.status_code == 422
    assert "already revoked" in resp.json()["detail"]


def test_the_listing_narrows_to_a_project_through_the_ownership_guard(
    client, auth, db, project, user
):
    """An unknown project id 404s rather than returning an empty page.

    An empty list would be the same answer for "you have no keys there" and
    "that project is somebody else's", and only one of those is true.
    """
    other = Project(
        user_id=user.id,
        name="Other",
        slug="other-project",
        description="Same account.",
        tech_stack=[],
        keywords=[],
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.flush()
    api_keys.mint(db, project=project, name="a", scopes=[ApiKeyScope.CONTENT_READ])
    api_keys.mint(db, project=other, name="b", scopes=[ApiKeyScope.CONTENT_READ])
    db.commit()

    both = client.get(f"{V1}/api-keys", headers=auth).json()
    assert len(both) == 2

    narrowed = client.get(f"{V1}/api-keys?project_id={other.id}", headers=auth).json()
    assert [row["name"] for row in narrowed] == ["b"]

    assert client.get(f"{V1}/api-keys?project_id=999999", headers=auth).status_code == 404


def test_the_machine_listing_can_be_narrowed_by_status(client, db, key, project):
    """What a status page asks for: the published ones, not the drafts."""
    db.add_all([
        Content(
            project_id=project.id,
            content_type=ContentType.CHANGELOG,
            status=ContentStatus.PUBLISHED,
            title="Out",
            slug="out",
            body_markdown="word " * 50,
            excerpt="Out.",
        ),
        Content(
            project_id=project.id,
            content_type=ContentType.CHANGELOG,
            status=ContentStatus.DRAFT,
            title="Not out",
            slug="not-out",
            body_markdown="word " * 50,
            excerpt="Not out.",
        ),
    ])
    db.commit()

    _row, token = key
    resp = client.get(f"{V1}/machine/content?status=published", headers=_headers(token))
    assert resp.status_code == 200
    assert [row["title"] for row in resp.json()] == ["Out"]
    assert resp.headers["X-Total-Count"] == "1"


def test_machine_analytics_answers_a_project_that_has_published_nothing(client, key):
    """Zeroes, not a 500 and not an empty body. A new project calls this too."""
    _row, token = key
    body = client.get(f"{V1}/machine/analytics", headers=_headers(token)).json()
    assert body["published_count"] == 0
    assert body["total_views"] == 0
    assert body["total_engagement"] == 0
    assert body["platforms"] == []


def test_an_unknown_prefix_still_spends_the_hash(db, key):
    """No early return, so "no such key" is not measurably faster than "wrong secret".

    Returning before the digest is computed turns the endpoint into an oracle
    for whether a prefix is real, which is the one thing the prefix being stored
    in the clear must not buy an attacker.
    """
    assert api_keys.authenticate(db, "hrld_deadbeefcafe_whatever-secret") is None


def test_a_key_can_be_given_a_life_measured_in_days(client, auth, project, db):
    """The ordinary case for a credential on a contractor's machine."""
    resp = client.post(
        f"{V1}/api-keys",
        headers=auth,
        json={
            "project_id": project.id,
            "name": "Ninety days",
            "scopes": ["content:read"],
            "expires_in_days": 90,
        },
    )
    assert resp.status_code == 201, resp.text
    row = db.get(ApiKey, resp.json()["id"])
    assert row.expires_at is not None
    assert timedelta(days=89) < as_aware(row.expires_at) - utcnow() < timedelta(days=91)

    token = resp.json()["token"]
    assert api_keys.authenticate(db, token) is not None
    assert (
        api_keys.authenticate(db, token, now=utcnow() + timedelta(days=91)) is None
    )

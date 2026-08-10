"""The router arms nothing had walked: deletes, races, and empty results.

Grouped by the thing that makes each one interesting rather than by endpoint:

* **Deletes** — the only operations here a user cannot undo. Both what they
  remove and, more importantly, what they leave behind.
* **Races** — the register 409 that the pre-check cannot see, and the login
  safety net behind ``verify_password``.
* **Nothing found** — an account with no projects, a curve for a publication
  that is not the first one. Empty is a legal answer and must not be a 500.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.trigger import Trigger, TriggerKind

API = "/api/v1"


# --------------------------------------------------------------------------- #
# Nothing found                                                               #
# --------------------------------------------------------------------------- #


def test_an_account_with_no_projects_gets_an_empty_list_not_a_batch_query(
    client, auth, sql_log
):
    """``_batch_counts`` must short-circuit rather than send ``IN ()``.

    A brand-new account hits this on its very first page load, which is a poor
    moment for the count query to be malformed.
    """
    sql_log.clear()

    resp = client.get(f"{API}/projects", headers=auth)

    assert resp.status_code == 200
    assert resp.json() == []
    assert not [s for s in sql_log if "count(" in s.lower()]


# --------------------------------------------------------------------------- #
# Projects: patch and delete                                                  #
# --------------------------------------------------------------------------- #


def test_patching_the_autopilot_platforms_stores_their_values(client, auth, project):
    """The list arrives as enum members and is stored as a JSON array of values.

    ``.value`` on the way in matters because the column is plain JSON: a member
    repr in there is a row the reader cannot parse back.
    """
    resp = client.patch(
        f"{API}/projects/{project.id}",
        headers=auth,
        json={"autopilot_platforms": ["devto", "mastodon"]},
    )

    assert resp.status_code == 200
    assert resp.json()["autopilot_platforms"] == ["devto", "mastodon"]


def test_clearing_the_autopilot_platforms_is_allowed(client, auth, project):
    resp = client.patch(
        f"{API}/projects/{project.id}", headers=auth, json={"autopilot_platforms": []}
    )

    assert resp.status_code == 200
    assert resp.json()["autopilot_platforms"] == []


def test_a_patch_that_names_the_same_name_does_not_touch_the_slug(client, auth, project):
    before = project.slug

    resp = client.patch(
        f"{API}/projects/{project.id}", headers=auth, json={"name": project.name}
    )

    assert resp.status_code == 200
    assert resp.json()["slug"] == before


def test_deleting_a_project_takes_its_content_with_it(client, auth, db, project):
    """The cascade is the point: content orphaned from its project is invisible
    in every list and still counts against the database."""
    db.add(
        Content(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.DRAFT,
            title="A draft",
            slug="a-draft",
            body_markdown="Body.",
        )
    )
    db.commit()

    resp = client.delete(f"{API}/projects/{project.id}", headers=auth)

    assert resp.status_code == 204
    assert client.get(f"{API}/projects/{project.id}", headers=auth).status_code == 404
    assert db.query(Content).filter_by(project_id=project.id).count() == 0


def test_deleting_a_project_twice_is_a_404_the_second_time(client, auth, project):
    assert client.delete(f"{API}/projects/{project.id}", headers=auth).status_code == 204
    assert client.delete(f"{API}/projects/{project.id}", headers=auth).status_code == 404


# --------------------------------------------------------------------------- #
# Auth: the races                                                             #
# --------------------------------------------------------------------------- #


def test_a_duplicate_signup_lost_at_the_database_is_a_409_not_a_500(
    client, db, monkeypatch
):
    """Two signups for the same address, both past the pre-check.

    The SELECT and the INSERT are separate statements. The unique constraint
    catches the loser, and the loser must read "pick another address" rather
    than an internal error — and must not leak the constraint's own message.
    """
    real_commit = db.commit

    def flaky_commit():
        db.rollback()
        db.commit = real_commit
        raise IntegrityError("INSERT INTO users", {}, Exception("unique"))

    monkeypatch.setattr(db, "commit", flaky_commit)

    resp = client.post(
        f"{API}/auth/register",
        json={
            "email": "racer@example.com",
            "password": "Str0ng-Passw0rd!",
            "full_name": "Racer",
        },
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Email already registered"
    assert "INSERT" not in resp.text
    assert "unique" not in resp.text


def test_the_pre_check_answers_the_ordinary_duplicate(client, user):
    resp = client.post(
        f"{API}/auth/register",
        json={"email": user.email, "password": "Str0ng-Passw0rd!", "full_name": "Again"},
    )

    assert resp.status_code == 409


def test_a_password_check_that_passes_for_a_missing_user_still_refuses(
    client, monkeypatch
):
    """The safety net behind the constant-time comparison.

    ``verify_password`` cannot return True against the dummy hash, so this is
    unreachable in production — but it is the last thing standing between a bug
    in the hashing library and a token issued for an account that does not
    exist, and an untested safety net is a comment.
    """
    monkeypatch.setattr("app.routers.auth.verify_password", lambda plain, hashed: True)

    resp = client.post(
        f"{API}/auth/login",
        data={"username": "nobody@example.com", "password": "anything"},
    )

    assert resp.status_code == 401
    assert "access_token" not in resp.json()


# --------------------------------------------------------------------------- #
# Triggers                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def trigger(db, project):
    row = Trigger(
        project_id=project.id,
        kind=TriggerKind.RSS,
        name="A feed",
        config={"url": "https://example.com/feed.xml"},
        is_active=True,
        consecutive_failures=3,
        last_error="the feed 500ed twice",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("patch", ""), ("delete", ""), ("post", "/rotate-secret"), ("get", "/events")],
)
def test_a_trigger_that_does_not_exist_is_a_404(client, auth, method, suffix):
    call = getattr(client, method)
    kwargs = {"json": {}} if method == "patch" else {}

    assert call(f"{API}/triggers/999999{suffix}", headers=auth, **kwargs).status_code == 404


def test_switching_a_trigger_off_leaves_the_failure_count_where_it_was(
    client, auth, db, trigger
):
    """Only re-enabling clears the counter — that is the user saying it is fixed.

    Clearing it on the way *out* would mean a trigger disabled by repeated
    failures comes back looking healthy, and the next failure starts counting
    from zero all over again.
    """
    resp = client.patch(
        f"{API}/triggers/{trigger.id}", headers=auth, json={"is_active": False}
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    db.refresh(trigger)
    assert trigger.consecutive_failures == 3
    assert trigger.last_error == "the feed 500ed twice"


def test_switching_a_trigger_back_on_clears_the_failures(client, auth, db, trigger):
    trigger.is_active = False
    db.commit()

    resp = client.patch(
        f"{API}/triggers/{trigger.id}", headers=auth, json={"is_active": True}
    )

    assert resp.status_code == 200
    db.refresh(trigger)
    assert trigger.consecutive_failures == 0
    assert trigger.last_error is None


# --------------------------------------------------------------------------- #
# Analytics                                                                   #
# --------------------------------------------------------------------------- #


def test_the_velocity_curve_finds_a_publication_that_is_not_the_first(
    client, auth, db, project
):
    """The endpoint scans the user's curves for a match — the loop has to keep
    going past the ones that are not it."""
    wanted = None
    for index in range(3):
        content = Content(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.PUBLISHED,
            title=f"Piece {index}",
            slug=f"piece-{index}",
            body_markdown="Body.",
        )
        db.add(content)
        db.flush()
        publication = Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=_a_while_ago(),
        )
        db.add(publication)
        db.flush()
        wanted = publication.id
    db.commit()

    resp = client.get(f"{API}/analytics/velocity/{wanted}", headers=auth)

    assert resp.status_code == 200
    assert resp.json()["publication_id"] == wanted


def test_a_velocity_curve_for_something_that_is_not_ours_is_a_404(client, auth, db):
    other = Project(
        user_id=999,
        name="Theirs",
        slug="theirs",
        description="Not ours.",
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.commit()

    assert client.get(f"{API}/analytics/velocity/424242", headers=auth).status_code == 404


def _a_while_ago():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(days=3)

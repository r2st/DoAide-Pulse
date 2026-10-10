"""Opening one growth chart must not read every post the account ever made.

``GET /analytics/velocity/{publication_id}`` returns a single curve. It used to
get there by building *every* curve on the account and then keeping the one
whose id matched. ``velocity.curves`` reads the whole append-only snapshot
series for each publication it builds, so the cost of looking at one chart was
the entire metric history of the account — the same unbounded read
:mod:`tests.test_velocity_window_budget` bounds for the calendar, except reached
by clicking a row rather than by loading a page.

The filter now sits in the query. What is asserted here is the property, not a
number: the rows read to answer for one publication do not change when the
account grows a second publication with a long history behind it. An absolute
count would need editing every time an unrelated snapshot joined a fixture; this
would still fail the moment the filter moves back out of the query.

The ownership half is asserted too, because moving a filter into a ``WHERE``
clause is exactly the kind of change that can quietly drop the ``user_id``
predicate sitting next to it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password
from app.services import velocity

#: Long enough that reading this publication's tail is unmistakable in the
#: count, short enough that inserting the rows stays cheap.
_DAYS_LIVE = 200


def _published(db, project, *, slug: str, days_ago: int, snapshots: int) -> Publication:
    """One published post with *snapshots* daily readings behind it."""
    content = Content(
        project_id=project.id,
        title=slug.replace("-", " ").title(),
        slug=slug,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        body_markdown="Body.",
    )
    db.add(content)
    db.flush()
    published_at = utcnow() - timedelta(days=days_ago)
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=published_at,
        external_url=f"https://dev.to/x/{slug}",
    )
    db.add(publication)
    db.flush()
    for day in range(1, snapshots + 1):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=published_at + timedelta(days=day),
                views=day * 10,
                reactions=1,
            )
        )
    db.commit()
    db.refresh(publication)
    return publication


@pytest.fixture
def subject(db, project) -> Publication:
    """The post whose chart is being opened. Three readings, and that is all."""
    return _published(db, project, slug="the-one-being-looked-at", days_ago=4, snapshots=3)


@pytest.fixture
def metrics_read(db):
    """Every ``ContentMetric`` row that reaches the session while this is active.

    Through ``loaded_as_persistent`` rather than the identity map afterwards,
    for the reason given in :mod:`tests.test_velocity_window_budget`: velocity
    reduces each snapshot to a ``Point`` and keeps no reference, so the rows are
    collectable before any later assertion could count them.
    """
    seen: list[int] = []

    def _record(session, instance):
        if isinstance(instance, ContentMetric):
            seen.append(instance.id)

    event.listen(db, "loaded_as_persistent", _record)
    try:
        yield seen
    finally:
        event.remove(db, "loaded_as_persistent", _record)


def _forget_everything(db, publication: Publication) -> int:
    """Empty the identity map, returning the id that survives it.

    A row already in the identity map is handed back without a fresh load, so
    without this a snapshot a fixture happened to touch would go uncounted.
    Expunging detaches the publication too, and a detached instance cannot
    answer ``.id`` — hence reading it out first and passing the plain integer on.
    """
    publication_id = publication.id
    db.expunge_all()
    return publication_id


# ---- The bound ------------------------------------------------------------ #


def test_one_curve_is_not_paid_for_with_the_whole_accounts_history(
    client, auth, db, project, subject, metrics_read
):
    """The regression itself. One chart, one publication's rows.

    The account also holds a post that has been live for most of a year. Nothing
    about the request mentions it, and nothing about the request should read it.
    """
    _published(db, project, slug="an-old-post", days_ago=_DAYS_LIVE, snapshots=_DAYS_LIVE)
    assert db.query(ContentMetric).count() > _DAYS_LIVE
    subject_id = _forget_everything(db, subject)

    resp = client.get(f"/api/v1/analytics/velocity/{subject_id}", headers=auth)

    assert resp.status_code == 200, resp.text
    assert len(metrics_read) == 3


def test_the_rows_read_do_not_grow_when_another_post_does(
    client, auth, db, project, subject, metrics_read
):
    """The property, stated without an absolute number.

    Asserted as "the same before and after", so it survives a fixture gaining a
    snapshot and still fails if the filter leaves the query.
    """
    subject_id = _forget_everything(db, subject)
    client.get(f"/api/v1/analytics/velocity/{subject_id}", headers=auth)
    alone = len(metrics_read)

    project = db.merge(project)
    _published(
        db, project, slug="a-busy-neighbour", days_ago=_DAYS_LIVE, snapshots=_DAYS_LIVE
    )
    metrics_read.clear()
    db.expunge_all()
    client.get(f"/api/v1/analytics/velocity/{subject_id}", headers=auth)

    assert len(metrics_read) == alone
    assert alone == 3


# ---- The answer ----------------------------------------------------------- #


def test_the_filtered_curve_is_the_curve_the_unfiltered_read_reports(
    client, auth, db, user, project, subject
):
    """A cheaper read that gives a different answer is not an optimisation.

    Compared against the unfiltered call with a second post in the account, so
    a filter that picked the wrong row would show up as well as one that
    truncated the right one.
    """
    _published(db, project, slug="another-post", days_ago=30, snapshots=30)

    detail = client.get(f"/api/v1/analytics/velocity/{subject.id}", headers=auth).json()
    (whole,) = [
        c for c in velocity.curves(db, user.id) if c.publication_id == subject.id
    ]

    expected = whole.as_dict()
    # ``published_at`` is a datetime on one side and its ISO form on the other;
    # every other key, including the two configuration-named window counts, has
    # to match value for value.
    assert {k: v for k, v in detail.items() if k not in {"published_at", "points"}} == {
        k: v for k, v in expected.items() if k != "published_at"
    }
    assert len(detail["points"]) == len(whole.points)


def test_the_detail_still_carries_its_points(client, auth, subject):
    served = client.get(f"/api/v1/analytics/velocity/{subject.id}", headers=auth).json()

    assert [p["views"] for p in served["points"]] == [10, 20, 30]


# ---- The ownership filter next to it -------------------------------------- #


def test_another_users_publication_is_a_404_not_a_curve(client, auth, db, subject):
    """Moving a filter into the ``WHERE`` clause is how the one beside it gets
    dropped. The id here is real and published — only the owner is wrong."""
    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    theirs = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        description="Not yours.",
        tone=Tone.TECHNICAL,
    )
    db.add(theirs)
    db.commit()
    their_publication = _published(
        db, theirs, slug="not-yours", days_ago=5, snapshots=3
    )

    resp = client.get(
        f"/api/v1/analytics/velocity/{their_publication.id}", headers=auth
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Publication not found."


def test_a_publication_that_does_not_exist_is_the_same_404(client, auth, subject):
    """Indistinguishable from "not yours", which is the point — see
    :func:`app.deps.owned_project`."""
    missing = client.get("/api/v1/analytics/velocity/999999", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Publication not found"

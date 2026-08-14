"""The weekly digest must not get slower every week it runs.

``content_metrics`` is append-only and nothing prunes it, so a user's snapshot
history grows for as long as they keep publishing. ``_gains`` used to read all
of it — every reading of every publication, ever — to answer a question about
fourteen days, discarding all but the last row before the window. The cost of
Monday's email therefore scaled with how long the account had existed.

Two assertions, and both are needed:

* **The numbers did not change.** A bounded read that gets a different answer is
  not an optimisation. The baseline outside the window is load-bearing: without
  it the earlier window's gain is measured from zero and "up 400% on the week
  before" becomes fiction.
* **The read is bounded.** Measured by counting the ``ContentMetric`` rows that
  end up in the session, because that is the thing that regressed — the query
  *count* never changed and would not have caught it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.services import digest


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def publication(db, project):
    content = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="A long-running piece",
        slug="a-long-running-piece",
        body_markdown="Body.",
        status=ContentStatus.PUBLISHED,
    )
    db.add(content)
    db.flush()
    row = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=_now() - timedelta(days=400),
        external_url="https://dev.to/x/a-long-running-piece",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _history(db, publication, *, days_back: int) -> None:
    """One daily snapshot per day, ending just before the two-week comparison.

    Views climb by 10 a day, so the last one before the window — whichever day
    that is — is the only reading of the whole run that changes the answer.
    """
    for day in range(days_back, 14, -1):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=_now() - timedelta(days=day),
                views=1000 - day * 10,
                clicks=0,
                reactions=0,
            )
        )
    db.commit()


def _the_two_windows(db, publication) -> None:
    """A reading in the comparison week and one in the reported week."""
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=_now() - timedelta(days=10),
            views=1000,
            clicks=5,
            reactions=2,
        )
    )
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=_now() - timedelta(days=2),
            views=1400,
            clicks=9,
            reactions=6,
        )
    )
    db.commit()


def _metrics_loaded(db) -> int:
    return sum(1 for obj in db.identity_map.values() if isinstance(obj, ContentMetric))


def _forget_everything(db, user):
    """Empty the identity map so the counts below are what ``build`` loaded.

    The user has to be fetched again afterwards: expunging detaches it, and a
    detached instance cannot answer ``user.id``.
    """
    user_id = user.id
    db.expunge_all()
    return db.get(User, user_id)


@pytest.mark.parametrize("days_back", [20, 400])
def test_the_numbers_do_not_depend_on_how_much_history_there_is(
    db, user, publication, days_back
):
    """Three weeks of history and thirteen months must agree.

    Both runs have a reading on day 15, so both have the same baseline for the
    comparison window — everything before that is noise the answer must ignore.
    """
    _history(db, publication, days_back=days_back)
    _the_two_windows(db, publication)
    user = _forget_everything(db, user)

    built = digest.build(db, user)

    # 1400 - 1000 this week, against 1000 - 850 (the day-15 reading) last week.
    assert built.movement.views == 400
    assert built.movement.previous_views == 150
    assert built.movement.clicks == 4
    assert built.movement.change == round((400 - 150) / 150, 3)


def test_thirteen_months_of_snapshots_are_not_read_to_answer_for_fourteen_days(
    db, user, publication
):
    """The bound: a handful of rows, not one per day since the post went up."""
    _history(db, publication, days_back=400)
    _the_two_windows(db, publication)
    user = _forget_everything(db, user)

    digest.build(db, user)

    # Two window readings plus one baseline. Generous room for the alerts pass,
    # and still nowhere near the 386 rows sitting in the table.
    assert db.query(ContentMetric).count() > 380
    assert _metrics_loaded(db) <= 10


def test_a_publication_first_seen_inside_the_window_counts_its_whole_reading(
    db, user, publication
):
    """No baseline is not a zero baseline — it means everything is new gain."""
    _the_two_windows(db, publication)
    user = _forget_everything(db, user)

    built = digest.build(db, user)

    assert built.movement.views == 400
    # Nothing before the comparison window, so its own gain is the whole 1000.
    assert built.movement.previous_views == 1000


def _body_reads(statements: list[str]) -> list[str]:
    """Statements that carry ``content.body_markdown`` back to Python."""
    return [s for s in statements if "content.body_markdown" in s]


def _syndicate(db, publication, *platforms) -> None:
    """The same piece, live on more platforms, inside the reported window."""
    for index, platform in enumerate(platforms):
        db.add(
            Publication(
                content_id=publication.content_id,
                platform=platform,
                status=PublicationStatus.PUBLISHED,
                published_at=_now() - timedelta(days=3),
                external_url=f"https://{platform.value}.example/x-{index}",
            )
        )
    db.commit()


def test_the_digest_reads_one_body_per_piece_not_per_platform(
    db, user, publication, sql_log
):
    """Reading time is a property of the piece, not of where it went out.

    ``published_rows`` has a row per publication, so selecting the ``Content``
    entity there carried a whole article once per platform it was syndicated to
    — three copies of the same body to compute the same reading time three
    times.

    Neither a query count nor a count of body-reading statements can see this:
    it was one statement before and it is one statement now, with the same three
    rows. What changed is the *width* of those rows, so the assertion is that no
    statement joining ``publications`` carries a body at all — the bodies come
    from a separate read keyed by content id, which is one row per piece by
    construction.
    """
    publication.published_at = _now() - timedelta(days=3)
    db.commit()
    _syndicate(db, publication, Platform.HASHNODE, Platform.MEDIUM)
    user = _forget_everything(db, user)

    sql_log.clear()
    digest.build(db, user)

    multiplied = [s for s in _body_reads(sql_log) if "publications" in s]
    assert multiplied == [], "\n".join(s[:300] for s in multiplied)
    # And the piece's body is still read — once — for the reading time.
    assert len(_body_reads(sql_log)) == 1, "\n".join(
        s[:300] for s in _body_reads(sql_log)
    )


def test_a_syndicated_piece_is_one_digest_entry_listing_every_platform(
    db, user, publication
):
    """The narrowing must not change what the email says.

    One entry per piece, every platform it went out on, the first external URL
    seen, and a reading time computed from the body — the same four things the
    entity-shaped read produced.
    """
    publication.published_at = _now() - timedelta(days=3)
    db.commit()
    _syndicate(db, publication, Platform.HASHNODE, Platform.MEDIUM)
    user = _forget_everything(db, user)

    built = digest.build(db, user)

    assert len(built.published) == 1
    entry = built.published[0]
    assert entry["title"] == "A long-running piece"
    assert sorted(entry["platforms"]) == ["devto", "hashnode", "medium"]
    assert entry["url"] is not None
    # "Body." — under a minute of reading, floored at one.
    assert entry["read_minutes"] == 1

"""Attribution by gain, picking a winner, and swapping it in.

Two things this file exists to pin down:

* Platform counters are **cumulative**. Summing the snapshots that land in a
  window counts the same views once per poll, which hands every contest to
  whichever headline was live longest. Attribution is by delta.
* Nothing swaps a live headline on a maybe. Evidence, margin, and a fair run
  for the incumbent all have to pass first.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import headlines
from app.tasks.headline_tasks import auto_select_headlines


def _now() -> datetime:
    return datetime.now(UTC)


def _no_close(session):
    """Hand a task the test's session without letting it close the shared one."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def __setattr__(self, name, value):
            # Writes go to the real session too. Without this the proxy quietly
            # absorbs them onto itself, so a task that configures its own session
            # — ``expire_on_commit``, say — appears to have done so while the
            # session under assertion carries on unchanged.
            setattr(session, name, value)

        def close(self):
            # The fixture owns this session's lifetime, not the task.
            pass

    return lambda: NoCloseProxy()


@pytest.fixture
def piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Original headline",
        slug="original-headline",
        status=ContentStatus.PUBLISHED,
        # Old enough that both windows below clear the minimum airtime.
        created_at=_now() - timedelta(days=20),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def publication(db, piece) -> Publication:
    row = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _snapshot(db, publication, *, at, views, engagement=0):
    """One cumulative reading, as a platform would report it."""
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=at,
            views=views,
            reactions=engagement,
        )
    )
    db.commit()


def _swap(db, content, title, *, at):
    headlines.apply_headline(content, title, now=at)
    db.commit()
    db.refresh(content)


def _two_headline_contest(db, content, publication, *, first_gain, second_gain):
    """One swap ten days in, with a given view gain either side of it.

    Both windows run ten days, so views-per-day is directly comparable and the
    only difference is the gain each headline earned.
    """
    start = content.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=first_gain // 2)
    _snapshot(db, publication, at=start + timedelta(days=9), views=first_gain)

    _swap(db, content, "Challenger headline", at=start + timedelta(days=10))

    _snapshot(
        db, publication, at=start + timedelta(days=11), views=first_gain + second_gain // 2
    )
    _snapshot(
        db, publication, at=start + timedelta(days=19), views=first_gain + second_gain
    )


# --------------------------------------------------------------------------- #
# Attribution by gain                                                         #
# --------------------------------------------------------------------------- #


def test_a_cumulative_counter_is_credited_as_growth_not_as_a_total(
    db, piece, publication
):
    """Three polls of 100/300/600 lifetime views is 600 gained, not 1000."""
    start = piece.created_at
    for offset, views in ((1, 100), (2, 300), (3, 600)):
        _snapshot(db, publication, at=start + timedelta(days=offset), views=views)

    window = headlines.performance(piece, db)[0]
    assert window.views == 600
    assert window.snapshots == 3


def test_a_counter_going_backwards_never_credits_a_negative(db, piece, publication):
    """A purge or a rescrape is not a headline making views disappear."""
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=500)
    _snapshot(db, publication, at=start + timedelta(days=2), views=200)

    window = headlines.performance(piece, db)[0]
    assert window.views == 500


def test_a_counter_that_dips_and_recovers_is_not_credited_twice(db, piece, publication):
    """The dip must not become the new baseline.

    Clamping the *gain* at zero but storing the dip means the recovery reads as
    fresh growth on the next poll: 500 → 200 → 500 is 500 views total, not 800.
    """
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=500)
    _snapshot(db, publication, at=start + timedelta(days=2), views=200)
    _snapshot(db, publication, at=start + timedelta(days=3), views=500)

    window = headlines.performance(piece, db)[0]
    assert window.views == 500


def test_a_snapshot_reporting_no_engagement_does_not_reset_the_baseline(
    db, piece, publication
):
    """An empty reading is "nothing to see", not "the counter went to zero".

    Bluesky's ``getPosts`` returns no post at all while one is unavailable, and
    the adapter records that honestly as a snapshot with every field ``None`` —
    whose ``engagement`` property coalesces to 0. Storing that as the baseline
    handed the piece's whole lifetime engagement to the next poll.
    """
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=10, engagement=40)
    # The blank reading: no views, no reactions, nothing.
    db.add(
        ContentMetric(
            publication_id=publication.id, captured_at=start + timedelta(days=2)
        )
    )
    db.commit()
    _snapshot(db, publication, at=start + timedelta(days=3), views=10, engagement=40)

    window = headlines.performance(piece, db)[0]
    assert window.engagement == 40
    assert window.views == 10


def test_a_dip_does_not_leak_gain_into_the_next_headline(db, piece, publication):
    """The double count is worst across a swap: it credits the wrong headline."""
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=1000)
    # The reading that goes missing, moments before the swap.
    _snapshot(db, publication, at=start + timedelta(days=9), views=0)

    _swap(db, piece, "Challenger headline", at=start + timedelta(days=10))

    _snapshot(db, publication, at=start + timedelta(days=11), views=1000)

    first, current = headlines.performance(piece, db)
    assert first.views == 1000
    # Not 1000 again. The challenger earned nothing while it was up.
    assert current.views == 0


def test_two_platforms_are_differenced_separately(db, piece, publication):
    """A delta only means something against the same platform's last reading.

    Both destinations are ones a headline change can reach — WordPress rather
    than Medium, which has no update API and is therefore excluded from
    attribution entirely. See ``test_a_headline_swap_reaches_the_reader``.
    """
    other = Publication(
        content_id=piece.id,
        platform=Platform.WORDPRESS,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(other)
    db.commit()

    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=100)
    _snapshot(db, other, at=start + timedelta(days=1, hours=1), views=40)
    _snapshot(db, publication, at=start + timedelta(days=2), views=150)
    _snapshot(db, other, at=start + timedelta(days=2, hours=1), views=90)

    window = headlines.performance(piece, db)[0]
    # 150 from Dev.to and 90 from WordPress, not a running total of both.
    assert window.views == 240


def test_views_per_day_makes_unequal_windows_comparable(db, piece, publication):
    """A fortnight of quiet beats an afternoon of noise on totals alone."""
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=100)
    _snapshot(db, publication, at=_now() - timedelta(days=2), views=1000)
    _swap(db, piece, "Second headline", at=_now() - timedelta(days=1))
    _snapshot(db, publication, at=_now() - timedelta(hours=2), views=1300)

    first, second = headlines.performance(piece, db)
    assert first.hours_live() == pytest.approx(19 * 24, abs=2)
    assert second.hours_live() == pytest.approx(24, abs=1)
    # The second window earned less in total, over far less time.
    assert second.views < first.views
    assert second.views_per_day() > first.views_per_day()


def test_a_window_with_no_readings_has_no_rate(db, piece):
    window = headlines.performance(piece, db)[0]
    assert window.snapshots == 0
    assert window.views_per_day() is None


# --------------------------------------------------------------------------- #
# Picking a winner                                                            #
# --------------------------------------------------------------------------- #


def test_no_data_is_not_a_verdict(db, piece):
    verdict = headlines.pick_winner(headlines.performance(piece, db))
    assert verdict.confident is False
    assert "Not enough data" in verdict.reason


def test_the_incumbent_winning_is_a_verdict_but_not_a_swap(db, piece, publication):
    _two_headline_contest(db, piece, publication, first_gain=200, second_gain=2000)

    verdict = headlines.pick_winner(headlines.performance(piece, db))
    assert verdict.title == "Challenger headline"
    assert verdict.confident is False
    assert "already live" in verdict.reason


def test_a_clear_win_for_a_past_headline_is_confident(db, piece, publication):
    _two_headline_contest(db, piece, publication, first_gain=4000, second_gain=200)

    verdict = headlines.pick_winner(headlines.performance(piece, db))
    assert verdict.title == "Original headline"
    assert verdict.confident is True
    assert verdict.score > verdict.current_score
    assert [w.title for w in verdict.ranked][0] == "Original headline"


def test_a_narrow_lead_is_called_noise(db, piece, publication):
    """Without a margin, headlines flap forever on measurement wobble."""
    _two_headline_contest(db, piece, publication, first_gain=1050, second_gain=1000)

    verdict = headlines.pick_winner(headlines.performance(piece, db))
    assert verdict.title == "Original headline"
    assert verdict.confident is False
    assert "noise" in verdict.reason


def test_the_margin_is_the_thing_that_decides_it(db, piece, publication):
    """Just over the bar flips the same comparison to confident."""
    over = int(1000 * (1 + settings.headline_winner_margin) * 1.1)
    _two_headline_contest(db, piece, publication, first_gain=over, second_gain=1000)

    verdict = headlines.pick_winner(headlines.performance(piece, db))
    assert verdict.confident is True


def test_a_headline_live_five_minutes_is_not_judged(db, piece, publication):
    """And so a swap cannot immediately trigger another one."""
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=500)
    _snapshot(db, publication, at=start + timedelta(days=9), views=5000)
    _swap(db, piece, "Just swapped in", at=_now() - timedelta(minutes=5))

    verdict = headlines.pick_winner(headlines.performance(piece, db))
    assert verdict.confident is False
    assert "fair run" in verdict.reason


def test_one_reading_is_not_evidence(db, piece, publication):
    start = piece.created_at
    _snapshot(db, publication, at=start + timedelta(days=1), views=900)
    _swap(db, piece, "Second headline", at=start + timedelta(days=10))
    _snapshot(db, publication, at=start + timedelta(days=15), views=1000)

    windows = headlines.performance(piece, db)
    assert windows[0].snapshots < settings.headline_min_snapshots
    verdict = headlines.pick_winner(windows)
    assert verdict.confident is False


# --------------------------------------------------------------------------- #
# Applying it                                                                 #
# --------------------------------------------------------------------------- #


def test_auto_select_swaps_the_winner_back_in(db, piece, publication):
    _two_headline_contest(db, piece, publication, first_gain=4000, second_gain=200)

    verdict, applied = headlines.auto_select(piece, db)
    db.commit()
    db.refresh(piece)

    assert applied is True
    assert verdict.confident is True
    assert piece.title == "Original headline"
    # The swap is recorded like any other, so the window it closes is measurable.
    assert piece.headline_history[-1]["title"] == "Challenger headline"
    assert len(piece.headline_history) == 2


def test_auto_select_leaves_a_winning_incumbent_alone(db, piece, publication):
    _two_headline_contest(db, piece, publication, first_gain=200, second_gain=2000)
    before = piece.title

    _, applied = headlines.auto_select(piece, db)

    assert applied is False
    assert piece.title == before


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


def test_winner_endpoint_reports_without_changing_anything(
    client, auth, db, piece, publication
):
    _two_headline_contest(db, piece, publication, first_gain=4000, second_gain=200)

    resp = client.get(f"/api/v1/content/{piece.id}/headlines/winner", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["title"] == "Original headline"
    assert body["confident"] is True
    assert body["applied"] is False
    assert body["ranked"]
    db.refresh(piece)
    assert piece.title == "Challenger headline"


def test_auto_select_endpoint_applies_it(client, auth, db, piece, publication):
    _two_headline_contest(db, piece, publication, first_gain=4000, second_gain=200)

    resp = client.post(
        f"/api/v1/content/{piece.id}/headlines/auto-select", headers=auth
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True

    db.refresh(piece)
    assert piece.title == "Original headline"


def test_auto_select_endpoint_is_not_an_error_when_nothing_wins(
    client, auth, db, piece
):
    """"The one live is best" is a successful answer to "pick the winner"."""
    resp = client.post(
        f"/api/v1/content/{piece.id}/headlines/auto-select", headers=auth
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert resp.json()["reason"]


def test_performance_endpoint_exposes_the_comparable_rate(
    client, auth, db, piece, publication
):
    _two_headline_contest(db, piece, publication, first_gain=1000, second_gain=500)

    resp = client.get(f"/api/v1/content/{piece.id}/headlines/performance", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body) == 2
    assert all("views_per_day" in row for row in body)
    assert body[0]["hours_live"] > 0


def test_headline_endpoints_are_scoped_to_the_owner(client, auth, db):
    from app.models.project import Project, Tone
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="nope@example.com", hashed_password=hash_password("hunter2hunter2")
    )
    db.add(stranger)
    db.flush()
    other = Project(
        user_id=stranger.id,
        name="Theirs",
        slug="theirs",
        description="Not yours.",
        tone=Tone.TECHNICAL,
    )
    db.add(other)
    db.flush()
    theirs = Content(
        project_id=other.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Theirs",
        slug="their-post",
    )
    db.add(theirs)
    db.commit()

    assert (
        client.get(
            f"/api/v1/content/{theirs.id}/headlines/winner", headers=auth
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/content/{theirs.id}/headlines/auto-select", headers=auth
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# The sweep                                                                   #
# --------------------------------------------------------------------------- #


def test_the_sweep_skips_projects_that_did_not_opt_in(
    db, project, piece, publication, monkeypatch
):
    _two_headline_contest(db, piece, publication, first_gain=4000, second_gain=200)
    assert project.auto_headline_winner is False

    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))
    result = auto_select_headlines()

    assert result == {"considered": 0, "swapped": 0}
    db.refresh(piece)
    assert piece.title == "Challenger headline"


def test_the_sweep_swaps_where_the_project_opted_in(
    db, project, piece, publication, monkeypatch
):
    project.auto_headline_winner = True
    db.commit()
    _two_headline_contest(db, piece, publication, first_gain=4000, second_gain=200)

    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))
    result = auto_select_headlines()

    assert result == {"considered": 1, "swapped": 1}
    db.refresh(piece)
    assert piece.title == "Original headline"


def test_the_sweep_ignores_a_piece_that_never_had_a_swap(
    db, project, piece, publication, monkeypatch
):
    """No history means no contest — and no query cost or log line."""
    project.auto_headline_winner = True
    db.commit()
    _snapshot(db, publication, at=piece.created_at + timedelta(days=1), views=500)

    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))
    assert auto_select_headlines() == {"considered": 0, "swapped": 0}


def test_the_sweep_does_not_re_read_every_candidate_after_the_first_swap(
    db, project, monkeypatch, sql_log
):
    """A swap commits, and a commit expired every candidate still to come.

    ``_candidates`` loads the whole ``Content`` for every published piece on
    every project that opted in — unbounded by design. Applying one headline
    commits, which expired all of them, and SQLAlchemy re-applies the original
    loader options when it refreshes an expired instance: each remaining
    candidate re-read its own article body, one row at a time, for a sweep that
    only ever writes a title.

    Every other test of this sweep has a single candidate, which is exactly the
    size at which the bug cannot appear.
    """
    project.auto_headline_winner = True
    db.commit()

    pieces = []
    for index in range(3):
        content = Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            title="Original headline",
            slug=f"contested-{index}",
            body_markdown="word " * 2000,
            status=ContentStatus.PUBLISHED,
            created_at=_now() - timedelta(days=20),
        )
        db.add(content)
        db.commit()
        db.refresh(content)
        publication = Publication(
            content_id=content.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
        )
        db.add(publication)
        db.commit()
        db.refresh(publication)
        _two_headline_contest(
            db, content, publication, first_gain=4000, second_gain=200
        )
        pieces.append(content)

    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))
    db.expire_all()
    sql_log.clear()

    result = auto_select_headlines()

    assert result == {"considered": 3, "swapped": 3}
    # Read before asserting on behaviour: ``db.refresh`` below reads a body per
    # piece itself, and would be counted as the sweep's.
    bodies = [s for s in sql_log if "body_markdown" in s]
    assert len(bodies) == 1, (
        f"{len(bodies)} statements carried an article body for 3 candidates:\n"
        + "\n".join(s[:200] for s in bodies)
    )

    # And all three still swap back to the winning headline.
    for content in pieces:
        db.refresh(content)
        assert content.title == "Original headline"

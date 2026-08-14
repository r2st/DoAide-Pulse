"""The dashboard's cost has to follow the account, not the deployment.

Three unbounded reads sat behind ``GET /analytics/overview``:

* ``_latest_metric_subquery`` grouped the whole of ``content_metrics`` — every
  account's snapshots, in an append-only table nothing prunes — and only then
  threw the other accounts away in the outer join. Six times per page load.
* ``by_project`` loaded every piece the account had ever written to count them
  by status in Python.
* ``read_time`` and ``engagement_trend`` selected whole ``Content`` entities to
  reach ``read_minutes``, a property derived from ``body_markdown`` alone —
  and ``Content.publications`` is ``lazy="selectin"``, so each of those reads
  dragged in every publication attached to every piece as well. Narrowing them
  to the one column left the last of it: the *body* still crossed the wire to
  produce one integer per row, which is why the count is stored on the row now
  and these two read ``word_count`` instead.

The correctness half matters as much as the cost: restricting a MAX(id) GROUP
BY to one user's publications cannot change which id is greatest within a
group, and the tests below pin the numbers as well as the queries.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.project import Project
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.security import hash_password
from app.services import analytics_service


def _publish(db, project, *, title, body, views, reads=None, days_ago=5):
    content = Content(
        project_id=project.id,
        title=title,
        slug=title.lower().replace(" ", "-"),
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        body_markdown=body,
    )
    db.add(content)
    db.commit()
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow() - timedelta(days=days_ago),
        external_id=f"ext-{content.id}",
    )
    db.add(publication)
    db.commit()
    db.add(
        ContentMetric(
            publication_id=publication.id,
            views=views,
            reads=reads,
            captured_at=utcnow() - timedelta(days=days_ago - 1),
        )
    )
    db.commit()
    return content, publication


@pytest.fixture
def stranger(db):
    """Another account with published content and snapshots of its own.

    The whole point of the fixture: everything below has to be unchanged by its
    existence, in the numbers *and* in what the queries touch.
    """
    other = User(
        email="stranger@example.com",
        hashed_password=hash_password("not-used-here"),
        is_active=True,
    )
    db.add(other)
    db.commit()
    project = Project(
        user_id=other.id, name="Theirs", slug="theirs", description="not yours"
    )
    db.add(project)
    db.commit()
    for index in range(3):
        _publish(
            db,
            project,
            title=f"Stranger piece {index}",
            body="word " * 500,
            views=9_999,
            reads=8_888,
        )
    return other


@pytest.fixture
def mine(db, project):
    """Two published pieces on the requesting account, of different lengths."""
    short = _publish(
        db, project, title="Short one", body="word " * 100, views=10, reads=4
    )
    long = _publish(
        db, project, title="Long one", body="word " * 3000, views=90, reads=40
    )
    return short, long


# --------------------------------------------------------------------------- #
# The latest-metric subquery                                                   #
# --------------------------------------------------------------------------- #


def _latest_metric_subqueries(statements: list[str]) -> list[str]:
    """Each ``MAX(id) GROUP BY publication_id`` subquery, sliced out on its own.

    Sliced rather than matched against the whole statement: every caller's own
    WHERE clause names ``projects.user_id`` as well, so looking for it anywhere
    in the text passes whether or not the *subquery* is the part that is
    scoped — which is exactly the bug being pinned.

    ``_pre_window_readings`` builds a second MAX(id) grouping that is bounded a
    different way, by an explicit list of publication ids. It is excluded here
    rather than asserted on, because a subquery handed its keys directly does
    not need the ownership join.
    """
    found: list[str] = []
    for statement in statements:
        start = statement.find("SELECT max(content_metrics.id)")
        while start != -1:
            end = statement.find("GROUP BY content_metrics.publication_id", start)
            chunk = statement[start:end]
            if "content_metrics.publication_id IN" not in chunk:
                found.append(chunk)
            start = statement.find("SELECT max(content_metrics.id)", end)
    return found


def test_the_latest_metric_subquery_is_scoped_to_the_user(
    db, user, mine, stranger, sql_log
):
    """It must not group over metrics belonging to other accounts.

    Asserted on the emitted SQL rather than on the result, because the result
    was always right — the outer join discarded the surplus. What was wrong was
    how much had to be grouped to get there.
    """
    sql_log.clear()
    analytics_service.totals(db, user.id)

    grouping = _latest_metric_subqueries(sql_log)
    assert grouping, "expected the latest-metric subquery to run"
    for subquery in grouping:
        assert "projects.user_id" in subquery, (
            "the MAX(id) GROUP BY runs over every account's snapshots:\n" + subquery
        )


def test_every_caller_of_the_subquery_gets_the_scoped_one(db, user, mine, stranger, sql_log):
    """``overview`` builds it six times; one unscoped rebuild undoes the fix."""
    sql_log.clear()
    analytics_service.overview(db, user.id)

    grouping = _latest_metric_subqueries(sql_log)
    assert len(grouping) >= 4, f"expected several, got {len(grouping)}"
    unscoped = [s for s in grouping if "projects.user_id" not in s]
    assert not unscoped, "\n".join(s[:200] for s in unscoped)


def test_scoping_the_subquery_did_not_change_a_single_number(db, user, mine, stranger):
    """Restricting the input cannot change the max within a group."""
    totals = analytics_service.totals(db, user.id)
    assert totals.views == 100
    assert totals.reads == 44
    assert totals.published_count == 2
    assert totals.publication_count == 2


def test_the_overview_reports_nothing_belonging_to_another_account(
    client, auth, mine, stranger
):
    resp = client.get("/api/v1/analytics/overview", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["totals"]["views"] == 100
    assert {row["title"] for row in body["top_content"]} == {"Short one", "Long one"}
    assert [row["name"] for row in body["by_project"]] == ["Herald"]


# --------------------------------------------------------------------------- #
# by_project                                                                   #
# --------------------------------------------------------------------------- #


def test_by_project_counts_in_sql_rather_than_loading_every_piece(
    db, user, project, mine, sql_log
):
    db.add(
        Content(
            project_id=project.id,
            title="A draft that is not published",
            slug="a-draft-that-is-not-published",
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.DRAFT,
            body_markdown="body",
        )
    )
    db.commit()

    sql_log.clear()
    rows = analytics_service.by_project(db, user.id)

    assert rows[0]["content"] == 3
    assert rows[0]["published"] == 2

    bodies = [
        s
        for s in sql_log
        if "content.body_markdown" in s and "GROUP BY" not in s and "max(" not in s
    ]
    assert not bodies, (
        "by_project read whole content rows to count them:\n"
        + "\n".join(s[:160] for s in bodies)
    )


def test_a_project_with_nothing_in_it_still_reports_zeroes(db, user, project):
    """The GROUP BY returns no row for an empty project; the seed must survive.

    A dict comprehension keyed on the aggregate rather than on the projects
    would silently drop these, and an empty project vanishing from the
    dashboard is the exact bug the ``stats`` seed exists to prevent.
    """
    db.add(Project(user_id=user.id, name="Empty", slug="empty", description="nothing"))
    db.commit()

    rows = {row["name"]: row for row in analytics_service.by_project(db, user.id)}
    assert rows["Empty"]["content"] == 0
    assert rows["Empty"]["published"] == 0
    assert rows["Empty"]["views"] == 0


# --------------------------------------------------------------------------- #
# read_time / engagement_trend                                                 #
# --------------------------------------------------------------------------- #


def test_read_time_reads_the_body_column_not_the_whole_entity(db, user, mine, sql_log):
    sql_log.clear()
    result = analytics_service.read_time(db, user.id)

    # 100 words → 1 minute (the floor), 3000 words → 14 minutes.
    assert result["published_pieces"] == 2
    assert result["total_words"] == 3100
    assert result["avg_read_minutes"] == 7.5

    publications = [
        s
        for s in sql_log
        if "FROM publications WHERE publications.content_id IN" in s
    ]
    assert not publications, (
        "selecting the Content entity dragged in its selectin publications:\n"
        + "\n".join(s[:160] for s in publications)
    )


def test_read_time_still_bands_by_length_and_weights_reads(db, user, mine):
    result = analytics_service.read_time(db, user.id)
    bands = {row["band"]: row for row in result["by_length"]}

    assert bands["short"]["publications"] == 1
    assert bands["long"]["publications"] == 1
    # 4 reads × 1 minute + 40 reads × 14 minutes.
    assert result["reader_minutes"] == 4 + 40 * 14
    assert bands["long"]["reader_minutes"] == 40 * 14


def test_the_engagement_trend_weights_by_the_piece_its_reads_belong_to(
    db, user, mine, sql_log
):
    sql_log.clear()
    days = analytics_service.engagement_trend(db, user.id, days=30)

    charted = [day for day in days if day["reader_minutes"]]
    assert charted, "expected the snapshots to land on a day in the window"
    assert sum(day["reader_minutes"] for day in days) == 4 + 40 * 14

    publications = [
        s
        for s in sql_log
        if "FROM publications WHERE publications.content_id IN" in s
    ]
    assert not publications, "\n".join(s[:160] for s in publications)


def _body_reads(statements: list[str]) -> list[str]:
    """Statements that carry ``content.body_markdown`` back to Python.

    Nothing in this module should produce any. Reading time is derived from the
    stored ``word_count`` now, so the article text has no reason to leave the
    database for a dashboard — the assertions below expect this list empty and
    ``_word_count_reads`` to hold the one keyed read that replaced it.
    """
    return [s for s in statements if "content.body_markdown" in s]


def _word_count_reads(statements: list[str]) -> list[str]:
    """Statements that carry ``content.word_count`` back to Python."""
    return [s for s in statements if "content.word_count" in s]


def _snapshot_reads(statements: list[str]) -> list[str]:
    """Statements that return a row per ``content_metrics`` snapshot."""
    return [s for s in statements if s.startswith("SELECT content_metrics.")]


def test_the_engagement_trend_reads_a_body_per_piece_not_per_snapshot(
    db, user, mine, sql_log
):
    """The trend query returns a row per *poll*; the body must not ride along.

    Selecting one column instead of the entity fixed the ``lazy="selectin"``
    load but not the row multiplication: the trend joins ``content_metrics``,
    so ``content.body_markdown`` came back once per snapshot per publication —
    at the six-hourly default, ~120 copies of each article over a 30-day
    window — to produce one reading time per publication.

    Pinned on the emitted SQL, because the numbers were always right. What was
    wrong was how many megabytes had to cross the wire to reach them.
    """
    # Four polls of each publication, all reporting the counters they already
    # reported. Cumulative counters mean no new gain, so every number below is
    # the one the single-snapshot fixture produces.
    for content, publication in mine:
        for poll in range(4):
            db.add(
                ContentMetric(
                    publication_id=publication.id,
                    views=10 if content.title == "Short one" else 90,
                    reads=4 if content.title == "Short one" else 40,
                    captured_at=utcnow() - timedelta(days=3, hours=6 * poll),
                )
            )
    db.commit()

    sql_log.clear()
    days = analytics_service.engagement_trend(db, user.id, days=30)

    assert sum(day["reader_minutes"] for day in days) == 4 + 40 * 14

    carrying_bodies = [s for s in _snapshot_reads(sql_log) if "body_markdown" in s]
    assert not carrying_bodies, (
        "the per-snapshot read ships a whole article body per poll:\n"
        + "\n".join(s[:200] for s in carrying_bodies)
    )
    assert not _body_reads(sql_log), (
        "the trend has no reason to read an article body at all:\n"
        + "\n".join(s[:200] for s in _body_reads(sql_log))
    )
    assert len(_word_count_reads(sql_log)) == 1, (
        "expected exactly one keyed read of the counts, got "
        + "\n".join(s[:200] for s in _word_count_reads(sql_log))
    )


def test_the_engagement_trend_counts_one_piece_once_across_two_platforms(
    db, user, project, sql_log
):
    """Publications share a piece; the count behind them is read once, not once each.

    Keying the fetch by content id rather than publication id is what makes
    this true, and syndication — the same article on Dev.to, Hashnode and
    Medium — is the normal case rather than the edge one.
    """
    content, first = _publish(
        db, project, title="Syndicated", body="word " * 3000, views=90, reads=40
    )
    second = Publication(
        content_id=content.id,
        platform=Platform.HASHNODE,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow() - timedelta(days=5),
        external_id=f"ext-{content.id}-hashnode",
    )
    db.add(second)
    db.commit()
    db.add(
        ContentMetric(
            publication_id=second.id,
            views=10,
            reads=5,
            captured_at=utcnow() - timedelta(days=4),
        )
    )
    db.commit()

    sql_log.clear()
    days = analytics_service.engagement_trend(db, user.id, days=30)

    # 3000 words → 14 minutes, applied to both publications' reads.
    assert sum(day["reader_minutes"] for day in days) == (40 + 5) * 14
    assert not _body_reads(sql_log), "\n".join(s[:200] for s in _body_reads(sql_log))
    assert len(_word_count_reads(sql_log)) == 1, "\n".join(
        s[:200] for s in _word_count_reads(sql_log)
    )
    assert first.id != second.id


def test_read_minutes_is_the_same_number_from_the_column_and_the_property(db, mine):
    """The free function and the property must not drift.

    ``analytics_service`` applies ``read_minutes_for`` to a selected column
    while every other caller reads ``content.read_minutes``; two spellings of
    the formula would put the dashboard and the content API on different
    numbers. The second assertion is the other half of the same worry, now that
    the count is stored: the column has to agree with the body it came from.
    """
    from app.models.content import read_minutes_for, word_count_of

    for content, _ in mine:
        assert read_minutes_for(content.word_count) == content.read_minutes
        assert word_count_of(content.body_markdown) == content.word_count


def test_read_time_reads_one_body_per_piece_not_per_publication(
    db, user, project, sql_log
):
    """Reading time is a property of the piece, not of where it went out.

    The band query has a row per publication, so a piece syndicated to three
    platforms billed its body three times to compute the same number three
    times. The bodies are read once, keyed by content id, and shared with the
    word-count query that had to read them anyway.
    """
    content, first = _publish(
        db, project, title="Syndicated", body="word " * 3000, views=90, reads=40
    )
    for index, platform in enumerate((Platform.HASHNODE, Platform.MEDIUM)):
        publication = Publication(
            content_id=content.id,
            platform=platform,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow() - timedelta(days=5),
            external_id=f"ext-{content.id}-{index}",
        )
        db.add(publication)
        db.flush()
        db.add(
            ContentMetric(
                publication_id=publication.id,
                views=10,
                reads=5,
                captured_at=utcnow() - timedelta(days=4),
            )
        )
    db.commit()

    sql_log.clear()
    result = analytics_service.read_time(db, user.id)

    assert result["published_pieces"] == 1
    assert result["total_words"] == 3000
    # 14 minutes, against every publication's own reads.
    assert result["reader_minutes"] == (40 + 5 + 5) * 14
    assert not _body_reads(sql_log), "\n".join(s[:200] for s in _body_reads(sql_log))
    assert len(_word_count_reads(sql_log)) == 1, "\n".join(
        s[:200] for s in _word_count_reads(sql_log)
    )
    assert first.id


def test_read_time_still_bands_a_piece_whose_status_was_walked_back(
    db, user, project
):
    """A publication can outlive its piece's ``PUBLISHED`` status.

    The shared count read is scoped to "published, or holds a publication" for
    exactly this row: it is absent from the word counts, which ask about
    published work, and present in the bands, which ask about publications.
    Scoping it to ``PUBLISHED`` alone would leave the band loop without a
    reading time for it.
    """
    content, _ = _publish(
        db, project, title="Walked back", body="word " * 3000, views=90, reads=40
    )
    content.status = ContentStatus.DRAFT
    db.commit()

    result = analytics_service.read_time(db, user.id)
    bands = {row["band"]: row for row in result["by_length"]}

    assert result["published_pieces"] == 0
    assert result["total_words"] == 0
    assert result["avg_read_minutes"] is None
    # Still banded, and still weighted by its own length.
    assert bands["long"]["publications"] == 1
    assert result["reader_minutes"] == 40 * 14

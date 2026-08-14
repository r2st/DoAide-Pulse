"""Query budgets for the readers that load content and ignore its publications.

``Content.publications`` is ``lazy="selectin"``. That is right for the content
list, which renders a row's publications and would otherwise issue one SELECT
per row. It is not free anywhere else: the strategy is a property of the
*mapping*, so every query that loads ``Content`` entities pays for it, including
the ones that only want a title.

Four readers were paying and not collecting — the dashboard's recent-content
column, the public RSS feed, the digest's published-this-week list and its top
table, and the analytics top-content table. None of them names a publication.

These assert on the *publications* SELECT specifically rather than a total, so
they say what they are about, and so they do not move when an unrelated query is
added next to them. ``lazyload`` is what the fix uses rather than ``noload``,
which means the failure mode of a future field reading ``.publications`` is a
slow response and a red test here, not an empty list served as fact.

Every piece gets its own project, for the reason ``test_n_plus_one`` gives: one
shared parent is served from the identity map and hides what is being measured.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus

PLATFORMS = [Platform.DEVTO, Platform.HASHNODE, Platform.MEDIUM]


def _seed(
    db, user_id: int, count: int, *, offset: int = 0, publications_each: int = 2
) -> list[int]:
    """*count* published pieces, each in its own project, each with publications.

    The publications matter: a selectin over nothing costs a query but returns
    no rows, and the point is that the query itself should not be issued.
    """
    published_at = datetime.now(UTC) - timedelta(days=1)
    ids = []
    for i in range(offset, offset + count):
        project = Project(
            user_id=user_id,
            name=f"Project {i}",
            slug=f"project-{i}",
            description="A thing that ships.",
            tone=Tone.TECHNICAL,
            live_url="https://example.test",
        )
        db.add(project)
        db.flush()
        content = Content(
            project_id=project.id,
            content_type=ContentType.ANNOUNCEMENT,
            status=ContentStatus.PUBLISHED,
            title=f"Post {i}",
            slug=f"post-{i}",
            body_markdown="Body words here.",
            excerpt="An excerpt.",
            published_at=published_at,
        )
        db.add(content)
        db.flush()
        ids.append(content.id)
        for platform in PLATFORMS[:publications_each]:
            publication = Publication(
                content_id=content.id,
                platform=platform,
                status=PublicationStatus.PUBLISHED,
                published_at=published_at,
                external_url=f"https://{platform.value}.test/post-{i}",
            )
            db.add(publication)
            db.flush()
            db.add(
                ContentMetric(
                    publication_id=publication.id,
                    captured_at=published_at + timedelta(hours=6),
                    views=100 + i,
                    reactions=3,
                    comments=1,
                    clicks=2,
                    shares=1,
                )
            )
    db.commit()
    db.expire_all()
    return ids


def _publication_selects(sql_log: list[str]) -> list[str]:
    """SELECTs whose FROM is ``publications`` — the shape a selectin load takes.

    Restricted to ``FROM publications`` so a query that merely *joins* the table
    to filter by it is not counted; those are deliberate and are not what a
    stray relationship load looks like.
    """
    return [s for s in sql_log if s.startswith("SELECT") and "FROM publications" in s]


def _selectin_loads(sql_log: list[str]) -> list[str]:
    """The relationship load specifically.

    SQLAlchemy's selectin emits the FK column first and aliases every column,
    which is what separates it from the hand-written publication queries these
    endpoints also run.
    """
    return [
        s
        for s in _publication_selects(sql_log)
        if s.startswith("SELECT publications.content_id AS publications_content_id")
    ]


# --------------------------------------------------------------------------- #
# The dashboard                                                               #
# --------------------------------------------------------------------------- #


def test_dashboard_does_not_load_publications_for_its_recent_column(
    client, auth, db, user, sql_log
):
    """The home page's recent list is five scalar fields and a project name."""
    _seed(db, user.id, 6)
    sql_log.clear()

    resp = client.get("/api/v1/analytics/dashboard", headers=auth)

    assert resp.status_code == 200
    assert _selectin_loads(sql_log) == []


def test_dashboard_still_reports_its_recent_content(client, auth, db, user, sql_log):
    """Dropping the load must not drop the rows it was attached to."""
    _seed(db, user.id, 3)

    body = client.get("/api/v1/analytics/dashboard", headers=auth).json()

    titles = {row["title"] for row in body["recent_content"]}
    assert titles == {"Post 0", "Post 1", "Post 2"}
    assert all(row["project_name"] for row in body["recent_content"])


def test_dashboard_publication_queries_do_not_grow_with_content(
    client, auth, db, user, sql_log
):
    """The property, not the number: twice the content, the same query count."""
    _seed(db, user.id, 3)
    sql_log.clear()
    client.get("/api/v1/analytics/dashboard", headers=auth)
    small = len(_publication_selects(sql_log))

    _seed(db, user.id, 9, offset=3)
    sql_log.clear()
    client.get("/api/v1/analytics/dashboard", headers=auth)

    assert len(_publication_selects(sql_log)) == small


# --------------------------------------------------------------------------- #
# The public feed                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def feed_url(client, db, user):
    _seed(db, user.id, 5)
    project_id = db.scalars(
        select(Project.id).where(Project.user_id == user.id).order_by(Project.id)
    ).first()
    return f"/api/v1/projects/{project_id}/feed.xml"


def test_rss_feed_touches_publications_not_at_all(client, db, sql_log, feed_url):
    """The unauthenticated endpoint, and the one a reader polls on a timer.

    ``build_feed`` reads title, excerpt, slug, canonical URL and date. Before
    this, half the feed's queries were for publications it never mentions.
    """
    sql_log.clear()

    resp = client.get(feed_url)

    assert resp.status_code == 200
    assert _publication_selects(sql_log) == []


def test_rss_feed_still_lists_the_published_items(client, sql_log, feed_url):
    resp = client.get(feed_url)

    assert resp.status_code == 200
    assert "<item>" in resp.text
    assert "Post 0" in resp.text
    assert "An excerpt." in resp.text


def test_rss_feed_runs_one_query_for_its_items(client, db, sql_log, feed_url):
    """One SELECT for the items. The project is already loaded by the route."""
    sql_log.clear()

    client.get(feed_url)

    content_selects = [
        s for s in sql_log if s.startswith("SELECT") and "FROM content" in s
    ]
    assert len(content_selects) == 1


# --------------------------------------------------------------------------- #
# The analytics top table                                                     #
# --------------------------------------------------------------------------- #


def test_top_content_reads_columns_rather_than_entities(db, user, sql_log):
    """It wants five fields; an entity load brings the body's JSON neighbours."""
    from app.services import analytics_service

    _seed(db, user.id, 4)
    sql_log.clear()

    rows = analytics_service.top_content(db, user.id)

    assert rows, "the fixture publishes with metrics, so there is something to rank"
    assert _selectin_loads(sql_log) == []


def test_top_content_still_reports_every_field_it_used_to(db, user, sql_log):
    """Including ``read_minutes``, which now comes from the free function.

    That is the substitution most likely to go wrong — the property and the
    function are the same arithmetic, and this asserts they stayed that way.
    """
    from app.models.content import read_minutes_of
    from app.services import analytics_service

    _seed(db, user.id, 2)

    rows = analytics_service.top_content(db, user.id)

    for row in rows:
        assert row["title"].startswith("Post ")
        assert row["content_type"] == ContentType.ANNOUNCEMENT.value
        assert row["project_id"]
        assert row["published_at"] is not None
        assert row["read_minutes"] == read_minutes_of("Body words here.")


def test_top_content_publication_queries_do_not_grow_with_content(db, user, sql_log):
    from app.services import analytics_service

    _seed(db, user.id, 2)
    sql_log.clear()
    analytics_service.top_content(db, user.id)
    small = len(_publication_selects(sql_log))

    _seed(db, user.id, 8, offset=2)
    sql_log.clear()
    analytics_service.top_content(db, user.id)

    assert len(_publication_selects(sql_log)) == small


# --------------------------------------------------------------------------- #
# The digest                                                                  #
# --------------------------------------------------------------------------- #


def test_digest_does_not_selectin_publications_it_already_joined(db, user, sql_log):
    """Its published list pairs each content with the publication it is about.

    The selectin fetched every *other* publication of each piece as well, which
    is a second query for rows the loop does not read.
    """
    from app.services import digest

    _seed(db, user.id, 4)
    sql_log.clear()

    digest.build(db, user)

    assert _selectin_loads(sql_log) == []


def test_digest_top_table_selects_two_columns_not_whole_pieces(db, user, sql_log):
    """Its top table reads one field per row, so it asks for two columns.

    Narrow rather than "no body anywhere": the published list in the same
    function loads whole pieces on purpose, because it renders ``read_minutes``.
    """
    from app.services import digest

    _seed(db, user.id, 3)
    sql_log.clear()

    digest.build(db, user)

    titles_query = [
        s for s in sql_log if s.startswith("SELECT content.id, content.title FROM")
    ]
    assert len(titles_query) == 1
    assert "body_markdown" not in titles_query[0]


# --------------------------------------------------------------------------- #
# The curve builder, which is where most of this was coming from              #
# --------------------------------------------------------------------------- #


def test_curves_does_not_fetch_publications_to_read_a_title(db, user, sql_log):
    """``velocity.curves`` selects ``(Publication, Content)`` and reads a title.

    The widest of these by far: nine callers, among them the alert pass that
    both the dashboard and the weekly digest run, and the calendar. Each was
    fetching every publication of every published piece the user owns.
    """
    from app.services import velocity

    _seed(db, user.id, 5)
    sql_log.clear()

    built = velocity.curves(db, user.id)

    assert len(built) == 10, "five pieces, two platforms each"
    assert _selectin_loads(sql_log) == []


def test_curves_still_carries_each_piece_title(db, user, sql_log):
    from app.services import velocity

    _seed(db, user.id, 3)

    titles = {curve.title for curve in velocity.curves(db, user.id)}

    assert titles == {"Post 0", "Post 1", "Post 2"}


def test_curves_query_count_does_not_grow_with_the_account(db, user, sql_log):
    """The property the fix is about: a constant number of statements.

    Without it the selectin is one extra query, but it is one extra query
    carrying every publication row the user has ever had — so this pins the
    statement count, and the sibling test above pins that the rows are not read.
    """
    from app.services import velocity

    _seed(db, user.id, 2)
    sql_log.clear()
    velocity.curves(db, user.id)
    small = len([s for s in sql_log if s.startswith("SELECT")])

    _seed(db, user.id, 10, offset=2)
    sql_log.clear()
    velocity.curves(db, user.id)

    assert len([s for s in sql_log if s.startswith("SELECT")]) == small


def test_alert_pass_inherits_the_saving(db, user, sql_log):
    """``alerts.build`` is ``curves`` plus arithmetic, and runs on two pages."""
    from app.services import alerts

    _seed(db, user.id, 4)
    sql_log.clear()

    alerts.build(db, user.id)

    assert _selectin_loads(sql_log) == []


def test_digest_still_names_the_pieces_and_their_platforms(db, user, sql_log):
    from app.services import digest

    _seed(db, user.id, 3)

    published = digest.build(db, user).published

    titles = {row["title"] for row in published}
    assert titles == {"Post 0", "Post 1", "Post 2"}
    for row in published:
        assert sorted(row["platforms"]) == sorted(p.value for p in PLATFORMS[:2])

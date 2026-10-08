"""``OFFSET`` paging over a sort with ties is paging over an arbitrary order.

Four listings sorted on a column that is not unique — ``/content`` and
``/content/queue/review`` on ``created_at``, ``/content/queue/publications`` on
``scheduled_for``, ``/projects`` on ``name``. Ties in an ``ORDER BY`` are not
"whatever order the rows went in": they are unconstrained, and the database
picks. It picks differently depending on the plan, and it picks differently for
the same plan once a concurrent write has moved a row — which on Postgres is
every ``UPDATE``, since MVCC writes a new tuple at the end of the heap rather
than in place.

That the order is the plan's to choose is easy to show, and not a claim about
one database being sloppy:

    sqlite> create table c(id integer primary key, created_at text);
    -- eight rows, all the same created_at
    sqlite> select id from c order by created_at desc;
    1 2 3 4 5 6 7 8
    sqlite> create index ix on c(created_at);
    sqlite> select id from c order by created_at desc;      -- same query
    8 7 6 5 4 3 2 1

Adding an index reversed the answer. Nothing about the rows changed. For a
listing that hands the client ``?offset=20`` next, that freedom is the bug: a
row that moves from page 2 to page 3 between the two requests is a row the
client is shown twice, and one it is never shown at all.

The tie is not hypothetical on any of the four. Publications are the clearest —
every ``pending``, ``publishing`` and ``failed`` row has a null
``scheduled_for``, so *most of the queue* sorts equal — and projects are the
next, since only ``(user_id, slug)`` is unique and the slug is uniquified
exactly because two projects on one account may share a name. For content,
``created_at`` carries ``server_default=func.now()``, and ``now()`` on Postgres
is the *transaction* clock: a batch the autopilot writes in one commit lands on
a single timestamp.

The fix is one column: end every paginated ``ORDER BY`` on the primary key.
``templates``, ``preview_links``, ``triggers`` and ``webhooks`` already did —
these four were the ones that did not, which is why the sweep at the bottom
exists rather than four hand-written assertions.

The digest tests are the same defect where it is not paging at all. ``failed``,
``upcoming`` and ``top`` are ``LIMIT 5`` slices taken over ties, and the digest
is built twice — once by ``GET /analytics/digest`` for the UI and once for the
mail that goes out. "What the UI shows and what lands in the inbox cannot
drift" is the promise those endpoints are written around, and a limit over an
arbitrary order does not keep it.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import digest

#: One instant shared by every row a test wants to tie together.
TIED = utcnow().replace(microsecond=0) - timedelta(days=1)


def _tied_content(db, project, count=8, *, status=ContentStatus.DRAFT):
    """*count* pieces sharing a single ``created_at``, in ascending id."""
    rows = []
    for index in range(count):
        row = Content(
            project_id=project.id,
            content_type=ContentType.TUTORIAL,
            status=status,
            title=f"Piece {index}",
            slug=f"piece-{index}",
            body_markdown="Body.",
            created_at=TIED,
            updated_at=TIED,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def _page(client, auth, url, *, size):
    """Walk *url* with ``limit``/``offset`` and return the ids, page by page."""
    pages = []
    offset = 0
    while True:
        joiner = "&" if "?" in url else "?"
        resp = client.get(f"{url}{joiner}limit={size}&offset={offset}", headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if not body:
            return pages
        pages.append([row["id"] for row in body])
        offset += size


# --------------------------------------------------------------------------- #
# The four listings, on rows that tie                                          #
# --------------------------------------------------------------------------- #


def test_content_ties_are_broken_newest_id_first(client, auth, db, project):
    """Eight pieces on one timestamp come back in one defined order.

    ``id`` descending rather than ascending: the listing documents itself as
    "newest first", and within a batch written in a single transaction the
    highest id *is* the newest. Breaking the tie the other way would have the
    listing report the batch backwards.
    """
    rows = _tied_content(db, project)
    expected = [row.id for row in reversed(rows)]

    resp = client.get("/api/v1/content?limit=100", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()] == expected


def test_content_pages_do_not_repeat_or_drop_a_tied_row(client, auth, db, project):
    """Three pages of three over eight tied rows: each row exactly once."""
    rows = _tied_content(db, project)

    pages = _page(client, auth, "/api/v1/content", size=3)

    seen = [content_id for page in pages for content_id in page]
    assert sorted(seen) == sorted(row.id for row in rows)
    assert len(seen) == len(set(seen)), f"a row was paged twice: {pages}"


def test_the_review_queue_ties_are_broken_too(client, auth, db, project):
    """The listing most likely to hold a batch written in one transaction."""
    rows = _tied_content(db, project, count=5, status=ContentStatus.REVIEW)
    expected = [row.id for row in reversed(rows)]

    resp = client.get("/api/v1/content/queue/review?limit=100", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()] == expected


def test_the_publication_queue_orders_rows_with_no_schedule(
    client, auth, db, project
):
    """Six publications with a null ``scheduled_for`` all sort equal.

    This is the ordinary state of the queue, not an edge case: nothing that is
    ``pending``, ``publishing`` or ``failed`` carries a schedule.
    """
    piece = _tied_content(db, project, count=1)[0]
    pubs = []
    for platform in list(Platform)[:6]:
        row = Publication(
            content_id=piece.id,
            platform=platform,
            status=PublicationStatus.PENDING,
        )
        db.add(row)
        pubs.append(row)
    db.commit()

    pages = _page(client, auth, "/api/v1/content/queue/publications", size=2)

    seen = [pub_id for page in pages for pub_id in page]
    assert seen == sorted(row.id for row in pubs)


def test_projects_sharing_a_name_page_without_repeating(client, auth, db, user):
    """``name`` is not unique — only ``(user_id, slug)`` is."""
    made = []
    for index in range(6):
        row = Project(
            user_id=user.id,
            name="Pulse",
            slug=f"pulse-{index}",
            tone=Tone.TECHNICAL,
        )
        db.add(row)
        made.append(row)
    db.commit()

    pages = _page(client, auth, "/api/v1/projects", size=2)

    seen = [project_id for page in pages for project_id in page]
    assert seen == sorted(row.id for row in made)
    assert len(seen) == len(set(seen)), f"a project was paged twice: {pages}"


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #

#: Every listing that takes ``limit``/``offset``, and the table whose primary
#: key has to end its ``ORDER BY``. A new one added without a tiebreaker is
#: what this is here to catch — the four above were all written by copying a
#: neighbour that had the same gap.
PAGINATED = [
    ("/api/v1/content", "content"),
    ("/api/v1/content/queue/review", "content"),
    ("/api/v1/content/queue/publications", "publications"),
    ("/api/v1/projects", "projects"),
    ("/api/v1/templates", "content_templates"),
    ("/api/v1/triggers", "triggers"),
    ("/api/v1/webhooks", "webhooks"),
]

#: ``ORDER BY`` up to the ``LIMIT``/``OFFSET`` that always follows it here.
_ORDER_BY = re.compile(r"\bORDER BY\b(.*?)(?:\bLIMIT\b|\bOFFSET\b|$)", re.IGNORECASE)


@pytest.mark.parametrize("url,table", PAGINATED)
def test_every_paginated_listing_ends_its_order_by_on_a_primary_key(
    client, auth, sql_log, url, table
):
    """The invariant, asserted against the SQL that actually goes out.

    Reading the router source would not do: the ``ORDER BY`` is assembled from
    a base query, options and a tail spread across two functions, and what
    matters is the statement the database is handed.
    """
    assert client.get(f"{url}?limit=5", headers=auth).status_code == 200

    ordered = [
        match.group(1).strip()
        for statement in sql_log
        if (match := _ORDER_BY.search(statement)) and f" {table}" in statement.lower()
    ]
    assert ordered, f"{url} emitted no ordered SELECT over {table}"

    for clause in ordered:
        last = clause.rsplit(",", 1)[-1].strip()
        assert re.match(rf"{table}\.id\b", last, re.IGNORECASE), (
            f"{url} sorts on a key that is not unique and pages over it; "
            f"ORDER BY ends on {last!r}, not {table}.id"
        )


# --------------------------------------------------------------------------- #
# The digest: a LIMIT over ties, built twice                                   #
# --------------------------------------------------------------------------- #


def test_the_upcoming_five_are_the_same_five_every_build(db, project, user):
    """Eight publications armed for one instant; the email shows five.

    Which five is not allowed to be the query plan's choice — ``digest_preview``
    renders one build and the mail carries another.
    """
    piece = _tied_content(db, project, count=1, status=ContentStatus.APPROVED)[0]
    when = utcnow() + timedelta(days=1)
    armed = []
    for platform in list(Platform)[:8]:
        row = Publication(
            content_id=piece.id,
            platform=platform,
            status=PublicationStatus.SCHEDULED,
            scheduled_for=when,
        )
        db.add(row)
        armed.append(row)
    db.commit()

    first = digest.build(db, user)
    second = digest.build(db, user)

    assert len(first.upcoming) == 5
    assert first.upcoming == second.upcoming
    # The five lowest ids: "the next five out" over a tie is the five that were
    # armed first, which is the only reading of "next" the rows support.
    assert [row["content_id"] for row in first.upcoming] == [piece.id] * 5


def test_a_piece_syndicated_at_one_instant_links_to_one_platform(db, project, user):
    """The digest's per-piece ``url`` is the first non-null it walks past.

    Three platforms published in a single call share a ``published_at``, so
    without a tiebreaker the link in the email was whichever row came back
    first — and it could differ between the preview and the mail.
    """
    piece = _tied_content(db, project, count=1, status=ContentStatus.PUBLISHED)[0]
    when = utcnow() - timedelta(hours=2)
    for platform in list(Platform)[:3]:
        db.add(
            Publication(
                content_id=piece.id,
                platform=platform,
                status=PublicationStatus.PUBLISHED,
                published_at=when,
                external_url=f"https://{platform.value}.example.com/piece",
            )
        )
    db.commit()

    built = [digest.build(db, user) for _ in range(2)]

    assert [d.published[0]["url"] for d in built] == [built[0].published[0]["url"]] * 2
    assert len(built[0].published) == 1
    assert len(built[0].published[0]["platforms"]) == 3


def test_the_top_five_break_view_ties_the_same_way_twice(db, project, user):
    """``top`` is a ``[:5]`` over pieces that commonly tie on views.

    Built from a ``dict`` keyed off a ``set`` union, so its input order is not
    something the caller controls; the sort key has to settle it.
    """
    from app.models.metrics import ContentMetric

    pieces = _tied_content(db, project, count=8, status=ContentStatus.PUBLISHED)
    when = utcnow() - timedelta(hours=1)
    for piece in pieces:
        publication = Publication(
            content_id=piece.id,
            platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=when,
        )
        db.add(publication)
        db.flush()
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=when,
                # Every piece on the same count: nothing but the tiebreaker
                # separates them.
                views=100,
                clicks=0,
            )
        )
    db.commit()

    first, second = digest.build(db, user), digest.build(db, user)

    assert len(first.top) == 5
    assert first.top == second.top
    assert [row["content_id"] for row in first.top] == sorted(
        row["content_id"] for row in first.top
    )

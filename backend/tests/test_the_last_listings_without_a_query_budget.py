"""Query counts for the five listings the two older budget files did not reach.

``test_n_plus_one`` pins the content list, the review queue, the dashboard, the
approved sweep, the bulk endpoints, the projects list and the feed.
``test_remaining_list_query_budgets`` pins triggers, templates, webhooks, the
publication queue and a trigger's own event log. Walking the route table for
``response_model=list[...]`` leaves exactly five with nothing measuring them:
a project's ideas, a webhook's delivery log, a draft's preview links, a piece's
headline performance, and the engagement trend.

All five are flat today. None of these tests fixes a bug; they exist for the
same reason the other two files do, which their docstrings state and this one
will not repeat: an N+1 arrives when a field is added to a response model, and
that is a one-line change that reads as free.

**Two shapes, and only the first fits the ``flat`` fixture.** For ideas,
deliveries and preview links the response is one item per row, so growing the
rows grows the response and ``flat`` can compare the two sizes directly. For
headline performance and the engagement trend it does not: the response is one
item per *headline window* and per *day*, both fixed while the underlying
publications and metric snapshots multiply. Those two get the assertion written
out by hand against the thing that actually grows, which is the point of the
measurement — a per-publication load would be invisible to a fixture watching
the length of a response that never changes.

**Where the parent trick does not apply.** The older files give every row its
own parent, because a shared parent is served from the identity map and hides
the N+1. Three of these listings hang off a single path parameter — one
webhook's deliveries, one draft's links — so there is no per-row parent to vary
and that precaution has nothing to bite on. What the assertion still covers
there is any *other* per-row load: a field that reaches for the content behind a
preview link, or resolves an event name per delivery.
"""
from __future__ import annotations

from datetime import timedelta

from app.models.content import Content, ContentIdea, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.preview_link import PreviewLink
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.services.crypto import encrypt_credentials
from app.services.preview_links import hash_token


def _content(db, project_id: int, i: int, **kwargs) -> Content:
    row = Content(
        project_id=project_id,
        content_type=ContentType.ANNOUNCEMENT,
        title=f"Post {i}",
        slug=f"post-{i}",
        body_markdown="Body.",
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def _selects(sql_log: list[str]) -> int:
    return len([s for s in sql_log if s.startswith("SELECT")])


# --------------------------------------------------------------------------
# One item per row: the ``flat`` fixture measures these directly.
# --------------------------------------------------------------------------


def test_the_ideas_listing_query_count_is_flat(flat, db, project):
    """``IdeaOut`` carries ``project_id``, an integer already on the row.

    ``few``/``many`` stay under the endpoint's hard ``limit(12)`` — it does not
    take a page size from the caller, so thirty rows would come back as twelve
    and the fixture's length assertion would fail before measuring anything.
    """
    project_id = project.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            db.add(
                ContentIdea(
                    project_id=project_id,
                    content_type=ContentType.TUTORIAL,
                    headline=f"Idea {i}",
                    rationale="Worth writing.",
                )
            )

    flat(f"/api/v1/projects/{project_id}/ideas", seed, few=3, many=10)


def test_the_delivery_log_query_count_is_flat(flat, db, user):
    """A delivery serialises from its own columns — including ``event``.

    The obvious future field here is the webhook's URL or description beside
    each attempt, which is the walk this would catch.
    """
    webhook = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        description="Test hook",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=encrypt_credentials({"secret": "s" * 32}),
    )
    db.add(webhook)
    db.flush()
    webhook_id = webhook.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            db.add(
                WebhookDelivery(
                    webhook_id=webhook_id,
                    event=WebhookEvent.CONTENT_PUBLISHED,
                    payload={"n": i},
                    status=DeliveryStatus.DELIVERED,
                )
            )

    flat(f"/api/v1/webhooks/{webhook_id}/deliveries?limit=200", seed)


def test_the_preview_link_listing_query_count_is_flat(flat, db, project):
    """Rows built directly rather than through ``issue``, which commits per link.

    The token is never shown by this endpoint, so the hash only has to be
    distinct and the right width — nothing here reads it back.
    """
    content = _content(db, project.id, 1)
    db.commit()
    content_id = content.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            db.add(
                PreviewLink(
                    content_id=content_id,
                    token_hash=hash_token(f"token-{i}"),
                    expires_at=utcnow() + timedelta(hours=24),
                )
            )

    flat(f"/api/v1/content/{content_id}/preview-links?limit=200", seed)


# --------------------------------------------------------------------------
# Fixed-length responses: the count has to be measured against the rows.
# --------------------------------------------------------------------------


def test_headline_performance_does_not_query_per_publication(
    client, auth, db, project, sql_log
):
    """One piece, more and more platforms. The response length never moves.

    ``performance`` attributes each snapshot to the headline that was live when
    it was taken, which means it walks every metric row for every publication of
    the piece. Doing that with a query per publication would be invisible to
    any assertion on the response, which stays at one window throughout.
    """
    content = _content(db, project.id, 1, status=ContentStatus.PUBLISHED)
    db.commit()
    content_id = content.id
    platforms = list(Platform)

    def add_publications(start: int, stop: int) -> None:
        for i in range(start, stop):
            pub = Publication(
                content_id=content_id,
                platform=platforms[i],
                status=PublicationStatus.PUBLISHED,
                published_at=utcnow() - timedelta(days=2),
            )
            db.add(pub)
            db.flush()
            for day in range(3):
                db.add(
                    ContentMetric(
                        publication_id=pub.id,
                        captured_at=utcnow() - timedelta(days=2 - day),
                        views=10 * (day + 1),
                        reactions=day,
                    )
                )
        db.commit()
        db.expire_all()

    add_publications(0, 2)
    sql_log.clear()
    first = client.get(
        f"/api/v1/content/{content_id}/headlines/performance", headers=auth
    )
    assert first.status_code == 200, first.text
    few = _selects(sql_log)

    add_publications(2, len(platforms))
    sql_log.clear()
    second = client.get(
        f"/api/v1/content/{content_id}/headlines/performance", headers=auth
    )
    assert second.status_code == 200, second.text
    many = _selects(sql_log)

    # The response is the same length both times — that is the whole reason
    # this cannot be handed to ``flat``.
    assert len(first.json()) == len(second.json()) == 1
    assert few == many, (
        f"headline performance issued {few} SELECTs for 2 publications and "
        f"{many} for {len(platforms)}: the count grows with the publications."
    )


def test_the_engagement_trend_does_not_query_per_publication(
    client, auth, db, project, sql_log
):
    """Thirty-one days out either way; the publications behind them multiply."""
    platforms = list(Platform)

    def add_pieces(start: int, stop: int) -> None:
        for i in range(start, stop):
            content = _content(db, project.id, i, status=ContentStatus.PUBLISHED)
            pub = Publication(
                content_id=content.id,
                platform=platforms[i % len(platforms)],
                status=PublicationStatus.PUBLISHED,
                published_at=utcnow() - timedelta(days=5),
            )
            db.add(pub)
            db.flush()
            for day in range(3):
                db.add(
                    ContentMetric(
                        publication_id=pub.id,
                        captured_at=utcnow() - timedelta(days=3 - day),
                        views=100 * (day + 1),
                        reads=10 * (day + 1),
                    )
                )
        db.commit()
        db.expire_all()

    add_pieces(0, 2)
    sql_log.clear()
    first = client.get("/api/v1/analytics/engagement-trend?days=30", headers=auth)
    assert first.status_code == 200, first.text
    few = _selects(sql_log)

    add_pieces(2, 12)
    sql_log.clear()
    second = client.get("/api/v1/analytics/engagement-trend?days=30", headers=auth)
    assert second.status_code == 200, second.text
    many = _selects(sql_log)

    assert len(first.json()) == len(second.json()) == 31
    assert few == many, (
        f"engagement trend issued {few} SELECTs for 2 pieces and {many} for 12: "
        "the count grows with the number of publications."
    )

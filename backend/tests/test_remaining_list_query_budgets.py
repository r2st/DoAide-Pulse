"""Query counts for the list endpoints that did not have one.

``test_n_plus_one`` pins the content list, the review queue, the dashboard, the
approved sweep, the bulk endpoints and the projects list. Five listings were
left: triggers, templates, webhooks, the publication queue, and a trigger's own
event log. All five are flat today — every one of them serialises from columns
and touches no relationship — and none of them had anything saying so.

That is the whole point of a budget test. An N+1 is never written on purpose; it
arrives when a field is added to a response model. ``TriggerOut`` gaining a
``project_name``, ``PublicationOut`` gaining the piece's title, ``WebhookOut``
gaining a delivery count — each is a one-line change that reads as free and puts
one SELECT per row on the wire. The endpoints those models serve are the ones a
user leaves open, so the cost lands on the busiest page rather than the rarest.

Assertions are on *flatness*, not on an absolute number, for the reason the
older file gives: a count that changes with an unrelated query becomes a test
everyone edits without reading. What must not change is that the count is the
same for three rows and for thirty.

The ``flat`` fixture that measures it started here and now lives in
``conftest``, because ``test_the_last_listings_without_a_query_budget`` makes
the same assertion about the listings this file did not reach.

Each row gets its own parent wherever a parent exists — its own project, its own
content — because SQLAlchemy's identity map serves the second reference to one
shared parent from memory, which turns exactly the N+1 being guarded against
into a single extra query that the assertion then passes.
"""
from __future__ import annotations

from app.models.content import Content, ContentStatus, ContentType
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.template import ContentTemplate, TemplateMode
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.webhook import Webhook
from app.schemas.template import MAX_TEMPLATES_PER_USER


def _project(db, user_id: int, i: int) -> Project:
    row = Project(
        user_id=user_id,
        name=f"Project {i}",
        slug=f"project-{i}",
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.flush()
    return row


def test_the_triggers_list_query_count_is_flat(flat, db, user):
    """One project per trigger, so a walk to ``Trigger.project`` would show."""
    user_id = user.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            project = _project(db, user_id, i)
            db.add(
                Trigger(
                    project_id=project.id,
                    kind=TriggerKind.SCHEDULE,
                    name=f"Trigger {i}",
                    config={"interval_days": 7},
                )
            )

    flat("/api/v1/triggers?limit=500", seed)


def test_the_templates_list_query_count_is_flat(flat, db, user):
    user_id = user.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            db.add(
                ContentTemplate(
                    user_id=user_id,
                    name=f"Template {i}",
                    mode=TemplateMode.LITERAL,
                    content_type=ContentType.ANNOUNCEMENT,
                    title_template="Shipped {{ project.name }}",
                    body_template="Shipped {{ project.name }}.",
                )
            )

    # The whole listing in one page. The ceiling here is MAX_TEMPLATES_PER_USER
    # rather than the 500 the other listings take, because that cap is what
    # bounds the collection — see `list_templates`.
    flat(f"/api/v1/templates?limit={MAX_TEMPLATES_PER_USER}", seed)


def test_the_webhooks_list_query_count_is_flat(flat, db, user):
    """Bounded by ``MAX_WEBHOOKS_PER_USER``, so the sizes here are small.

    The cap is on ``create``, not on this read — the model can hold more rows
    than the endpoint that makes them will allow — so the listing is still
    written directly rather than through the API.
    """
    user_id = user.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            db.add(
                Webhook(
                    user_id=user_id,
                    url=f"https://hooks.example.com/{i}",
                    description=f"Endpoint {i}",
                    events=["content.published"],
                )
            )

    flat("/api/v1/webhooks", seed, few=2, many=10)


def test_the_publication_queue_query_count_is_flat(flat, db, user):
    """Each publication under its own content under its own project.

    The endpoint joins through both to establish ownership, and the join only
    filters — it does not populate ``Publication.content``. A response model
    that reached for the piece's title would lazy-load one row per publication,
    and every one of them would be a different row.
    """
    user_id = user.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            project = _project(db, user_id, i)
            content = Content(
                project_id=project.id,
                content_type=ContentType.ANNOUNCEMENT,
                status=ContentStatus.APPROVED,
                title=f"Post {i}",
                slug=f"post-{i}",
                body_markdown="Body.",
            )
            db.add(content)
            db.flush()
            db.add(
                Publication(
                    content_id=content.id,
                    platform=Platform.DEVTO,
                    status=PublicationStatus.PENDING,
                )
            )

    flat("/api/v1/content/queue/publications?limit=200", seed)


def test_a_triggers_event_log_query_count_is_flat(flat, db, project):
    """One trigger, many firings — the shape the activity list actually has."""
    trigger = Trigger(
        project_id=project.id,
        kind=TriggerKind.RSS,
        name="Blog feed",
        config={"feed_url": "https://example.com/feed.xml"},
    )
    db.add(trigger)
    db.commit()
    trigger_id = trigger.id

    def seed(offset: int, count: int) -> None:
        for i in range(offset, offset + count):
            db.add(
                TriggerEvent(
                    trigger_id=trigger_id,
                    dedupe_key=f"entry-{i}",
                    headline=f"Entry {i}",
                    payload={"title": f"Entry {i}"},
                    status=TriggerEventStatus.RECEIVED,
                )
            )

    flat(f"/api/v1/triggers/{trigger_id}/events?limit=200", seed)

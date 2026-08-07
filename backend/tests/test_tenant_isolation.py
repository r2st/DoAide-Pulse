"""One account must never reach another account's rows.

Herald is single-tenant per user with no sharing model at all: every object
hangs off a project, and every project hangs off a user. That makes the check
mechanical, and mechanical is exactly what wants a test sweep rather than a
reading — one endpoint added without its guard is a whole account readable by
anyone who can guess an integer.

Two shapes are covered, and the second is the one that survives a careless fix:

* **A bare id.** ``GET /content/{someone else's id}`` must 404.
* **A mismatched pair.** ``POST /content/{mine}/retry/{theirs}`` — the outer id
  passes the ownership check, and the inner one is then looked up by primary key
  alone. That is the shape that leaks when a guard is written for the object in
  the path prefix and not for the one after it.

404, never 403: a 403 on somebody else's id confirms the id exists, which is an
enumeration oracle for no benefit. See ``deps.owned_project``.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.template import ContentTemplate, TemplateMode
from app.models.trigger import Trigger, TriggerKind
from app.models.user import User
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.security import hash_password
from app.services import webhooks as webhook_service

V1 = "/api/v1"


@pytest.fixture
def stranger(db) -> User:
    """A second account, with none of the first one's things."""
    row = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def stranger_auth(client, stranger) -> dict[str, str]:
    resp = client.post(
        f"{V1}/auth/login",
        data={"username": stranger.email, "password": "hunter2hunter2"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def theirs(db, user, project):
    """One of everything, all belonging to ``user`` — the victim's account."""
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.PUBLISHED,
        title="Their private draft",
        slug="their-private-draft",
        body_markdown="## Secret\n\n" + ("word " * 120),
        excerpt="Secret.",
        meta_description="Something they have not shipped yet.",
    )
    db.add(content)
    db.flush()

    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.FAILED,
        error="it went wrong",
    )
    trigger = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="theirs",
        token=webhook_service.generate_secret(),
        config={},
    )
    template = ContentTemplate(
        user_id=user.id,
        name="Their template",
        mode=TemplateMode.LITERAL,
        content_type=ContentType.ANNOUNCEMENT,
        title_template="A title",
        body_template="A body long enough to be a body.",
        variables=[],
    )
    hook = Webhook(
        user_id=user.id,
        url="https://theirs.example.com/hook",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhook_service.store_secret("s3cret"),
    )
    db.add_all([publication, trigger, template, hook])
    db.flush()

    delivery = WebhookDelivery(
        webhook_id=hook.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload={"event": "content.published", "data": {}},
        status=DeliveryStatus.FAILED,
    )
    db.add(delivery)
    db.commit()

    return {
        "project": project.id,
        "content": content.id,
        "publication": publication.id,
        "trigger": trigger.id,
        "template": template.id,
        "webhook": hook.id,
        "delivery": delivery.id,
    }


def _routes(ids: dict[str, int]) -> list[tuple[str, str]]:
    """Every object-scoped route, addressed at the victim's ids."""
    c, p = ids["content"], ids["project"]
    return [
        # Content
        ("GET", f"{V1}/content/{c}"),
        ("PATCH", f"{V1}/content/{c}"),
        ("DELETE", f"{V1}/content/{c}"),
        ("POST", f"{V1}/content/{c}/approve"),
        ("GET", f"{V1}/content/{c}/links"),
        ("GET", f"{V1}/content/{c}/social"),
        ("GET", f"{V1}/content/{c}/internal-links"),
        ("POST", f"{V1}/content/{c}/repurpose"),
        ("POST", f"{V1}/content/{c}/headlines"),
        ("POST", f"{V1}/content/{c}/headlines/apply"),
        ("GET", f"{V1}/content/{c}/headlines/performance"),
        ("GET", f"{V1}/content/{c}/headlines/winner"),
        ("POST", f"{V1}/content/{c}/headlines/auto-select"),
        ("POST", f"{V1}/content/{c}/publish"),
        ("GET", f"{V1}/content/{c}/schedule/suggestions"),
        ("POST", f"{V1}/content/{c}/schedule"),
        ("DELETE", f"{V1}/content/{c}/schedule"),
        ("POST", f"{V1}/content/{c}/retry/{ids['publication']}"),
        # Projects
        ("GET", f"{V1}/projects/{p}"),
        ("PATCH", f"{V1}/projects/{p}"),
        ("DELETE", f"{V1}/projects/{p}"),
        ("POST", f"{V1}/projects/{p}/scan"),
        ("GET", f"{V1}/projects/{p}/ideas"),
        # Calendar
        ("PATCH", f"{V1}/calendar/content/{c}"),
        # Analytics
        ("GET", f"{V1}/analytics/velocity/{ids['publication']}"),
        # Triggers
        ("PATCH", f"{V1}/triggers/{ids['trigger']}"),
        ("DELETE", f"{V1}/triggers/{ids['trigger']}"),
        ("POST", f"{V1}/triggers/{ids['trigger']}/rotate-secret"),
        ("POST", f"{V1}/triggers/{ids['trigger']}/check"),
        ("GET", f"{V1}/triggers/{ids['trigger']}/events"),
        # Templates
        ("GET", f"{V1}/templates/{ids['template']}"),
        ("PATCH", f"{V1}/templates/{ids['template']}"),
        ("DELETE", f"{V1}/templates/{ids['template']}"),
        ("POST", f"{V1}/templates/{ids['template']}/preview"),
        ("POST", f"{V1}/templates/{ids['template']}/use"),
        # Webhooks
        ("PATCH", f"{V1}/webhooks/{ids['webhook']}"),
        ("DELETE", f"{V1}/webhooks/{ids['webhook']}"),
        ("POST", f"{V1}/webhooks/{ids['webhook']}/rotate-secret"),
        ("POST", f"{V1}/webhooks/{ids['webhook']}/ping"),
        ("GET", f"{V1}/webhooks/{ids['webhook']}/deliveries"),
        (
            "POST",
            f"{V1}/webhooks/{ids['webhook']}/deliveries/{ids['delivery']}/redeliver",
        ),
    ]


def test_every_object_route_hides_another_accounts_row(
    client, stranger_auth, theirs, db
):
    """One request per object-scoped route, all as the wrong account."""
    leaked: list[tuple[str, str, int]] = []
    for method, path in _routes(theirs):
        resp = client.request(method, path, headers=stranger_auth, json={})
        # 404 is the answer. 422 would mean the body was validated *before* the
        # ownership check and is also acceptable — it reveals nothing about the
        # object — but 2xx and 403 are both leaks.
        if resp.status_code not in (404, 422):
            leaked.append((method, path, resp.status_code))

    assert leaked == []


def test_the_owner_can_still_reach_their_own_rows(client, auth, theirs):
    """The sweep above must not be passing because everything 404s for everyone."""
    resp = client.get(f"{V1}/content/{theirs['content']}", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["title"] == "Their private draft"

    assert client.get(f"{V1}/projects/{theirs['project']}", headers=auth).status_code == 200
    assert client.get(f"{V1}/templates/{theirs['template']}", headers=auth).status_code == 200
    assert (
        client.get(f"{V1}/webhooks/{theirs['webhook']}/deliveries", headers=auth).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# Mismatched pairs                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def mine(db, stranger):
    """The attacker's own project, content and webhook, to pair with stolen ids."""
    project = Project(
        user_id=stranger.id,
        name="Mine",
        slug="mine",
        description="A project of my own.",
        tone=Tone.TECHNICAL,
    )
    db.add(project)
    db.flush()
    content = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.DRAFT,
        title="My draft",
        slug="my-draft",
        body_markdown="Words.",
    )
    hook = Webhook(
        user_id=stranger.id,
        url="https://mine.example.com/hook",
        events=[WebhookEvent.CONTENT_PUBLISHED.value],
        encrypted_secret=webhook_service.store_secret("mine"),
    )
    db.add_all([content, hook])
    db.commit()
    return {"project": project.id, "content": content.id, "webhook": hook.id}


def test_retrying_someone_elses_publication_through_my_own_content_is_refused(
    client, stranger_auth, theirs, mine, db
):
    """The inner id is looked up by primary key — it has to be re-checked."""
    resp = client.post(
        f"{V1}/content/{mine['content']}/retry/{theirs['publication']}",
        headers=stranger_auth,
    )

    assert resp.status_code == 404
    # And nothing was re-armed.
    assert db.get(Publication, theirs["publication"]).status == PublicationStatus.FAILED


def test_redelivering_someone_elses_delivery_through_my_own_webhook_is_refused(
    client, stranger_auth, theirs, mine, db
):
    resp = client.post(
        f"{V1}/webhooks/{mine['webhook']}/deliveries/{theirs['delivery']}/redeliver",
        headers=stranger_auth,
    )

    assert resp.status_code == 404
    assert db.get(WebhookDelivery, theirs["delivery"]).status == DeliveryStatus.FAILED


def test_rescheduling_someone_elses_publication_through_my_own_content_is_refused(
    client, stranger_auth, theirs, mine, db
):
    # Inside the scheduling horizon, so a 422 could only mean the time was
    # rejected rather than the publication.
    soon = (utcnow() + timedelta(days=3)).isoformat()
    resp = client.patch(
        f"{V1}/calendar/content/{mine['content']}",
        headers=stranger_auth,
        json={"scheduled_for": soon, "publication_id": theirs["publication"]},
    )

    assert resp.status_code == 404
    assert db.get(Publication, theirs["publication"]).scheduled_for is None


def test_a_template_cannot_be_pointed_at_someone_elses_project(
    client, stranger_auth, theirs
):
    """``default_project_id`` is a foreign key the caller chooses the value of."""
    resp = client.post(
        f"{V1}/templates",
        headers=stranger_auth,
        json={
            "name": "Borrowed",
            "mode": "literal",
            "content_type": "announcement",
            "title_template": "A title",
            "body_template": "A body long enough to count as a body.",
            "variables": [],
            "default_project_id": theirs["project"],
        },
    )

    assert resp.status_code == 404


def test_a_trigger_cannot_be_created_on_someone_elses_project(
    client, stranger_auth, theirs
):
    resp = client.post(
        f"{V1}/triggers",
        headers=stranger_auth,
        json={
            "project_id": theirs["project"],
            "kind": "webhook",
            "name": "borrowed",
            "config": {},
        },
    )

    assert resp.status_code == 404


def test_content_cannot_be_generated_into_someone_elses_project(
    client, stranger_auth, theirs
):
    resp = client.post(
        f"{V1}/content/generate",
        headers=stranger_auth,
        json={"project_id": theirs["project"], "content_type": "announcement"},
    )

    assert resp.status_code == 404


def test_listing_content_filtered_by_someone_elses_project_returns_nothing(
    client, stranger_auth, theirs
):
    """A filter is not a guard, so the base query has to carry the ownership."""
    resp = client.get(
        f"{V1}/content", headers=stranger_auth, params={"project_id": theirs["project"]}
    )

    assert resp.status_code == 200
    assert resp.json() == []

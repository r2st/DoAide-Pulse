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

import re
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute

from app.deps import get_current_user
from app.main import create_app
from app.models.api_key import ApiKey, ApiKeyScope
from app.models.content import Content, ContentIdea, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.template import ContentTemplate, TemplateMode
from app.models.trigger import Trigger, TriggerEvent, TriggerKind
from app.models.user import User
from app.models.webhook import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent
from app.security import hash_password
from app.services import api_keys as api_key_service
from app.services import preview_links
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
    idea = ContentIdea(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        headline="Something they have not written yet",
        rationale="Theirs.",
        source={},
    )
    # The frozen inbound body of one firing. Reachable on its own now that the
    # listing leaves payloads out by default, which makes it exactly the shape
    # this file exists for: an inner id looked up under an outer one the caller
    # may legitimately own.
    event = TriggerEvent(
        trigger_id=trigger.id,
        headline="Their deploy went out",
        payload={"raw": {"authorization": "a header they sent us"}},
    )
    db.add_all([delivery, idea, event])
    db.flush()
    link, _raw = preview_links.issue(db, content, ttl_hours=24)
    # A live machine credential on their project. The token is thrown away —
    # this file is about the *management* routes, where a stranger addresses
    # somebody else's key by id with their own session token.
    key, _token = api_key_service.mint(
        db,
        project=project,
        name="Their CI",
        scopes=[ApiKeyScope.CONTENT_READ],
    )
    db.commit()

    return {
        "project_id": project.id,
        "content_id": content.id,
        "publication_id": publication.id,
        "trigger_id": trigger.id,
        "event_id": event.id,
        "template_id": template.id,
        "webhook_id": hook.id,
        "delivery_id": delivery.id,
        "idea_id": idea.id,
        "link_id": link.id,
        "key_id": key.id,
    }


#: Path parameters that do not address a row, with the reason each is exempt.
#: Keyed by parameter name so this is a decision about two known parameters and
#: not a licence for the next one — an unmapped parameter fails
#: ``test_every_addressable_route_is_in_the_sweep`` until it appears in one list
#: or the other.
_NOT_AN_OBJECT_ID = {
    # The platform enum. A connection is looked up by (user_id, platform), so
    # there is no id to guess: the same value addresses a different row per
    # account, which is the opposite of the shape this file hunts for.
    "platform": "an enum; connections are keyed by (user_id, platform)",
}


APP = create_app()


def _addressable_routes() -> list[APIRoute]:
    """Every authenticated route that addresses something by a path parameter.

    Derived from the app rather than listed by hand. The list this replaced was
    accurate on the day it was written and had fallen five routes behind — a
    sweep that is maintained by remembering to maintain it reports "no leaks"
    just as confidently about the routes it has never heard of.
    """
    return [
        route
        for route in _walk(APP.routes)
        if get_current_user in _dependency_calls(route)
        and re.search(r"{(\w+)}", route.path)
    ]


def _walk(routes):
    """Every :class:`APIRoute` under *routes*, however deeply nested.

    ``include_router`` groups its endpoints behind a single entry, so a flat
    pass over ``app.routes`` finds one route in this app and declares the sweep
    complete. Same walk as :mod:`tests.test_every_endpoint_is_documented`.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)
        yield from _walk(getattr(included, "routes", None) or getattr(route, "routes", []))


def _dependency_calls(route: APIRoute) -> set:
    """Every dependency callable on *route*, including nested ones."""
    seen = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            seen.add(dependant.call)
        stack.extend(dependant.dependencies)
    return seen


def _address(route: APIRoute, ids: dict[str, int]) -> str | None:
    """*route*'s real URL with every parameter filled in from the victim's ids.

    ``None`` when some parameter is not an object id — see
    :data:`_NOT_AN_OBJECT_ID`.

    Built through ``url_path_for`` rather than by substituting into
    ``route.path``, and that is not a stylistic preference. This FastAPI
    mounts an included router as a single entry that keeps the router's own
    unprefixed paths — ``route.path`` here is ``/content/{content_id}``, while
    the URL that reaches the handler is ``/api/v1/content/{content_id}``.
    Formatting the first and requesting it gets a 404 from the router for a
    path that does not exist, which is indistinguishable from the 404 this file
    is trying to prove, so the whole sweep passes without ever reaching an
    endpoint. ``url_path_for`` resolves through the mounted tree, so it cannot
    be wrong in that direction: a name it cannot route raises instead.
    """
    params = {}
    for name in re.findall(r"{(\w+)}", route.path):
        if name in _NOT_AN_OBJECT_ID:
            return None
        params[name] = ids[name]
    return APP.url_path_for(route.name, **params)


def _label(route: APIRoute) -> str:
    methods = ",".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
    return f"{methods} {route.path}"


ADDRESSABLE = _addressable_routes()


def test_the_sweep_found_the_routes_it_is_meant_to_sweep():
    """A guard on the walk. A walk that returned nothing would make every
    assertion below vacuous and the file would still pass."""
    assert len(ADDRESSABLE) > 40, f"only found {len(ADDRESSABLE)} routes"


def test_the_sweep_requests_urls_that_actually_route(client, auth, theirs):
    """The vacuity guard that matters most, and the one that was missing.

    Every request in the sweep expects a 404, and a URL that does not exist
    answers 404 too. So a sweep addressing the wrong path — the un-prefixed
    ``route.path`` instead of the mounted one, say — passes completely while
    never reaching a single handler. Asserted as the *owner*: every address the
    sweep builds must be something this account is allowed to reach, so a 404
    here means the URL is wrong rather than the guard working.
    """
    unroutable: list[str] = []
    for route in ADDRESSABLE:
        path = _address(route, theirs)
        if path is None:
            continue
        method = next(iter(route.methods - {"HEAD", "OPTIONS"}))
        # HEAD against the same path: it reaches routing and the ownership
        # guard without running a POST's side effects or needing a valid body.
        # Starlette answers 405 for a path that exists and does not take HEAD,
        # and 404 only when nothing is registered there at all.
        resp = client.request("HEAD", path, headers=auth)
        if resp.status_code == 404:
            unroutable.append(f"{method} {path}")

    assert not unroutable, (
        f"the sweep addresses {unroutable}, which route to nothing. Those "
        f"requests 404 because the URL is wrong, not because the guard held — "
        f"see `_address`."
    )


def test_every_addressable_route_is_in_the_sweep(theirs):
    """The rule that keeps this file from falling behind the app.

    Every path parameter on an authenticated route either names a row the
    ``theirs`` fixture creates, or is listed in :data:`_NOT_AN_OBJECT_ID` with
    a reason. A new endpoint with a new kind of id fails here — loudly, naming
    the parameter — rather than being quietly left out of the leak sweep.
    """
    unmapped = sorted(
        {
            name
            for route in ADDRESSABLE
            for name in re.findall(r"{(\w+)}", route.path)
            if name not in _NOT_AN_OBJECT_ID and name not in theirs
        }
    )
    assert not unmapped, (
        f"path parameter(s) {unmapped} address rows the isolation sweep has no "
        f"victim for. Add one to the `theirs` fixture keyed by the parameter "
        f"name, or list the parameter in `_NOT_AN_OBJECT_ID` with the reason "
        f"it does not address a row."
    )


@pytest.mark.parametrize(
    "route", [pytest.param(r, id=_label(r)) for r in ADDRESSABLE]
)
def test_every_object_route_hides_another_accounts_row(
    route, client, stranger_auth, theirs, db
):
    """One request per object-scoped route, all as the wrong account.

    Parametrised rather than looped so a leak names the endpoint in the test id
    instead of only in an assertion message at the end of a list.
    """
    path = _address(route, theirs)
    if path is None:
        pytest.skip(f"no object id in {route.path}")
    method = next(iter(route.methods - {"HEAD", "OPTIONS"}))

    resp = client.request(method, path, headers=stranger_auth, json={})

    # 404 is the answer. 422 would mean the body was validated *before* the
    # ownership check and is also acceptable — it reveals nothing about the
    # object — but 2xx and 403 are both leaks.
    assert resp.status_code in (404, 422), (
        f"{method} {path} answered {resp.status_code} to an account that does "
        f"not own the row"
    )


def test_the_owner_can_still_reach_their_own_rows(client, auth, theirs):
    """The sweep above must not be passing because everything 404s for everyone."""
    resp = client.get(f"{V1}/content/{theirs['content_id']}", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["title"] == "Their private draft"

    assert client.get(f"{V1}/projects/{theirs['project_id']}", headers=auth).status_code == 200
    assert client.get(f"{V1}/templates/{theirs['template_id']}", headers=auth).status_code == 200
    assert (
        client.get(f"{V1}/webhooks/{theirs['webhook_id']}/deliveries", headers=auth).status_code
        == 200
    )


def test_every_id_the_sweep_uses_names_a_row_that_exists(db, theirs):
    """The vacuity guard the sweep depends on.

    Every request above is expected to 404, which is also what an id that names
    nothing produces. A fixture that stopped creating one of these rows — or a
    key that fell out of the dict and read back as some stale integer — would
    turn that row's endpoints into a test that asserts a 404 against an empty
    table and passes for the wrong reason.
    """
    from app.models.preview_link import PreviewLink

    rows = {
        "project_id": Project,
        "content_id": Content,
        "publication_id": Publication,
        "trigger_id": Trigger,
        "event_id": TriggerEvent,
        "template_id": ContentTemplate,
        "webhook_id": Webhook,
        "delivery_id": WebhookDelivery,
        "idea_id": ContentIdea,
        "link_id": PreviewLink,
        "key_id": ApiKey,
    }
    # Every id the sweep fills in is accounted for here, so a new one cannot be
    # added to the fixture without also being shown to exist.
    assert set(rows) == set(theirs)

    missing = sorted(key for key, model in rows.items() if db.get(model, theirs[key]) is None)
    assert not missing, f"the sweep addresses {missing}, which name no row"


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
    return {
        "project_id": project.id,
        "content_id": content.id,
        "webhook_id": hook.id,
    }


def test_retrying_someone_elses_publication_through_my_own_content_is_refused(
    client, stranger_auth, theirs, mine, db
):
    """The inner id is looked up by primary key — it has to be re-checked."""
    resp = client.post(
        f"{V1}/content/{mine['content_id']}/retry/{theirs['publication_id']}",
        headers=stranger_auth,
    )

    assert resp.status_code == 404
    # And nothing was re-armed.
    assert db.get(Publication, theirs["publication_id"]).status == PublicationStatus.FAILED


def test_redelivering_someone_elses_delivery_through_my_own_webhook_is_refused(
    client, stranger_auth, theirs, mine, db
):
    resp = client.post(
        f"{V1}/webhooks/{mine['webhook_id']}/deliveries/{theirs['delivery_id']}/redeliver",
        headers=stranger_auth,
    )

    assert resp.status_code == 404
    assert db.get(WebhookDelivery, theirs["delivery_id"]).status == DeliveryStatus.FAILED


def test_rescheduling_someone_elses_publication_through_my_own_content_is_refused(
    client, stranger_auth, theirs, mine, db
):
    # Inside the scheduling horizon, so a 422 could only mean the time was
    # rejected rather than the publication.
    soon = (utcnow() + timedelta(days=3)).isoformat()
    resp = client.patch(
        f"{V1}/calendar/content/{mine['content_id']}",
        headers=stranger_auth,
        json={"scheduled_for": soon, "publication_id": theirs["publication_id"]},
    )

    assert resp.status_code == 404
    assert db.get(Publication, theirs["publication_id"]).scheduled_for is None


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
            "default_project_id": theirs["project_id"],
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
            "project_id": theirs["project_id"],
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
        json={"project_id": theirs["project_id"], "content_type": "announcement"},
    )

    assert resp.status_code == 404


def test_listing_content_filtered_by_someone_elses_project_returns_nothing(
    client, stranger_auth, theirs
):
    """A filter is not a guard, so the base query has to carry the ownership."""
    resp = client.get(
        f"{V1}/content", headers=stranger_auth, params={"project_id": theirs["project_id"]}
    )

    assert resp.status_code == 200
    assert resp.json() == []

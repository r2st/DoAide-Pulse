"""The template picker stopped shipping every template's text.

A ``body_template`` is up to ``50_000`` characters and an account may keep
``MAX_TEMPLATES_PER_USER`` of them, so ``GET /templates`` — which had no paging
at all — answered with just over five megabytes at the cap. Nothing on the other
end read one: the picker draws a name, a mode badge, a blank count and the
placeholder chips, and the chips come from ``placeholders_used``, which is a
handful of names rather than the text they were found in.

So the listing leaves bodies out unless asked, pages like every other listing
here, and the editor fetches the one template it is opening from
``GET /templates/{id}``.

Three groups: the listing's new default, the way back to the old shape, and the
paging. The measurement at the bottom is the one that would have caught this —
it asserts the response *width*, which no query-count budget can see.
"""
from __future__ import annotations

import pytest

from app.models.template import ContentTemplate
from app.schemas.template import MAX_TEMPLATES_PER_USER

API = "/api/v1/templates"

#: Long enough that a hundred of them is unmistakable on the wire, and a legal
#: value: the schema caps ``body_template`` at 50,000.
BIG_BODY = ("word " * 10_000)[:50_000]


def _seed(db, user_id: int, count: int, *, body: str = BIG_BODY) -> None:
    for index in range(count):
        db.add(
            ContentTemplate(
                user_id=user_id,
                name=f"Template {index}",
                title_template="{{project.name}} ships {{thing}}",
                body_template=body,
                variables=[{"name": "thing", "label": "Thing", "required": True}],
            )
        )
    db.commit()


# --------------------------------------------------------------------------- #
# The listing                                                                  #
# --------------------------------------------------------------------------- #


def test_the_listing_omits_bodies_by_default(client, auth, db, user):
    _seed(db, user.id, 1)

    resp = client.get(API, headers=auth)

    assert resp.status_code == 200, resp.text
    row = resp.json()[0]
    # Null, not "": a blank body is a real state, and the two must not read the
    # same to a client deciding whether to go and fetch one.
    assert row["body_template"] is None
    # Everything the picker actually renders is still here, including the chips
    # that are derived from the body it no longer ships.
    assert row["name"] == "Template 0"
    assert row["title_template"] == "{{project.name}} ships {{thing}}"
    assert sorted(row["placeholders_used"]) == ["project.name", "thing"]
    assert len(row["variables"]) == 1


def test_a_template_with_a_blank_body_reads_the_same_as_any_other(client, auth, db, user):
    """``None`` means omitted, so a genuinely empty body must not be ``None``."""
    _seed(db, user.id, 1, body="")

    listed = client.get(API, headers=auth).json()[0]
    fetched = client.get(f"{API}/{listed['id']}", headers=auth).json()

    assert listed["body_template"] is None, "omitted in the listing, like any other"
    assert fetched["body_template"] == "", "and empty — not null — when fetched"


def test_the_omitted_body_is_what_makes_the_response_small(client, auth, db, user):
    """The measurement. A width, not a query count — nothing else can see this.

    Both halves are asserted because only the pair is meaningful: the default
    has to be small *and* the data has to have been big.
    """
    _seed(db, user.id, MAX_TEMPLATES_PER_USER)

    lean = client.get(API, headers=auth)
    fat = client.get(f"{API}?include_bodies=true", headers=auth)

    assert len(lean.json()) == len(fat.json()) == MAX_TEMPLATES_PER_USER
    assert len(fat.content) > 5_000_000, "the shape this replaces"
    assert len(lean.content) < 100_000, "and what it costs now"


# --------------------------------------------------------------------------- #
# The way back                                                                 #
# --------------------------------------------------------------------------- #


def test_asking_for_bodies_gets_them(client, auth, db, user):
    _seed(db, user.id, 2)

    resp = client.get(f"{API}?include_bodies=true", headers=auth)

    assert resp.status_code == 200, resp.text
    assert [row["body_template"] for row in resp.json()] == [BIG_BODY, BIG_BODY]


def test_the_detail_endpoint_always_carries_the_body(client, auth, db, user):
    """The editor's one round trip. Bounded by one body, not by the account."""
    _seed(db, user.id, 1)
    template_id = client.get(API, headers=auth).json()[0]["id"]

    resp = client.get(f"{API}/{template_id}", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json()["body_template"] == BIG_BODY


def test_a_write_answers_with_the_body_it_was_given(client, auth, project):
    """POST and PATCH are single-template responses, so they carry it too."""
    created = client.post(
        API,
        json={
            "name": "Changelog",
            "body_template": "Shipped {{thing}}.",
            "variables": [{"name": "thing"}],
            "default_project_id": project.id,
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    assert created.json()["body_template"] == "Shipped {{thing}}."

    patched = client.patch(
        f"{API}/{created.json()['id']}",
        json={"body_template": "Also shipped {{thing}}."},
        headers=auth,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["body_template"] == "Also shipped {{thing}}."


# --------------------------------------------------------------------------- #
# The paging                                                                   #
# --------------------------------------------------------------------------- #


def test_the_listing_pages_and_counts(client, auth, db, user):
    _seed(db, user.id, 5, body="short")

    resp = client.get(f"{API}?limit=2&offset=1", headers=auth)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2
    # The header reports the whole matching set, not the page — as everywhere.
    assert resp.headers["X-Total-Count"] == "5"


def test_the_count_follows_the_project_filter(client, auth, db, user, project):
    _seed(db, user.id, 3, body="short")
    listed = client.get(API, headers=auth).json()
    template = client.patch(
        f"{API}/{listed[0]['id']}", json={"default_project_id": project.id}, headers=auth
    )
    assert template.status_code == 200, template.text

    resp = client.get(f"{API}?project_id={project.id}", headers=auth)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert resp.headers["X-Total-Count"] == "1"


@pytest.mark.parametrize("limit", [-1, 0, MAX_TEMPLATES_PER_USER + 1])
def test_a_limit_outside_the_range_is_refused(client, auth, limit):
    """Including the negative one: SQLite reads ``LIMIT -1`` as "no limit"."""
    resp = client.get(f"{API}?limit={limit}", headers=auth)
    assert resp.status_code == 422, resp.text


def test_paging_past_the_end_is_an_empty_page_not_an_error(client, auth, db, user):
    _seed(db, user.id, 2, body="short")

    resp = client.get(f"{API}?offset=50", headers=auth)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    assert resp.headers["X-Total-Count"] == "2"

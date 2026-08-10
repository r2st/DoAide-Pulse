"""Template HTTP and model branches that a straight create-and-use never hits.

The per-user ceiling, the project filter, the unique-name collision on *update*
(the create side is already covered), and the two model accessors the renderer
reads declared variables through.
"""
from __future__ import annotations

import pytest

from app.models.project import Project
from app.models.template import ContentTemplate, TemplateMode
from app.routers import templates as template_router
from app.schemas.template import MAX_VARIABLES, TemplateCreate

API = "/api/v1/templates"


def _payload(**over) -> dict:
    body = {
        "name": "Release note",
        "description": "",
        "mode": "literal",
        "content_type": "announcement",
        "title_template": "{{headline}}",
        "body_template": "{{headline}} shipped.",
        "variables": [{"name": "headline", "label": "Headline", "required": True}],
    }
    body.update(over)
    return body


@pytest.fixture
def second_project(db, user) -> Project:
    row = Project(user_id=user.id, name="Other", slug="other")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Listing                                                                     #
# --------------------------------------------------------------------------- #


def test_listing_filtered_by_project_keeps_only_that_project_s_defaults(
    client, auth, project, second_project
):
    client.post(
        API,
        json=_payload(name="For Herald", default_project_id=project.id),
        headers=auth,
    )
    client.post(
        API,
        json=_payload(name="For Other", default_project_id=second_project.id),
        headers=auth,
    )
    client.post(API, json=_payload(name="Unattached"), headers=auth)

    resp = client.get(API, params={"project_id": project.id}, headers=auth)

    assert resp.status_code == 200, resp.text
    assert [t["name"] for t in resp.json()] == ["For Herald"]


# --------------------------------------------------------------------------- #
# Ceilings and collisions                                                     #
# --------------------------------------------------------------------------- #


def test_the_per_account_template_ceiling_is_enforced(client, auth, monkeypatch):
    monkeypatch.setattr(template_router, "MAX_TEMPLATES_PER_USER", 2)
    assert client.post(API, json=_payload(name="one"), headers=auth).status_code == 201
    assert client.post(API, json=_payload(name="two"), headers=auth).status_code == 201

    resp = client.post(API, json=_payload(name="three"), headers=auth)

    assert resp.status_code == 409
    assert "At most 2 templates" in resp.json()["detail"]


def test_renaming_a_template_onto_a_name_you_already_use_is_refused(client, auth):
    client.post(API, json=_payload(name="Taken"), headers=auth)
    other = client.post(API, json=_payload(name="Free"), headers=auth).json()

    resp = client.patch(f"{API}/{other['id']}", json={"name": "Taken"}, headers=auth)

    assert resp.status_code == 409
    assert "Taken" in resp.json()["detail"]


def test_repointing_a_template_at_a_project_that_is_not_yours_404s(
    client, auth, db, user
):
    from app.models.user import User
    from app.security import hash_password

    stranger = User(
        email="stranger@example.com",
        full_name="Stranger",
        hashed_password=hash_password("hunter2hunter2"),
    )
    db.add(stranger)
    db.commit()
    theirs = Project(user_id=stranger.id, name="Theirs", slug="theirs")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)
    mine = client.post(API, json=_payload(), headers=auth).json()

    resp = client.patch(
        f"{API}/{mine['id']}", json={"default_project_id": theirs.id}, headers=auth
    )

    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Schema refusals                                                             #
# --------------------------------------------------------------------------- #


def test_a_template_made_only_of_whitespace_has_no_name(client, auth):
    resp = client.post(API, json=_payload(name="   "), headers=auth)

    assert resp.status_code == 422
    assert "needs a name" in resp.text


def test_more_variables_than_a_template_may_declare_is_refused():
    many = [
        {"name": f"v{i}", "label": f"V{i}"} for i in range(MAX_VARIABLES + 1)
    ]
    with pytest.raises(ValueError, match="variables per template"):
        TemplateCreate(
            name="Too many",
            body_template=" ".join(f"{{{{{v['name']}}}}}" for v in many),
            variables=many,
        )


# --------------------------------------------------------------------------- #
# Model accessors                                                             #
# --------------------------------------------------------------------------- #


def test_a_declared_variable_can_be_looked_up_by_name(db, user):
    template = ContentTemplate(
        user_id=user.id,
        name="T",
        mode=TemplateMode.LITERAL,
        variables=[
            "not a dict",
            {"name": "headline", "label": "Headline", "required": True},
            {"label": "nameless"},
        ],
    )
    db.add(template)
    db.commit()

    assert template.variable("headline")["required"] is True
    assert template.variable("absent") is None
    assert template.variable_names == ["headline"]


def test_a_template_with_no_variables_declares_none(db, user):
    template = ContentTemplate(
        user_id=user.id, name="T", mode=TemplateMode.LITERAL, variables=None
    )
    db.add(template)
    db.commit()

    assert template.variable("anything") is None
    assert template.variable_names == []

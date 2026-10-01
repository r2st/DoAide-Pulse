"""The template HTTP surface: validation on write, and the two ways to use one."""
from __future__ import annotations

from app.models.content import Content
from app.models.template import ContentTemplate
from app.models.user import User
from app.security import hash_password

API = "/api/v1/templates"


def _create(client, auth, **overrides):
    payload = {
        "name": "Weekly changelog",
        "mode": "literal",
        "content_type": "announcement",
        "title_template": "{{project.name}} — week of {{date.long}}",
        "body_template": "## What shipped\n\n{{summary}}\n\nRead more: {{link}}",
        "variables": [
            {"name": "summary", "required": True},
            {"name": "link"},
        ],
    }
    payload.update(overrides)
    return client.post(API, json=payload, headers=auth)


# --------------------------------------------------------------------------- #
# Validation on write                                                          #
# --------------------------------------------------------------------------- #


def test_a_template_is_created_with_the_placeholders_it_uses(client, auth):
    resp = _create(client, auth)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mode_label"] == "Use it as written"
    assert body["placeholders_used"] == [
        "project.name",
        "date.long",
        "summary",
        "link",
    ]
    assert body["use_count"] == 0


def test_a_variable_label_defaults_to_a_readable_form_of_its_name(client, auth):
    resp = _create(
        client,
        auth,
        body_template="{{release_version}}",
        variables=[{"name": "release_version"}],
    )

    assert resp.json()["variables"][0]["label"] == "Release version"


def test_a_misspelled_placeholder_is_refused_while_the_author_can_still_see_it(
    client, auth
):
    """The whole reason validation lives on write. See app.services.templates."""
    resp = _create(
        client,
        auth,
        body_template="Shipped {{versoin}}",
        variables=[{"name": "version"}],
    )

    assert resp.status_code == 422
    assert "{{versoin}}" in resp.text


def test_a_builtin_placeholder_needs_no_declaration(client, auth):
    resp = _create(
        client, auth, body_template="{{project.name}} on {{date.today}}", variables=[]
    )

    assert resp.status_code == 201, resp.text


def test_a_variable_may_not_shadow_a_builtin_namespace(client, auth):
    resp = _create(
        client, auth, body_template="{{project}}", variables=[{"name": "project"}]
    )

    assert resp.status_code == 422
    assert "built-in" in resp.text


def test_two_variables_with_the_same_name_are_refused(client, auth):
    resp = _create(
        client,
        auth,
        body_template="{{v}}",
        variables=[{"name": "v"}, {"name": "v", "default": "2"}],
    )

    assert resp.status_code == 422
    assert "twice" in resp.text


def test_a_variable_name_with_a_space_in_it_is_refused(client, auth):
    resp = _create(
        client, auth, body_template="hi", variables=[{"name": "release version"}]
    )

    assert resp.status_code == 422


def test_two_templates_may_not_share_a_name(client, auth):
    _create(client, auth)

    resp = _create(client, auth)

    assert resp.status_code == 409


def test_a_default_project_belonging_to_someone_else_is_not_found(
    client, auth, db, project
):
    stranger = User(email="x@example.com", hashed_password=hash_password("pw" * 8))
    db.add(stranger)
    db.commit()

    resp = _create(client, auth, default_project_id=project.id + 999)

    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Reading, patching, deleting                                                  #
# --------------------------------------------------------------------------- #


def test_builtins_are_listed_for_the_editors_insert_menu(client, auth):
    resp = client.get(f"{API}/builtins", headers=auth)

    assert resp.status_code == 200
    names = {b["name"] for b in resp.json()}
    assert "project.name" in names
    assert "signal.headline" in names
    assert all(b["description"] for b in resp.json())


def test_only_your_own_templates_are_listed(client, auth, db, user):
    _create(client, auth)
    stranger = User(email="x@example.com", hashed_password=hash_password("pw" * 8))
    db.add(stranger)
    db.commit()
    db.add(ContentTemplate(user_id=stranger.id, name="Theirs", variables=[]))
    db.commit()

    listed = client.get(API, headers=auth).json()

    assert [t["name"] for t in listed] == ["Weekly changelog"]


def test_someone_elses_template_is_not_found(client, auth, db):
    stranger = User(email="x@example.com", hashed_password=hash_password("pw" * 8))
    db.add(stranger)
    db.commit()
    theirs = ContentTemplate(user_id=stranger.id, name="Theirs", variables=[])
    db.add(theirs)
    db.commit()

    assert client.get(f"{API}/{theirs.id}", headers=auth).status_code == 404
    assert client.delete(f"{API}/{theirs.id}", headers=auth).status_code == 404


def test_patching_revalidates_the_whole_template_not_just_the_patch(client, auth):
    """Deleting a variable whose placeholder is still in the body is the bug."""
    template_id = _create(client, auth).json()["id"]

    resp = client.patch(
        f"{API}/{template_id}",
        json={"variables": [{"name": "summary", "required": True}]},
        headers=auth,
    )

    assert resp.status_code == 422
    assert "{{link}}" in resp.text


def test_a_patch_that_removes_the_placeholder_too_is_accepted(client, auth):
    template_id = _create(client, auth).json()["id"]

    resp = client.patch(
        f"{API}/{template_id}",
        json={
            "body_template": "## What shipped\n\n{{summary}}",
            "variables": [{"name": "summary", "required": True}],
        },
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    # The title's own placeholders are untouched by a body-only patch.
    assert resp.json()["placeholders_used"] == ["project.name", "date.long", "summary"]


def test_deleting_a_template_leaves_the_pieces_it_produced(client, auth, db, project):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]
    client.post(
        f"{API}/{template_id}/use",
        json={"values": {"summary": "Things."}},
        headers=auth,
    )

    assert client.delete(f"{API}/{template_id}", headers=auth).status_code == 204
    assert db.query(Content).count() == 1


# --------------------------------------------------------------------------- #
# Preview                                                                      #
# --------------------------------------------------------------------------- #


def test_a_preview_renders_without_saving_anything(client, auth, db, project):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/preview",
        json={"values": {"summary": "Templates landed.", "link": "https://x.test"}},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"].startswith("Pulse — week of")
    assert "Templates landed." in body["body"]
    assert "Read more: https://x.test" in body["body"]
    assert body["is_complete"] is True
    assert db.query(Content).count() == 0


def test_a_preview_of_a_half_filled_template_renders_anyway(client, auth, project):
    """More useful than an error while the author is still typing."""
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(f"{API}/{template_id}/preview", json={"values": {}}, headers=auth)

    assert resp.status_code == 200
    assert resp.json()["missing"] == ["summary"]
    assert resp.json()["is_complete"] is False


def test_an_empty_optional_value_takes_its_line_with_it(client, auth, project):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/preview",
        json={"values": {"summary": "Things happened."}},
        headers=auth,
    )

    assert "Read more" not in resp.json()["body"]


def test_a_preview_can_borrow_a_different_projects_facts(
    client, auth, db, project, user
):
    from app.models.project import Project

    other = Project(user_id=user.id, name="Sidecar", slug="sidecar")
    db.add(other)
    db.commit()
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/preview",
        json={"values": {"summary": "x"}, "project_id": other.id},
        headers=auth,
    )

    assert resp.json()["title"].startswith("Sidecar — ")


# --------------------------------------------------------------------------- #
# Use                                                                          #
# --------------------------------------------------------------------------- #


def test_a_literal_template_produces_a_draft_with_no_model_call(
    client, auth, db, project
):
    """The point of `literal`: deterministic, instant, and exactly as written."""
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/use",
        json={"values": {"summary": "Templates landed.", "link": "https://x.test"}},
        headers=auth,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["body_markdown"] == (
        "## What shipped\n\nTemplates landed.\n\nRead more: https://x.test"
    )
    assert body["title"].startswith("Pulse — week of")
    # No model was involved, so there is nothing to be uncertain about.
    assert body["confidence"] == 1.0

    content = db.query(Content).one()
    assert content.source["kind"] == "template"
    assert content.source["template_id"] == template_id


def test_a_literal_draft_gets_an_excerpt_from_its_own_prose(client, auth, db, project):
    """Nothing else would supply one — no model ran."""
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    client.post(
        f"{API}/{template_id}/use",
        json={"values": {"summary": "Templates landed today."}},
        headers=auth,
    )

    content = db.query(Content).one()
    assert content.excerpt == "Templates landed today."
    assert content.meta_description


def test_using_a_template_without_a_required_value_is_refused(client, auth, project):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(f"{API}/{template_id}/use", json={"values": {}}, headers=auth)

    assert resp.status_code == 422
    assert "summary" in resp.text


def test_using_a_template_with_no_project_anywhere_is_refused(client, auth):
    template_id = _create(client, auth).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/use",
        json={"values": {"summary": "x"}},
        headers=auth,
    )

    assert resp.status_code == 422
    assert "which project" in resp.text


def test_using_a_template_counts_the_use(client, auth, db, project):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    for _ in range(2):
        client.post(
            f"{API}/{template_id}/use",
            json={"values": {"summary": "x"}},
            headers=auth,
        )

    assert client.get(f"{API}/{template_id}", headers=auth).json()["use_count"] == 2


def test_two_uses_of_one_template_do_not_collide_on_the_slug(
    client, auth, db, project
):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    for _ in range(2):
        resp = client.post(
            f"{API}/{template_id}/use",
            json={"values": {"summary": "x"}},
            headers=auth,
        )
        assert resp.status_code == 201, resp.text

    slugs = {c.slug for c in db.query(Content).all()}
    assert len(slugs) == 2


def test_a_prompt_template_briefs_the_model_and_keeps_the_templated_headline(
    client, auth, db, project, monkeypatch
):
    """`prompt` mode templates the instructions; the title stays the author's."""
    seen: dict[str, str] = {}
    real = __import__(
        "app.services.content_generator", fromlist=["generate"]
    ).generate

    def spy(project_arg, content_type, **kwargs):
        seen["instructions"] = kwargs.get("instructions", "")
        return real(project_arg, content_type, **kwargs)

    monkeypatch.setattr("app.services.content_generator.generate", spy)

    template_id = _create(
        client,
        auth,
        mode="prompt",
        title_template="Release notes: {{summary}}",
        body_template="Write about {{summary}} for {{project.name}}.",
        variables=[{"name": "summary", "required": True}],
        default_project_id=project.id,
    ).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/use",
        json={"values": {"summary": "the template engine"}},
        headers=auth,
    )

    assert resp.status_code == 201, resp.text
    assert seen["instructions"] == (
        "Write about the template engine for Pulse."
    )
    assert resp.json()["title"] == "Release notes: the template engine"
    assert db.query(Content).one().source["mode"] == "prompt"


def test_the_content_type_can_be_overridden_for_one_piece(client, auth, project):
    template_id = _create(client, auth, default_project_id=project.id).json()["id"]

    resp = client.post(
        f"{API}/{template_id}/use",
        json={"values": {"summary": "x"}, "content_type": "tutorial"},
        headers=auth,
    )

    assert resp.json()["content_type"] == "tutorial"


def test_the_whole_surface_needs_authentication(client):
    assert client.get(API).status_code == 401
    assert client.post(API, json={"name": "x"}).status_code == 401
    assert client.get(f"{API}/builtins").status_code == 401

"""Template PATCH errors must not leak Pydantic internals.

``TemplateCreate(**merged)`` raises ``ValidationError`` (a ``ValueError``
subclass) when the merged result fails a field or model validator.
``str(ValidationError)`` includes the model class name, a Pydantic docs URL,
and the field path in Pydantic notation — none of which belongs in a toast
message.  The router must extract just the human-readable validation messages.
"""
from __future__ import annotations

API = "/api/v1/templates"


def _payload(**over) -> dict:
    body = {
        "name": "Release note",
        "mode": "literal",
        "content_type": "announcement",
        "title_template": "{{headline}}",
        "body_template": "{{headline}} shipped.",
        "variables": [{"name": "headline", "label": "Headline", "required": True}],
    }
    body.update(over)
    return body


def test_patch_undeclared_placeholder_error_is_clean(client, auth):
    """Removing a variable while its placeholder remains in the body triggers
    the model-level validator inside ``TemplateCreate(**merged)``.  The 422
    detail must be the validator's own message, not Pydantic's verbose dump."""
    created = client.post(API, json=_payload(), headers=auth)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    resp = client.patch(
        f"{API}/{tid}",
        json={"variables": []},
        headers=auth,
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str), f"expected a string, got {type(detail).__name__}"
    assert "TemplateCreate" not in detail
    assert "pydantic.dev" not in detail
    assert "headline" in detail


def test_patch_duplicate_variable_error_is_clean(client, auth):
    """Declaring the same variable name twice must return a clean message."""
    created = client.post(API, json=_payload(), headers=auth)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    resp = client.patch(
        f"{API}/{tid}",
        json={
            "body_template": "{{thing}}",
            "title_template": "{{thing}}",
            "variables": [
                {"name": "thing"},
                {"name": "thing"},
            ],
        },
        headers=auth,
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "TemplateCreate" not in detail
    assert "pydantic.dev" not in detail
    assert "thing" in detail


def test_patch_too_many_variables_error_is_clean(client, auth):
    """Exceeding the variable limit must not leak Pydantic internals."""
    from app.schemas.template import MAX_VARIABLES

    created = client.post(API, json=_payload(), headers=auth)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    variables = [{"name": f"v{i}"} for i in range(MAX_VARIABLES + 1)]
    placeholders = " ".join(f"{{{{{v['name']}}}}}" for v in variables)

    resp = client.patch(
        f"{API}/{tid}",
        json={"body_template": placeholders, "title_template": "", "variables": variables},
        headers=auth,
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "TemplateCreate" not in detail
    assert "pydantic.dev" not in detail

"""A list field's count cap is not a bound on the list.

``tags: list[str] = Field(max_length=30)`` reads as bounded and is not. The
thirty caps how many entries arrive; nothing caps how long each one is, so
thirty entries of a megabyte each satisfied every check the API had. The values
then landed in a JSON column — which, unlike a ``String(n)``, has no width of
its own to refuse them — and went on to an LLM prompt (``project.tech_stack``,
joined into the brief verbatim) or straight out to a publishing adapter
(``content.tags``).

It is the same shape of blind spot as ``test_schema_caps_fit_their_columns``:
the scalar fields are pinned against their columns, and the list fields were
pinned against nothing. So the load-bearing test here is the structural one —
it reads the request body of every route the app actually serves, and fails on
a list-of-strings field that has no per-entry limit, including one added long
after this file was written.
"""
from __future__ import annotations

from typing import Annotated, Union, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app.main import app
from app.models.content import TAG_MAX_LENGTH
from app.schemas.content import ContentCreate, ContentUpdate
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.services.seo import KEYWORD_MAX_LENGTH

# --------------------------------------------------------------------------- #
# Reading the constraint off an annotation                                     #
# --------------------------------------------------------------------------- #


def _union_members(annotation: object) -> list[object]:
    """*annotation* flattened if it is a union, or ``[annotation]`` if not.

    ``list[Tag] | None`` and ``Optional[list[Tag]]`` are different objects with
    the same meaning, and PATCH schemas are written in the first form while
    older code uses the second.
    """
    import types

    if get_origin(annotation) in (Union, types.UnionType):
        return list(get_args(annotation))
    return [annotation]


def _string_item_type(annotation: object) -> object | None:
    """The element type of a list-of-strings field, or ``None``.

    ``None`` covers both "not a list" and "a list of something that is not a
    string" — ``list[Platform]`` is bounded by its enum, and a bytes-length cap
    on an enum member would mean nothing.
    """
    for member in _union_members(annotation):
        if get_origin(member) is not list:
            continue
        args = get_args(member)
        if not args:
            continue
        item = args[0]
        base = get_args(item)[0] if get_origin(item) is Annotated else item
        if base is str:
            return item
    return None


def _item_max_length(item: object) -> int | None:
    """The ``max_length`` declared on a list's element type, if any."""
    if get_origin(item) is not Annotated:
        return None
    for meta in get_args(item)[1:]:
        cap = getattr(meta, "max_length", None)
        if cap is not None:
            return cap
    return None


def _api_routes(routes: object) -> list[APIRoute]:
    """Every ``APIRoute`` under *routes*, however deeply included.

    FastAPI 0.140 wraps an ``include_router`` call in a container that holds the
    router it was given rather than flattening its routes into ``app.routes``,
    so a walk that only follows ``.routes`` finds the four documentation
    endpoints and none of the API. Following ``original_router`` as well is what
    reaches the real ones — and if a future version drops that attribute, the
    count guard below fails loudly rather than passing over an empty list.
    """
    found: list[APIRoute] = []
    for route in routes or []:
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        found.extend(_api_routes(getattr(route, "routes", None)))
        nested = getattr(route, "original_router", None)
        if nested is not None:
            found.extend(_api_routes(getattr(nested, "routes", None)))
    return found


def _request_bodies() -> list[type[BaseModel]]:
    """Every model the app accepts as a request body, deduplicated."""
    seen: dict[str, type[BaseModel]] = {}
    for route in _api_routes(app.routes):
        # ``dependant.body_params`` rather than ``body_field``: an endpoint may
        # take more than one body parameter, and FastAPI folds those into a
        # synthetic model that is not the schema anyone declared.
        for param in route.dependant.body_params:
            for member in _union_members(param.field_info.annotation):
                if isinstance(member, type) and issubclass(member, BaseModel):
                    seen[f"{member.__module__}.{member.__qualname__}"] = member
    return list(seen.values())


# --------------------------------------------------------------------------- #
# The structural check                                                         #
# --------------------------------------------------------------------------- #


def test_the_app_serves_request_bodies_for_this_to_read():
    """A helper that quietly finds nothing would make every test below pass."""
    assert len(_request_bodies()) > 10


def test_every_list_of_strings_the_api_accepts_bounds_its_entries():
    unbounded = [
        f"{model.__name__}.{name}"
        for model in _request_bodies()
        for name, field in model.model_fields.items()
        if (item := _string_item_type(field.annotation)) is not None
        and _item_max_length(item) is None
    ]

    assert unbounded == [], (
        "these fields cap how many entries arrive but not how long each one is; "
        "annotate the element type, e.g. list[Tag] — see app.schemas.limits"
    )


def test_every_list_of_strings_the_api_accepts_also_caps_the_count():
    """The other half of the pair. One entry of a megabyte, ten thousand times."""
    uncounted = [
        f"{model.__name__}.{name}"
        for model in _request_bodies()
        for name, field in model.model_fields.items()
        if _string_item_type(field.annotation) is not None
        and not any(getattr(meta, "max_length", None) for meta in field.metadata)
    ]

    assert uncounted == []


# --------------------------------------------------------------------------- #
# The fields themselves                                                        #
# --------------------------------------------------------------------------- #

#: (schema, field, cap). The four inbound lists, named so a cap that is quietly
#: widened shows up here as well as in the structural test above.
BOUNDED_LISTS = [
    (ContentCreate, "tags", TAG_MAX_LENGTH),
    (ContentCreate, "keywords", KEYWORD_MAX_LENGTH),
    (ContentUpdate, "tags", TAG_MAX_LENGTH),
    (ContentUpdate, "keywords", KEYWORD_MAX_LENGTH),
    (ProjectCreate, "tech_stack", TAG_MAX_LENGTH),
    (ProjectCreate, "keywords", KEYWORD_MAX_LENGTH),
    (ProjectUpdate, "tech_stack", TAG_MAX_LENGTH),
    (ProjectUpdate, "keywords", KEYWORD_MAX_LENGTH),
]


def _minimal(schema: type[BaseModel], field: str, value: list[str]) -> dict:
    """The smallest payload *schema* accepts, with *field* set to *value*."""
    required = {
        ContentCreate: {"project_id": 1, "title": "A title"},
        ContentUpdate: {},
        ProjectCreate: {"name": "A project"},
        ProjectUpdate: {},
    }[schema]
    return {**required, field: value}


@pytest.mark.parametrize(
    "schema,field,cap",
    BOUNDED_LISTS,
    ids=[f"{s.__name__}.{f}" for s, f, _ in BOUNDED_LISTS],
)
def test_an_entry_one_character_over_the_cap_is_refused(schema, field, cap):
    with pytest.raises(ValidationError) as exc:
        schema(**_minimal(schema, field, ["fine", "x" * (cap + 1)]))

    # The refusal names the entry, not just the field: with thirty of them, "one
    # of your tags is too long" is not an error a user can act on.
    error = exc.value.errors()[0]
    assert error["loc"][-2:] == (field, 1), error["loc"]


@pytest.mark.parametrize(
    "schema,field,cap",
    BOUNDED_LISTS,
    ids=[f"{s.__name__}.{f}" for s, f, _ in BOUNDED_LISTS],
)
def test_an_entry_exactly_at_the_cap_is_accepted(schema, field, cap):
    payload = schema(**_minimal(schema, field, ["x" * cap]))

    assert getattr(payload, field) == ["x" * cap]


@pytest.mark.parametrize(
    "schema,field,cap",
    BOUNDED_LISTS,
    ids=[f"{s.__name__}.{f}" for s, f, _ in BOUNDED_LISTS],
)
def test_the_count_cap_still_applies_alongside_the_length_one(schema, field, cap):
    """Neither check may have replaced the other."""
    with pytest.raises(ValidationError, match="too_long|at most"):
        schema(**_minimal(schema, field, ["ok"] * 200))


def test_the_keyword_cap_is_the_one_normalize_keywords_enforces():
    """Accepting a keyword the SEO pass then drops is worse than refusing it.

    ``normalize_keywords`` discards anything over ``KEYWORD_MAX_LENGTH``, and
    ``POST /content`` runs it over the payload before storing. So a longer
    keyword used to be accepted with a 201 and then simply not be there.
    """
    from app.services.seo import normalize_keywords

    over = "x" * (KEYWORD_MAX_LENGTH + 1)
    assert normalize_keywords([over]) == []

    with pytest.raises(ValidationError):
        ContentCreate(project_id=1, title="A title", keywords=[over])


# --------------------------------------------------------------------------- #
# What must *not* have been bounded                                            #
# --------------------------------------------------------------------------- #


def test_the_project_read_model_still_accepts_a_long_stored_entry():
    """The bound is new; the rows are not.

    ``ProjectOut`` derives from ``ProjectBase``, so a per-entry cap on the base
    would make a project stored before this change unreadable — and the endpoint
    that 500s would be the only one that could show the user the value to
    shorten. The caps therefore sit on ``ProjectCreate``/``ProjectUpdate``, and
    this is the test that keeps them from drifting up into the base.
    """
    from app.schemas.project import ProjectOut

    field = ProjectOut.model_fields["tech_stack"]

    assert _item_max_length(_string_item_type(field.annotation)) is None


def test_the_content_read_model_still_accepts_a_long_stored_tag():
    from app.schemas.content import ContentOut

    field = ContentOut.model_fields["tags"]

    assert _item_max_length(_string_item_type(field.annotation)) is None


def test_a_list_of_enums_is_not_mistaken_for_a_list_of_strings():
    """``Platform`` subclasses ``str``. Its members are bounded by the enum."""
    from app.schemas.project import ProjectCreate

    field = ProjectCreate.model_fields["autopilot_platforms"]

    assert _string_item_type(field.annotation) is None

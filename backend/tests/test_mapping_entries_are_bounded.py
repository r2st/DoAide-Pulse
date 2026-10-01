"""A mapping has two sides, and ``min_length=1`` bounds neither of them.

The dict counterpart to ``test_list_items_are_bounded``. ``ConnectionCreate``
declared::

    credentials: dict[str, str] = Field(min_length=1)

which reads as a constraint and is a *floor* on the number of entries — "send
at least one" — with no ceiling on the count, no ceiling on a key and no
ceiling on a value.

The router's own checks do not close it, because they all run after pydantic
has parsed the body:

* an unknown key is refused with a 400 that *names the key*, so the key is
  echoed into the response before anything has bounded it;
* a value is stripped, handed to ``adapter.verify`` — which puts it on the
  network, to the platform — and only then encrypted into a ``Text`` column,
  which unlike a ``String(n)`` has no width of its own to refuse it.

So a single ``PUT /settings/connections`` carrying one ten-megabyte "api_key"
was a ten-megabyte outbound request and a ten-megabyte row, both from a payload
that satisfied every check the API had.

The structural test is the load-bearing one, as it is in the list file: it
reads the request body of every route the app serves, so a ``dict[str, str]``
field added after today fails the suite rather than inheriting the gap.
"""
from __future__ import annotations

import types
from typing import Annotated, Union, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app.main import app
from app.schemas.limits import (
    CREDENTIAL_KEY_MAX_LENGTH,
    CREDENTIAL_VALUE_MAX_LENGTH,
    MAX_CREDENTIAL_FIELDS,
)
from app.schemas.settings import ConnectionCreate


def _union_members(annotation: object) -> list[object]:
    if get_origin(annotation) in (Union, types.UnionType):
        return list(get_args(annotation))
    return [annotation]


def _string_mapping_args(annotation: object) -> tuple[object, object] | None:
    """``(key type, value type)`` for a ``dict[str, str]`` field, or ``None``.

    ``dict[str, Any]`` is deliberately excluded. Those fields exist —
    ``TriggerCreate.config`` and the two template ``values`` — and they are
    bounded, but not here and not by an annotation: a trigger config is checked
    against ``ALLOWED_CONFIG`` for the kind it belongs to, and a template value
    is clipped at ``templates.VALUE_LIMIT`` as it is rendered. Neither bound can
    be expressed as a type, because neither is the same for every key. What can
    be expressed as a type is a mapping of strings to strings, and that is the
    shape this rule covers.
    """
    for member in _union_members(annotation):
        if get_origin(member) is not dict:
            continue
        args = get_args(member)
        if len(args) != 2:
            continue
        bases = [
            get_args(arg)[0] if get_origin(arg) is Annotated else arg for arg in args
        ]
        # ``dict[str, Any]`` falls out here: its value base is ``Any``, not
        # ``str``.
        if bases == [str, str]:
            return args
    return None


def _max_length(annotation: object) -> int | None:
    if get_origin(annotation) is not Annotated:
        return None
    for meta in get_args(annotation)[1:]:
        cap = getattr(meta, "max_length", None)
        if cap is not None:
            return cap
    return None


def _api_routes(routes: object) -> list[APIRoute]:
    """Every ``APIRoute`` under *routes* — see the note in the list-bounds file."""
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
    seen: dict[str, type[BaseModel]] = {}
    for route in _api_routes(app.routes):
        for param in route.dependant.body_params:
            for member in _union_members(param.field_info.annotation):
                if isinstance(member, type) and issubclass(member, BaseModel):
                    seen[f"{member.__module__}.{member.__qualname__}"] = member
    return list(seen.values())


def _string_mapping_fields() -> list[tuple[type[BaseModel], str, object, object]]:
    return [
        (model, name, *args)
        for model in _request_bodies()
        for name, field in model.model_fields.items()
        if (args := _string_mapping_args(field.annotation)) is not None
    ]


# --------------------------------------------------------------------------- #
# The structural check                                                         #
# --------------------------------------------------------------------------- #


def test_the_app_serves_a_string_mapping_for_this_to_read():
    """A helper that quietly finds nothing would make every test below pass."""
    found = {f"{model.__name__}.{name}" for model, name, _, _ in _string_mapping_fields()}

    assert "ConnectionCreate.credentials" in found, found


def test_every_string_mapping_the_api_accepts_bounds_its_values():
    unbounded = [
        f"{model.__name__}.{name}"
        for model, name, _, value in _string_mapping_fields()
        if _max_length(value) is None
    ]

    assert unbounded == [], (
        "these fields accept a mapping of strings with no ceiling on the "
        "values; annotate the value type, e.g. dict[CredentialKey, "
        "CredentialValue] — see app.schemas.limits"
    )


def test_every_string_mapping_the_api_accepts_bounds_its_keys():
    """The side that is easy to forget, because the router looks like it covers it.

    It does not: an unrecognised key is refused, but the refusal quotes the key
    back, so an unbounded key is an unbounded error message.
    """
    unbounded = [
        f"{model.__name__}.{name}"
        for model, name, key, _ in _string_mapping_fields()
        if _max_length(key) is None
    ]

    assert unbounded == []


def test_every_string_mapping_the_api_accepts_caps_the_entry_count():
    """One entry of a kilobyte, a hundred thousand times, is the same attack."""
    uncounted = [
        f"{model.__name__}.{name}"
        for model, name, _, _ in _string_mapping_fields()
        for field in [model.model_fields[name]]
        if not any(getattr(meta, "max_length", None) for meta in field.metadata)
    ]

    assert uncounted == []


# --------------------------------------------------------------------------- #
# The field itself                                                             #
# --------------------------------------------------------------------------- #


def _credentials(value: dict[str, str]) -> ConnectionCreate:
    return ConnectionCreate(platform="devto", credentials=value)


def test_a_value_one_character_over_the_cap_is_refused():
    with pytest.raises(ValidationError) as exc:
        _credentials({"api_key": "x" * (CREDENTIAL_VALUE_MAX_LENGTH + 1)})

    # The refusal names the entry rather than the field: with several
    # credentials in one payload, "one of your values is too long" is not an
    # error anyone can act on.
    assert exc.value.errors()[0]["loc"][-2:] == ("credentials", "api_key")


def test_a_value_exactly_at_the_cap_is_accepted():
    at_cap = "x" * CREDENTIAL_VALUE_MAX_LENGTH

    assert _credentials({"api_key": at_cap}).credentials == {"api_key": at_cap}


def test_a_key_one_character_over_the_cap_is_refused():
    with pytest.raises(ValidationError):
        _credentials({"k" * (CREDENTIAL_KEY_MAX_LENGTH + 1): "fine"})


def test_a_key_exactly_at_the_cap_is_accepted():
    """Refused later as an unknown field — but by the router, with a 400."""
    key = "k" * CREDENTIAL_KEY_MAX_LENGTH

    assert list(_credentials({key: "fine"}).credentials) == [key]


def test_more_entries_than_any_adapter_has_fields_is_refused():
    with pytest.raises(ValidationError, match="too_long|at most"):
        _credentials({f"field_{i}": "v" for i in range(MAX_CREDENTIAL_FIELDS + 1)})


def test_the_empty_mapping_is_still_refused():
    """The floor that was there before this change has not been replaced by it."""
    with pytest.raises(ValidationError, match="too_short|at least"):
        _credentials({})


def test_the_cap_is_generous_enough_for_a_real_credential():
    """A bound that refuses a legitimate token locks a user out of their platform.

    Every credential Pulse asks for is an API key, an app password, a handle,
    a repo path or a site URL. The longest is a signed token; a kilobyte of one
    has to go through.
    """
    assert CREDENTIAL_VALUE_MAX_LENGTH >= 1024

    token = "ey" + "A" * 1000
    assert _credentials({"api_key": token}).credentials == {"api_key": token}


# --------------------------------------------------------------------------- #
# The endpoint                                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def devto(monkeypatch) -> list[dict]:
    """A stubbed Dev.to adapter, and the credentials ``verify`` was given.

    The list is the assertion that matters in the first test below: ``verify``
    is what puts a credential on the network, so "the value was refused" and
    "nothing was sent" have to be checked separately.
    """
    from app.models.publication import Platform
    from app.services import publishers
    from app.services.publishers.base import CredentialField

    seen: list[dict] = []
    adapter = publishers.get_adapter(Platform.DEVTO)
    monkeypatch.setattr(adapter, "verify", lambda creds: seen.append(creds) or "@someone")
    monkeypatch.setattr(
        adapter,
        "credential_fields",
        [CredentialField(key="api_key", label="API Key", required=True)],
    )
    return seen


def test_an_oversized_credential_never_reaches_the_adapter(client, auth, devto):
    """The point of the bound: refused before anything is sent anywhere."""
    response = client.put(
        "/api/v1/settings/connections",
        json={
            "platform": "devto",
            "credentials": {"api_key": "x" * (CREDENTIAL_VALUE_MAX_LENGTH + 1)},
        },
        headers=auth,
    )

    assert response.status_code == 422
    assert devto == []


def test_a_credential_at_the_cap_still_connects(client, auth, devto):
    """The other half: the bound refuses the oversized one and nothing else."""
    response = client.put(
        "/api/v1/settings/connections",
        json={
            "platform": "devto",
            "credentials": {"api_key": "x" * CREDENTIAL_VALUE_MAX_LENGTH},
        },
        headers=auth,
    )

    assert response.status_code == 200, response.text
    assert devto == [{"api_key": "x" * CREDENTIAL_VALUE_MAX_LENGTH}]


def test_the_bounds_are_in_the_generated_schema():
    """Annotations rather than validators, so a client can see the limit.

    A caller that has to discover a ceiling by tripping over it will discover
    it in production.
    """
    schema = ConnectionCreate.model_json_schema()
    credentials = schema["properties"]["credentials"]

    assert credentials["maxProperties"] == MAX_CREDENTIAL_FIELDS
    assert credentials["minProperties"] == 1
    assert credentials["additionalProperties"]["maxLength"] == CREDENTIAL_VALUE_MAX_LENGTH
    assert credentials["propertyNames"]["maxLength"] == CREDENTIAL_KEY_MAX_LENGTH

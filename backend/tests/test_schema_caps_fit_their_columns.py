"""A request schema may not accept more than the column behind it can hold.

This is a bug class the suite is structurally blind to. Herald runs on
PostgreSQL and tests on SQLite, and the two do not agree about what ``VARCHAR(n)``
means: PostgreSQL refuses an over-long value with ``StringDataRightTruncation``,
SQLite stores it and says nothing. So a ``max_length`` wider than its column is
green here, forever, and a 500 in production — and it is the worst shape of 500,
because the API validated the request first and told the caller it was fine.

It is not a hypothetical shape either. ``ContentCreate.meta_description``
accepted 500 characters into a ``String(320)``, and ``canonical_url`` accepted
700 into a ``String(500)``, for as long as both fields have existed.

The pairing below is written out by hand rather than inferred from field names.
Names collide across models that have nothing to do with each other — three
different tables have a ``description``, and a password-reset ``token`` is not a
trigger ``token`` — so a name-matched check reports mismatches between fields
that never meet. Each entry here is a claim that *this* schema is what writes
*that* model, which is the thing worth pinning.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import String, inspect

from app.models.content import Content, ContentIdea
from app.models.platform_connection import PlatformConnection
from app.models.project import Project
from app.models.template import ContentTemplate
from app.models.trigger import Trigger
from app.models.user import User
from app.models.webhook import Webhook
from app.schemas.auth import PreferencesUpdate, UserCreate
from app.schemas.content import ContentCreate, ContentUpdate, HeadlineApplyIn
from app.schemas.project import ProjectBase, ProjectCreate, ProjectUpdate
from app.schemas.settings import ConnectionCreate
from app.schemas.template import TemplateBase, TemplateCreate, TemplateUpdate
from app.schemas.trigger import TriggerCreate, TriggerUpdate
from app.schemas.webhook import WebhookCreate, WebhookUpdate

#: (request schema, model it writes). Every inbound schema in the tree that
#: lands in a column, so a new one that is forgotten here shows up as a gap in
#: :func:`test_every_write_schema_is_paired_with_its_model` rather than as
#: silence.
WRITE_PAIRS: list[tuple[type[BaseModel], type]] = [
    (ContentCreate, Content),
    (ContentUpdate, Content),
    (HeadlineApplyIn, Content),
    (ProjectBase, Project),
    (ProjectCreate, Project),
    (ProjectUpdate, Project),
    (UserCreate, User),
    (PreferencesUpdate, User),
    (TemplateBase, ContentTemplate),
    (TemplateCreate, ContentTemplate),
    (TemplateUpdate, ContentTemplate),
    (TriggerCreate, Trigger),
    (TriggerUpdate, Trigger),
    (WebhookCreate, Webhook),
    (WebhookUpdate, Webhook),
    (ConnectionCreate, PlatformConnection),
]


def _schema_caps(schema: type[BaseModel]) -> dict[str, int]:
    """Each field's ``max_length``, where it declares one.

    Pydantic keeps the constraint in the field's metadata rather than on the
    field itself, and a field can carry several annotations, so this walks them
    all and keeps the largest — the widest value the schema will actually let
    through.
    """
    caps: dict[str, int] = {}
    for name, field in schema.model_fields.items():
        lengths = [
            getattr(meta, "max_length", None)
            for meta in field.metadata
            if getattr(meta, "max_length", None) is not None
        ]
        if lengths:
            caps[name] = max(lengths)
    return caps


def _column_widths(model: type) -> dict[str, int]:
    """Each ``String(n)`` column's ``n``. ``Text`` columns are unbounded and absent."""
    widths: dict[str, int] = {}
    for column in inspect(model).columns:
        if isinstance(column.type, String) and column.type.length:
            widths[column.key] = column.type.length
    return widths


@pytest.mark.parametrize(
    ("schema", "model"),
    WRITE_PAIRS,
    ids=[f"{s.__name__}->{m.__name__}" for s, m in WRITE_PAIRS],
)
def test_no_field_accepts_more_than_its_column_holds(schema, model):
    """The check itself.

    Only fields the schema and the model *both* name are compared. A schema
    field with no column (``ContentCreate.campaign_key`` goes into a JSON blob)
    and a column no schema writes (``Content.slug`` is derived) are both fine;
    what is not fine is the pair that exists and disagrees.
    """
    widths = _column_widths(model)
    for field, cap in _schema_caps(schema).items():
        width = widths.get(field)
        if width is None:
            continue
        assert cap <= width, (
            f"{schema.__name__}.{field} accepts {cap} characters but "
            f"{model.__name__}.{field} is String({width}). PostgreSQL answers "
            f"the overflow with StringDataRightTruncation, which nothing "
            f"catches, so the caller gets a 500 for a request this schema "
            f"already accepted. Narrow the field, or widen the column in a "
            f"migration."
        )


def test_the_two_fields_that_were_wrong_are_pinned_at_their_column_width():
    """Named explicitly, because the check above passes just as well if a field is dropped.

    These are the two that shipped wrong. Pinning the exact numbers means a
    later "let's allow a longer meta description" edit has to go past the
    migration question rather than around it.
    """
    caps = _schema_caps(ContentCreate)
    widths = _column_widths(Content)
    assert caps["meta_description"] == widths["meta_description"] == 320
    assert caps["canonical_url"] == widths["canonical_url"] == 500
    assert caps["title"] == widths["title"] == 300


def test_every_write_schema_is_paired_with_its_model():
    """Keeps :data:`WRITE_PAIRS` from going stale as the API grows.

    A new inbound schema with a ``max_length`` on it is exactly the thing this
    module exists to check, and the failure mode of a hand-written table is that
    nobody adds the row. So the ones that are deliberately unpaired are listed,
    and anything else that declares a cap has to be accounted for.
    """
    import inspect as _inspect
    import pkgutil
    from importlib import import_module

    import app.schemas as schemas_pkg

    #: Schemas with a cap that legitimately write nothing: response models, and
    #: request models whose text never reaches a column of its own.
    unpaired = {
        # Responses — the cap is documentation of what comes back.
        "ProjectOut",
        "TemplateOut",
        "TemplateVariable",
        # Requests whose fields land in JSON blobs or nowhere.
        "GenerateRequest",  # instructions -> the prompt, and source JSON
        "InlineEditIn",  # selection -> the prompt, nothing persisted
        "BulkContentIn",  # a list of ids
        "BulkPublishIn",
        "PasswordResetRequest",  # email -> looked up, never written
        "PasswordResetConfirm",  # token/password -> hashed before storage
    }
    paired = {schema.__name__ for schema, _ in WRITE_PAIRS}

    missing: list[str] = []
    for _, module_name, _ in pkgutil.iter_modules(schemas_pkg.__path__):
        module = import_module(f"app.schemas.{module_name}")
        for name, obj in vars(module).items():
            if not (_inspect.isclass(obj) and issubclass(obj, BaseModel)):
                continue
            if obj.__module__ != module.__name__:
                continue
            if name in paired or name in unpaired or not _schema_caps(obj):
                continue
            missing.append(name)

    assert not missing, (
        f"These schemas declare a max_length and are in neither WRITE_PAIRS nor "
        f"the unpaired list: {sorted(missing)}. Add the model each one writes, "
        f"or say why it writes nothing."
    )


def test_content_idea_headlines_are_not_reachable_from_a_request():
    """The one bounded column with no schema in front of it, pinned as such.

    ``ContentIdea.headline`` is ``String(300)`` and every value that reaches it
    comes from the generator rather than from a request body, so there is no
    schema to pair it with. That is only safe while it stays true — an endpoint
    that lets a caller name an idea would need a cap, and this is where the
    absence is recorded.
    """
    assert _column_widths(ContentIdea)["headline"] == 300


# --------------------------------------------------------------------------- #
# The other half of the blind spot                                             #
# --------------------------------------------------------------------------- #
#
# A request schema is not the only thing that writes a column. The two columns
# below are filled from a model's JSON and from GitHub's API respectively, and
# neither source is under any obligation to be short. "No schema to pair it
# with" is what makes them *unchecked*, not what makes them safe: the cap has to
# live wherever the value is built, and these pin that it does.


def test_the_generator_caps_an_idea_headline_at_its_column():
    from app.services.content_generator import HEADLINE_LIMIT

    assert _column_widths(ContentIdea)["headline"] == HEADLINE_LIMIT


def test_the_github_reader_caps_a_release_tag_at_its_watermark_column():
    from app.models.project import RELEASE_TAG_MAX_LENGTH

    assert _column_widths(Project)["last_seen_release_tag"] == RELEASE_TAG_MAX_LENGTH

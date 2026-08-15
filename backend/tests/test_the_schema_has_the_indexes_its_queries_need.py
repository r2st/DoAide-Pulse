"""Index coverage, as three rules that maintain themselves.

The first two walk the metadata, so they cover tables that do not exist yet. The
third compares the schema the migrations *build* against the schema the models
*declare*, which is the one that had actually drifted.

**Why the third rule is not redundant.** Every other schema test in this suite —
and every fixture in it — gets its tables from ``Base.metadata.create_all``,
which reads the models directly and never runs a migration. So a column or an
index that a model declares and no migration creates is present in every test
run and absent from every deployed database. That is not hypothetical:
``Publication.published_at`` has carried ``index=True`` since the initial schema
and ``a1b2c3d4e5f6`` built the other four indexes on that table without it, so
production spent the project's whole life without an index on the column the
calendar's window query, the weekly digest and ``first_publish_at`` all filter
by range. Nothing could have caught it except a test that migrates.
"""
from __future__ import annotations

import pathlib

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

import app.models  # noqa: F401  - registers every table on the metadata
from alembic import command
from app.config import settings as app_settings
from app.database import Base

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _leading_columns(table) -> set[str]:
    """Every column that begins an index or a unique constraint on *table*.

    Leading position is what matters and is the whole reason this is not a
    membership test against "columns mentioned anywhere in an index". A
    composite on ``(trigger_id, created_at)`` does nothing for a query that
    filters on ``created_at`` alone, and a database will not use it that way.
    """
    leading = {i.columns.keys()[0] for i in table.indexes if len(i.columns)}
    leading |= {
        c.columns.keys()[0]
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint" and len(c.columns)
    }
    return leading


def test_every_foreign_key_is_indexed():
    """A foreign key with no index under it is two problems, not one.

    It is slow to *join* and slow to *delete through*: ``ON DELETE CASCADE`` and
    ``ON DELETE SET NULL`` are enforced by finding the rows that point at the
    row being deleted, and Postgres does that with a sequential scan when there
    is nothing to look in — inside the deleting transaction, holding its locks.

    ``content_templates.default_project_id`` was the one that had neither, and
    it was also a plain filter (``GET /templates?project_id=``). Every other FK
    in the schema already had ``index=True``, which is why this is worth pinning
    rather than fixing case by case: the convention is already unanimous and
    this is what keeps the next one from being the exception.
    """
    missing = [
        f"{table.name}.{column.name} -> "
        + ",".join(sorted(fk.target_fullname for fk in column.foreign_keys))
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.foreign_keys and not column.primary_key
        and column.name not in _leading_columns(table)
    ]
    assert not missing, (
        "foreign key columns with no index leading on them: "
        + "; ".join(sorted(missing))
        + ". Add index=True to the column, and a migration that creates it."
    )


@pytest.mark.parametrize(
    ("table_name", "index_name", "columns"),
    [
        # ``GET /content`` — filters on project_id, orders by created_at DESC
        # with id DESC as the tiebreaker, pages with OFFSET.
        (
            "content",
            "ix_content_project_created",
            ["project_id", "created_at", "id"],
        ),
        # ``GET /projects/{id}/feed.xml`` — equality on project_id and status,
        # then the ordered read the LIMIT takes its window from. Anonymous, and
        # polled on a subscriber's timer rather than a user's click.
        (
            "content",
            "ix_content_project_status_published",
            ["project_id", "status", "published_at", "id"],
        ),
    ],
)
def test_the_listing_sorts_have_an_index_under_them(table_name, index_name, columns):
    """Both listings could already *find* their rows; neither could order them.

    ``project_id`` and ``status`` were indexed separately, so the filter was
    cheap and the sort was not — ``created_at`` and ``published_at`` are the two
    columns ``content`` is ordered by and neither had an index. The column order
    is asserted, not just the presence of a name: an index is only usable for a
    sort if its leading columns are the equality filters and the rest are the
    ORDER BY in the same sequence, so a well-meant reordering silently returns
    the query to sorting by hand.
    """
    table = Base.metadata.tables[table_name]
    by_name = {index.name: index for index in table.indexes}
    assert index_name in by_name, (
        f"{index_name} is gone from {table_name}; the listing it serves is back "
        "to sorting rows the database had to read first."
    )
    assert by_name[index_name].columns.keys() == columns


def test_the_migrations_build_the_schema_the_models_declare(tmp_path, monkeypatch):
    """Run every migration onto an empty database and diff it against the models.

    This is the rule that found ``ix_publications_published_at``. The suite's own
    schema comes from ``create_all``, so the models are the only thing it has
    ever checked — a migration that forgets something is invisible to every other
    test here and visible in production only as a query that got slower than it
    should be, or a column that is not there at all.

    Filtered to what this comparison can speak about honestly across backends.
    The migrations and the test database are both SQLite here, so the diff is
    apples to apples, but ``compare_metadata`` still reports type-affinity noise
    that says nothing about whether a migration was written — an index that
    exists in one and not the other is unambiguous, and that is what is asserted.
    """
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path}"

    # ``alembic/env.py`` takes the URL from ``settings.database_url`` and
    # ignores the one on the config object, so pointing this run at the
    # temporary file means patching the setting — otherwise ``upgrade`` runs
    # against whatever the suite's own database is and the diff below compares
    # the models against an empty schema.
    monkeypatch.setattr(app_settings, "database_url", url)

    # Built without ``alembic.ini`` on purpose, rather than passing its path.
    # ``env.py`` calls ``fileConfig(config.config_file_name)`` whenever there is
    # one, and ``fileConfig`` defaults to ``disable_existing_loggers=True`` — it
    # tears down every logger the app configured at import and replaces them
    # with the ini's three. That leaks out of this test and into the rest of the
    # session: the next test to assert on a log record through ``caplog`` finds
    # nothing was emitted, and the failure surfaces four files away with no
    # visible connection to a migration. Leaving ``config_file_name`` unset
    # skips that branch entirely, and ``script_location`` is the only thing in
    # the ini that running a migration needs.
    config = Config()
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    structural = [
        entry
        for entry in diff
        if isinstance(entry, tuple)
        and entry
        and entry[0]
        in {
            "add_table",
            "remove_table",
            "add_column",
            "remove_column",
            "add_index",
            "remove_index",
            "add_constraint",
            "remove_constraint",
        }
    ]
    assert not structural, (
        "the migrations do not build the schema the models declare: "
        f"{structural}. Every entry here is something the test suite has "
        "because it calls create_all, and a deployed database does not."
    )

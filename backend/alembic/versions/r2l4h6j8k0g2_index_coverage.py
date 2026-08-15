"""index coverage for the listing sorts and the last unindexed foreign key

Four indexes, no columns and no data. Each one is named by the query it exists
for, and none of them changes an answer — only what the database has to read to
give it.

``ix_publications_published_at`` is a repair, not a new idea. ``Publication``
has declared ``index=True`` on that column since the initial schema, and the
initial migration built the other four indexes on that table and silently missed
this one. Nothing caught it because the test suite builds its schema with
``Base.metadata.create_all``, which reads the model and therefore always had the
index — only a *migrated* database lacked it, which means only production did.
The column is a range filter in the calendar's window query
(``published_at.between(...)``), in the weekly digest, in
``analytics_service.first_publish_at`` and in ``velocity``, so the miss landed
on the reads that scan a date range rather than fetch a row.
``test_migrations_match_the_models`` now fails on this class of drift instead of
leaving it to be noticed years later.

``ix_content_templates_default_project_id`` closes the only foreign key in the
schema that had no index under it. It is a filter (``GET /templates?project_id=``
narrows on the column directly) *and* the referencing side of an
``ON DELETE SET NULL``, which Postgres enforces by finding the rows pointing at
the project being deleted — unindexed, that scan runs inside the delete's
transaction. ``test_every_foreign_key_is_indexed`` now fails on the next one.

``ix_content_project_created`` and ``ix_content_project_status_published`` exist
for the *sort*, which is the part of those two queries nothing covered.
``project_id`` and ``status`` were separately indexed already, so the rows were
found cheaply and then sorted on ``created_at`` / ``published_at`` — the two
columns this table orders by, and the two it had no index on. Both carry the
trailing ``id`` the queries use as a tiebreaker, because a tiebreak the index
cannot satisfy leaves the sort in place and gives back most of what the index
was for.

Plain ``CREATE INDEX``, not ``CONCURRENTLY``. Alembic runs this inside a
transaction, which ``CONCURRENTLY`` cannot join, and ``deploy.sh`` has already
stopped the worker and the beat by the time it runs — the API keeps serving
reads throughout, and the write lock is on a table whose row count makes the
build a matter of milliseconds. If that stops being true, this is the migration
to split out and run by hand.

Revision ID: r2l4h6j8k0g2
Revises: q1k3g5i7j9f1
Create Date: 2026-08-15 11:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'r2l4h6j8k0g2'
down_revision: str | None = 'q1k3g5i7j9f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        'ix_publications_published_at',
        'publications',
        ['published_at'],
        unique=False,
    )
    op.create_index(
        'ix_content_templates_default_project_id',
        'content_templates',
        ['default_project_id'],
        unique=False,
    )
    op.create_index(
        'ix_content_project_created',
        'content',
        ['project_id', 'created_at', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_content_project_status_published',
        'content',
        ['project_id', 'status', 'published_at', 'id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_content_project_status_published', table_name='content')
    op.drop_index('ix_content_project_created', table_name='content')
    op.drop_index(
        'ix_content_templates_default_project_id', table_name='content_templates'
    )
    op.drop_index('ix_publications_published_at', table_name='publications')

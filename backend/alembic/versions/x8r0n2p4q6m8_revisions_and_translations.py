"""content_revisions, content_translations, platform_connections.language

Three additions for one round, kept in one migration because they are one
feature between them: a piece can now be edited reversibly and published in
more than one language, and a destination can say which.

``content_revisions`` holds past versions of the seven fields a human edits.
The ``(content_id, revision)`` unique constraint is load-bearing rather than
hygienic: ``revision`` is ``Content.version`` at snapshot time, which is the
counter the ``ETag`` already exposes, so the pair names exactly one text and two
writers racing to snapshot the same version get an ``IntegrityError`` instead of
two rows both claiming to be version 4. ``ON DELETE CASCADE`` from ``content``,
because a history of a deleted piece is not something anybody can act on.

``content_translations`` holds one row per (piece, language), with the same
reasoning behind its unique constraint: "the French version" has to name one
thing. ``source_version`` defaults to ``0``, which is lower than any real
``Content.version`` and therefore reads as stale — correct for a row created
before its text exists, because a pending translation must not publish.

``platform_connections.language`` is ``'en'`` NOT NULL with a server default, so
the backfill puts the fleet into exactly the state it is already in: every
existing connection publishes English today and continues to. Same argument as
``projects.engagement_threshold`` in w7q9m1o3p5l7, and the server default stays
on for the same reason — the seed script and a hand-run INSERT during an
incident do not name every column.

Both new tables index ``content_id`` alone as well as through the composite.
Postgres can serve ``WHERE content_id = ?`` from the leading column of the
composite and does not need the second index; SQLite's planner is less reliable
about it, and the delete-cascade check on the parent runs the bare predicate.
The cost is two small indexes on tables that are written once per edit.

Revision ID: x8r0n2p4q6m8
Revises: w7q9m1o3p5l7
Create Date: 2026-08-15 22:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'x8r0n2p4q6m8'
down_revision: str | None = 'w7q9m1o3p5l7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'content_revisions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('content_id', sa.Integer(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('body_markdown', sa.Text(), nullable=False),
        sa.Column('excerpt', sa.Text(), nullable=False),
        sa.Column('meta_description', sa.String(length=320), nullable=False),
        sa.Column('keywords', sa.JSON(), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('focus_keyword', sa.String(length=100), nullable=False),
        sa.Column('word_count', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('author_user_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=200), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(['content_id'], ['content.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['author_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_id', 'revision', name='uq_revision_per_content'),
    )
    op.create_index(
        op.f('ix_content_revisions_content_id'), 'content_revisions', ['content_id'], unique=False
    )
    op.create_index(
        op.f('ix_content_revisions_author_user_id'),
        'content_revisions',
        ['author_user_id'],
        unique=False,
    )
    op.create_index(
        'ix_revision_content_newest', 'content_revisions', ['content_id', 'revision'], unique=False
    )

    op.create_table(
        'content_translations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('content_id', sa.Integer(), nullable=False),
        sa.Column('language', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('body_markdown', sa.Text(), nullable=False),
        sa.Column('excerpt', sa.Text(), nullable=False),
        sa.Column('meta_description', sa.String(length=320), nullable=False),
        sa.Column('source_version', sa.Integer(), nullable=False),
        sa.Column('quality_issues', sa.JSON(), nullable=False),
        sa.Column('error', sa.String(length=500), nullable=False),
        sa.Column('generated_by_provider', sa.String(length=40), nullable=True),
        sa.Column('generated_by_model', sa.String(length=120), nullable=True),
        sa.Column('translated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(['content_id'], ['content.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('content_id', 'language', name='uq_translation_per_language'),
    )
    op.create_index(
        op.f('ix_content_translations_content_id'),
        'content_translations',
        ['content_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_content_translations_status'), 'content_translations', ['status'], unique=False
    )
    op.create_index(
        'ix_translation_content_language',
        'content_translations',
        ['content_id', 'language'],
        unique=False,
    )

    with op.batch_alter_table('platform_connections', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'language',
                sa.String(length=16),
                nullable=False,
                server_default='en',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('platform_connections', schema=None) as batch_op:
        batch_op.drop_column('language')

    op.drop_index('ix_translation_content_language', table_name='content_translations')
    op.drop_index(op.f('ix_content_translations_status'), table_name='content_translations')
    op.drop_index(op.f('ix_content_translations_content_id'), table_name='content_translations')
    op.drop_table('content_translations')

    op.drop_index('ix_revision_content_newest', table_name='content_revisions')
    op.drop_index(op.f('ix_content_revisions_author_user_id'), table_name='content_revisions')
    op.drop_index(op.f('ix_content_revisions_content_id'), table_name='content_revisions')
    op.drop_table('content_revisions')

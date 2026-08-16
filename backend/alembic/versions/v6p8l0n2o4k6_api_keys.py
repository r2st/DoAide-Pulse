"""api_keys

Per-project machine credentials. See :mod:`app.models.api_key` for what each
column is for and :mod:`app.services.api_keys` for why the stored form is a
SHA-256 digest rather than bcrypt.

Three details worth stating, because a reader of the DDL alone would have to
guess at all three:

* ``prefix`` is ``UNIQUE``, not merely indexed. It is the lookup handle, and
  authentication resolves a token by finding exactly one row for it — a
  duplicate would make ``authenticate`` non-deterministic in a way no test that
  mints keys the normal way could produce.

* ``user_id`` is denormalised from ``projects.user_id``. It is on the hot path
  of every machine request (the owner's ``is_active`` is checked on each one),
  and a join to reach it would double the query count of the cheapest endpoints
  on the install.

* ``rotated_from_id`` is a self-reference with ``ON DELETE SET NULL``, unlike
  the two ``CASCADE``s beside it. The ancestor is history; losing the history
  must not take a working credential with it.

No backfill and no server defaults: this is a new table, and every row in it is
written by :func:`app.services.api_keys.mint`, which supplies every column.

Revision ID: v6p8l0n2o4k6
Revises: u5o7k9m1n3j5
Create Date: 2026-08-15 22:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'v6p8l0n2o4k6'
down_revision: str | None = 'u5o7k9m1n3j5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('prefix', sa.String(length=32), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rotated_from_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['rotated_from_id'], ['api_keys.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_api_keys_user_id', 'api_keys', ['user_id'])
    op.create_index('ix_api_keys_project_id', 'api_keys', ['project_id'])
    op.create_index('ix_api_keys_prefix', 'api_keys', ['prefix'], unique=True)
    op.create_index('ix_api_keys_rotated_from_id', 'api_keys', ['rotated_from_id'])
    op.create_index(
        'ix_api_keys_project_created', 'api_keys', ['project_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_api_keys_project_created', table_name='api_keys')
    op.drop_index('ix_api_keys_rotated_from_id', table_name='api_keys')
    op.drop_index('ix_api_keys_prefix', table_name='api_keys')
    op.drop_index('ix_api_keys_project_id', table_name='api_keys')
    op.drop_index('ix_api_keys_user_id', table_name='api_keys')
    op.drop_table('api_keys')

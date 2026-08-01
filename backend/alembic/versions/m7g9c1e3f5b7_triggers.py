"""triggers + trigger_events

The generalized trigger system: a project is written about because a repo moved,
a feed gained an entry, something POSTed to a URL, or a schedule came round —
rather than only the first of those. Two tables, mirroring the outbound-webhook
shape: the standing configuration, and one row per firing carrying its own
payload, dedupe key and outcome.

Revision ID: m7g9c1e3f5b7
Revises: l6f8b0d2e4a6
Create Date: 2026-07-31 22:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'm7g9c1e3f5b7'
down_revision: str | None = 'l6f8b0d2e4a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'triggers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=True),
        sa.Column('encrypted_secret', sa.Text(), nullable=False, server_default=''),
        sa.Column('state', sa.JSON(), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fire_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_triggers_project_id', 'triggers', ['project_id'])
    # Unique: the token is the only thing identifying a trigger on an
    # unauthenticated inbound request, so a collision would be a cross-account
    # content injection rather than a mere clash.
    op.create_index('ix_triggers_token', 'triggers', ['token'], unique=True)

    op.create_table(
        'trigger_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('trigger_id', sa.Integer(), nullable=False),
        sa.Column('dedupe_key', sa.String(length=200), nullable=True),
        sa.Column('headline', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='received'),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
        sa.Column('content_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['trigger_id'], ['triggers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # The dedupe guard. NULL keys do not collide, which is what makes "this
        # firing has nothing to compare against" expressible.
        sa.UniqueConstraint('trigger_id', 'dedupe_key', name='uq_trigger_event_dedupe'),
    )
    op.create_index('ix_trigger_events_trigger_id', 'trigger_events', ['trigger_id'])
    op.create_index('ix_trigger_events_status', 'trigger_events', ['status'])
    op.create_index(
        'ix_trigger_event_trigger_created',
        'trigger_events',
        ['trigger_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_trigger_event_trigger_created', table_name='trigger_events')
    op.drop_index('ix_trigger_events_status', table_name='trigger_events')
    op.drop_index('ix_trigger_events_trigger_id', table_name='trigger_events')
    op.drop_table('trigger_events')
    op.drop_index('ix_triggers_token', table_name='triggers')
    op.drop_index('ix_triggers_project_id', table_name='triggers')
    op.drop_table('triggers')

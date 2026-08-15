"""llm_usage table, projects.last_scan_duration_ms, projects.scan_count

Three columns' worth of observability, in one revision because they arrive for
one reason: ``/api/v1/metrics`` is unauthenticated and answers from the database,
and every number on it has to be readable by a process other than the one that
produced it. See :mod:`app.models.llm_usage` for why the token accounting is a
table rather than a counter, and ``Project.scan_count`` for why the scan
frequency is a counter rather than a table.

``scan_count`` takes ``server_default='0'`` and keeps it, for the reason
``content.version`` kept its own: the column is NOT NULL, existing rows need a
value, and the seeder and hand-written INSERTs do not go through the mapper that
would otherwise supply one.

No backfill. Every pre-existing project starts at zero scans, which is not the
truth — they have been scanned for weeks — but it is the only honest thing the
column can say, because the scans it is counting were never counted. The
alternative, deriving a count from ``last_scanned_at`` and the scan interval,
would invent a number and then be indistinguishable from a measured one. The
counter means "completed scans since this column existed", and the endpoint
reports it beside ``last_scanned_at``, which does have history.

``last_scan_duration_ms`` is nullable and starts NULL for the same reason: no
scan has been timed yet, and NULL is how that is said.

Revision ID: s3m5i7k9l1h3
Revises: r2l4h6j8k0g2
Create Date: 2026-08-15 21:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 's3m5i7k9l1h3'
down_revision: str | None = 'r2l4h6j8k0g2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'llm_usage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.Column('provider', sa.String(length=40), nullable=False),
        sa.Column('model', sa.String(length=120), nullable=False),
        sa.Column('purpose', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_llm_usage_created_at'), 'llm_usage', ['created_at'], unique=False
    )
    # The composite the summary query actually uses — "this window, grouped by
    # provider". The single-column index above is what the purge sweep walks.
    op.create_index(
        'ix_llm_usage_created_provider',
        'llm_usage',
        ['created_at', 'provider'],
        unique=False,
    )

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_scan_duration_ms', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('scan_count', sa.Integer(), nullable=False, server_default='0')
        )


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('scan_count')
        batch_op.drop_column('last_scan_duration_ms')

    op.drop_index('ix_llm_usage_created_provider', table_name='llm_usage')
    op.drop_index(op.f('ix_llm_usage_created_at'), table_name='llm_usage')
    op.drop_table('llm_usage')

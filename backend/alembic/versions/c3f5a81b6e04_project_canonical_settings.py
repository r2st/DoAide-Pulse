"""projects.auto_canonical, projects.canonical_platform

Pulse has always sent ``canonical_url`` from every adapter and never set it, so
each syndicated copy was published as if it were the original and the search
ranking was split between them. These two columns are what decides the field:
whether to fill it automatically, and which destination counts as the original.

``auto_canonical`` backfills to true. That changes behaviour for existing
projects — the next publish will start setting the field — which is the intent,
and it is the safe direction: the field is only ever filled when it is empty.

Revision ID: c3f5a81b6e04
Revises: b7e1c9a4d2f8
Create Date: 2026-07-29 23:45:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3f5a81b6e04'
down_revision: str | None = 'b7e1c9a4d2f8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'auto_canonical', sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        # Nullable and default-null: "no destination is privileged, whichever
        # publishes first wins" is a real setting, not a missing one.
        batch_op.add_column(
            sa.Column('canonical_platform', sa.String(length=30), nullable=True)
        )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('auto_canonical', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('canonical_platform')
        batch_op.drop_column('auto_canonical')

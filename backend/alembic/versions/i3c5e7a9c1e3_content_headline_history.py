"""content.headline_history

Append-only log of past headlines and the time window each one was live, so
engagement snapshots (content_metrics) can be attributed back to whichever
headline was live when they were captured — the basis for the headline
performance comparison. Empty list means the headline has never been changed;
the current title's window is derived at read time rather than stored.

Default '[]', not null: every existing row gets an empty history, which is
correct since no piece has had a headline swap recorded before this column
existed.

Revision ID: i3c5e7a9c1e3
Revises: h2b4d6f8a0c1
Create Date: 2026-07-31 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'i3c5e7a9c1e3'
down_revision: str | None = 'h2b4d6f8a0c1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('headline_history', sa.JSON(), nullable=False, server_default='[]')
        )


def downgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_column('headline_history')

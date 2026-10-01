"""Add index on content.published_at for analytics range queries

The analytics service filters ``Content.published_at >= since`` on every
time-series call and the content listing can sort by it. Without an index the
database scans the whole table to answer a question about the last thirty days.

Revision ID: z0t2p4r6s8o0
Revises: y9s1o3q5r7n9
Create Date: 2026-10-01 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'z0t2p4r6s8o0'
down_revision: str | None = 'y9s1o3q5r7n9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index('ix_content_published_at', 'content', ['published_at'])


def downgrade() -> None:
    op.drop_index('ix_content_published_at', table_name='content')

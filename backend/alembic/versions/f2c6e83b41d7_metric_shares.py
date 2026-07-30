"""content_metrics.shares

Boosts, reposts and retweets. Added with the Mastodon and Bluesky adapters,
which both report it and neither of which reports a view count — mapping it onto
``clicks`` or folding it into ``reactions`` would have made the one signal that
*extends* a post's reach indistinguishable from the ones that merely measure it.

Nullable with no backfill, on purpose: NULL means "this platform does not report
it", which is what every existing row is and what Dev.to and Medium rows will go
on being. A zero would be a claim nobody shared the post.

Revision ID: f2c6e83b41d7
Revises: e4b7c2d95a10
Create Date: 2026-07-30 10:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f2c6e83b41d7'
down_revision: str | None = 'e4b7c2d95a10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('content_metrics', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shares', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('content_metrics', schema=None) as batch_op:
        batch_op.drop_column('shares')

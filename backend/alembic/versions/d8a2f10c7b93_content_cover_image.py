"""content.cover_image_url

The one field every blogging platform surfaces in its feed and Pulse had no
column for. Stored as an absolute URL rather than an uploaded file: Dev.to,
Hashnode, Medium and WordPress all fetch the image from their own servers, so
holding the bytes here would add object storage to the deploy and change nothing
about what the platforms receive.

Revision ID: d8a2f10c7b93
Revises: c3f5a81b6e04
Create Date: 2026-07-30 00:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd8a2f10c7b93'
down_revision: str | None = 'c3f5a81b6e04'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cover_image_url', sa.String(length=700), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_column('cover_image_url')

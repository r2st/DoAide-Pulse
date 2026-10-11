"""Add marketing_images column to content

Revision ID: a1b2f7e8d9c0
Revises: a0b1c2d3e4f5
Create Date: 2026-10-09 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2f7e8d9c0'
down_revision: str | None = 'a0b1c2d3e4f5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('content', sa.Column('marketing_images', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    op.drop_column('content', 'marketing_images')

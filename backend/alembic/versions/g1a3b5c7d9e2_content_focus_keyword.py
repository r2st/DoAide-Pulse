"""content.focus_keyword

The primary SEO keyword for a content piece. Populated automatically from the
first project keyword during generation. Drives the SEO audit score: keyword
density, first-paragraph presence, subheading inclusion, and slug checks.

Default empty string, not null — every existing row gets '' which is correct
since no piece had a focus keyword before this column existed.

Revision ID: g1a3b5c7d9e2
Revises: f2c6e83b41d7
Create Date: 2026-07-30 14:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'g1a3b5c7d9e2'
down_revision: str | None = 'f2c6e83b41d7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('focus_keyword', sa.String(100), nullable=False, server_default='')
        )


def downgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_column('focus_keyword')

"""users.weekly_digest_enabled

Opt-out for the weekly performance email. Defaults to true for existing rows:
it is the only thing that closes the loop between publishing and knowing
whether it worked, and nothing is sent for a week with nothing in it — or at
all, unless SMTP is configured.

Revision ID: k5e7a9c1d3f5
Revises: j4d6f8b0c2e4
Create Date: 2026-07-31 14:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'k5e7a9c1d3f5'
down_revision: str | None = 'j4d6f8b0c2e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'weekly_digest_enabled',
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('weekly_digest_enabled')

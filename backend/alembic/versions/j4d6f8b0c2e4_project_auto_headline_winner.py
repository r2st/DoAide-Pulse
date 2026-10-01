"""projects.auto_headline_winner

Per-project opt-in to Pulse swapping in the best-performing past headline on
its own. Defaults to false for every existing row, and deliberately: a title
changing under the author without their say-so is startling, and the
measurement carries a known bias toward whichever headline was live when the
post launched (see app.services.headlines.pick_winner).

Revision ID: j4d6f8b0c2e4
Revises: i3c5e7a9c1e3
Create Date: 2026-07-31 13:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'j4d6f8b0c2e4'
down_revision: str | None = 'i3c5e7a9c1e3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'auto_headline_winner',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('auto_headline_winner')

"""publications.as_draft

``as_draft`` was already accepted by ``POST /content/{id}/publish`` and offered
as a checkbox in the editor, but there was nowhere to keep it: the flag was read
off the request and then dropped, so every publish went out live. It has to live
on the row rather than travel with the dispatch call, because a scheduled
publication is executed by a beat sweep that has nothing but the row.

Existing rows default to ``false``, which is what they in fact did.

Revision ID: b7e1c9a4d2f8
Revises: 0cfd3627b4c3
Create Date: 2026-07-29 23:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7e1c9a4d2f8'
down_revision: str | None = '0cfd3627b4c3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('publications', schema=None) as batch_op:
        # server_default backfills the existing rows; the model owns the default
        # from here on, so it is dropped again below.
        batch_op.add_column(
            sa.Column(
                'as_draft', sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    with op.batch_alter_table('publications', schema=None) as batch_op:
        batch_op.alter_column('as_draft', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('publications', schema=None) as batch_op:
        batch_op.drop_column('as_draft')

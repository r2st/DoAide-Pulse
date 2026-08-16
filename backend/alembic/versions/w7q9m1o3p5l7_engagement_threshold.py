"""projects.engagement_threshold, content.engagement_notified_at

The two halves of the ``content.engagement_threshold`` webhook: the number a
project considers interesting, and the latch that makes the event fire once per
piece rather than on every metrics sweep for the rest of that piece's life. See
:mod:`app.services.engagement_alerts`.

``engagement_threshold`` is ``0`` with a server default rather than nullable.
Zero means "never notify", which is the behaviour every existing row has today,
so the default backfills the fleet into exactly the state it is already in and
the column can be ``NOT NULL`` from the first moment. A nullable column would
have meant every reader carrying an ``or 0`` for a distinction — "no threshold"
versus "a threshold of none" — with no second meaning here. Same argument the
``autopilot_min_interval_hours`` migration makes, and the server default stays
on for the same reason: the seed script and a hand-run ``INSERT`` during an
incident do not name every column, and a project created without this one
should be quiet, not broken.

``engagement_notified_at`` *is* nullable, and the contrast is the point. Its
``NULL`` is not a default standing in for a value — it is the state "this has
not happened yet", which is exactly one of the two things the column exists to
distinguish. Backfilled as ``NULL`` across the board, which is correct: no piece
has been announced, because nothing could announce one until now.

Revision ID: w7q9m1o3p5l7
Revises: v6p8l0n2o4k6
Create Date: 2026-08-15 23:05:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'w7q9m1o3p5l7'
down_revision: str | None = 'v6p8l0n2o4k6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'engagement_threshold',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'engagement_notified_at',
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_column('engagement_notified_at')
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('engagement_threshold')

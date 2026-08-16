"""publications.duration_ms

How long the last attempt's call to the platform took. See
:class:`app.models.publication.Publication` for what is and is not inside that
measurement, and why it is recorded for a failed attempt as well as a
successful one.

Nullable, no server default, no backfill. NULL means "no attempt has reached a
platform yet", and every row that exists when this runs is in exactly that
position as far as the column can tell — the attempts they made were never
timed. A default of ``0`` would be a lie in the one direction that matters,
because zero is the fastest possible platform and the averages in
``/api/v1/metrics`` would report a live install's whole history as instant
until enough new rows outweighed it. ``app.services.ops_metrics.publish_rates``
counts the timed rows separately so the ones this revision leaves NULL are
visible as an absence rather than folded into the mean.

Revision ID: t4n6j8l0m2i4
Revises: s3m5i7k9l1h3
Create Date: 2026-08-15 22:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 't4n6j8l0m2i4'
down_revision: str | None = 's3m5i7k9l1h3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('publications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('duration_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('publications', schema=None) as batch_op:
        batch_op.drop_column('duration_ms')

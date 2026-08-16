"""projects.autopilot_min_interval_hours

How many hours a project must go between automated scans. See
:class:`app.models.project.Project` for why this is per project rather than the
one deployment-wide ``autopilot_scan_interval_seconds`` it sits beside, and
:func:`app.models.project.scan_due` for what the number is measured against.

``0`` with a server default, not NULL. Zero is the behaviour every existing row
already has — scanned on every sweep — so the default backfills the fleet into
exactly the state it is in, and the column can be ``NOT NULL`` from the first
moment without a data migration. A nullable column would have meant every
reader carrying a ``or 0``, for a distinction ("no interval" vs "an interval of
none") that has no second meaning here.

The server default stays on the column rather than being dropped after the
backfill. The write paths all send a value, but the seed script and a hand-run
``INSERT`` during an incident do not, and a project created without this column
should scan, not fail.

Revision ID: u5o7k9m1n3j5
Revises: t4n6j8l0m2i4
Create Date: 2026-08-15 21:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'u5o7k9m1n3j5'
down_revision: str | None = 't4n6j8l0m2i4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'autopilot_min_interval_hours',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('autopilot_min_interval_hours')

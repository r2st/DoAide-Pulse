"""content.version

Optimistic concurrency control for the editor. See ``Content.version`` for what
the column is for; this is only how it arrives on a table that already has rows.

``server_default='1'`` because the column is NOT NULL and every existing row
needs a value. Kept afterwards rather than dropped: SQLAlchemy supplies the
version itself on an ORM insert, but the seeder, a fixture, and any hand-written
INSERT do not, and a NOT NULL column with no default turns those into an error
for a field nobody outside the mapper is supposed to think about. Postgres would
also need a second ALTER to drop it, for no gain.

No backfill loop. Every pre-existing row starts at 1, which is the truth the
column is claiming — "this row has been written some number of times we did not
count, and here is the number we will count from". Nothing compares a version
across the migration boundary: a client holding a stale ``If-Match`` from before
the deploy has no version to hold, because the field did not exist in the
responses it read.

Revision ID: q1k3g5i7j9f1
Revises: p0j2f4h6i8e0
Create Date: 2026-08-14 16:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'q1k3g5i7j9f1'
down_revision: str | None = 'p0j2f4h6i8e0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('version', sa.Integer(), nullable=False, server_default='1')
        )


def downgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_column('version')

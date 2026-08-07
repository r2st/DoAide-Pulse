"""users.tokens_valid_from

The cutoff that makes a password reset end the sessions that were already open.
Herald's access tokens are stateless JWTs with no revocation list, so before
this a reset changed what the *next* sign-in needed and nothing else: somebody
resetting because their account had been taken left the attacker's bearer token
working for the rest of its lifetime.

Nullable with no default, deliberately. NULL means "the password has never been
changed on this row", and every token is accepted — which is the right reading
for rows that predate the column. Backfilling ``now()`` instead would sign every
existing session out on deploy to defend against something that has not
happened.

Revision ID: o9i1e3g5h7d9
Revises: n8h0d2f4g6c8
Create Date: 2026-08-07 07:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'o9i1e3g5h7d9'
down_revision: str | None = 'n8h0d2f4g6c8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('tokens_valid_from', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('tokens_valid_from')

"""publications.live_title — the headline the destination is actually showing

A headline swap changes ``content.title``. Until now nothing recorded whether
the destination was ever told. That mattered because
``app.services.headlines`` closes one attribution window and opens another on
the swap, and then credits every later engagement gain to the new headline —
including gains from a Bluesky post that has no title at all and a Dev.to
article whose retitle failed.

NULL means "published before this column existed". Attribution reads NULL as
"assume it tracks the piece's title", which is exactly how those rows were
already being counted, so there is no backfill and no behaviour change for
them. The guard that does the real work is the adapter capability
(``Adapter.supports_title_update``), which is a property of the platform and
needs no stored state.

No server default, for the opposite reason to
``platform_connections.language`` in x8r0n2p4q6m8: there is no one right value
for a row this column has never been written for. A default of ``''`` would be
indistinguishable from a piece genuinely published with an empty headline, and
a default naming the current title would assert a sync that never happened.

Revision ID: y9s1o3q5r7n9
Revises: x8r0n2p4q6m8
Create Date: 2026-08-21 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'y9s1o3q5r7n9'
down_revision: str | None = 'x8r0n2p4q6m8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'publications', sa.Column('live_title', sa.String(length=300), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('publications', 'live_title')

"""projects.utm_enabled, projects.utm_campaign

Attribution for the links published posts point at. Only Dev.to reports stats
back to Herald, so without tagging there is no way to answer "which platform
actually sent traffic" — with it, the project's own analytics answers it for
every platform at once.

``utm_enabled`` backfills to true, which changes behaviour for existing
projects: their next publish starts tagging share links and ``project_url``.
That is the intent, and it is confined to links Herald itself writes into a
post — ``canonical_url`` is deliberately never tagged (see app.services.utm).

``utm_campaign`` backfills to the empty string rather than the slug, because
empty *means* "use the slug" and computing it here would freeze today's slug
into every row.

Revision ID: e4b7c2d95a10
Revises: d8a2f10c7b93
Create Date: 2026-07-30 09:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e4b7c2d95a10'
down_revision: str | None = 'd8a2f10c7b93'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'utm_enabled', sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch_op.add_column(
            sa.Column(
                'utm_campaign',
                sa.String(length=120),
                nullable=False,
                server_default='',
            )
        )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('utm_enabled', server_default=None)
        batch_op.alter_column('utm_campaign', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('utm_campaign')
        batch_op.drop_column('utm_enabled')

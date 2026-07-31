"""unique slugs and performance indexes

Add unique constraints on (project_id, slug) for content and (user_id, slug)
for projects to prevent race conditions in slug generation. Add composite
indexes on (publication_id, captured_at) for content_metrics and (status,
scheduled_for) for publications to cover the most common query patterns.

Revision ID: h2b4d6f8a0c1
Revises: g1a3b5c7d9e2
Create Date: 2026-07-31 07:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'h2b4d6f8a0c1'
down_revision: str | None = 'g1a3b5c7d9e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Unique constraints ---
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_content_project_slug', ['project_id', 'slug']
        )

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_project_user_slug', ['user_id', 'slug']
        )

    # --- Performance indexes ---
    with op.batch_alter_table('content_metrics', schema=None) as batch_op:
        batch_op.create_index(
            'ix_content_metrics_pub_captured',
            ['publication_id', 'captured_at'],
        )

    with op.batch_alter_table('publications', schema=None) as batch_op:
        batch_op.create_index(
            'ix_publication_status_scheduled',
            ['status', 'scheduled_for'],
        )


def downgrade() -> None:
    with op.batch_alter_table('publications', schema=None) as batch_op:
        batch_op.drop_index('ix_publication_status_scheduled')

    with op.batch_alter_table('content_metrics', schema=None) as batch_op:
        batch_op.drop_index('ix_content_metrics_pub_captured')

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_constraint('uq_project_user_slug', type_='unique')

    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_constraint('uq_content_project_slug', type_='unique')

"""draft preview links

Shareable, expiring, read-only links to a single draft. Only a SHA-256 hash of
the token is stored, same reasoning as password_reset_tokens. Multi-use, so
there is a view_count/last_viewed_at pair instead of a used_at.

Revision ID: 5693038b5fe4
Revises: o9i1e3g5h7d9
Create Date: 2026-08-09 22:53:06.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '5693038b5fe4'
down_revision: str | None = 'o9i1e3g5h7d9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'preview_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('content_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('view_count', sa.Integer(), nullable=False),
        sa.Column('last_viewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['content_id'], ['content.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('preview_links', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_preview_links_content_id'), ['content_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_preview_links_token_hash'), ['token_hash'], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table('preview_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_preview_links_token_hash'))
        batch_op.drop_index(batch_op.f('ix_preview_links_content_id'))

    op.drop_table('preview_links')

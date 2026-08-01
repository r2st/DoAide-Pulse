"""content_templates

Reusable content shapes: a title and body carrying ``{{placeholders}}``, plus
the declared list of variables that fill them. Owned by the user rather than the
project, because the reason to write one is to use it more than once and a
per-project template would have to be copied to be reused.

Revision ID: n8h0d2f4g6c8
Revises: m7g9c1e3f5b7
Create Date: 2026-08-01 10:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'n8h0d2f4g6c8'
down_revision: str | None = 'm7g9c1e3f5b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'content_templates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('mode', sa.String(length=20), nullable=False, server_default='literal'),
        sa.Column(
            'content_type', sa.String(length=30), nullable=False, server_default='announcement'
        ),
        sa.Column('title_template', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('body_template', sa.Text(), nullable=False, server_default=''),
        sa.Column('variables', sa.JSON(), nullable=False),
        sa.Column('default_project_id', sa.Integer(), nullable=True),
        sa.Column('use_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        # SET NULL, not CASCADE: deleting a project should not delete the
        # author's writing. The default project is only the picker's default.
        sa.ForeignKeyConstraint(['default_project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        # Templates are chosen from a list by name, so two called "Weekly
        # changelog" is a usability bug the database can simply prevent.
        sa.UniqueConstraint('user_id', 'name', name='uq_template_user_name'),
    )
    op.create_index('ix_content_templates_user_id', 'content_templates', ['user_id'])
    op.create_index(
        'ix_template_user_updated', 'content_templates', ['user_id', 'updated_at']
    )


def downgrade() -> None:
    op.drop_index('ix_template_user_updated', table_name='content_templates')
    op.drop_index('ix_content_templates_user_id', table_name='content_templates')
    op.drop_table('content_templates')

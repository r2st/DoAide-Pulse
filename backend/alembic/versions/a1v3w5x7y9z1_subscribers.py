"""Add subscribers table for embed widget email collection

Revision ID: a1v3w5x7y9z1
Revises: z0t2p4r6s8o0
Create Date: 2026-10-09 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a1v3w5x7y9z1"
down_revision = "z0t2p4r6s8o0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscribers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("source", sa.String(100), nullable=False, server_default="embed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("email", name="uq_subscriber_email"),
    )


def downgrade() -> None:
    op.drop_table("subscribers")

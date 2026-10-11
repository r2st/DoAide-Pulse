"""content_metrics composite index for latest-metric lookup

The analytics subquery ``MAX(id) GROUP BY publication_id`` runs on every
dashboard and overview load. A composite index on (publication_id, id)
lets PostgreSQL satisfy the grouping with a backwards index-only scan
instead of scanning the full append-only table.

Revision ID: p0j2f4h6i8k0
Revises: o9i1e3g5h7d9
Create Date: 2026-10-02
"""
from alembic import op

revision = "p0j2f4h6i8k0"
down_revision = "o9i1e3g5h7d9"


def upgrade() -> None:
    op.create_index(
        "ix_content_metrics_pub_latest",
        "content_metrics",
        ["publication_id", "id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_content_metrics_pub_latest", table_name="content_metrics")

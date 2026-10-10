"""Add CHECK constraints to content_revisions and content_translations

Revision and word_count in content_revisions had no database-level guard against
negative or zero values.  content_translations.source_version likewise.  The
application layer prevented these, but a direct SQL write or a future code path
could have stored nonsense.

Revision ID: a1b2c3d4e5f7
Revises: z0t2p4r6s8o0
Create Date: 2026-10-11 01:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "z0t2p4r6s8o0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_revision_positive",
        "content_revisions",
        "revision > 0",
    )
    op.create_check_constraint(
        "ck_revision_word_count_nonneg",
        "content_revisions",
        "word_count >= 0",
    )
    op.create_check_constraint(
        "ck_translation_source_version_nonneg",
        "content_translations",
        "source_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_translation_source_version_nonneg", "content_translations", type_="check")
    op.drop_constraint("ck_revision_word_count_nonneg", "content_revisions", type_="check")
    op.drop_constraint("ck_revision_positive", "content_revisions", type_="check")

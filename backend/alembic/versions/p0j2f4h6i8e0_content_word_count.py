"""content.word_count

The word count was a Python property over ``body_markdown``, so every caller
that wanted it had to select the body. That is the whole article — up to
``BODY_MARKDOWN_MAX_LENGTH``, 200_000 characters — fetched to produce one small
integer, and the callers that want it want it in bulk: ``GET /content`` pages
500 rows, the review queue 200, and ``length_performance`` reads *every*
published piece the account has. Storing the count turns those into a narrow
column read.

Added with ``server_default='0'`` because the column is NOT NULL on a table with
rows in it, and the default is kept afterwards rather than dropped: it is the
right answer for an INSERT that omits the column (an empty body has no words),
it matches the model's own ``default=0``, and dropping it would need a second
ALTER on Postgres for no gain.

The backfill counts in Python rather than SQL on purpose. ``word_count_of`` is
``len(body.split())`` — splitting on runs of *any* whitespace and yielding
nothing for an empty string — and the SQL spelling of that ("length minus
length-without-spaces, plus one") disagrees with it on tabs, newlines, runs of
two spaces and the empty body, which is every article with a fenced code block
in it. A backfill that disagrees with the write path is worse than no backfill:
it is wrong quietly, and only for the old rows.

Chunked by id, one UPDATE per row within a chunk. Pulse's content table is
per-account and small; correctness of the count matters more here than the
number of round trips in a migration that runs once.

Revision ID: p0j2f4h6i8e0
Revises: 5693038b5fe4
Create Date: 2026-08-14 15:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'p0j2f4h6i8e0'
down_revision: str | None = '5693038b5fe4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows read per round trip during the backfill. Bounded so the migration's
#: memory does not scale with the table: a chunk holds this many article bodies.
_CHUNK = 500


def upgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'word_count', sa.Integer(), nullable=False, server_default='0'
            )
        )

    conn = op.get_bind()
    select_chunk = sa.text(
        'SELECT id, body_markdown FROM content '
        'WHERE id > :after ORDER BY id LIMIT :chunk'
    )
    set_count = sa.text('UPDATE content SET word_count = :count WHERE id = :id')

    after = 0
    while True:
        rows = conn.execute(
            select_chunk, {'after': after, 'chunk': _CHUNK}
        ).fetchall()
        if not rows:
            break
        for row_id, body in rows:
            conn.execute(
                set_count, {'count': len((body or '').split()), 'id': row_id}
            )
        after = rows[-1][0]


def downgrade() -> None:
    with op.batch_alter_table('content', schema=None) as batch_op:
        batch_op.drop_column('word_count')

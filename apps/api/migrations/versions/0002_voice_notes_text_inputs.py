"""voice_notes.media_url nullable: typed text inputs are stored as transcripts too (FR-2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("voice_notes", "media_url", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM voice_notes WHERE media_url IS NULL")
    op.alter_column("voice_notes", "media_url", existing_type=sa.Text(), nullable=False)

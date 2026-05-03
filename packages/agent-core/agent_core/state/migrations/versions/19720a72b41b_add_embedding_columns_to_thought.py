"""add embedding columns to thought

Revision ID: 19720a72b41b
Revises: 90861088948c
Create Date: 2026-05-02 23:35:02.788351

Adds two columns to support OpenBrain semantic memory:
  - thought.embedding         : JSON list[float], nullable (None until indexed)
  - thought.embedding_model   : VARCHAR, nullable, indexed

JSON column type maps to JSONB on Postgres and JSON-as-TEXT on SQLite — both
backends covered by the same migration without conditionals.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  — for sqlmodel.AutoString and friends in autogenerate

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "19720a72b41b"
down_revision: str | None = "90861088948c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "thought",
        sa.Column("embedding", sa.JSON(), nullable=True),
    )
    op.add_column(
        "thought",
        sa.Column("embedding_model", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_thought_embedding_model",
        "thought",
        ["embedding_model"],
    )


def downgrade() -> None:
    op.drop_index("ix_thought_embedding_model", table_name="thought")
    op.drop_column("thought", "embedding_model")
    op.drop_column("thought", "embedding")

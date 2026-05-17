"""add person table

Revision ID: fb856cb5fcf4
Revises: 19720a72b41b
Create Date: 2026-05-03

Adds the ``person`` table — relationship CRM with autonomy + privacy
implications. Lifted from Esby's installed-chief-of-staff ``people`` table
during the Sprint 13 migration; both products consume it.

Schema design:
  - String UUID PK (matches every other agent-core table; portable across
    SQLite + Postgres without sequence-juggling).
  - JSON columns (contact_methods, metadata_json) — JSONB on Postgres,
    JSON-as-TEXT on SQLite.
  - autonomy_override stored as VARCHAR via SAEnum(native_enum=False) so
    migrations stay portable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fb856cb5fcf4"
down_revision: str | None = "19720a72b41b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "person",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("organization", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column(
            "stakeholder_class",
            sa.String(),
            nullable=False,
            server_default="unknown_external",
        ),
        sa.Column(
            "autonomy_override",
            sa.String(length=32),
            nullable=False,
            server_default="inherit",
        ),
        sa.Column("relationship_intensity", sa.Integer(), nullable=True),
        sa.Column("response_sla", sa.String(), nullable=True),
        sa.Column(
            "never_autonomous_send",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "sensitive_memory_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("contact_methods", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("notes_path", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index("ix_person_name", "person", ["name"])
    op.create_index("ix_person_organization", "person", ["organization"])
    op.create_index("ix_person_stakeholder_class", "person", ["stakeholder_class"])
    op.create_index("ix_person_autonomy_override", "person", ["autonomy_override"])
    op.create_index("ix_person_created_at", "person", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_person_created_at", table_name="person")
    op.drop_index("ix_person_autonomy_override", table_name="person")
    op.drop_index("ix_person_stakeholder_class", table_name="person")
    op.drop_index("ix_person_organization", table_name="person")
    op.drop_index("ix_person_name", table_name="person")
    op.drop_table("person")

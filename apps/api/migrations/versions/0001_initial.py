"""Initial schema: pgvector extension and all PRD tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "trade": (
        "electrician",
        "plumber",
        "tailor",
        "driver",
        "beautician",
        "carpenter",
        "delivery",
        "other",
    ),
    "job_status": ("draft", "active", "hidden", "closed"),
    "application_status": ("interested", "shortlisted", "hired", "rejected"),
    "message_direction": ("inbound", "outbound"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _base_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    dim = get_settings().embedding_dim

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=False)

    op.create_table(
        "workers",
        *_base_columns(),
        sa.Column("wa_phone_hash", sa.String(64), nullable=False),
        sa.Column("wa_phone_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("language", sa.String(8), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workers"),
        sa.UniqueConstraint("wa_phone_hash", name="uq_workers_wa_phone_hash"),
    )

    op.create_table(
        "employers",
        *_base_columns(),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_employers"),
        sa.UniqueConstraint("email", name="uq_employers_email"),
    )

    op.create_table(
        "worker_profiles",
        *_base_columns(),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("profile_json", postgresql.JSONB(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("embedding", Vector(dim), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            ondelete="CASCADE",
            name="fk_worker_profiles_worker_id_workers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_worker_profiles"),
        sa.UniqueConstraint("worker_id", name="uq_worker_profiles_worker_id"),
    )

    op.create_table(
        "voice_notes",
        *_base_columns(),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("language", sa.String(8), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            ondelete="CASCADE",
            name="fk_voice_notes_worker_id_workers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_voice_notes"),
    )
    op.create_index("ix_voice_notes_worker_id", "voice_notes", ["worker_id"])

    op.create_table(
        "jobs",
        *_base_columns(),
        sa.Column("employer_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("trade", _enum("trade"), nullable=False),
        sa.Column(
            "skills", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("pay_min", sa.Integer(), nullable=False),
        sa.Column("pay_max", sa.Integer(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("openings", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", _enum("job_status"), server_default="draft", nullable=False),
        sa.Column("embedding", Vector(dim), nullable=True),
        sa.CheckConstraint("pay_min <= pay_max", name="ck_jobs_pay_range"),
        sa.CheckConstraint("openings >= 1", name="ck_jobs_openings_positive"),
        sa.ForeignKeyConstraint(
            ["employer_id"],
            ["employers.id"],
            ondelete="CASCADE",
            name="fk_jobs_employer_id_employers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_employer_id", "jobs", ["employer_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "matches",
        *_base_columns(),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_response", sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            ondelete="CASCADE",
            name="fk_matches_worker_id_workers",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], ondelete="CASCADE", name="fk_matches_job_id_jobs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_matches"),
        sa.UniqueConstraint("worker_id", "job_id", name="uq_matches_worker_id_job_id"),
    )
    op.create_index("ix_matches_job_id", "matches", ["job_id"])

    op.create_table(
        "applications",
        *_base_columns(),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status", _enum("application_status"), server_default="interested", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            ondelete="CASCADE",
            name="fk_applications_worker_id_workers",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], ondelete="CASCADE", name="fk_applications_job_id_jobs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_applications"),
        sa.UniqueConstraint("worker_id", "job_id", name="uq_applications_worker_id_job_id"),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])

    op.create_table(
        "courses",
        *_base_columns(),
        sa.Column("skill", sa.String(100), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_courses"),
    )
    op.create_index("ix_courses_skill", "courses", ["skill"])

    op.create_table(
        "messages",
        *_base_columns(),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column("direction", _enum("message_direction"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("wa_message_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["workers.id"],
            ondelete="CASCADE",
            name="fk_messages_worker_id_workers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("wa_message_id", name="uq_messages_wa_message_id"),
    )
    op.create_index("ix_messages_worker_id", "messages", ["worker_id"])

    op.create_table(
        "audit_log",
        *_base_columns(),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    for table in (
        "audit_log",
        "messages",
        "courses",
        "applications",
        "matches",
        "jobs",
        "voice_notes",
        "worker_profiles",
        "employers",
        "workers",
    ):
        op.drop_table(table)
    for name in reversed(ENUMS):
        op.execute(f"DROP TYPE IF EXISTS {name}")
    # The vector extension is left installed; other database objects may depend on it.

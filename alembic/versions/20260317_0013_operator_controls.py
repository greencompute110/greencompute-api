"""add operator control persistence

Revision ID: 20260317_0013
Revises: 20260317_0012
Create Date: 2026-03-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0013"
down_revision = "20260317_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("miners", sa.Column("drained", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("miners", "drained", server_default=None)

    op.add_column("placements", sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("placements", sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_placements_cooldown_until", "placements", ["cooldown_until"], unique=False)
    op.alter_column("placements", "failure_count", server_default=None)

    op.add_column("builds", sa.Column("retry_exhausted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("builds", "retry_exhausted", server_default=None)

    op.create_table(
        "build_attempts",
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("build_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_class", sa.String(length=128), nullable=True),
        sa.Column("last_operation", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index("ix_build_attempts_attempt", "build_attempts", ["attempt"], unique=False)
    op.create_index("ix_build_attempts_build_id", "build_attempts", ["build_id"], unique=False)
    op.create_index("ix_build_attempts_started_at", "build_attempts", ["started_at"], unique=False)
    op.create_index("ix_build_attempts_status", "build_attempts", ["status"], unique=False)

    op.create_table(
        "build_logs",
        sa.Column("log_id", sa.String(length=64), nullable=False),
        sa.Column("build_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("ix_build_logs_attempt", "build_logs", ["attempt"], unique=False)
    op.create_index("ix_build_logs_build_id", "build_logs", ["build_id"], unique=False)
    op.create_index("ix_build_logs_created_at", "build_logs", ["created_at"], unique=False)
    op.create_index("ix_build_logs_stage", "build_logs", ["stage"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_build_logs_stage", table_name="build_logs")
    op.drop_index("ix_build_logs_created_at", table_name="build_logs")
    op.drop_index("ix_build_logs_build_id", table_name="build_logs")
    op.drop_index("ix_build_logs_attempt", table_name="build_logs")
    op.drop_table("build_logs")

    op.drop_index("ix_build_attempts_status", table_name="build_attempts")
    op.drop_index("ix_build_attempts_started_at", table_name="build_attempts")
    op.drop_index("ix_build_attempts_build_id", table_name="build_attempts")
    op.drop_index("ix_build_attempts_attempt", table_name="build_attempts")
    op.drop_table("build_attempts")

    op.drop_column("builds", "retry_exhausted")
    op.drop_index("ix_placements_cooldown_until", table_name="placements")
    op.drop_column("placements", "cooldown_until")
    op.drop_column("placements", "failure_count")
    op.drop_column("miners", "drained")

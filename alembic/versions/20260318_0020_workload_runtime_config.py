"""add workload runtime config

Revision ID: 20260318_0020
Revises: 20260318_0019
Create Date: 2026-03-18 00:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0020"
down_revision = "20260318_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workloads",
        sa.Column(
            "runtime",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(
                """'{"runtime_kind":"hf-causal-lm","model_identifier":"sshleifer/tiny-gpt2"}'::json"""
            ),
        ),
    )
    op.alter_column("workloads", "runtime", server_default=None)


def downgrade() -> None:
    op.drop_column("workloads", "runtime")

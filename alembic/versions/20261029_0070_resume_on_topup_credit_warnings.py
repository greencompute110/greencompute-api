"""deployments.resume_on_topup + credit_warnings

- deployments.resume_on_topup: opt-in; a pod suspended for an empty balance
  is resumed by the gateway's credit watch once a top-up covers an hour of it
  again. Existing rows: false (nobody opted in).
- deployments.suspended_reason: why a deployment is suspended, for code
  ("balance", "idle", "resuming"); last_error is prose that every status
  report rewrites, so it can't be relied on. Existing suspended rows: set to
  "balance" where last_error still says so; most won't (the machine's own
  re-report used to blank it), so those stay NULL and only suspensions after
  this migration are eligible for auto-resume.
- credit_warnings: the low-credit email each customer last got (level 24 h or
  2 h), so each level is sent once per run-down.

Revision ID: 20261029_0070
Revises: 20261019_0069
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0070"
down_revision = "20261019_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("resume_on_topup", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("deployments", sa.Column("suspended_reason", sa.String(length=32), nullable=True))
    op.execute(sa.text(
        "UPDATE deployments SET suspended_reason = 'balance' "
        "WHERE state = 'suspended' AND last_error LIKE 'Insufficient balance%'"
    ))
    op.create_table(
        "credit_warnings",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("level_hours", sa.Integer(), nullable=True),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("credit_warnings")
    op.drop_column("deployments", "suspended_reason")
    op.drop_column("deployments", "resume_on_topup")

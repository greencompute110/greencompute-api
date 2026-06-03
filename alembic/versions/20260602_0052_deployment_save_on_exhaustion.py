"""deployments: save_on_exhaustion + suspended_at + storage_remainder_mcents

Credit-exhaustion pod lifecycle (opt-in "save my pod"). When a rental runs out
of credits it is SUSPENDED (pod-safe). What happens next is driven by these
three additive columns:

  • save_on_exhaustion  — the user's opt-in choice to preserve the pod and pay
                          a daily storage hold instead of having it deleted.
  • suspended_at        — when the pod last entered SUSPENDED; the clock for the
                          delete reaper and storage accrual. Left NULL for any
                          pod already suspended before this ships, which
                          grandfathers it out of the reaper (never auto-deleted).
  • storage_remainder_mcents — sub-cent storage-charge accumulator, mirrors
                          metering_remainder_mcents.

Purely additive with safe server defaults — existing rows are unaffected and
no destructive behaviour activates until the control-plane gates
(GREENCOMPUTE_POD_STORAGE_BILLING_ENABLED / _EXHAUSTION_REAPER_ENABLED) are
turned on.

Revision ID: 20260602_0052
Revises: 20260602_0051
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa


revision = "20260602_0052"
down_revision = "20260602_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column(
            "save_on_exhaustion",
            sa.Boolean(),
            nullable=False,
            # Default TRUE (opt-out): a customer's work is preserved by default.
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "deployments",
        sa.Column(
            "suspended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "deployments",
        sa.Column(
            "storage_remainder_mcents",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("deployments", "storage_remainder_mcents")
    op.drop_column("deployments", "suspended_at")
    op.drop_column("deployments", "save_on_exhaustion")

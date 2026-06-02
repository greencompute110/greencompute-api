"""stripe_sessions.payment_intent_id — refund/dispute join key (BILL-M1 Part B)

Adds an ADDITIVE, nullable `payment_intent_id` column (+ a non-unique lookup
index) to `stripe_sessions`. Stripe refund/dispute webhook events carry a
`payment_intent` / `charge`, NOT the checkout-session id, so we need this column
to map those events back to the originating top-up.

Strictly additive and fully reversible:
  - All EXISTING rows stay NULL (historical sessions predate the column;
    historical refunds are reconciled manually). No balances, no other column,
    no constraint on existing data are touched.
  - The column is nullable with no NOT NULL backfill, so the ALTER is a cheap
    metadata-only change. Only NEW checkout sessions populate it going forward.
  - downgrade() drops the index and the column, restoring the prior schema
    exactly.

The kind='chargeback' / kind='refund' ledger values used by Part B are already
free-form String(32) — no enum/constraint migration is needed — and refund
idempotency reuses the existing `uq_ledger_deposit_ref` index via
`deposit_ref = "stripe-refund:{event_id}"`, so no new constraint is added here.

Revision ID: 20260602_0051
Revises: 20260602_0050
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa


revision = "20260602_0051"
down_revision = "20260602_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stripe_sessions",
        sa.Column("payment_intent_id", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_stripe_sessions_payment_intent_id",
        "stripe_sessions",
        ["payment_intent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_stripe_sessions_payment_intent_id", table_name="stripe_sessions")
    op.drop_column("stripe_sessions", "payment_intent_id")

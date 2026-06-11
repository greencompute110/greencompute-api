"""inference_balance_holds — short-lived authorization holds for in-flight inference

Adds a NEW table `inference_balance_holds`. Each row is a reservation of a
single in-flight inference request's worst-case cost against the user's balance.
The pre-flight gate computes available balance as
``users.balance_credits - SUM(amount_cents of this user's non-expired holds)``
and reserves the request's estimate before dispatch, so N concurrent requests
from a near-zero balance can no longer ALL pass the old `balance > 0` check and
extract near-free bulk inference. The hold is deleted once the real per-token
charge settles; `expires_at` is a TTL backstop so a crashed/abandoned request
cannot reserve capacity forever.

Strictly additive: CREATE TABLE only — no existing table/column is altered.
Reversible: downgrade() drops only the new table (no FK, no orphans).

Revision ID: 20260611_0055
Revises: 20260604_0054
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa


revision = "20260611_0055"
down_revision = "20260604_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inference_balance_holds",
        sa.Column("reference_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inference_balance_holds_user_id", "inference_balance_holds", ["user_id"])
    op.create_index("ix_inference_balance_holds_expires_at", "inference_balance_holds", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_inference_balance_holds_expires_at", table_name="inference_balance_holds")
    op.drop_index("ix_inference_balance_holds_user_id", table_name="inference_balance_holds")
    op.drop_table("inference_balance_holds")

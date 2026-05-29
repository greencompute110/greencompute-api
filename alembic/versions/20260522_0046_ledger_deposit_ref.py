"""ledger_entries: add unique deposit_ref (idempotency key for crypto deposits)

Closes a double-credit exploit: the deposit watcher / admin-confirm path
deduped on invoice_id, so one on-chain transaction could credit multiple
same-amount invoices. deposit_ref records the globally-unique on-chain
transfer (currency:tx_hash:log_index, or chain:block:event_index) and is
UNIQUE, so a single transfer credits at most one invoice — enforced at the
DB layer, which also makes concurrent gateway replicas safe.

Multiple NULLs are allowed (non-deposit rows: usage / stripe / manual), so
the constraint only applies to real on-chain deposit credits.

Revision ID: 20260522_0046
Revises: 20260522_0045
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa


revision = "20260522_0046"
down_revision = "20260522_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column("deposit_ref", sa.String(300), nullable=True),
    )
    # Unique index on a nullable column: NULLs are distinct in both Postgres
    # and SQLite, so existing/legacy rows (all NULL) don't collide.
    op.create_index(
        "uq_ledger_deposit_ref",
        "ledger_entries",
        ["deposit_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_ledger_deposit_ref", table_name="ledger_entries")
    op.drop_column("ledger_entries", "deposit_ref")

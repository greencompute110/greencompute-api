"""crypto top-ups: unique invoice amounts, deposit log, verified wallets

Deposit addresses are shared, so the watcher decides whose payment it is from
the amount alone. Two users opening $20 invoices were both told to send exactly
20 USDC, and the first payment credited whoever opened first.

- crypto_invoices.amount_units: the exact amount to send, in millionths, made
  unique among pending invoices at the same address by a partial unique index.
  NULL for older invoices, which the index ignores.
- crypto_deposits: every transfer seen at a deposit address, credited or not,
  so an unmatched payment can be claimed by linking its wallet or assigned by
  an admin.
- verified_wallets: wallets whose owner proved control by signing a message;
  payments from them are credited to that owner. One owner per wallet.
- crypto_invoices.review_*: a user's request to have a payment checked by
  hand when it wasn't credited, with the transaction they reported.

Strictly additive, reversible.

Revision ID: 20261012_0062
Revises: 20260908_0061
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op

revision = "20261012_0062"
down_revision = "20260908_0061"
branch_labels = None
depends_on = None

PENDING_WITH_UNITS = "status = 'pending' AND amount_units IS NOT NULL"


def upgrade() -> None:
    op.add_column("crypto_invoices", sa.Column("amount_units", sa.BigInteger(), nullable=True))
    op.add_column("crypto_invoices", sa.Column("review_status", sa.String(16), nullable=True))
    op.add_column("crypto_invoices", sa.Column("review_note", sa.Text(), nullable=True))
    op.add_column(
        "crypto_invoices", sa.Column("review_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_crypto_invoices_review_status", "crypto_invoices", ["review_status"])
    op.create_index(
        "uq_crypto_invoices_pending_amount",
        "crypto_invoices",
        ["currency", "deposit_address", "amount_units"],
        unique=True,
        postgresql_where=sa.text(PENDING_WITH_UNITS),
        sqlite_where=sa.text(PENDING_WITH_UNITS),
    )

    op.create_table(
        "crypto_deposits",
        sa.Column("deposit_ref", sa.String(300), primary_key=True),
        sa.Column("currency", sa.String(32), nullable=False),
        sa.Column("tx_hash", sa.String(256), nullable=True),
        sa.Column("sender", sa.String(128), nullable=False),
        sa.Column("deposit_address", sa.String(256), nullable=False),
        sa.Column("amount_raw", sa.BigInteger(), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("usd_price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="unmatched"),
        sa.Column("matched_by", sa.String(32), nullable=True),
        sa.Column("invoice_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("credited_cents", sa.BigInteger(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_crypto_deposits_currency", "crypto_deposits", ["currency"])
    op.create_index("ix_crypto_deposits_tx_hash", "crypto_deposits", ["tx_hash"])
    op.create_index("ix_crypto_deposits_sender", "crypto_deposits", ["sender"])
    op.create_index("ix_crypto_deposits_status", "crypto_deposits", ["status"])
    op.create_index("ix_crypto_deposits_user_id", "crypto_deposits", ["user_id"])

    op.create_table(
        "verified_wallets",
        sa.Column("wallet_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("address", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_verified_wallets_user_id", "verified_wallets", ["user_id"])
    op.create_index(
        "uq_verified_wallets_chain_address", "verified_wallets", ["chain", "address"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_verified_wallets_chain_address", table_name="verified_wallets")
    op.drop_index("ix_verified_wallets_user_id", table_name="verified_wallets")
    op.drop_table("verified_wallets")
    for ix in ("user_id", "status", "sender", "tx_hash", "currency"):
        op.drop_index(f"ix_crypto_deposits_{ix}", table_name="crypto_deposits")
    op.drop_table("crypto_deposits")
    op.drop_index("uq_crypto_invoices_pending_amount", table_name="crypto_invoices")
    op.drop_index("ix_crypto_invoices_review_status", table_name="crypto_invoices")
    for column in ("review_requested_at", "review_note", "review_status", "amount_units"):
        op.drop_column("crypto_invoices", column)

"""managed_wallets / managed_wallet_payouts — custodied provider miner wallets

Adds TWO new tables. Touches nothing existing, so this is inert for current
traffic and fully reversible.

WHY: the subnet was criticised for concentrating emissions. The live cause was
that `miner_whitelist` held exactly ONE hotkey (the team's own fleet key
5FmpATto…), so the validator resolved one uid and set 100% of weight to it every
epoch — on-chain, indistinguishable from deliberately hoarding emissions. Real
distribution needs every provider to hold their own hotkey, neuron and uid.

Providers are GPU operators, not Bittensor users, so the platform creates and
custodies the keypair on their behalf: they supply a payout address, fund the
coldkey for the registration burn, and we forward their alpha emissions to them.

`*_mnemonic_enc` are AES-256-GCM ciphertext (see infrastructure/secretbox.py),
bound to their wallet_id and role via AAD. They are Text rather than a fixed
width because the v1 envelope carries a scheme prefix and base64 nonce, and a
future KMS-wrapped scheme will be longer.

`managed_wallet_payouts.extrinsic_hash` is UNIQUE on purpose: it is the
idempotency key that stops a retried payout worker paying a provider twice for
the same accrual. NULL is allowed because the row is written before submission —
Postgres treats NULLs as distinct, so multiple in-flight rows coexist and only
confirmed ones contend.

Revision ID: 20260908_0060
Revises: 20260801_0059
Create Date: 2026-09-08
"""
import sqlalchemy as sa
from alembic import op

revision = "20260908_0060"
down_revision = "20260801_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_wallets",
        sa.Column("wallet_id", sa.String(64), primary_key=True),
        sa.Column("application_id", sa.String(64), nullable=False),
        sa.Column("coldkey_ss58", sa.String(128), nullable=False),
        sa.Column("hotkey_ss58", sa.String(128), nullable=False),
        sa.Column("coldkey_mnemonic_enc", sa.Text(), nullable=False),
        sa.Column("hotkey_mnemonic_enc", sa.Text(), nullable=False),
        sa.Column("payout_address", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False, server_default="110"),
        sa.Column("required_funding_tao", sa.Float(), nullable=False, server_default="0"),
        sa.Column("funded_tao", sa.Float(), nullable=False, server_default="0"),
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.Column("total_paid_alpha", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_payout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_managed_wallets_application_id", "managed_wallets", ["application_id"])
    op.create_index("ix_managed_wallets_state", "managed_wallets", ["state"])
    op.create_index(
        "ix_managed_wallets_coldkey_ss58", "managed_wallets", ["coldkey_ss58"], unique=True
    )
    op.create_index(
        "ix_managed_wallets_hotkey_ss58", "managed_wallets", ["hotkey_ss58"], unique=True
    )

    op.create_table(
        "managed_wallet_payouts",
        sa.Column("payout_id", sa.String(64), primary_key=True),
        sa.Column("wallet_id", sa.String(64), nullable=False),
        sa.Column("destination", sa.String(128), nullable=False),
        sa.Column("alpha_amount", sa.Float(), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False, server_default="110"),
        sa.Column("extrinsic_hash", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_managed_wallet_payouts_wallet_id", "managed_wallet_payouts", ["wallet_id"]
    )
    op.create_index("ix_managed_wallet_payouts_status", "managed_wallet_payouts", ["status"])
    op.create_index(
        "ix_managed_wallet_payouts_extrinsic_hash",
        "managed_wallet_payouts",
        ["extrinsic_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_managed_wallet_payouts_extrinsic_hash", "managed_wallet_payouts")
    op.drop_index("ix_managed_wallet_payouts_status", "managed_wallet_payouts")
    op.drop_index("ix_managed_wallet_payouts_wallet_id", "managed_wallet_payouts")
    op.drop_table("managed_wallet_payouts")
    op.drop_index("ix_managed_wallets_hotkey_ss58", "managed_wallets")
    op.drop_index("ix_managed_wallets_coldkey_ss58", "managed_wallets")
    op.drop_index("ix_managed_wallets_state", "managed_wallets")
    op.drop_index("ix_managed_wallets_application_id", "managed_wallets")
    op.drop_table("managed_wallets")

"""gamma payments: gamma_spent_totals, gamma_spends

Customers pay by spending SN110 gamma at our service hotkey (Bittensor's
"Subnet services" standard). The chain records what each hotkey spent there,
GammaSpent(netuid, service, hotkey); the gateway's gamma watcher reads it at
the finalized head and credits the growth.

- gamma_spent_totals: the last total read per (netuid, service, hotkey), the
  subnet's RegisteredSubnetCounter it was read under, and the cents credited
  for it so far (so fractions of a cent carry over to the next spend).
- gamma_spends: one row per growth: customer payments (credited, also
  logged in crypto_deposits with currency "gamma"), treasury buybacks (never
  credited) and spends from sanctioned wallets (blocked).

New tables only: nothing existing is locked or rewritten.

Never downgrade this on a database that has accepted gamma: the downgrade
refuses while gamma_spent_totals has rows, because without them every
recorded spend would be credited again.

Revision ID: 20261124_0083
Revises: 20261118_0082
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_0083"
down_revision = "20261118_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gamma_spent_totals",
        sa.Column("netuid", sa.Integer(), primary_key=True),
        sa.Column("service_hotkey", sa.String(64), primary_key=True),
        sa.Column("hotkey", sa.String(64), primary_key=True),
        sa.Column("subnet_counter", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_raw", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("credited_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_block", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gamma_spent_totals_user_id", "gamma_spent_totals", ["user_id"])
    op.create_table(
        "gamma_spends",
        sa.Column("spend_id", sa.String(300), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("subnet_counter", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("service_hotkey", sa.String(64), nullable=False),
        sa.Column("hotkey", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount_raw", sa.BigInteger(), nullable=False),
        sa.Column("total_raw", sa.BigInteger(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("credited_cents", sa.BigInteger(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gamma_spends_hotkey", "gamma_spends", ["hotkey"])
    op.create_index("ix_gamma_spends_kind", "gamma_spends", ["kind"])
    op.create_index("ix_gamma_spends_block_number", "gamma_spends", ["block_number"])
    op.create_index("ix_gamma_spends_user_id", "gamma_spends", ["user_id"])
    op.create_index("ix_gamma_spends_observed_at", "gamma_spends", ["observed_at"])


def downgrade() -> None:
    # gamma_spent_totals is what stops a spend being credited twice: without
    # it the watcher would credit every hotkey's whole on-chain total again
    # at its next spend. So this refuses once anything has been recorded.
    bind = op.get_bind()
    if bind.execute(sa.text("select count(*) from gamma_spent_totals")).scalar():
        raise RuntimeError(
            "gamma_spent_totals has rows: dropping it would let recorded gamma spends be credited again. "
            "Don't downgrade past 20261124_0083 on a database that has accepted gamma."
        )
    op.drop_table("gamma_spends")
    op.drop_table("gamma_spent_totals")

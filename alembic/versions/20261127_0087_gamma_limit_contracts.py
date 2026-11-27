"""gamma_spends.waiting_cents; contracts, contract_payments

- gamma_spends.waiting_cents: the part of a customer's gamma spend that is
  waiting for room under the weekly gamma limit. NOT NULL with a constant
  default: catalog-only on Postgres, and the table is tiny.
- gamma_spends.coldkey: the deposit's coldkey at the time of the spend, so
  waiting gamma can be screened again before it is credited.
- contracts, contract_payments: bare-metal contracts staff enter, and the
  money received on them (the "contract" revenue stream of the report).

Downgrading is refused while any gamma is waiting: dropping the column would
forget credit customers are still owed.

Revision ID: 20261127_0087
Revises: 20261124_0086
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "20261127_0087"
down_revision = "20261124_0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("gamma_spends", sa.Column("waiting_cents", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("gamma_spends", sa.Column("coldkey", sa.String(64), nullable=True))
    op.create_table(
        "contracts",
        sa.Column("contract_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("farmer_id", sa.String(64), nullable=True),
        sa.Column("gpu_model", sa.String(64), nullable=False, server_default=""),
        sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("monthly_cents", sa.BigInteger(), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_contracts_farmer_id", "contracts", ["farmer_id"])
    op.create_table(
        "contract_payments",
        sa.Column("payment_id", sa.String(64), primary_key=True),
        sa.Column("contract_id", sa.String(64), nullable=False),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("reference", sa.String(256), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_contract_payments_contract_id", "contract_payments", ["contract_id"])
    op.create_index("ix_contract_payments_received_on", "contract_payments", ["received_on"])


def downgrade() -> None:
    waiting = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM gamma_spends WHERE waiting_cents > 0")).scalar()
    if waiting:
        raise RuntimeError(
            f"{waiting} gamma spend(s) are waiting for the weekly limit: credit them (raise or remove the limit) "
            "before downgrading, or their credit would be forgotten"
        )
    op.drop_index("ix_contract_payments_received_on", table_name="contract_payments")
    op.drop_index("ix_contract_payments_contract_id", table_name="contract_payments")
    op.drop_table("contract_payments")
    op.drop_index("ix_contracts_farmer_id", table_name="contracts")
    op.drop_table("contracts")
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    with op.batch_alter_table("gamma_spends") as batch:
        batch.drop_column("coldkey")
        batch.drop_column("waiting_cents")

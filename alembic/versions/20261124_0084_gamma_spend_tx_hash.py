"""gamma_spends.tx_hash

The spend_gamma extrinsic of a gamma payment, recorded when the watcher finds
the spend's GammaSpent event in the blocks since its last read (it is also
written to crypto_deposits.tx_hash, where the "this transaction was already
credited" check for staff-confirmed invoices looks).

A nullable column with no default: catalog-only on Postgres, on a table made
one migration ago.

Revision ID: 20261124_0084
Revises: 20261124_0083
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_0084"
down_revision = "20261124_0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("gamma_spends", sa.Column("tx_hash", sa.String(128), nullable=True))
    op.create_index("ix_gamma_spends_tx_hash", "gamma_spends", ["tx_hash"])


def downgrade() -> None:
    op.drop_index("ix_gamma_spends_tx_hash", table_name="gamma_spends")
    with op.batch_alter_table("gamma_spends") as batch:
        batch.drop_column("tx_hash")

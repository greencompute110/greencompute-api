"""farmers, node_farmers, farmer_payments: the public network report

The report const asked for (GPUs by type and by farmer, revenue, buybacks)
needs to know whose machine each node is: every node reports the one miner
hotkey, so staff assign machines to farmers.

- farmers: a provider, its public label ("Farmer A") and contract terms.
- node_farmers: node_id -> farmer, and a flag to leave a machine out.
- farmer_payments: USD paid to a farmer for a month's statement.

New tables only: nothing existing is locked or rewritten.

Revision ID: 20261124_0085
Revises: 20261124_0084
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_0085"
down_revision = "20261124_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "farmers",
        sa.Column("farmer_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("public_label", sa.String(64), nullable=False, unique=True),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "node_farmers",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("farmer_id", sa.String(64), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_node_farmers_farmer_id", "node_farmers", ["farmer_id"])
    op.create_table(
        "farmer_payments",
        sa.Column("payment_id", sa.String(64), primary_key=True),
        sa.Column("farmer_id", sa.String(64), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference", sa.String(256), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_farmer_payments_farmer_id", "farmer_payments", ["farmer_id"])
    op.create_index("ix_farmer_payments_month", "farmer_payments", ["month"])


def downgrade() -> None:
    op.drop_table("farmer_payments")
    op.drop_table("node_farmers")
    op.drop_table("farmers")

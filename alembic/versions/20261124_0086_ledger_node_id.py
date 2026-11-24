"""ledger_entries.node_id

The machine a rental charge was for, written by the meter. A rental can be
moved to another machine; a farmer's monthly statement has to follow the
machine that ran each charge, not the one the rental is on today.

A nullable column with no default: catalog-only on Postgres, no rewrite of
the ledger. It needs the table's lock for an instant, and the meter writes to
this table every minute, so it gives up after 5 s rather than queue (run the
migration again if it times out). Old rows stay NULL: the report falls back
to the rental's current machine for them.

Revision ID: 20261124_0086
Revises: 20261124_0085
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "20261124_0086"
down_revision = "20261124_0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("ledger_entries", sa.Column("node_id", sa.String(128), nullable=True))


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_column("node_id")

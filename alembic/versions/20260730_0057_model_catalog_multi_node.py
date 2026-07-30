"""model_catalog.multi_node — persist the distributed-serving topology

Adds ONE nullable JSON column to `model_catalog`. NULL means "ordinary
single-node model", which is every existing row.

Why: `ModelCatalogEntry.multi_node` existed on the protocol model and was
accepted by POST /validator/v1/catalog, but `upsert_catalog_entry` had no column
to write it to — so the topology was silently DROPPED on save and the
distributed reconciler never saw any distributed model to place. Caught by
end-to-end testing on the fleet, not by unit tests (which exercised the pydantic
model directly and never round-tripped through the DB).

Strictly additive: one nullable column, no backfill. Reversible.

Revision ID: 20260730_0057
Revises: 20260729_0056
Create Date: 2026-07-30
"""
import sqlalchemy as sa
from alembic import op

revision = "20260730_0057"
down_revision = "20260729_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_catalog",
        sa.Column("multi_node", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_catalog", "multi_node")

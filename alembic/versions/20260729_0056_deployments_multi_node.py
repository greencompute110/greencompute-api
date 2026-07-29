"""deployments.multi_node — distributed-replica membership

Adds ONE nullable JSON column to `deployments`. NULL means "ordinary
single-node deployment", which is every existing row, so the change is inert
for all current traffic.

When populated, the row is one RANK of a model served across several nodes as a
single engine (a model too large for one chassis, e.g. a trillion-parameter
MoE). The payload carries the rank's role and the coordinates it needs to join
the replica:

    {"replica_id": "kimi-k3-r1", "role": "head"|"worker", "rank": 0,
     "node_count": 8, "gpus_per_node": 8,
     "tensor_parallel_size": 8, "pipeline_parallel_size": 8,
     "head_host": "10.0.0.1"}

The node-agent reads it to decide whether to start a Ray head and serve, or to
join the head and donate GPUs.

Strictly additive: one nullable column, no backfill, no existing column
altered. Reversible: downgrade drops only the new column.

Revision ID: 20260729_0056
Revises: 20260611_0055
Create Date: 2026-07-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20260729_0056"
down_revision = "20260611_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("multi_node", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployments", "multi_node")

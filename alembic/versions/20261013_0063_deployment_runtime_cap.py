"""deployments: ready_at and max_runtime_minutes

- ready_at: when the deployment first became ready, which is when billing
  starts. Lets clients see exactly how long they have been paying.
- max_runtime_minutes: optional automatic stop that many minutes after
  ready_at, so a crashed or forgotten client can't keep a rental billing
  until its balance runs out. NULL means no limit (every existing row).

Strictly additive, reversible.

Revision ID: 20261013_0063
Revises: 20261012_0062
Create Date: 2026-10-13
"""
import sqlalchemy as sa
from alembic import op

revision = "20261013_0063"
down_revision = "20261012_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deployments", sa.Column("max_runtime_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "max_runtime_minutes")
    op.drop_column("deployments", "ready_at")

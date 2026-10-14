"""api_keys: per-key limits; deployments.created_by_key_id

- api_keys.max_active_rentals, api_keys.daily_spend_limit_cents: optional
  limits so a leaked or runaway key can't drain an account. NULL (every
  existing key) means no limit.
- deployments.created_by_key_id: which key started the rental, so that key's
  spending can be added up and its limits applied.

Strictly additive, reversible.

Revision ID: 20261013_0064
Revises: 20261013_0063
Create Date: 2026-10-13
"""
import sqlalchemy as sa
from alembic import op

revision = "20261013_0064"
down_revision = "20261013_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("max_active_rentals", sa.Integer(), nullable=True))
    op.add_column("api_keys", sa.Column("daily_spend_limit_cents", sa.BigInteger(), nullable=True))
    op.add_column("deployments", sa.Column("created_by_key_id", sa.String(64), nullable=True))
    op.create_index("ix_deployments_created_by_key_id", "deployments", ["created_by_key_id"])


def downgrade() -> None:
    op.drop_index("ix_deployments_created_by_key_id", table_name="deployments")
    op.drop_column("deployments", "created_by_key_id")
    op.drop_column("api_keys", "daily_spend_limit_cents")
    op.drop_column("api_keys", "max_active_rentals")

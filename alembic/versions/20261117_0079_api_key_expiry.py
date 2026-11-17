"""api key expiry

- api_keys.expires_at: when the key stops working (401 api_key_expired).
  NULL, as on every key made so far: it never expires.

One nullable column with no default: on Postgres a catalog-only change, no
table rewrite, safe on the live database.

Revision ID: 20261117_0079
Revises: 20261116_0078
Create Date: 2026-11-17
"""
import sqlalchemy as sa
from alembic import op

revision = "20261117_0079"
down_revision = "20261116_0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # batch_alter_table so the drop also works on SQLite (no DROP COLUMN
    # before 3.35); on Postgres it issues a plain ALTER TABLE.
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("expires_at")

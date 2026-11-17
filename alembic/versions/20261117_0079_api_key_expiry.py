"""api key expiry

- api_keys.expires_at: when the key stops working (401 api_key_expired).
  NULL, as on every key made so far: it never expires.

One nullable column with no default: on Postgres a catalog-only change, no
table rewrite, safe on the live database, with a 5 s lock_timeout
so it can't queue behind a long transaction.

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


def _short_lock_wait() -> None:
    """On Postgres, give up after 5 s rather than queue for the table's lock
    behind a long transaction: a queued ALTER blocks every request that needs
    the table after it. Retry the migration if it times out. SET LOCAL lasts
    until the migration transaction (alembic/env.py runs one) ends."""
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _short_lock_wait()
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    _short_lock_wait()
    # batch_alter_table so the drop also works on SQLite (no DROP COLUMN
    # before 3.35); on Postgres it issues a plain ALTER TABLE.
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("expires_at")

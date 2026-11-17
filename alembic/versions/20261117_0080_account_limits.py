"""account-wide limits

- users.daily_spend_limit_cents: what the whole account (every key
  together) may spend in any 24 hours; at it, new rentals and paid inference
  are refused and running rentals are paused (suspended).
- users.max_active_rentals: rentals the account may have running at once.

Both NULL, as for every account so far: no limit. Nullable columns with no
default: on Postgres catalog-only changes, no table rewrite, safe on the live
database, with a 5 s lock_timeout so it can't queue behind a long transaction.

Revision ID: 20261117_0080
Revises: 20261117_0079
Create Date: 2026-11-17
"""
import sqlalchemy as sa
from alembic import op

revision = "20261117_0080"
down_revision = "20261117_0079"
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
    op.add_column("users", sa.Column("daily_spend_limit_cents", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("max_active_rentals", sa.Integer(), nullable=True))


def downgrade() -> None:
    _short_lock_wait()
    # batch_alter_table so the drops also work on SQLite (no DROP COLUMN
    # before 3.35); on Postgres it issues plain ALTER TABLE statements.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("max_active_rentals")
        batch.drop_column("daily_spend_limit_cents")

"""fleets.deleted_at: DELETE /platform/fleets/{id} removes a fleet

DELETE stopped a fleet but left it listed (and readable, and deletable
again) for a week. Now it stops it and sets deleted_at: lists leave it out
and GET/PATCH/DELETE by id answer 404. A fleet is only ever `stopped` by that
DELETE, so the fleets stopped so far are marked deleted too (at the time they
were stopped).

A nullable column with no default: a catalog-only change on Postgres, with a
5 s lock_timeout so it can't queue behind a long transaction; the backfill
touches only the (few) stopped fleets.

Revision ID: 20261118_0081
Revises: 20261117_0080
Create Date: 2026-11-18
"""
import sqlalchemy as sa
from alembic import op

revision = "20261118_0081"
down_revision = "20261117_0080"
branch_labels = None
depends_on = None


def _short_lock_wait() -> None:
    """On Postgres, give up after 5 s rather than queue for the table's lock
    behind a long transaction (see 0080). SET LOCAL lasts until the migration
    transaction ends."""
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _short_lock_wait()
    op.add_column("fleets", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE fleets SET deleted_at = updated_at WHERE state = 'stopped' AND deleted_at IS NULL")


def downgrade() -> None:
    _short_lock_wait()
    with op.batch_alter_table("fleets") as batch:
        batch.drop_column("deleted_at")

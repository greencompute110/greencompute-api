"""deployments: billable_since

Private models are now charged by the hour while they run, like rentals. The
hourly meter never charges for time before billable_since: it is stamped each
time a deployment becomes ready, so a private model that was already running
when this shipped is charged from now on, never from its ready_at.

- Adds the nullable column.
- Sets it to the migration time on every deployment that is ready right now
  (the running rentals and private models), so the billing monitor doesn't
  report the running private models as "never charged" before the meter's
  first pass. Rows in any other state get it when they next become ready.

Downgrade drops the column.

Revision ID: 20261019_0069
Revises: 20261017_0068
Create Date: 2026-10-19
"""
import sqlalchemy as sa

from alembic import op

revision = "20261019_0069"
down_revision = "20261017_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("billable_since", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE deployments SET billable_since = CURRENT_TIMESTAMP WHERE state = 'ready'"))


def downgrade() -> None:
    op.drop_column("deployments", "billable_since")

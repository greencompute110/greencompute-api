"""deployments.hourly_rate_cents — per-GPU-per-hour rate locked at placement

Stores the cents/GPU/hour rate on the deployment row at placement time so
that the per-minute metering loop doesn't have to rely on a lookup against
a shared rate table (which would retroactively change the rate on active
rentals if we ever edited the code constants).

Default is 10 cents = $0.10/hr — matches the legacy hardcoded behaviour
so existing active rentals keep their current rate when this column is
back-filled via the DEFAULT.

Revision ID: 20260421_0035
Revises: 20260421_0034
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa


revision = "20260421_0035"
down_revision = "20260421_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column(
            "hourly_rate_cents",
            sa.Integer(),
            nullable=False,
            server_default="10",
        ),
    )


def downgrade() -> None:
    op.drop_column("deployments", "hourly_rate_cents")

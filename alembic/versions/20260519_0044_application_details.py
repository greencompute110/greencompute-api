"""green_energy_applications: add `details` JSON column

The provider/data-center application form expanded substantially — full
business + DC addresses, accreditations, infrastructure, support,
environment, node specs. Rather than add a column per field (and migrate
every time sales tweaks the form), we store the whole structured payload
as JSONB.

Revision ID: 20260519_0044
Revises: 20260515_0043
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa


revision = "20260519_0044"
down_revision = "20260515_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "green_energy_applications",
        sa.Column(
            "details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("green_energy_applications", "details")

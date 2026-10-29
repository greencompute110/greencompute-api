"""deployments.name

A renter's name for a deployment (letters, digits, "." "_" "-", up to 48),
passed to a pod as GC_DEPLOYMENT_NAME, e.g. as a mining pool's worker name.
Existing rows: NULL.

Revision ID: 20261029_0072
Revises: 20261029_0071
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0072"
down_revision = "20261029_0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("name", sa.String(length=48), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "name")

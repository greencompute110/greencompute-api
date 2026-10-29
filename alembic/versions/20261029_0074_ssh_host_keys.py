"""deployments.ssh_host_keys

A pod's SSH host public keys ("type base64"), sent by the machine when the pod
becomes ready, so renters can check the host key instead of turning checking
off when a host port is reused. Existing rows: NULL (no keys known).

Revision ID: 20261029_0074
Revises: 20261029_0073
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0074"
down_revision = "20261029_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("ssh_host_keys", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "ssh_host_keys")

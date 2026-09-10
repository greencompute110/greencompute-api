"""green_energy_applications.payout_address — managed-wallet applicants

Adds ONE nullable column and relaxes `hotkey` to allow the empty string.

Why: the managed-wallet flow lets a provider apply WITHOUT a Bittensor wallet.
They supply the address where their alpha should be sent; the platform creates
and custodies the coldkey/hotkey and registers the neuron for them. Until that
registration lands there IS no hotkey, so `hotkey` is empty for these rows and
`payout_address` identifies the applicant instead.

Both flows coexist deliberately. Existing self-custody applicants keep a hotkey
and a NULL payout_address, so this migration is inert for every current row.

Strictly additive, reversible.

Revision ID: 20260908_0061
Revises: 20260908_0060
Create Date: 2026-09-08
"""
import sqlalchemy as sa
from alembic import op

revision = "20260908_0061"
down_revision = "20260908_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "green_energy_applications",
        sa.Column("payout_address", sa.String(128), nullable=True),
    )
    # Managed-wallet rows carry no hotkey until their neuron registers.
    op.alter_column(
        "green_energy_applications",
        "hotkey",
        existing_type=sa.String(128),
        nullable=False,
        server_default="",
    )


def downgrade() -> None:
    op.alter_column(
        "green_energy_applications",
        "hotkey",
        existing_type=sa.String(128),
        nullable=False,
        server_default=None,
    )
    op.drop_column("green_energy_applications", "payout_address")

"""admin_accounts: admins by account instead of a shared admin key

Accounts listed here get admin access on the website through a managed,
per-person admin key. Strictly additive, reversible.

Revision ID: 20261017_0066
Revises: 20261014_0065
Create Date: 2026-10-17
"""
import sqlalchemy as sa

from alembic import op

revision = "20261017_0066"
down_revision = "20261014_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_accounts",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("added_by", sa.String(128), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("admin_accounts")

"""provider_servers — self-service provider server onboarding

Tracks a provider's physical servers onboarded via the UI (SSH auto-
provisioning of a miner node-agent). No SSH credential is stored; the
provisioning job uses it transiently and discards it.

Revision ID: 20260529_0047
Revises: 20260522_0046
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa


revision = "20260529_0047"
down_revision = "20260522_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_servers",
        sa.Column("server_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=True),
        sa.Column("hotkey", sa.String(128), server_default=""),
        sa.Column("payout_address", sa.String(256), server_default=""),
        sa.Column("label", sa.String(64), server_default=""),
        sa.Column("ssh_host", sa.String(255), server_default=""),
        sa.Column("ssh_port", sa.Integer(), server_default="22"),
        sa.Column("ssh_user", sa.String(64), server_default="root"),
        sa.Column("node_id", sa.String(128), server_default=""),
        sa.Column("status", sa.String(32), server_default="pending"),
        sa.Column("gpu_model", sa.String(64), server_default=""),
        sa.Column("gpu_count", sa.Integer(), server_default="0"),
        sa.Column("vram_gb_per_gpu", sa.Integer(), server_default="0"),
        sa.Column("cpu_cores", sa.Integer(), server_default="0"),
        sa.Column("memory_gb", sa.Integer(), server_default="0"),
        sa.Column("public_ip", sa.String(64), server_default=""),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provision_log", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_provider_servers_owner", "provider_servers", ["owner_user_id"])
    op.create_index("ix_provider_servers_hotkey", "provider_servers", ["hotkey"])
    op.create_index("ix_provider_servers_status", "provider_servers", ["status"])
    op.create_index("ix_provider_servers_created", "provider_servers", ["created_at"])


def downgrade() -> None:
    for ix in ("created", "status", "hotkey", "owner"):
        op.drop_index(f"ix_provider_servers_{ix}", table_name="provider_servers")
    op.drop_table("provider_servers")

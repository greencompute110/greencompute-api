"""fleets + fleet_members ("keep N running")

A fleet is a target count of rentals from one workload; the gateway's fleet
loop starts members until it is met (and, with `replace`, starts a new one
whenever a member stops). fleet_members maps each deployment it started to
its slot (the NN of the prefix-NN name).

Revision ID: 20261029_0073
Revises: 20261029_0072
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0073"
down_revision = "20261029_0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fleets",
        sa.Column("fleet_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_key_id", sa.String(length=64), nullable=True),
        sa.Column("workload_id", sa.String(length=64), nullable=False),
        sa.Column("name_prefix", sa.String(length=44), nullable=False),
        sa.Column("target", sa.Integer(), nullable=False),
        sa.Column("replace", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("max_runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("save_on_exhaustion", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resume_on_topup", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failures_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_fleets_owner_user_id", "fleets", ["owner_user_id"])
    op.create_index("ix_fleets_state", "fleets", ["state"])
    op.create_table(
        "fleet_members",
        sa.Column("deployment_id", sa.String(length=64), primary_key=True),
        sa.Column("fleet_id", sa.String(length=64), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ran", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_fleet_members_fleet_id", "fleet_members", ["fleet_id"])


def downgrade() -> None:
    op.drop_index("ix_fleet_members_fleet_id", table_name="fleet_members")
    op.drop_table("fleet_members")
    op.drop_index("ix_fleets_state", table_name="fleets")
    op.drop_index("ix_fleets_owner_user_id", table_name="fleets")
    op.drop_table("fleets")

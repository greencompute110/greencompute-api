"""initial persistence schema

Revision ID: 20260317_0001
Revises:
Create Date: 2026-03-17 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miners",
        sa.Column("hotkey", sa.String(length=128), primary_key=True),
        sa.Column("payout_address", sa.String(length=256), nullable=False),
        sa.Column("api_base_url", sa.String(length=512), nullable=False),
        sa.Column("validator_url", sa.String(length=512), nullable=False),
        sa.Column("supported_workload_kinds", sa.JSON(), nullable=False),
    )
    op.create_table(
        "heartbeats",
        sa.Column("hotkey", sa.String(length=128), primary_key=True),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("active_deployments", sa.Integer(), nullable=False),
        sa.Column("active_leases", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "capacities",
        sa.Column("hotkey", sa.String(length=128), primary_key=True),
        sa.Column("nodes", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "workloads",
        sa.Column("workload_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("image", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("security_tier", sa.String(length=32), nullable=False),
        sa.Column("pricing_class", sa.String(length=32), nullable=False),
        sa.Column("requirements", sa.JSON(), nullable=False),
        sa.Column("public", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "deployments",
        sa.Column("deployment_id", sa.String(length=64), primary_key=True),
        sa.Column("workload_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=True),
        sa.Column("node_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("requested_instances", sa.Integer(), nullable=False),
        sa.Column("ready_instances", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deployments_hotkey", "deployments", ["hotkey"])
    op.create_index("ix_deployments_state", "deployments", ["state"])
    op.create_index("ix_deployments_workload_id", "deployments", ["workload_id"])
    op.create_table(
        "lease_assignments",
        sa.Column("assignment_id", sa.String(length=64), primary_key=True),
        sa.Column("deployment_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("workload_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
    )
    op.create_index("ix_lease_assignments_hotkey", "lease_assignments", ["hotkey"])
    op.create_index("ix_lease_assignments_workload_id", "lease_assignments", ["workload_id"])
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("workload_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("compute_seconds", sa.Float(), nullable=False),
        sa.Column("latency_ms_p95", sa.Float(), nullable=False),
        sa.Column("occupancy_seconds", sa.Float(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_records_deployment_id", "usage_records", ["deployment_id"])
    op.create_index("ix_usage_records_hotkey", "usage_records", ["hotkey"])
    op.create_index("ix_usage_records_workload_id", "usage_records", ["workload_id"])
    op.create_table(
        "deployment_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deployment_events_deployment_id", "deployment_events", ["deployment_id"])
    op.create_table(
        "builds",
        sa.Column("build_id", sa.String(length=64), primary_key=True),
        sa.Column("image", sa.String(length=512), nullable=False),
        sa.Column("context_uri", sa.String(length=1024), nullable=False),
        sa.Column("dockerfile_path", sa.String(length=256), nullable=False),
        sa.Column("public", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("artifact_uri", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_builds_image", "builds", ["image"])
    op.create_index("ix_builds_status", "builds", ["status"])
    op.create_table(
        "validator_capabilities",
        sa.Column("hotkey", sa.String(length=128), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "probe_challenges",
        sa.Column("challenge_id", sa.String(length=64), primary_key=True),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_probe_challenges_hotkey", "probe_challenges", ["hotkey"])
    op.create_table(
        "probe_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("throughput", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("benchmark_signature", sa.String(length=256), nullable=True),
        sa.Column("proxy_suspected", sa.Boolean(), nullable=False),
        sa.Column("readiness_failures", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_probe_results_challenge_id", "probe_results", ["challenge_id"])
    op.create_index("ix_probe_results_hotkey", "probe_results", ["hotkey"])
    op.create_table(
        "scorecards",
        sa.Column("hotkey", sa.String(length=128), primary_key=True),
        sa.Column("capacity_weight", sa.Float(), nullable=False),
        sa.Column("reliability_score", sa.Float(), nullable=False),
        sa.Column("performance_score", sa.Float(), nullable=False),
        sa.Column("security_score", sa.Float(), nullable=False),
        sa.Column("fraud_penalty", sa.Float(), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_scorecards_final_score", "scorecards", ["final_score"])
    op.create_table(
        "weight_snapshots",
        sa.Column("snapshot_id", sa.String(length=64), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_weight_snapshots_netuid", "weight_snapshots", ["netuid"])


def downgrade() -> None:
    op.drop_index("ix_weight_snapshots_netuid", table_name="weight_snapshots")
    op.drop_table("weight_snapshots")
    op.drop_index("ix_scorecards_final_score", table_name="scorecards")
    op.drop_table("scorecards")
    op.drop_index("ix_probe_results_hotkey", table_name="probe_results")
    op.drop_index("ix_probe_results_challenge_id", table_name="probe_results")
    op.drop_table("probe_results")
    op.drop_index("ix_probe_challenges_hotkey", table_name="probe_challenges")
    op.drop_table("probe_challenges")
    op.drop_table("validator_capabilities")
    op.drop_index("ix_builds_status", table_name="builds")
    op.drop_index("ix_builds_image", table_name="builds")
    op.drop_table("builds")
    op.drop_index("ix_deployment_events_deployment_id", table_name="deployment_events")
    op.drop_table("deployment_events")
    op.drop_index("ix_usage_records_workload_id", table_name="usage_records")
    op.drop_index("ix_usage_records_hotkey", table_name="usage_records")
    op.drop_index("ix_usage_records_deployment_id", table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_index("ix_lease_assignments_workload_id", table_name="lease_assignments")
    op.drop_index("ix_lease_assignments_hotkey", table_name="lease_assignments")
    op.drop_table("lease_assignments")
    op.drop_index("ix_deployments_workload_id", table_name="deployments")
    op.drop_index("ix_deployments_state", table_name="deployments")
    op.drop_index("ix_deployments_hotkey", table_name="deployments")
    op.drop_table("deployments")
    op.drop_table("workloads")
    op.drop_table("capacities")
    op.drop_table("heartbeats")
    op.drop_table("miners")


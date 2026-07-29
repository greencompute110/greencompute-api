from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MinerORM(Base):
    __tablename__ = "miners"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    payout_address: Mapped[str] = mapped_column(String(256))
    api_base_url: Mapped[str] = mapped_column(String(512))
    validator_url: Mapped[str] = mapped_column(String(512))
    auth_secret: Mapped[str] = mapped_column(String(255))
    drained: Mapped[bool] = mapped_column(Boolean, default=False)
    supported_workload_kinds: Mapped[list[str]] = mapped_column(JSON)


class UserORM(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    balance_credits: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class APIKeyORM(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    scopes: Mapped[list[str]] = mapped_column(JSON)
    secret: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HeartbeatORM(Base):
    __tablename__ = "heartbeats"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    active_deployments: Mapped[int] = mapped_column(Integer, default=0)
    active_leases: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CapacityORM(Base):
    __tablename__ = "capacities"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServerORM(Base):
    __tablename__ = "servers"

    server_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    validator_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NodeInventoryORM(Base):
    __tablename__ = "node_inventory"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    server_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CapacityHistoryORM(Base):
    __tablename__ = "capacity_history"

    history_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    server_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    available_gpus: Mapped[int] = mapped_column(Integer)
    total_gpus: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WorkloadORM(Base):
    __tablename__ = "workloads"

    workload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    image: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    readme: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    workload_alias: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    ingress_host: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    security_tier: Mapped[str] = mapped_column(String(32))
    pricing_class: Mapped[str] = mapped_column(String(32))
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON)
    lifecycle: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeploymentORM(Base):
    __tablename__ = "deployments"

    deployment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    hotkey: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    requested_instances: Mapped[int] = mapped_column(Integer)
    ready_instances: Mapped[int] = mapped_column(Integer, default=0)
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ssh_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    port_mappings: Mapped[dict[int, int]] = mapped_column(JSON, default=dict)
    hourly_rate_cents: Mapped[int] = mapped_column(Integer, default=10)
    metering_remainder_mcents: Mapped[int] = mapped_column(Integer, default=0)
    # Credit-exhaustion lifecycle (opt-in "save my pod").
    # save_on_exhaustion: preserve this pod (storage-billed) when credits run
    #   out instead of deleting it shortly after. Defaults TRUE (opt-OUT) so a
    #   customer's work is saved by default; they can untick to let it delete.
    # suspended_at: when the pod last entered SUSPENDED — the clock for the
    #   delete reaper and storage accrual. NULL on pre-existing suspended pods,
    #   which grandfathers them out of the reaper entirely.
    # storage_remainder_mcents: sub-cent storage-charge accumulator, mirrors
    #   metering_remainder_mcents.
    save_on_exhaustion: Mapped[bool] = mapped_column(Boolean, default=True)
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    storage_remainder_mcents: Mapped[int] = mapped_column(Integer, default=0)
    deployment_fee_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fee_acknowledged: Mapped[bool] = mapped_column(Boolean, default=True)
    warmup_state: Mapped[str] = mapped_column(String(32), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_retry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    health_check_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Distributed-replica membership. NULL for every ordinary single-node
    # deployment. When set, this row is ONE RANK of a model served across
    # several nodes as a single engine:
    #   {"replica_id", "role": "head"|"worker", "rank", "node_count",
    #    "gpus_per_node", "tensor_parallel_size", "pipeline_parallel_size",
    #    "head_host"}
    # The node-agent reads it to decide whether to bring up a Ray head + serve,
    # or join the head and donate GPUs (see multinode_launch.py).
    multi_node: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaseAssignmentORM(Base):
    __tablename__ = "lease_assignments"

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="assigned")


class GpuReservationORM(Base):
    """Authoritative GPU-reservation ledger (ORCH-C2).

    A reservation is the scheduler's own record that a deployment holds a
    block of GPUs on a (hotkey, node). It is a SEPARATE source of truth from
    the miner-reported `CapacityORM.available_gpus`, so a stale/clobbering
    heartbeat can never silently free GPUs that the control-plane has
    committed. Effective availability is derived on read as the stricter of
    (physically-reported, total − sum(active reservations)).

    One active reservation per deployment (UNIQUE deployment_id). Releasing a
    reservation sets `status='released'` and stamps `released_at` rather than
    deleting the row, preserving an audit trail. Nothing here touches leases,
    deployment state, balances, or node-agent reconcile.
    """

    __tablename__ = "gpu_reservations"

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    gpu_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_gpu_reservations_hotkey_node_status", "hotkey", "node_id", "status"),
    )


class PlacementORM(Base):
    __tablename__ = "placements"

    placement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(64), index=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    server_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="assigned")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaseHistoryORM(Base):
    __tablename__ = "lease_history"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(64), index=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UsageRecordORM(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(String(64), index=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    request_count: Mapped[int] = mapped_column(Integer, default=1)
    streamed_request_count: Mapped[int] = mapped_column(Integer, default=0)
    stream_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    compute_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms_p95: Mapped[float] = mapped_column(Float, default=0.0)
    occupancy_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvocationRecordORM(Base):
    __tablename__ = "invocation_records"

    invocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    deployment_id: Mapped[str] = mapped_column(String(64), index=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    model: Mapped[str] = mapped_column(String(255), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    routed_host: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    resolution_basis: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    routing_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="succeeded")
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSecretORM(Base):
    __tablename__ = "user_secrets"

    secret_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkloadShareORM(Base):
    __tablename__ = "workload_shares"

    share_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    shared_with_user_id: Mapped[str] = mapped_column(String(64), index=True)
    permission: Mapped[str] = mapped_column(String(32), default="invoke")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeploymentEventORM(Base):
    __tablename__ = "deployment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BuildORM(Base):
    __tablename__ = "builds"

    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    image: Mapped[str] = mapped_column(String(512), index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    context_uri: Mapped[str] = mapped_column(String(1024))
    dockerfile_path: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    readme: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), index=True)
    registry_repository: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    registry_manifest_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    build_log_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    executor_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    build_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_operation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cleanup_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BuildContextORM(Base):
    __tablename__ = "build_contexts"

    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_uri: Mapped[str] = mapped_column(String(1024))
    normalized_context_uri: Mapped[str] = mapped_column(String(1024))
    dockerfile_path: Mapped[str] = mapped_column(String(256))
    dockerfile_object_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    context_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    staged_context_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    context_manifest_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BuildEventORM(Base):
    __tablename__ = "build_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BuildAttemptORM(Base):
    __tablename__ = "build_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="accepted")
    restarted_from_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restarted_from_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    restart_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_operation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BuildJobORM(Base):
    __tablename__ = "build_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    current_stage: Mapped[str] = mapped_column(String(64), index=True, default="accepted")
    last_completed_stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stage_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    restarted_from_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restarted_from_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    restart_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    executor_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    last_recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BuildJobCheckpointORM(Base):
    __tablename__ = "build_job_checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    build_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, index=True)
    stage: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    recovered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BuildLogORM(Base):
    __tablename__ = "build_logs"

    log_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, index=True)
    stage: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ValidatorCapabilityORM(Base):
    __tablename__ = "validator_capabilities"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ProbeChallengeORM(Base):
    __tablename__ = "probe_challenges"

    challenge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProbeResultORM(Base):
    __tablename__ = "probe_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    challenge_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    latency_ms: Mapped[float] = mapped_column(Float)
    throughput: Mapped[float] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    benchmark_signature: Mapped[str | None] = mapped_column(String(256), nullable=True)
    proxy_suspected: Mapped[bool] = mapped_column(Boolean, default=False)
    readiness_failures: Mapped[int] = mapped_column(Integer, default=0)
    # A.5 audit-trail columns: digests of the exact prompt sent + response
    # received, so auditors can spot miners serving canned / proxied output.
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScoreCardORM(Base):
    __tablename__ = "scorecards"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    capacity_weight: Mapped[float] = mapped_column(Float)
    reliability_score: Mapped[float] = mapped_column(Float)
    performance_score: Mapped[float] = mapped_column(Float)
    security_score: Mapped[float] = mapped_column(Float)
    fraud_penalty: Mapped[float] = mapped_column(Float)
    utilization_score: Mapped[float] = mapped_column(Float, default=1.0)
    rental_revenue_bonus: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScoreCardHistoryORM(Base):
    """Append-only history of scorecards, one row per (hotkey, epoch) pair.

    ScoreCardORM keeps 'latest wins' for fast queries; this table is what
    auditors need to re-derive the weights we published each epoch."""

    __tablename__ = "scorecard_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epoch_id: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    capacity_weight: Mapped[float] = mapped_column(Float)
    reliability_score: Mapped[float] = mapped_column(Float)
    performance_score: Mapped[float] = mapped_column(Float)
    security_score: Mapped[float] = mapped_column(Float)
    fraud_penalty: Mapped[float] = mapped_column(Float)
    utilization_score: Mapped[float] = mapped_column(Float, default=1.0)
    rental_revenue_bonus: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditReportORM(Base):
    """Per-epoch audit report — the canonical artifact that independent
    auditors (greencompute-audit) fetch, SHA256-check against the on-chain
    Commitments hash, and use to replay our scoring math."""

    __tablename__ = "audit_reports"

    epoch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    netuid: Mapped[int] = mapped_column(Integer, index=True)
    epoch_start_block: Mapped[int] = mapped_column(Integer)
    epoch_end_block: Mapped[int] = mapped_column(Integer, index=True)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    report_sha256: Mapped[str] = mapped_column(String(64), index=True)
    signature: Mapped[str] = mapped_column(Text, default="")
    signer_hotkey: Mapped[str] = mapped_column(String(128), default="")
    chain_commitment_tx: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WeightSnapshotORM(Base):
    __tablename__ = "weight_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    netuid: Mapped[int] = mapped_column(Integer, index=True)
    weights: Mapped[dict[str, float]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowEventORM(Base):
    __tablename__ = "workflow_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BusDeliveryORM(Base):
    __tablename__ = "bus_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    consumer: Mapped[str] = mapped_column(String(128), index=True)
    subject: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Miner whitelist ---


class MinerWhitelistORM(Base):
    __tablename__ = "miner_whitelist"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    energy_source: Mapped[str] = mapped_column(String(128), default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Green-energy applications ---


class GreenEnergyApplicationORM(Base):
    __tablename__ = "green_energy_applications"

    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    signature: Mapped[str] = mapped_column(Text, default="")
    organization: Mapped[str] = mapped_column(String(255), default="")
    energy_source: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full structured form payload — business + DC address, accreditations,
    # renewable energy mix, infrastructure, support, environment, node specs.
    # Schema-less JSON so sales can iterate on the form without a migration.
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GreenEnergyAttachmentORM(Base):
    __tablename__ = "green_energy_attachments"

    attachment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    data_b64: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelCatalogORM(Base):
    """Admin-approved model that the inference pool hosts."""

    __tablename__ = "model_catalog"

    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    hf_repo: Mapped[str] = mapped_column(String(255), default="")
    template: Mapped[str] = mapped_column(String(32), default="vllm")
    min_vram_gb_per_gpu: Mapped[int] = mapped_column(Integer, default=24)
    gpu_count: Mapped[int] = mapped_column(Integer, default=1)
    max_model_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), default="public", index=True)
    min_replicas: Mapped[int] = mapped_column(Integer, default=1)
    max_replicas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_hotkey: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CatalogSubmissionORM(Base):
    """Miner-proposed catalog addition; admin approves/rejects."""

    __tablename__ = "model_catalog_submissions"

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(128), index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True, default="")
    signature: Mapped[str] = mapped_column(Text, default="")
    display_name: Mapped[str] = mapped_column(String(255), default="")
    hf_repo: Mapped[str] = mapped_column(String(255), default="")
    template: Mapped[str] = mapped_column(String(32), default="vllm")
    min_vram_gb_per_gpu: Mapped[int] = mapped_column(Integer, default=24)
    gpu_count: Mapped[int] = mapped_column(Integer, default=1)
    max_model_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InferenceDemandStatsORM(Base):
    """Per-minute per-model invocation stats. Keyed by (model_id, window_start)
    where window_start is the minute truncation of the event timestamp."""

    __tablename__ = "inference_demand_stats"

    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    invocations: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens_sum: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens_sum: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms_sum: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MinerPayoutAccrualORM(Base):
    """One row per billable inference request. Captures what the miner
    earned for serving the call; aggregation + on-chain payout is a later
    admin job."""

    __tablename__ = "miner_payout_accrual"

    accrual_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    deployment_id: Mapped[str] = mapped_column(String(64), index=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cents_earned: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# --- Billing ---


class LedgerEntryORM(Base):
    __tablename__ = "ledger_entries"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Globally-unique on-chain deposit reference for crypto top-ups, e.g.
    # "usdt-eth:0xabc…:7" (currency:tx_hash:log_index) or "tao:5123456:3"
    # (chain:block:event_index). UNIQUE so a single on-chain transfer can
    # credit at most ONE invoice, ever — this is the idempotency key that
    # stops the "create N same-amount invoices, pay once, get credited N
    # times" exploit. NULL for non-deposit ledger rows (usage, stripe,
    # manual adjustments); SQLite + Postgres both treat multiple NULLs as
    # distinct, so the unique constraint only bites real deposit refs.
    deposit_ref: Mapped[str | None] = mapped_column(String(300), nullable=True, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InferenceBalanceHoldORM(Base):
    """Short-lived authorization hold against a user's balance for one in-flight
    inference request. The pre-flight gate reserves the request's worst-case
    cost here; the gate's available balance is ``balance_credits - SUM(active
    holds)``, so N concurrent requests from a near-zero balance can't all pass
    (each must fit under the running reservation). The hold is released after
    the real charge settles; ``expires_at`` is a TTL backstop so a crashed /
    abandoned request can't reserve capacity forever. Keyed by request_id."""

    __tablename__ = "inference_balance_holds"

    reference_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # = request_id
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CryptoInvoiceORM(Base):
    __tablename__ = "crypto_invoices"

    invoice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    currency: Mapped[str] = mapped_column(String(32))
    amount_crypto: Mapped[float] = mapped_column(Float)
    amount_usd: Mapped[float] = mapped_column(Float)
    bonus_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_credits: Mapped[int] = mapped_column(BigInteger)
    deposit_address: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StripeSessionORM(Base):
    __tablename__ = "stripe_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    stripe_session_id: Mapped[str] = mapped_column(String(256), unique=True)
    amount_usd: Mapped[float] = mapped_column(Float)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # Stripe PaymentIntent id (e.g. "pi_…") for this checkout session, used to
    # join refund/dispute webhook events (which carry payment_intent / charge,
    # not the checkout session id) back to the originating top-up (BILL-M1
    # Part B). Nullable + non-unique: historical sessions predate this column
    # and stay NULL; only new checkouts populate it going forward.
    payment_intent_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- Flux orchestrator ---


class FluxStateORM(Base):
    __tablename__ = "flux_states"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    total_gpus: Mapped[int] = mapped_column(Integer)
    inference_gpus: Mapped[int] = mapped_column(Integer, default=0)
    rental_gpus: Mapped[int] = mapped_column(Integer, default=0)
    idle_gpus: Mapped[int] = mapped_column(Integer, default=0)
    inference_floor_pct: Mapped[float] = mapped_column(Float, default=0.20)
    rental_floor_pct: Mapped[float] = mapped_column(Float, default=0.10)
    inference_demand_score: Mapped[float] = mapped_column(Float, default=0.0)
    rental_demand_score: Mapped[float] = mapped_column(Float, default=0.0)
    last_rebalanced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FluxRebalanceEventORM(Base):
    __tablename__ = "flux_rebalance_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(String(128))
    gpu_index: Mapped[int] = mapped_column(Integer)
    from_mode: Mapped[str] = mapped_column(String(32))
    to_mode: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RentalWaitQueueORM(Base):
    __tablename__ = "rental_wait_queue"

    deployment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    position: Mapped[int] = mapped_column(Integer)
    estimated_wait_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Bittensor chain integration ---


class MetagraphEntryORM(Base):
    __tablename__ = "metagraph_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    netuid: Mapped[int] = mapped_column(Integer, index=True)
    uid: Mapped[int] = mapped_column(Integer, index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True)
    coldkey: Mapped[str] = mapped_column(String(128))
    stake: Mapped[float] = mapped_column(Float, default=0.0)
    trust: Mapped[float] = mapped_column(Float, default=0.0)
    incentive: Mapped[float] = mapped_column(Float, default=0.0)
    emission: Mapped[float] = mapped_column(Float, default=0.0)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChainWeightCommitORM(Base):
    __tablename__ = "chain_weight_commits"

    commit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    netuid: Mapped[int] = mapped_column(Integer, index=True)
    tx_hash: Mapped[str] = mapped_column(String(128))
    uids: Mapped[list[int]] = mapped_column(JSON)
    weights: Mapped[list[float]] = mapped_column(JSON)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BareMetalInquiryORM(Base):
    """Inbound lead from the dedicated bare-metal node CTA on /rental."""

    __tablename__ = "bare_metal_inquiries"

    inquiry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), index=True)
    company: Mapped[str] = mapped_column(String(255), default="")
    card_type: Mapped[str] = mapped_column(String(32), default="")
    node_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_vram_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_gb_per_node: Mapped[int | None] = mapped_column(Integer, nullable=True)
    work_type: Mapped[str] = mapped_column(String(64), default="")
    deployment_date: Mapped[str] = mapped_column(String(64), default="")
    duration: Mapped[str] = mapped_column(String(128), default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GpuCapacityOverrideORM(Base):
    """Admin-controlled override for the public capacity surface.

    One row per gpu_model. When present, replaces measured counts in the
    /platform/v1/gpu-pool response. Deleting the row falls back to live
    measurement.
    """

    __tablename__ = "gpu_capacity_overrides"

    gpu_model: Mapped[str] = mapped_column(String(64), primary_key=True)
    total_gpus: Mapped[int] = mapped_column(Integer, default=0)
    available_gpus: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(255), default="")
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommercialInquiryORM(Base):
    """Inbound lead from the public /contact-sales form.

    Stored append-only — sales triages by editing `status` + `notes`. No
    PII is normalized into separate columns; everything the prospect typed
    stays in the row so we don't lose context when a CRM import happens.
    """

    __tablename__ = "commercial_inquiries"

    inquiry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), index=True)
    company: Mapped[str] = mapped_column(String(255), default="")
    gpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[str] = mapped_column(String(128), default="")
    deployment_date: Mapped[str] = mapped_column(String(64), default="")
    budget: Mapped[str] = mapped_column(String(128), default="")
    use_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KeyValueStateORM(Base):
    """Generic key-value state, used by the on-chain deposit watcher to
    track per-chain `last_scanned_block` (e.g. key="watcher:eth:last_block",
    value="20500123"). Reusable for future watcher-like cron state."""

    __tablename__ = "key_value_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class ProviderServerORM(Base):
    """A provider's physical server onboarded via the self-service UI.

    No SSH credential is stored — provisioning uses it transiently then
    discards it. `provision_log` is append-only progress text shown live.
    """

    __tablename__ = "provider_servers"

    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    hotkey: Mapped[str] = mapped_column(String(128), index=True, default="")
    payout_address: Mapped[str] = mapped_column(String(256), default="")
    label: Mapped[str] = mapped_column(String(64), default="")
    ssh_host: Mapped[str] = mapped_column(String(255), default="")
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    node_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    gpu_model: Mapped[str] = mapped_column(String(64), default="")
    gpu_count: Mapped[int] = mapped_column(Integer, default=0)
    vram_gb_per_gpu: Mapped[int] = mapped_column(Integer, default=0)
    cpu_cores: Mapped[int] = mapped_column(Integer, default=0)
    memory_gb: Mapped[int] = mapped_column(Integer, default=0)
    public_ip: Mapped[str] = mapped_column(String(64), default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provision_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

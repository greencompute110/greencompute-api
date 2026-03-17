from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
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
    supported_workload_kinds: Mapped[list[str]] = mapped_column(JSON)


class UserORM(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    name: Mapped[str] = mapped_column(String(100))
    image: Mapped[str] = mapped_column(String(512))
    workload_alias: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    ingress_host: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    security_tier: Mapped[str] = mapped_column(String(32))
    pricing_class: Mapped[str] = mapped_column(String(32))
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeploymentORM(Base):
    __tablename__ = "deployments"

    deployment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workload_id: Mapped[str] = mapped_column(String(64), index=True)
    hotkey: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    requested_instances: Mapped[int] = mapped_column(Integer)
    ready_instances: Mapped[int] = mapped_column(Integer, default=0)
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_retry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    health_check_failures: Mapped[int] = mapped_column(Integer, default=0)
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
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="succeeded")
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
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
    context_uri: Mapped[str] = mapped_column(String(1024))
    dockerfile_path: Mapped[str] = mapped_column(String(256))
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
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScoreCardORM(Base):
    __tablename__ = "scorecards"

    hotkey: Mapped[str] = mapped_column(String(128), primary_key=True)
    capacity_weight: Mapped[float] = mapped_column(Float)
    reliability_score: Mapped[float] = mapped_column(Float)
    performance_score: Mapped[float] = mapped_column(Float)
    security_score: Mapped[float] = mapped_column(Float)
    fraud_penalty: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float] = mapped_column(Float, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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

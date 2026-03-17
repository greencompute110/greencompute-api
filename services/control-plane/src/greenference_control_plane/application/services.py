from __future__ import annotations

from typing import Any
from datetime import UTC, datetime, timedelta

from greenference_persistence import SubjectBus, WorkflowEventRepository, create_subject_bus, get_metrics_store
from greenference_persistence.runtime import load_runtime_settings
from greenference_protocol import (
    CapacityUpdate,
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    Heartbeat,
    InvocationRecord,
    LeaseAssignment,
    MinerRegistration,
    UsageRecord,
    WorkloadSpec,
)
from greenference_control_plane.config import settings
from greenference_control_plane.domain.metering import UsageAggregator
from greenference_control_plane.domain.scheduler import PlacementPolicy
from greenference_control_plane.domain.state import transition_state
from greenference_control_plane.infrastructure.repository import ControlPlaneRepository


class ControlPlaneService:
    def __init__(
        self,
        repository: ControlPlaneRepository | None = None,
        workflow_repository: WorkflowEventRepository | None = None,
        bus: SubjectBus | None = None,
    ) -> None:
        self.repository = repository or ControlPlaneRepository()
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.repository.engine,
            session_factory=self.repository.session_factory,
        )
        runtime_settings = load_runtime_settings("greenference-control-plane")
        self.bus = bus or create_subject_bus(
            engine=self.workflow_repository.engine,
            session_factory=self.workflow_repository.session_factory,
            workflow_repository=self.workflow_repository,
            nats_url=runtime_settings.nats_url,
            transport=runtime_settings.bus_transport,
        )
        self.placement_policy = PlacementPolicy()
        self.usage_aggregator = UsageAggregator()
        self.metrics = get_metrics_store("greenference-control-plane")

    def register_miner(self, registration: MinerRegistration) -> MinerRegistration:
        return self.repository.upsert_miner(registration)

    def record_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat:
        return self.repository.upsert_heartbeat(heartbeat)

    def update_capacity(self, update: CapacityUpdate) -> CapacityUpdate:
        return self.repository.upsert_capacity(update)

    def list_servers(self):
        return self.repository.list_servers()

    def list_nodes(self):
        return self.repository.list_nodes()

    def list_capacity_history(self, limit: int | None = None):
        return self.repository.list_capacity_history(limit=limit)

    def list_placements(self, limit: int | None = None):
        return self.repository.list_placements(limit=limit)

    def upsert_workload(self, workload: WorkloadSpec) -> WorkloadSpec:
        return self.repository.upsert_workload(workload)

    def list_workloads(self) -> list[WorkloadSpec]:
        return self.repository.list_workloads()

    def find_workload_by_name(self, name: str) -> WorkloadSpec | None:
        return self.repository.find_workload_by_name(name)

    def create_deployment(self, request: DeploymentCreateRequest | dict) -> DeploymentRecord:
        payload = request if isinstance(request, DeploymentCreateRequest) else DeploymentCreateRequest(**request)
        workload = self.repository.get_workload(payload.workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {payload.workload_id}")
        deployment = DeploymentRecord(
            workload_id=payload.workload_id,
            requested_instances=payload.requested_instances,
        )
        self.repository.create_deployment(deployment)
        self.bus.publish(
            "deployment.requested",
            {
                "deployment_id": deployment.deployment_id,
                "workload_id": deployment.workload_id,
                "requested_instances": deployment.requested_instances,
            },
        )
        self.metrics.increment("deployment.requested")
        return deployment

    def _assign_lease(self, workload: WorkloadSpec, deployment_id: str) -> LeaseAssignment | None:
        nodes = []
        for node in self.repository.list_nodes():
            heartbeat = self.repository.get_heartbeat(node.hotkey)
            if heartbeat and not heartbeat.healthy:
                continue
            nodes.append(node)
        assignment = self.placement_policy.assign_lease(workload, deployment_id, nodes)
        if assignment is None:
            return None
        self.repository.adjust_node_capacity(
            assignment.hotkey,
            assignment.node_id,
            -workload.requirements.gpu_count,
        )
        assignment.expires_at = datetime.now(UTC) + timedelta(seconds=settings.default_lease_ttl_seconds)
        return assignment

    def list_leases(self, hotkey: str) -> list[LeaseAssignment]:
        return self.repository.list_assignments(hotkey, statuses=["assigned", "activating"])

    def list_deployments(self) -> list[DeploymentRecord]:
        return self.repository.list_deployments()

    def update_deployment_status(self, update: DeploymentStatusUpdate) -> DeploymentRecord:
        deployment = self.repository.get_deployment(update.deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {update.deployment_id}")
        deployment.state = transition_state(deployment.state, update.state)
        deployment.ready_instances = update.ready_instances if update.state == DeploymentState.READY else 0
        deployment.endpoint = update.endpoint or deployment.endpoint
        deployment.last_error = update.error
        if update.state == DeploymentState.READY:
            deployment.health_check_failures = 0
        deployment.updated_at = update.observed_at
        self.repository.add_deployment_event(update)
        assignment_status = {
            DeploymentState.PULLING: "activating",
            DeploymentState.STARTING: "activating",
            DeploymentState.READY: "active",
            DeploymentState.FAILED: "failed",
            DeploymentState.TERMINATED: "terminated",
        }.get(update.state)
        if assignment_status is not None:
            self.repository.update_assignment_status(update.deployment_id, assignment_status, reason=update.error)
        saved = self.repository.update_deployment(deployment)
        subject = {
            DeploymentState.READY: "deployment.ready",
            DeploymentState.FAILED: "deployment.failed",
            DeploymentState.TERMINATED: "deployment.terminated",
        }.get(update.state, "deployment.status.updated")
        self.bus.publish(
            subject,
            {
                "deployment_id": saved.deployment_id,
                "state": saved.state.value,
                "hotkey": saved.hotkey,
                "endpoint": saved.endpoint,
                "error": saved.last_error,
            },
        )
        self.metrics.increment(f"deployment.state.{saved.state.value}")
        return saved

    def list_ready_deployments(self, workload_id: str) -> list[DeploymentRecord]:
        return sorted(
            self.repository.list_ready_deployments(workload_id),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def resolve_ready_deployment(self, workload_id: str) -> DeploymentRecord | None:
        ready = self.list_ready_deployments(workload_id)
        return ready[0] if ready else None

    def record_deployment_health_failure(self, deployment_id: str, error: str) -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        deployment.health_check_failures += 1
        deployment.last_error = error
        deployment.updated_at = datetime.now(UTC)
        if deployment.health_check_failures >= settings.deployment_health_failure_threshold:
            self.repository.update_deployment(deployment)
            return self.update_deployment_status(
                DeploymentStatusUpdate(
                    deployment_id=deployment_id,
                    state=DeploymentState.FAILED,
                    error=error,
                    observed_at=deployment.updated_at,
                )
            )
        self.metrics.increment("deployment.health.failed")
        return self.repository.update_deployment(deployment)

    def clear_deployment_health_failures(self, deployment_id: str) -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        if deployment.health_check_failures == 0 and deployment.last_error is None:
            return deployment
        deployment.health_check_failures = 0
        deployment.last_error = None
        deployment.updated_at = datetime.now(UTC)
        self.metrics.increment("deployment.health.recovered")
        return self.repository.update_deployment(deployment)

    def record_usage(self, record: UsageRecord) -> UsageRecord:
        self.bus.publish(
            "usage.recorded",
            record.model_dump(mode="json"),
        )
        self.metrics.increment("usage.queued")
        return record

    def usage_summary(self) -> dict[str, dict[str, float]]:
        return self.usage_aggregator.aggregate(self.repository.list_usage_records())

    @staticmethod
    def _ensure_utc(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp

    def miner_health_report(self, now: datetime | None = None) -> list[dict[str, Any]]:
        observed_at = now or datetime.now(UTC)
        reports: list[dict[str, Any]] = []
        for miner in self.repository.list_miners():
            heartbeat = self.repository.get_heartbeat(miner.hotkey)
            capacities = self.repository.get_capacity(miner.hotkey)
            reason = "healthy"
            status = "healthy"
            stale_seconds: int | None = None
            if heartbeat is None:
                status = "unhealthy"
                reason = "missing heartbeat"
            else:
                heartbeat_observed_at = self._ensure_utc(heartbeat.observed_at)
                stale_seconds = int((observed_at - heartbeat_observed_at).total_seconds())
                if not heartbeat.healthy:
                    status = "unhealthy"
                    reason = "miner reported unhealthy"
                elif stale_seconds > settings.miner_heartbeat_timeout_seconds:
                    status = "unhealthy"
                    reason = f"heartbeat stale by {stale_seconds} seconds"
            reports.append(
                {
                    "hotkey": miner.hotkey,
                    "status": status,
                    "reason": reason,
                    "last_heartbeat_at": self._ensure_utc(heartbeat.observed_at) if heartbeat is not None else None,
                    "stale_seconds": stale_seconds,
                    "active_leases": heartbeat.active_leases if heartbeat is not None else 0,
                    "active_deployments": heartbeat.active_deployments if heartbeat is not None else 0,
                    "node_count": len(capacities.nodes) if capacities is not None else 0,
                }
            )
        return sorted(reports, key=lambda item: (item["status"], item["hotkey"]))

    def reassignment_history(self) -> list[dict[str, Any]]:
        return [
            event.model_dump(mode="json")
            for event in self.workflow_repository.list_events(subjects=["deployment.reassigned"])
        ]

    def stuck_deployments_report(self, now: datetime | None = None) -> list[dict[str, Any]]:
        observed_at = now or datetime.now(UTC)
        reports: list[dict[str, Any]] = []
        for deployment in self.repository.list_deployments():
            reason: str | None = None
            if deployment.state == DeploymentState.PENDING:
                reason = deployment.last_error or "awaiting scheduler assignment"
            elif deployment.state in {DeploymentState.SCHEDULED, DeploymentState.PULLING, DeploymentState.STARTING}:
                reason = deployment.last_error or f"deployment stalled in {deployment.state.value}"
            elif deployment.state == DeploymentState.READY and deployment.health_check_failures > 0:
                reason = deployment.last_error or "deployment has health check failures"
            if reason is None:
                continue
            updated_at = self._ensure_utc(deployment.updated_at)
            age_seconds = int((observed_at - updated_at).total_seconds())
            reports.append(
                {
                    "deployment_id": deployment.deployment_id,
                    "workload_id": deployment.workload_id,
                    "state": deployment.state.value,
                    "hotkey": deployment.hotkey,
                    "node_id": deployment.node_id,
                    "reason": reason,
                    "retry_count": deployment.retry_count,
                    "health_check_failures": deployment.health_check_failures,
                    "updated_at": updated_at,
                    "age_seconds": age_seconds,
                }
            )
        return sorted(reports, key=lambda item: item["age_seconds"], reverse=True)

    def process_timeouts(self, now: datetime | None = None) -> list[DeploymentRecord]:
        observed_at = now or datetime.now(UTC)
        expired: list[DeploymentRecord] = []
        for assignment in self.repository.list_assignments(statuses=["assigned", "activating"]):
            expires_at = assignment.expires_at
            if expires_at is None:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at > observed_at:
                continue
            deployment = self.repository.get_deployment(assignment.deployment_id)
            if deployment is None or deployment.state in {
                DeploymentState.READY,
                DeploymentState.FAILED,
                DeploymentState.TERMINATED,
            }:
                continue
            expired.append(
                self.update_deployment_status(
                    DeploymentStatusUpdate(
                        deployment_id=deployment.deployment_id,
                        state=DeploymentState.FAILED,
                        error="lease expired before deployment became ready",
                        observed_at=observed_at,
                    )
                )
            )
        return expired

    def process_unhealthy_miners(self, now: datetime | None = None) -> list[DeploymentRecord]:
        observed_at = now or datetime.now(UTC)
        reassigned: list[DeploymentRecord] = []
        for assignment in self.repository.list_assignments(statuses=["assigned", "activating", "active"]):
            heartbeat = self.repository.get_heartbeat(assignment.hotkey)
            if heartbeat is not None:
                heartbeat_observed_at = heartbeat.observed_at
                if heartbeat_observed_at.tzinfo is None:
                    heartbeat_observed_at = heartbeat_observed_at.replace(tzinfo=UTC)
            else:
                heartbeat_observed_at = None
            if heartbeat is not None and heartbeat.healthy and heartbeat_observed_at is not None:
                age_seconds = (observed_at - heartbeat_observed_at).total_seconds()
                if age_seconds <= settings.miner_heartbeat_timeout_seconds:
                    continue
                reason = (
                    f"miner heartbeat stale after {int(age_seconds)} seconds for hotkey={assignment.hotkey}"
                )
            elif heartbeat is not None and not heartbeat.healthy:
                reason = f"miner marked unhealthy for hotkey={assignment.hotkey}"
            else:
                reason = f"miner heartbeat missing for hotkey={assignment.hotkey}"
            deployment = self._requeue_assignment(assignment, reason, observed_at)
            if deployment is not None:
                reassigned.append(deployment)
        return reassigned

    def process_pending_events(self, limit: int = 10) -> dict[str, list]:
        events = self.bus.claim_pending(
            "control-plane-worker",
            ["deployment.requested", "usage.recorded", "invocation.recorded"],
            limit=limit,
        )
        scheduled: list[DeploymentRecord] = []
        usage_records: list[UsageRecord] = []
        invocation_records: list[InvocationRecord] = []

        for event in events:
            if event.subject == "deployment.requested":
                processed = self._process_deployment_request(event)
                if processed is not None:
                    scheduled.append(processed)
                continue
            if event.subject == "usage.recorded":
                usage_record = self._process_usage_record(event)
                if usage_record is not None:
                    usage_records.append(usage_record)
                continue
            if event.subject == "invocation.recorded":
                invocation_record = self._process_invocation_record(event)
                if invocation_record is not None:
                    invocation_records.append(invocation_record)
                continue
            self.bus.mark_failed(event.delivery_id, f"unsupported workflow subject={event.subject}")

        self.metrics.set_gauge(
            "workflow.pending.deployment.requested",
            float(
                len(
                    self.bus.list_deliveries(
                        consumer="control-plane-worker",
                        subjects=["deployment.requested"],
                        statuses=["pending"],
                    )
                )
            ),
        )
        self.metrics.set_gauge(
            "workflow.pending.usage.recorded",
            float(
                len(
                    self.bus.list_deliveries(
                        consumer="control-plane-worker",
                        subjects=["usage.recorded"],
                        statuses=["pending"],
                    )
                )
            ),
        )
        self.metrics.set_gauge(
            "workflow.pending.invocation.recorded",
            float(
                len(
                    self.bus.list_deliveries(
                        consumer="control-plane-worker",
                        subjects=["invocation.recorded"],
                        statuses=["pending"],
                    )
                )
            ),
        )
        return {
            "deployments": [item.model_dump(mode="json") for item in scheduled],
            "usage_records": [item.model_dump(mode="json") for item in usage_records],
            "invocation_records": [item.model_dump(mode="json") for item in invocation_records],
        }

    def _process_deployment_request(self, event) -> DeploymentRecord | None:
        deployment = self.repository.get_deployment(str(event.payload["deployment_id"]))
        if deployment is None:
            self.bus.mark_failed(event.delivery_id, "deployment not found")
            return None
        if deployment.state != DeploymentState.PENDING:
            self.bus.mark_completed(event.delivery_id)
            return None

        workload = self.repository.get_workload(deployment.workload_id)
        if workload is None:
            self.bus.mark_failed(event.delivery_id, "workload not found")
            return None

        assignment = self._assign_lease(workload, deployment.deployment_id)
        if assignment is None:
            deployment.retry_count = max(deployment.retry_count, event.attempts)
            deployment.last_error = "no compatible miner capacity available"
            deployment.updated_at = datetime.now(UTC)
            self.repository.update_deployment(deployment)
            if event.attempts >= settings.deployment_request_retry_limit:
                failed = self.update_deployment_status(
                    DeploymentStatusUpdate(
                        deployment_id=deployment.deployment_id,
                        state=DeploymentState.FAILED,
                        error=(
                            "no compatible miner capacity available after "
                            f"{settings.deployment_request_retry_limit} attempts"
                        ),
                        observed_at=deployment.updated_at,
                    )
                )
                self.bus.mark_failed(event.delivery_id, failed.last_error or "deployment scheduling failed")
                return None
            self.bus.mark_failed(
                event.delivery_id,
                "no compatible miner capacity available",
                retryable=True,
                retry_after_seconds=float(settings.deployment_request_retry_delay_seconds),
            )
            return None

        deployment.hotkey = assignment.hotkey
        deployment.node_id = assignment.node_id
        deployment.state = DeploymentState.SCHEDULED
        deployment.retry_count = max(deployment.retry_count, max(0, event.attempts - 1))
        deployment.health_check_failures = 0
        deployment.last_error = None
        deployment.updated_at = datetime.now(UTC)
        self.repository.save_assignment(assignment)
        saved = self.repository.update_deployment(deployment)
        self.bus.publish(
            "deployment.scheduled",
            {
                "deployment_id": saved.deployment_id,
                "workload_id": saved.workload_id,
                "hotkey": saved.hotkey,
                "node_id": saved.node_id,
            },
        )
        self.bus.mark_completed(event.delivery_id)
        self.metrics.increment("deployment.scheduled")
        return saved

    def _process_usage_record(self, event) -> UsageRecord | None:
        record = UsageRecord(**event.payload)
        saved = self.repository.add_usage_record(record)
        self.bus.mark_completed(event.delivery_id)
        self.metrics.increment("usage.persisted")
        return saved

    def record_invocation(self, record: InvocationRecord) -> InvocationRecord:
        self.bus.publish(
            "invocation.recorded",
            record.model_dump(mode="json"),
        )
        self.metrics.increment("invocation.queued")
        return record

    def list_invocations(self, limit: int | None = None) -> list[InvocationRecord]:
        return self.repository.list_invocation_records(limit=limit)

    def get_invocation(self, invocation_id: str) -> InvocationRecord | None:
        return self.repository.get_invocation_record(invocation_id)

    def export_recent_invocations(self, limit: int = 50) -> dict[str, Any]:
        invocations = self.repository.list_invocation_records(limit=limit)
        latencies = [record.latency_ms for record in invocations]
        success_count = len([record for record in invocations if record.status == "succeeded"])
        stream_count = len([record for record in invocations if record.stream])
        return {
            "items": [record.model_dump(mode="json") for record in invocations],
            "summary": {
                "count": len(invocations),
                "success_count": success_count,
                "failure_count": len(invocations) - success_count,
                "stream_count": stream_count,
                "latency_ms_avg": (sum(latencies) / len(latencies)) if latencies else 0.0,
                "latency_ms_max": max(latencies) if latencies else 0.0,
            },
        }

    def _process_invocation_record(self, event) -> InvocationRecord | None:
        record = InvocationRecord(**event.payload)
        saved = self.repository.add_invocation_record(record)
        self.bus.mark_completed(event.delivery_id)
        self.metrics.increment("invocation.persisted")
        return saved

    def _requeue_assignment(
        self,
        assignment: LeaseAssignment,
        reason: str,
        observed_at: datetime,
    ) -> DeploymentRecord | None:
        deployment = self.repository.get_deployment(assignment.deployment_id)
        if deployment is None or deployment.state in {DeploymentState.FAILED, DeploymentState.TERMINATED}:
            return None
        workload = self.repository.get_workload(deployment.workload_id)
        if workload is None:
            return None
        self.repository.adjust_node_capacity(
            assignment.hotkey,
            assignment.node_id,
            workload.requirements.gpu_count,
        )
        self.repository.update_assignment_status(assignment.deployment_id, "reassigned", reason=reason)
        deployment.state = DeploymentState.PENDING
        deployment.hotkey = None
        deployment.node_id = None
        deployment.endpoint = None
        deployment.ready_instances = 0
        deployment.health_check_failures = 0
        deployment.retry_count += 1
        deployment.last_error = reason
        deployment.updated_at = observed_at
        saved = self.repository.update_deployment(deployment)
        self.bus.publish(
            "deployment.requested",
            {
                "deployment_id": saved.deployment_id,
                "workload_id": saved.workload_id,
                "requested_instances": saved.requested_instances,
            },
        )
        self.bus.publish(
            "deployment.reassigned",
            {
                "deployment_id": saved.deployment_id,
                "previous_hotkey": assignment.hotkey,
                "previous_node_id": assignment.node_id,
                "reason": reason,
            },
        )
        self.metrics.increment("deployment.reassigned")
        return saved


service = ControlPlaneService()

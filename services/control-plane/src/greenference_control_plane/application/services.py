from __future__ import annotations

from datetime import UTC, datetime, timedelta

from greenference_protocol import (
    CapacityUpdate,
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    Heartbeat,
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
    def __init__(self, repository: ControlPlaneRepository | None = None) -> None:
        self.repository = repository or ControlPlaneRepository()
        self.placement_policy = PlacementPolicy()
        self.usage_aggregator = UsageAggregator()

    def register_miner(self, registration: MinerRegistration) -> MinerRegistration:
        return self.repository.upsert_miner(registration)

    def record_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat:
        return self.repository.upsert_heartbeat(heartbeat)

    def update_capacity(self, update: CapacityUpdate) -> CapacityUpdate:
        return self.repository.upsert_capacity(update)

    def upsert_workload(self, workload: WorkloadSpec) -> WorkloadSpec:
        return self.repository.upsert_workload(workload)

    def list_workloads(self) -> list[WorkloadSpec]:
        return self.repository.list_workloads()

    def find_workload_by_name(self, name: str) -> WorkloadSpec | None:
        return self.repository.find_workload_by_name(name)

    def create_deployment(self, request: DeploymentCreateRequest) -> DeploymentRecord:
        workload = self.repository.get_workload(request.workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {request.workload_id}")
        deployment = DeploymentRecord(
            workload_id=request.workload_id,
            requested_instances=request.requested_instances,
        )
        self.repository.create_deployment(deployment)
        assignment = self._assign_lease(workload, deployment.deployment_id)
        if assignment:
            deployment.hotkey = assignment.hotkey
            deployment.node_id = assignment.node_id
            deployment.state = DeploymentState.SCHEDULED
            deployment.updated_at = datetime.now(UTC)
            self.repository.save_assignment(assignment)
            self.repository.update_deployment(deployment)
        return deployment

    def _assign_lease(self, workload: WorkloadSpec, deployment_id: str) -> LeaseAssignment | None:
        nodes = []
        for update in self.repository.list_capacities():
            heartbeat = self.repository.get_heartbeat(update.hotkey)
            if heartbeat and not heartbeat.healthy:
                continue
            nodes.extend(update.nodes)
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
        return self.repository.list_assignments(hotkey)

    def list_deployments(self) -> list[DeploymentRecord]:
        return self.repository.list_deployments()

    def update_deployment_status(self, update: DeploymentStatusUpdate) -> DeploymentRecord:
        deployment = self.repository.get_deployment(update.deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {update.deployment_id}")
        deployment.state = transition_state(deployment.state, update.state)
        deployment.ready_instances = update.ready_instances
        deployment.endpoint = update.endpoint or deployment.endpoint
        deployment.last_error = update.error
        deployment.updated_at = update.observed_at
        self.repository.add_deployment_event(update)
        return self.repository.update_deployment(deployment)

    def resolve_ready_deployment(self, workload_id: str) -> DeploymentRecord | None:
        ready = self.repository.list_ready_deployments(workload_id)
        return sorted(ready, key=lambda item: item.updated_at, reverse=True)[0] if ready else None

    def record_usage(self, record: UsageRecord) -> UsageRecord:
        return self.repository.add_usage_record(record)

    def usage_summary(self) -> dict[str, dict[str, float]]:
        return self.usage_aggregator.aggregate(self.repository.list_usage_records())


service = ControlPlaneService()

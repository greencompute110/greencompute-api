from __future__ import annotations

import logging
from typing import Any
from collections import Counter
from datetime import UTC, datetime, timedelta

from greencompute_persistence import SubjectBus, WorkflowEventRepository, create_subject_bus, get_metrics_store
from greencompute_persistence.runtime import load_runtime_settings
from greencompute_protocol import (
    CapacityUpdate,
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    DeploymentUpdateRequest,
    Heartbeat,
    InvocationRecord,
    LeaseAssignment,
    MinerRegistration,
    NodeCapability,
    UsageRecord,
    WorkloadKind,
    WorkloadSpec,
)
from greencompute_control_plane.config import settings
from greencompute_control_plane.domain.metering import UsageAggregator
from greencompute_control_plane.domain.scheduler import PlacementPolicy
from greencompute_control_plane.domain.state import transition_state
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository

logger = logging.getLogger(__name__)


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
        runtime_settings = load_runtime_settings("greencompute-control-plane")
        self.runtime_settings = runtime_settings
        self.bus = bus or create_subject_bus(
            engine=self.workflow_repository.engine,
            session_factory=self.workflow_repository.session_factory,
            workflow_repository=self.workflow_repository,
            nats_url=runtime_settings.nats_url,
            transport=runtime_settings.bus_transport,
        )
        self.placement_policy = PlacementPolicy()
        self.usage_aggregator = UsageAggregator()
        self.metrics = get_metrics_store("greencompute-control-plane")
        self._recovery_state: dict[str, object | None] = {
            "last_recovery_at": None,
            "last_recovery_error": None,
            "requeued_deliveries": 0,
            "resumed_subjects": {},
        }

    def register_miner(self, registration: MinerRegistration) -> MinerRegistration:
        return self.repository.upsert_miner(registration)

    def drain_miner(self, hotkey: str) -> MinerRegistration:
        miner = self.repository.set_miner_drained(hotkey, True)
        if miner is None:
            raise KeyError(f"miner not found: {hotkey}")
        self.metrics.increment("miner.drained")
        return miner

    def undrain_miner(self, hotkey: str) -> MinerRegistration:
        miner = self.repository.set_miner_drained(hotkey, False)
        if miner is None:
            raise KeyError(f"miner not found: {hotkey}")
        self.metrics.increment("miner.undrained")
        return miner

    def record_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat:
        return self.repository.upsert_heartbeat(heartbeat)

    def update_capacity(self, update: CapacityUpdate) -> CapacityUpdate:
        # Miners occasionally report the wrong VRAM because the operator set
        # GREENCOMPUTE_VRAM_GB_PER_GPU to a stale value on the miner host
        # (common case: a 5090 box keeps the old 24GB env from when it was a
        # 4090). Correct the VRAM server-side against the known-good table
        # in protocol so the UI never shows misleading numbers.
        from greencompute_protocol import canonical_vram_gb
        corrected = []
        for node in update.nodes:
            canonical = canonical_vram_gb(node.gpu_model, node.vram_gb_per_gpu)
            if canonical is not None and canonical != node.vram_gb_per_gpu:
                corrected.append(node.model_copy(update={"vram_gb_per_gpu": canonical}))
            else:
                corrected.append(node)
        update = update.model_copy(update={"nodes": corrected})
        return self.repository.upsert_capacity(update)

    def list_servers(self):
        return self.repository.list_servers()

    def get_server_by_hotkey(self, hotkey: str):
        return self.repository.get_server_by_hotkey(hotkey)

    def list_nodes(self):
        return self.repository.list_nodes()

    def list_capacity_history(self, limit: int | None = None):
        return self.repository.list_capacity_history(limit=limit)

    def list_placements(self, limit: int | None = None):
        return self.repository.list_placements(limit=limit)

    def list_lease_history(self, limit: int | None = None):
        return self.repository.list_lease_history(limit=limit)

    def upsert_workload(self, workload: WorkloadSpec) -> WorkloadSpec:
        return self.repository.upsert_workload(workload)

    def list_workloads(self) -> list[WorkloadSpec]:
        return self.repository.list_workloads()

    def find_workload_by_name(self, name: str) -> WorkloadSpec | None:
        return self.repository.find_workload_by_name(name)

    def find_workload_by_alias(self, alias: str) -> WorkloadSpec | None:
        return self.repository.find_workload_by_alias(alias)

    def find_workload_by_ingress_host(self, ingress_host: str) -> WorkloadSpec | None:
        return self.repository.find_workload_by_ingress_host(ingress_host)

    def create_deployment(self, request: DeploymentCreateRequest | dict) -> DeploymentRecord:
        owner_user_id = request.get("owner_user_id") if isinstance(request, dict) else None
        payload = request if isinstance(request, DeploymentCreateRequest) else DeploymentCreateRequest(**request)
        workload = self.repository.get_workload(payload.workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {payload.workload_id}")
        deployment = DeploymentRecord(
            workload_id=payload.workload_id,
            owner_user_id=owner_user_id,
            requested_instances=payload.requested_instances,
            deployment_fee_usd=self._estimate_deployment_fee(workload, payload.requested_instances),
            fee_acknowledged=payload.accept_fee,
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
        all_nodes = self.repository.list_nodes()
        nodes = []
        cooldown_only_nodes = []
        skip_reasons: dict[str, list[str]] = {}
        now_ts = datetime.now(UTC)
        for node in all_nodes:
            reasons: list[str] = []
            miner = self.repository.get_miner(node.hotkey)
            if miner is not None and miner.drained:
                reasons.append("drained")
            heartbeat = self.repository.get_heartbeat(node.hotkey)
            if heartbeat and not heartbeat.healthy:
                reasons.append("unhealthy")
            # Stale heartbeat — mirrors the age threshold `process_unhealthy_miners`
            # uses to kick deployments off a miner. If we don't also filter here,
            # the scheduler re-picks the same stale miner → requeued → re-picked
            # in a tight infinite loop (observed: 147 events/min on prod).
            if heartbeat is not None:
                hb_ts = heartbeat.observed_at
                if hb_ts is not None:
                    if hb_ts.tzinfo is None:
                        hb_ts = hb_ts.replace(tzinfo=UTC)
                    if (now_ts - hb_ts).total_seconds() > settings.miner_heartbeat_timeout_seconds:
                        reasons.append("heartbeat_stale")
            else:
                reasons.append("heartbeat_missing")
            if self._is_node_stale(node):
                reasons.append("stale")
            if self._is_server_for_node_stale(node):
                reasons.append("server_stale")
            in_cooldown = self._is_node_in_cooldown(node.node_id, deployment_id=deployment_id)
            if in_cooldown:
                reasons.append("cooldown")
            if reasons:
                skip_reasons[node.node_id] = reasons
                if reasons == ["cooldown"]:
                    cooldown_only_nodes.append(node)
                continue
            nodes.append(node)
        if not nodes and cooldown_only_nodes:
            logger.info(
                "all eligible nodes in cooldown for deployment %s, bypassing cooldown "
                "(cooldown only useful with multiple nodes)",
                deployment_id,
            )
            nodes = cooldown_only_nodes
        if not nodes:
            logger.warning(
                "no eligible nodes for deployment %s (workload=%s kind=%s gpu=%d vram=%d): "
                "total_registered=%d skipped=%s",
                deployment_id,
                workload.workload_id,
                workload.kind.value,
                workload.requirements.gpu_count,
                workload.requirements.min_vram_gb_per_gpu,
                len(all_nodes),
                skip_reasons or "none registered",
            )
        else:
            logger.info(
                "scheduling deployment %s: %d eligible nodes out of %d total",
                deployment_id, len(nodes), len(all_nodes),
            )
        assignment = self.placement_policy.assign_lease(workload, deployment_id, nodes)
        if assignment is None:
            logger.warning(
                "scheduler returned no match for deployment %s among %d eligible nodes "
                "(workload kind=%s gpu=%d vram=%d cpu=%d mem=%d)",
                deployment_id, len(nodes), workload.kind.value,
                workload.requirements.gpu_count,
                workload.requirements.min_vram_gb_per_gpu,
                workload.requirements.cpu_cores,
                workload.requirements.memory_gb,
            )
            return None
        self.repository.adjust_node_capacity(
            assignment.hotkey,
            assignment.node_id,
            -workload.requirements.gpu_count,
        )
        assignment.expires_at = datetime.now(UTC) + timedelta(seconds=settings.default_lease_ttl_seconds)
        return assignment

    def list_leases(self, hotkey: str) -> list[LeaseAssignment]:
        return self.repository.list_assignments(hotkey, statuses=["assigned", "activating", "active"])

    def list_deployments(self) -> list[DeploymentRecord]:
        return self.repository.list_deployments()

    def update_deployment(self, deployment_id: str, request: DeploymentUpdateRequest) -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        workload = self.repository.get_workload(deployment.workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {deployment.workload_id}")
        if request.requested_instances is not None:
            deployment.requested_instances = request.requested_instances
            deployment.deployment_fee_usd = self._estimate_deployment_fee(workload, deployment.requested_instances)
        if request.fee_acknowledged is not None:
            deployment.fee_acknowledged = request.fee_acknowledged
        deployment.updated_at = datetime.now(UTC)
        return self.repository.update_deployment(deployment)

    def update_deployment_status(self, update: DeploymentStatusUpdate) -> DeploymentRecord:
        deployment = self.repository.get_deployment(update.deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {update.deployment_id}")
        deployment.state = transition_state(deployment.state, update.state)
        deployment.ready_instances = update.ready_instances if update.state == DeploymentState.READY else 0
        deployment.endpoint = update.endpoint or deployment.endpoint
        if update.ssh_private_key:
            deployment.ssh_private_key = update.ssh_private_key
        if update.port_mappings is not None:
            deployment.port_mappings = update.port_mappings
        deployment.last_error = update.error
        deployment.failure_class = self._classify_deployment_failure(update.error, update.state, deployment.failure_class)
        if update.state == DeploymentState.READY:
            deployment.health_check_failures = 0
            deployment.retry_exhausted = False
            deployment.warmup_state = "ready"
        elif update.state in {DeploymentState.PULLING, DeploymentState.STARTING}:
            deployment.warmup_state = "warming"
        elif update.state in {DeploymentState.FAILED, DeploymentState.TERMINATED}:
            deployment.warmup_state = "stopped"
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

    @staticmethod
    def _estimate_deployment_fee(workload: WorkloadSpec, requested_instances: int) -> float:
        gpu_count = workload.requirements.gpu_count
        base_hourly = 0.1 * gpu_count
        return round(base_hourly * 3 * requested_instances, 4)

    def list_ready_deployments(self, workload_id: str) -> list[DeploymentRecord]:
        routable: list[DeploymentRecord] = []
        for deployment in self.repository.list_ready_deployments(workload_id):
            if deployment.node_id is None:
                continue
            node = self._find_node(deployment.node_id)
            if node is None or self._is_node_stale(node):
                continue
            heartbeat = self.repository.get_heartbeat(deployment.hotkey or "")
            if heartbeat is not None and not heartbeat.healthy:
                continue
            routable.append(deployment)
        return sorted(routable, key=lambda item: item.updated_at, reverse=True)

    def resolve_ready_deployment(self, workload_id: str) -> DeploymentRecord | None:
        ready = self.list_ready_deployments(workload_id)
        return ready[0] if ready else None

    def record_deployment_health_failure(self, deployment_id: str, error: str) -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        deployment.health_check_failures += 1
        deployment.last_error = error
        deployment.failure_class = "health_check_failure"
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

    def recovery_status(self) -> dict[str, object | None]:
        return dict(self._recovery_state)

    def recover_inflight_events(self) -> dict[str, object | None]:
        subjects = ["deployment.requested", "usage.recorded", "invocation.recorded"]
        stale_after_seconds = max(self.runtime_settings.worker_poll_interval_seconds * 4, 2.0)
        requeued = self.bus.requeue_stale_processing(
            "control-plane-worker",
            subjects,
            stale_after_seconds=stale_after_seconds,
        )
        resumed_subjects: dict[str, int] = {}
        for delivery in requeued:
            resumed_subjects[delivery.subject] = resumed_subjects.get(delivery.subject, 0) + 1
        self._recovery_state = {
            "last_recovery_at": datetime.now(UTC),
            "last_recovery_error": None,
            "requeued_deliveries": len(requeued),
            "resumed_subjects": resumed_subjects,
        }
        return self.recovery_status()

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
                    "drained": miner.drained,
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

    def miner_drift_report(self, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        observed_at = now or datetime.now(UTC)
        miners = self.miner_health_report(now=observed_at)
        stale_nodes: list[dict[str, Any]] = []
        stale_servers: list[dict[str, Any]] = []
        for node in self.repository.list_nodes():
            if self._is_node_stale(node, now=observed_at):
                stale_nodes.append(
                    {
                        "hotkey": node.hotkey,
                        "node_id": node.node_id,
                        "server_id": node.server_id,
                        "reason": "node inventory stale",
                    }
                )
        for server in self.repository.list_servers():
            if self._is_server_stale(server.observed_at, now=observed_at):
                stale_servers.append(
                    {
                        "server_id": server.server_id,
                        "hotkey": server.hotkey,
                        "hostname": server.hostname,
                        "reason": "server inventory stale",
                    }
                )
        return {
            "miners": [item for item in miners if item["status"] != "healthy"],
            "nodes": stale_nodes,
            "servers": stale_servers,
        }

    def deployment_retry_report(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for deployment in self.repository.list_deployments():
            if deployment.retry_count == 0 and not deployment.retry_exhausted:
                continue
            reports.append(
                {
                    "deployment_id": deployment.deployment_id,
                    "workload_id": deployment.workload_id,
                    "state": deployment.state.value,
                    "retry_count": deployment.retry_count,
                    "retry_exhausted": deployment.retry_exhausted,
                    "last_retry_reason": deployment.last_retry_reason,
                    "failure_class": deployment.failure_class,
                    "last_error": deployment.last_error,
                }
            )
        return reports

    def placement_exclusion_report(self, now: datetime | None = None) -> list[dict[str, Any]]:
        observed_at = now or datetime.now(UTC)
        servers = {server.server_id: server for server in self.repository.list_servers()}
        exclusions: list[dict[str, Any]] = []
        latest_by_node: dict[str, Any] = {}
        for placement in self.repository.list_placements(limit=1000):
            latest_by_node.setdefault(placement.node_id, placement)
        for node in self.repository.list_nodes():
            miner = self.repository.get_miner(node.hotkey)
            latest = latest_by_node.get(node.node_id)
            if miner is not None and miner.drained:
                exclusions.append({"hotkey": node.hotkey, "node_id": node.node_id, "reason": "miner_drained"})
                continue
            if self._is_node_stale(node, now=observed_at):
                exclusions.append({"hotkey": node.hotkey, "node_id": node.node_id, "reason": "node_stale"})
                continue
            server = servers.get(node.server_id or "")
            if server is not None and self._is_server_stale(server.observed_at, now=observed_at):
                exclusions.append({"hotkey": node.hotkey, "node_id": node.node_id, "reason": "server_stale"})
                continue
            if latest is not None and latest.cooldown_until is not None and self._ensure_utc(latest.cooldown_until) > observed_at:
                exclusions.append(
                    {
                        "hotkey": node.hotkey,
                        "node_id": node.node_id,
                        "reason": "placement_cooldown",
                        "cooldown_until": latest.cooldown_until,
                        "failure_count": latest.failure_count,
                    }
                )
        return exclusions

    def routing_eligibility_report(
        self,
        workload_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        observed_at = now or datetime.now(UTC)
        reports: list[dict[str, Any]] = []
        for deployment in self.repository.list_deployments():
            if workload_id is not None and deployment.workload_id != workload_id:
                continue
            reasons: list[str] = []
            node = self._find_node(deployment.node_id) if deployment.node_id else None
            miner = self.repository.get_miner(deployment.hotkey or "") if deployment.hotkey else None
            if deployment.state != DeploymentState.READY:
                reasons.append(f"deployment_state:{deployment.state.value}")
            if deployment.endpoint is None:
                reasons.append("missing_endpoint")
            if deployment.hotkey is None:
                reasons.append("missing_hotkey")
            if miner is not None and miner.drained:
                reasons.append("miner_drained")
            heartbeat = self.repository.get_heartbeat(deployment.hotkey or "") if deployment.hotkey else None
            if heartbeat is not None and not heartbeat.healthy:
                reasons.append("miner_unhealthy")
            if node is None and deployment.node_id is not None:
                reasons.append("node_missing")
            if node is not None and self._is_node_stale(node, now=observed_at):
                reasons.append("node_stale")
            if node is not None and self._is_server_for_node_stale(node, now=observed_at):
                reasons.append("server_stale")
            if deployment.node_id and self._is_node_in_cooldown(deployment.node_id, now=observed_at):
                reasons.append("placement_cooldown")
            reports.append(
                {
                    "deployment_id": deployment.deployment_id,
                    "workload_id": deployment.workload_id,
                    "hotkey": deployment.hotkey,
                    "node_id": deployment.node_id,
                    "endpoint": deployment.endpoint,
                    "state": deployment.state.value,
                    "eligible": len(reasons) == 0,
                    "reasons": reasons,
                    "last_error": deployment.last_error,
                    "failure_class": deployment.failure_class,
                }
            )
        return reports

    def deployment_failure_report(self) -> list[dict[str, Any]]:
        return [
            deployment.model_dump(mode="json")
            for deployment in self.repository.list_deployments()
            if deployment.state == DeploymentState.FAILED or deployment.retry_exhausted
        ]

    def fleet_orchestration_report(self) -> dict[str, Any]:
        servers = self.repository.list_servers()
        nodes = self.repository.list_nodes()
        placements = self.repository.list_placements(limit=1000)
        leases = self.repository.list_lease_history(limit=1000)
        stale_inventory = self.miner_drift_report()
        exclusions = self.placement_exclusion_report()
        routing = self.routing_eligibility_report()
        deployment_failures = self.deployment_failure_report()

        placement_status_counts = Counter(placement.status for placement in placements)
        placement_server_counts = Counter((placement.server_id or "unknown") for placement in placements)
        lease_status_counts = Counter(lease.status for lease in leases)
        exclusion_reason_counts = Counter(
            str(item.get("reason") or "unknown")
            for item in exclusions
        )
        routing_reason_counts = Counter()
        for record in routing:
            for reason in record["reasons"]:
                routing_reason_counts[reason] += 1
        deployment_state_counts = Counter(deployment.state.value for deployment in self.repository.list_deployments())
        failure_class_counts = Counter(
            str(item.get("failure_class") or "unknown")
            for item in deployment_failures
        )

        return {
            "servers": {
                "total": len(servers),
                "stale": len(stale_inventory["servers"]),
                "by_hotkey": dict(Counter(server.hotkey for server in servers)),
                "items": [server.model_dump(mode="json") for server in servers],
            },
            "nodes": {
                "total": len(nodes),
                "stale": len(stale_inventory["nodes"]),
                "cooling_down": exclusion_reason_counts.get("placement_cooldown", 0),
                "total_gpus": sum(node.gpu_count for node in nodes),
                "available_gpus": sum(node.available_gpus for node in nodes),
                "by_hotkey": dict(Counter(node.hotkey for node in nodes)),
            },
            "placements": {
                "total": len(placements),
                "counts_by_status": dict(placement_status_counts),
                "by_server": dict(placement_server_counts),
                "recent": [placement.model_dump(mode="json") for placement in placements[:20]],
            },
            "leases": {
                "total": len(leases),
                "counts_by_status": dict(lease_status_counts),
                "recent": [lease.model_dump(mode="json") for lease in leases[:20]],
            },
            "routing": {
                "eligible": sum(1 for record in routing if record["eligible"]),
                "ineligible": sum(1 for record in routing if not record["eligible"]),
                "ineligible_reason_counts": dict(routing_reason_counts),
            },
            "exclusions": {
                "total": len(exclusions),
                "counts_by_reason": dict(exclusion_reason_counts),
                "items": exclusions,
            },
            "deployments": {
                "counts_by_state": dict(deployment_state_counts),
                "failure_classes": dict(failure_class_counts),
                "recent_failures": deployment_failures[:20],
            },
            "stale_inventory": stale_inventory,
        }

    def invocation_failure_report(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            record.model_dump(mode="json")
            for record in self.repository.list_invocation_records(limit=limit)
            if record.status != "succeeded"
        ]

    def operator_status(self) -> dict[str, Any]:
        unhealthy_miners = [item for item in self.miner_health_report() if item["status"] != "healthy"]
        stuck_deployments = self.stuck_deployments_report()
        failed_builds = []
        build_events = self.workflow_repository.list_events(subjects=["build.failed"], statuses=None)
        for event in build_events:
            failed_builds.append(event.model_dump(mode="json"))
        delivery_status_counts: dict[str, int] = {}
        for status in ["pending", "processing", "completed", "failed"]:
            delivery_status_counts[status] = len(self.bus.list_deliveries(statuses=[status]))
        return {
            "workers": {
                "queue_depth": delivery_status_counts["pending"],
                "delivery_status_counts": delivery_status_counts,
                "recovery": self.recovery_status(),
            },
            "fleet_orchestration": self.fleet_orchestration_report(),
            "unhealthy_miners": unhealthy_miners,
            "drained_miners": [item for item in self.miner_health_report() if item["drained"]],
            "stale_inventory": self.miner_drift_report(),
            "placement_exclusions": self.placement_exclusion_report(),
            "routing_eligibility": self.routing_eligibility_report(),
            "stuck_deployments": stuck_deployments,
            "deployment_failures": self.deployment_failure_report(),
            "failed_invocations": self.invocation_failure_report(limit=20),
            "failed_builds": failed_builds[-20:],
        }

    def requeue_deployment(self, deployment_id: str) -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        assignment = next(
            (item for item in self.repository.list_assignments(statuses=["assigned", "activating", "active"]) if item.deployment_id == deployment_id),
            None,
        )
        if assignment is not None:
            requeued = self._requeue_assignment(assignment, "operator requested requeue", datetime.now(UTC))
            if requeued is not None:
                return requeued
        deployment.state = DeploymentState.PENDING
        deployment.hotkey = None
        deployment.node_id = None
        deployment.endpoint = None
        deployment.ready_instances = 0
        deployment.retry_count = 0
        deployment.retry_exhausted = False
        deployment.last_error = "operator requested requeue"
        deployment.last_retry_reason = deployment.last_error
        deployment.updated_at = datetime.now(UTC)
        saved = self.repository.update_deployment(deployment)
        self.bus.publish(
            "deployment.requested",
            {"deployment_id": saved.deployment_id, "workload_id": saved.workload_id, "requested_instances": saved.requested_instances},
        )
        return saved

    def fail_deployment(self, deployment_id: str) -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        assignment = next(
            (item for item in self.repository.list_assignments(statuses=["assigned", "activating", "active"]) if item.deployment_id == deployment_id),
            None,
        )
        if assignment is not None:
            workload = self.repository.get_workload(deployment.workload_id)
            if workload is not None:
                self.repository.adjust_node_capacity(assignment.hotkey, assignment.node_id, workload.requirements.gpu_count)
            self.repository.update_assignment_status(deployment_id, "terminated", reason="operator forced failure")
            self.repository.update_placement_status(
                deployment_id,
                "failed",
                reason="operator forced failure",
                increment_failure=True,
                cooldown_until=datetime.now(UTC) + timedelta(seconds=settings.placement_failure_cooldown_seconds),
            )
        return self.update_deployment_status(
            DeploymentStatusUpdate(
                deployment_id=deployment_id,
                state=DeploymentState.FAILED,
                error="operator forced failure",
                observed_at=datetime.now(UTC),
            )
        )

    def cleanup_deployment(self, deployment_id: str, reason: str = "operator cleanup") -> DeploymentRecord:
        deployment = self.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        active_assignment = next(
            (
                item
                for item in self.repository.list_assignments(statuses=["assigned", "activating", "active"])
                if item.deployment_id == deployment_id
            ),
            None,
        )
        if active_assignment is not None:
            workload = self.repository.get_workload(deployment.workload_id)
            if workload is not None:
                self.repository.adjust_node_capacity(
                    active_assignment.hotkey,
                    active_assignment.node_id,
                    workload.requirements.gpu_count,
                )
            self.repository.update_assignment_status(deployment_id, "terminated", reason=reason)
            self.repository.update_placement_status(
                deployment_id,
                "terminated",
                reason=reason,
                cooldown_until=datetime.min.replace(tzinfo=UTC),
            )

        deployment.state = DeploymentState.TERMINATED
        deployment.ready_instances = 0
        deployment.endpoint = None
        deployment.last_error = reason
        deployment.failure_class = "operator_cleanup"
        deployment.updated_at = datetime.now(UTC)
        saved = self.repository.update_deployment(deployment)
        self.repository.add_deployment_event(
            DeploymentStatusUpdate(
                deployment_id=deployment_id,
                state=DeploymentState.TERMINATED,
                error=reason,
                observed_at=saved.updated_at,
            )
        )
        self.metrics.increment("deployment.cleanup")
        return saved

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

    def process_idle_inference_deployments(self, now: datetime | None = None) -> list[str]:
        """Auto-suspend private-endpoint inference deployments with no
        recent invocations.

        Scope: READY deployments whose workload.kind is INFERENCE **and**
        that are NOT flux-managed (metadata['managed_by'] != 'flux').
        Flux-managed catalog replicas churn via demand-reactive rebalance,
        not a timer.

        Threshold is `settings.idle_private_endpoint_timeout_seconds`
        (default 1800s = 30 min) measured from the later of:
          - the last invocation against this deployment, or
          - the deployment's READY transition (if never invoked).

        Returns the list of deployment IDs that were suspended this cycle.
        """
        observed_at = now or datetime.now(UTC)
        timeout = settings.idle_private_endpoint_timeout_seconds
        suspended: list[str] = []
        for deployment in self.repository.list_deployments_by_state(DeploymentState.READY):
            workload = self.repository.get_workload(deployment.workload_id)
            if workload is None or workload.kind != WorkloadKind.INFERENCE:
                continue
            if (workload.metadata or {}).get("managed_by") == "flux":
                continue  # catalog replica — Flux rebalance owns lifecycle

            last_inv = self.repository.last_invocation_at(deployment.deployment_id)
            ready_since = self._ensure_utc(deployment.updated_at) if deployment.updated_at else observed_at
            last_activity = last_inv if last_inv is not None else ready_since
            last_activity = self._ensure_utc(last_activity)
            idle_seconds = (observed_at - last_activity).total_seconds()
            if idle_seconds < timeout:
                continue

            logger.info(
                "Auto-suspending idle private-endpoint inference deployment %s "
                "(idle %ds, threshold %ds, last_invocation=%s)",
                deployment.deployment_id,
                int(idle_seconds),
                timeout,
                last_inv.isoformat() if last_inv else "never",
            )
            try:
                self.update_deployment_status(
                    DeploymentStatusUpdate(
                        deployment_id=deployment.deployment_id,
                        state=DeploymentState.SUSPENDED,
                        error=f"idle — no invocations in {timeout // 60} min",
                    )
                )
                self.metrics.increment("deployment.suspended.idle_inference")
                suspended.append(deployment.deployment_id)
            except Exception:
                logger.exception(
                    "failed to suspend idle deployment %s", deployment.deployment_id
                )
        return suspended

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
            deployment.last_retry_reason = deployment.last_error
            deployment.failure_class = "scheduler_failure"
            deployment.updated_at = datetime.now(UTC)
            self.repository.update_deployment(deployment)
            if event.attempts >= settings.deployment_request_retry_limit:
                deployment.retry_exhausted = True
                deployment.updated_at = datetime.now(UTC)
                self.repository.update_deployment(deployment)
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
        # Lock the per-GPU-per-hour rate onto the deployment row at placement
        # time based on the allocated node's GPU model. This way the metering
        # loop has a stable rate to debit against, and any future edits to
        # GPU_RATE_CENTS_PER_HOUR don't retroactively change active rentals.
        from greencompute_protocol import rate_for_gpu
        assigned_node = next(
            (n for n in self.repository.list_nodes() if n.node_id == assignment.node_id),
            None,
        )
        deployment.hourly_rate_cents = rate_for_gpu(
            assigned_node.gpu_model if assigned_node else None
        )
        deployment.state = DeploymentState.SCHEDULED
        deployment.retry_count = max(deployment.retry_count, max(0, event.attempts - 1))
        deployment.health_check_failures = 0
        deployment.last_error = None
        deployment.last_retry_reason = None
        deployment.failure_class = None
        deployment.retry_exhausted = False
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
        self.repository.update_placement_status(
            assignment.deployment_id,
            "failed",
            reason=reason,
            increment_failure=True,
            cooldown_until=observed_at + timedelta(seconds=settings.placement_failure_cooldown_seconds),
        )
        deployment.state = DeploymentState.PENDING
        deployment.hotkey = None
        deployment.node_id = None
        deployment.endpoint = None
        deployment.ready_instances = 0
        deployment.health_check_failures = 0
        deployment.retry_count += 1
        deployment.last_error = reason
        deployment.last_retry_reason = reason
        deployment.failure_class = "miner_failure"
        deployment.retry_exhausted = False
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

    def _find_node(self, node_id: str) -> NodeCapability | None:
        for node in self.repository.list_nodes():
            if node.node_id == node_id:
                return node
        return None

    def _is_server_for_node_stale(self, node: NodeCapability, now: datetime | None = None) -> bool:
        if not node.server_id:
            return False
        for server in self.repository.list_servers():
            if server.server_id == node.server_id:
                return self._is_server_stale(server.observed_at, now=now)
        return False

    def _is_node_in_cooldown(
        self, node_id: str, now: datetime | None = None, deployment_id: str | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        for placement in self.repository.list_placements(limit=1000):
            if placement.node_id != node_id or placement.cooldown_until is None:
                continue
            if deployment_id is not None and placement.deployment_id != deployment_id:
                continue
            if self._ensure_utc(placement.cooldown_until) > observed_at:
                return True
        return False

    def _is_node_stale(self, node: NodeCapability, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(UTC)
        raw = node.observed_at
        if raw is None:
            return False
        if isinstance(raw, str):
            node_observed_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            node_observed_at = raw
        node_observed_at = self._ensure_utc(node_observed_at)
        return (observed_at - node_observed_at).total_seconds() > settings.node_inventory_timeout_seconds

    def _is_server_stale(self, observed_at: datetime, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return (current - self._ensure_utc(observed_at)).total_seconds() > settings.server_observed_timeout_seconds

    @staticmethod
    def _classify_deployment_failure(
        error: str | None,
        state: DeploymentState,
        existing: str | None,
    ) -> str | None:
        if state == DeploymentState.READY:
            return None
        if state not in {DeploymentState.FAILED, DeploymentState.TERMINATED}:
            return existing
        message = (error or "").lower()
        if "machine lost" in message or "machine_loss" in message:
            return "machine_loss"
        if "lease" in message:
            return "lease_loss"
        if "capacity" in message or "scheduler" in message:
            return "scheduler_failure"
        if "health" in message:
            return "health_check_failure"
        if "heartbeat" in message or "miner" in message:
            return "miner_failure"
        if "upstream" in message or "timeout" in message:
            return "upstream_failure"
        return existing or "deployment_failure"

    # --- Usage metering ---

    def meter_usage(self) -> dict[str, int]:
        """Deduct per-minute GPU usage from user balances for all READY deployments.

        Returns dict of deployment_id -> cents deducted. Suspends deployments
        when user balance is insufficient.
        """
        from greencompute_gateway.infrastructure.billing_repository import BillingRepository, InsufficientBalanceError

        billing_repo = BillingRepository()
        ready_deployments = self.repository.list_deployments_by_state(DeploymentState.READY)
        result: dict[str, int] = {}

        for deployment in ready_deployments:
            if not deployment.owner_user_id:
                continue

            workload = self.repository.get_workload(deployment.workload_id)
            # Inference workloads are metered per-token at request time
            # (see gateway's _charge_inference_tokens). Charging hourly here
            # too would be double-billing. Only pod/vm rentals are hourly.
            if workload is not None and workload.kind == WorkloadKind.INFERENCE:
                continue
            gpu_count = workload.requirements.gpu_count if workload else 1
            # Per-minute cost in *millicents* — integer math, no float rounding.
            # Hourly-cents × 1000 / 60 = millicents per minute.
            #   1× 4090 @ 40¢/hr  →  40 × 1000 / 60 = 667 mcents/min
            #   3× 5090 @ 70¢/hr  →  70 × 3 × 1000 / 60 = 3500 mcents/min
            # The accumulator on the deployment row holds any sub-cent
            # remainder so hourly totals converge exactly to the advertised
            # rate instead of biasing +50% (small) or −25% (2×).
            hourly_cents = deployment.hourly_rate_cents or 10
            # Round-to-nearest (not truncate) so the per-minute increment
                #   40 × 1000 × 1 / 60 = 666.67  → 667 (not 666)
            # Over 60 minutes at 667 mcents/min = 40020 mcents → 40 full cents
            # billed + 20 mcents carried over. Drift is < 1 cent/hour.
            add_mcents = round(
                hourly_cents * 1000 * gpu_count * deployment.requested_instances / 60
            )
            whole_cents, _remainder = self.repository.accrue_metering(
                deployment.deployment_id,
                add_mcents=add_mcents,
            )
            if whole_cents <= 0:
                # Sub-cent accumulation this cycle — nothing to debit yet.
                # The remainder keeps adding up on the deployment row.
                continue
            amount = whole_cents

            try:
                billing_repo.debit_user(
                    user_id=deployment.owner_user_id,
                    amount_cents=amount,
                    kind="usage",
                    reference_id=deployment.deployment_id,
                    description=f"GPU usage {gpu_count}× GPU @ ${hourly_cents/100:.2f}/hr",
                )
                result[deployment.deployment_id] = amount
            except InsufficientBalanceError:
                logger.warning(
                    "Suspending deployment %s — user %s has insufficient balance",
                    deployment.deployment_id,
                    deployment.owner_user_id,
                )
                try:
                    self.update_deployment_status(
                        DeploymentStatusUpdate(
                            deployment_id=deployment.deployment_id,
                            state=DeploymentState.SUSPENDED,
                            error="Insufficient balance — top up your credits to resume",
                        )
                    )
                    self.metrics.increment("deployment.suspended.insufficient_balance")
                except Exception as exc:
                    logger.error("Failed to suspend deployment %s: %s", deployment.deployment_id, exc)
            except KeyError:
                # User not found — skip
                pass

        if result:
            self.metrics.increment("metering.cycles")
            logger.info("Metered %d deployments, total %d cents", len(result), sum(result.values()))

        return result


service = ControlPlaneService()

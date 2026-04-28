from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from greencompute_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greencompute_persistence.db import needs_bootstrap
from greencompute_persistence.orm import (
    BuildORM,
    CapacityHistoryORM,
    CapacityORM,
    DeploymentEventORM,
    DeploymentORM,
    HeartbeatORM,
    InvocationRecordORM,
    LeaseAssignmentORM,
    LeaseHistoryORM,
    MinerORM,
    MinerWhitelistORM,
    NodeInventoryORM,
    PlacementORM,
    ServerORM,
    UsageRecordORM,
    WorkloadORM,
    WorkloadShareORM,
)
from greencompute_protocol import (
    CapacityHistoryRecord,
    CapacityUpdate,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    Heartbeat,
    InvocationRecord,
    LeaseAssignment,
    LeaseHistoryRecord,
    MinerRegistration,
    NodeCapability,
    PlacementRecord,
    ServerRecord,
    UsageRecord,
    WorkloadSpec,
)


def _serialize_port_mappings(pm: dict[int, int]) -> dict[str, int]:
    # JSON object keys must be strings; store {container_port_str: host_port_int}.
    return {str(k): int(v) for k, v in (pm or {}).items()}


def _deserialize_port_mappings(raw: object) -> dict[int, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


class ControlPlaneRepository:
    def __init__(self, database_url: str | None = None, bootstrap: bool | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    def upsert_miner(self, registration: MinerRegistration) -> MinerRegistration:
        with session_scope(self.session_factory) as session:
            row = session.get(MinerORM, registration.hotkey) or MinerORM(hotkey=registration.hotkey)
            row.payout_address = registration.payout_address
            row.api_base_url = registration.api_base_url
            row.validator_url = registration.validator_url
            row.auth_secret = registration.auth_secret
            row.drained = registration.drained
            row.supported_workload_kinds = [item.value for item in registration.supported_workload_kinds]
            session.add(row)
        return registration

    def get_miner(self, hotkey: str) -> MinerRegistration | None:
        with session_scope(self.session_factory) as session:
            row = session.get(MinerORM, hotkey)
            return self._to_registration(row) if row else None

    def list_miners(self) -> list[MinerRegistration]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(MinerORM)).all()
            return [self._to_registration(row) for row in rows]

    def is_hotkey_whitelisted(self, hotkey: str) -> bool:
        with session_scope(self.session_factory) as session:
            return session.get(MinerWhitelistORM, hotkey) is not None

    def set_miner_drained(self, hotkey: str, drained: bool) -> MinerRegistration | None:
        with session_scope(self.session_factory) as session:
            row = session.get(MinerORM, hotkey)
            if row is None:
                return None
            row.drained = drained
            session.add(row)
            session.flush()
            session.refresh(row)
            return self._to_registration(row)

    def upsert_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat:
        with session_scope(self.session_factory) as session:
            row = session.get(HeartbeatORM, heartbeat.hotkey) or HeartbeatORM(hotkey=heartbeat.hotkey)
            row.healthy = heartbeat.healthy
            row.active_deployments = heartbeat.active_deployments
            row.active_leases = heartbeat.active_leases
            row.observed_at = heartbeat.observed_at
            session.add(row)
        return heartbeat

    def get_heartbeat(self, hotkey: str) -> Heartbeat | None:
        with session_scope(self.session_factory) as session:
            row = session.get(HeartbeatORM, hotkey)
            return self._to_heartbeat(row) if row else None

    def upsert_capacity(self, update: CapacityUpdate) -> CapacityUpdate:
        with session_scope(self.session_factory) as session:
            row = session.get(CapacityORM, update.hotkey) or CapacityORM(hotkey=update.hotkey)
            normalized_nodes = [self._normalize_node(update.hotkey, node, update.observed_at) for node in update.nodes]

            # Merge by node_id with the existing nodes array. Supports the
            # "single hotkey, multiple physical boxes" deployment pattern
            # where each box's heartbeat carries only its own nodes — without
            # this merge, the latest heartbeat would silently overwrite the
            # others' entries (capacities row's primary key is just `hotkey`).
            #
            # Stale entries (older than `node_inventory_timeout_seconds`) are
            # dropped here so a dead box's row doesn't linger forever.
            from greencompute_control_plane.config import settings as _cp_settings

            existing = row.nodes or []
            incoming_ids = {n.node_id for n in normalized_nodes}
            stale_cutoff = update.observed_at - timedelta(
                seconds=_cp_settings.node_inventory_timeout_seconds
            )
            preserved: list[dict[str, Any]] = []
            for prev in existing:
                if not isinstance(prev, dict):
                    continue
                if prev.get("node_id") in incoming_ids:
                    # Will be replaced by the fresh entry below
                    continue
                obs_str = prev.get("observed_at")
                obs_dt: datetime | None = None
                if isinstance(obs_str, str):
                    try:
                        obs_dt = datetime.fromisoformat(obs_str.replace("Z", "+00:00"))
                    except ValueError:
                        obs_dt = None
                if obs_dt is not None and obs_dt < stale_cutoff:
                    continue
                preserved.append(prev)
            preserved.extend(node.model_dump(mode="json") for node in normalized_nodes)
            row.nodes = preserved
            row.observed_at = update.observed_at
            session.add(row)
            miner = session.get(MinerORM, update.hotkey)
            for node in normalized_nodes:
                server_id = node.server_id or f"{update.hotkey}-server"
                server = session.get(ServerORM, server_id) or ServerORM(server_id=server_id)
                server.hotkey = update.hotkey
                server.hostname = node.hostname
                server.api_base_url = miner.api_base_url if miner is not None else None
                server.validator_url = miner.validator_url if miner is not None else None
                server.observed_at = update.observed_at
                session.add(server)

                inventory = session.get(NodeInventoryORM, node.node_id) or NodeInventoryORM(node_id=node.node_id)
                inventory.hotkey = update.hotkey
                inventory.server_id = server_id
                inventory.payload = node.model_dump(mode="json")
                inventory.observed_at = update.observed_at
                session.add(inventory)

                session.add(
                    CapacityHistoryORM(
                        history_id=CapacityHistoryRecord(
                            hotkey=update.hotkey,
                            server_id=server_id,
                            node_id=node.node_id,
                            available_gpus=node.available_gpus,
                            total_gpus=node.gpu_count,
                            observed_at=update.observed_at,
                        ).history_id,
                        hotkey=update.hotkey,
                        server_id=server_id,
                        node_id=node.node_id,
                        available_gpus=node.available_gpus,
                        total_gpus=node.gpu_count,
                        observed_at=update.observed_at,
                    )
                )
        return update

    def get_capacity(self, hotkey: str) -> CapacityUpdate | None:
        with session_scope(self.session_factory) as session:
            row = session.get(CapacityORM, hotkey)
            return self._to_capacity(row) if row else None

    def list_capacities(self) -> list[CapacityUpdate]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(CapacityORM)).all()
            return [self._to_capacity(row) for row in rows]

    def adjust_node_capacity(self, hotkey: str, node_id: str, delta_gpus: int) -> CapacityUpdate | None:
        update = self.get_capacity(hotkey)
        if update is None:
            return None
        for node in update.nodes:
            if node.node_id == node_id:
                node.available_gpus += delta_gpus
                break
        return self.upsert_capacity(update)

    def upsert_workload(self, workload: WorkloadSpec) -> WorkloadSpec:
        with session_scope(self.session_factory) as session:
            row = session.get(WorkloadORM, workload.workload_id) or WorkloadORM(workload_id=workload.workload_id)
            row.owner_user_id = workload.owner_user_id
            row.name = workload.name
            row.image = workload.image
            row.display_name = workload.display_name
            row.readme = workload.readme
            row.logo_uri = workload.logo_uri
            row.tags = workload.tags
            row.workload_alias = workload.workload_alias
            row.ingress_host = workload.ingress_host
            row.kind = workload.kind.value
            row.security_tier = workload.security_tier.value
            row.pricing_class = workload.pricing_class
            row.requirements = workload.requirements.model_dump(mode="json")
            row.runtime = workload.runtime.model_dump(mode="json")
            row.lifecycle = workload.lifecycle.model_dump(mode="json")
            row.public = workload.public
            row.metadata_json = workload.metadata
            row.created_at = workload.created_at
            session.add(row)
        return workload

    def get_workload(self, workload_id: str) -> WorkloadSpec | None:
        with session_scope(self.session_factory) as session:
            row = session.get(WorkloadORM, workload_id)
            return self._to_workload(row) if row else None

    def find_workload_by_name(self, name: str) -> WorkloadSpec | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(WorkloadORM).where(WorkloadORM.name == name))
            return self._to_workload(row) if row else None

    def find_workload_by_alias(self, alias: str) -> WorkloadSpec | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(WorkloadORM).where(WorkloadORM.workload_alias == alias))
            return self._to_workload(row) if row else None

    def find_workload_by_ingress_host(self, ingress_host: str) -> WorkloadSpec | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(WorkloadORM).where(WorkloadORM.ingress_host == ingress_host))
            return self._to_workload(row) if row else None

    def list_workloads(self) -> list[WorkloadSpec]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(WorkloadORM)).all()
            return [self._to_workload(row) for row in rows]

    def delete_workload(self, workload_id: str) -> WorkloadSpec | None:
        with session_scope(self.session_factory) as session:
            row = session.get(WorkloadORM, workload_id)
            if row is None:
                return None
            workload = self._to_workload(row)
            deployment_ids = [
                r.deployment_id
                for r in session.scalars(select(DeploymentORM).where(DeploymentORM.workload_id == workload_id)).all()
            ]
            for dep_id in deployment_ids:
                for la in session.scalars(select(LeaseAssignmentORM).where(LeaseAssignmentORM.deployment_id == dep_id)).all():
                    session.delete(la)
                for p in session.scalars(select(PlacementORM).where(PlacementORM.deployment_id == dep_id)).all():
                    session.delete(p)
                for lh in session.scalars(select(LeaseHistoryORM).where(LeaseHistoryORM.deployment_id == dep_id)).all():
                    session.delete(lh)
                for de in session.scalars(select(DeploymentEventORM).where(DeploymentEventORM.deployment_id == dep_id)).all():
                    session.delete(de)
                for ur in session.scalars(select(UsageRecordORM).where(UsageRecordORM.deployment_id == dep_id)).all():
                    session.delete(ur)
                for inv in session.scalars(select(InvocationRecordORM).where(InvocationRecordORM.deployment_id == dep_id)).all():
                    session.delete(inv)
            for share in session.scalars(select(WorkloadShareORM).where(WorkloadShareORM.workload_id == workload_id)).all():
                session.delete(share)
            for dep in session.scalars(select(DeploymentORM).where(DeploymentORM.workload_id == workload_id)).all():
                session.delete(dep)
            session.delete(row)
            return workload

    def create_deployment(self, deployment: DeploymentRecord) -> DeploymentRecord:
        with session_scope(self.session_factory) as session:
            row = DeploymentORM(
                deployment_id=deployment.deployment_id,
                workload_id=deployment.workload_id,
                owner_user_id=deployment.owner_user_id,
                hotkey=deployment.hotkey,
                node_id=deployment.node_id,
                state=deployment.state.value,
                requested_instances=deployment.requested_instances,
                ready_instances=deployment.ready_instances,
                endpoint=deployment.endpoint,
                ssh_private_key=deployment.ssh_private_key,
                port_mappings=_serialize_port_mappings(deployment.port_mappings),
                hourly_rate_cents=deployment.hourly_rate_cents,
                deployment_fee_usd=deployment.deployment_fee_usd,
                fee_acknowledged=deployment.fee_acknowledged,
                warmup_state=deployment.warmup_state,
                last_error=deployment.last_error,
                failure_class=deployment.failure_class,
                last_retry_reason=deployment.last_retry_reason,
                retry_count=deployment.retry_count,
                retry_exhausted=deployment.retry_exhausted,
                health_check_failures=deployment.health_check_failures,
                created_at=deployment.created_at,
                updated_at=deployment.updated_at,
            )
            session.add(row)
        return deployment

    def get_deployment(self, deployment_id: str) -> DeploymentRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(DeploymentORM, deployment_id)
            return self._to_deployment(row) if row else None

    def update_deployment(self, deployment: DeploymentRecord) -> DeploymentRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(DeploymentORM, deployment.deployment_id)
            if row is None:
                row = DeploymentORM(deployment_id=deployment.deployment_id, workload_id=deployment.workload_id)
            row.owner_user_id = deployment.owner_user_id
            row.hotkey = deployment.hotkey
            row.node_id = deployment.node_id
            row.state = deployment.state.value
            row.requested_instances = deployment.requested_instances
            row.ready_instances = deployment.ready_instances
            row.endpoint = deployment.endpoint
            row.ssh_private_key = deployment.ssh_private_key
            row.port_mappings = _serialize_port_mappings(deployment.port_mappings)
            row.hourly_rate_cents = deployment.hourly_rate_cents
            row.deployment_fee_usd = deployment.deployment_fee_usd
            row.fee_acknowledged = deployment.fee_acknowledged
            row.warmup_state = deployment.warmup_state
            row.last_error = deployment.last_error
            row.failure_class = deployment.failure_class
            row.last_retry_reason = deployment.last_retry_reason
            row.retry_count = deployment.retry_count
            row.retry_exhausted = deployment.retry_exhausted
            row.health_check_failures = deployment.health_check_failures
            row.created_at = deployment.created_at
            row.updated_at = deployment.updated_at
            session.add(row)
        return deployment

    def list_deployments(self) -> list[DeploymentRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(DeploymentORM)).all()
            return [self._to_deployment(row) for row in rows]

    def list_ready_deployments(self, workload_id: str) -> list[DeploymentRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(DeploymentORM).where(
                    DeploymentORM.workload_id == workload_id,
                    DeploymentORM.state == DeploymentState.READY.value,
                )
            ).all()
            return [self._to_deployment(row) for row in rows]

    def list_deployments_by_state(self, state: DeploymentState) -> list[DeploymentRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(DeploymentORM).where(DeploymentORM.state == state.value)
            ).all()
            return [self._to_deployment(row) for row in rows]

    def accrue_metering(
        self,
        deployment_id: str,
        *,
        add_mcents: int,
    ) -> tuple[int, int]:
        """Accumulate per-minute fractional-cent usage on the deployment row.

        Returns (whole_cents_to_debit, new_remainder_mcents). Only the
        whole-cent portion gets debited from the user; the remainder stays
        on the row for the next cycle so hourly totals converge exactly.

        Atomic — a single SELECT FOR UPDATE / UPDATE inside one session so
        two concurrent metering cycles (shouldn't happen, but) can't both
        read and double-debit. Returns (0, 0) if the deployment is missing.
        """
        with session_scope(self.session_factory) as session:
            row = session.get(DeploymentORM, deployment_id, with_for_update=True)
            if row is None:
                return 0, 0
            total = (row.metering_remainder_mcents or 0) + add_mcents
            whole_cents = total // 1000
            remainder = total - whole_cents * 1000
            row.metering_remainder_mcents = remainder
            session.add(row)
            return whole_cents, remainder

    def save_assignment(self, assignment: LeaseAssignment) -> LeaseAssignment:
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(LeaseAssignmentORM).where(LeaseAssignmentORM.deployment_id == assignment.deployment_id)
            ) or LeaseAssignmentORM(assignment_id=assignment.assignment_id, deployment_id=assignment.deployment_id)
            row.workload_id = assignment.workload_id
            row.hotkey = assignment.hotkey
            row.node_id = assignment.node_id
            row.assigned_at = assignment.assigned_at
            row.expires_at = assignment.expires_at
            row.status = assignment.status
            session.add(row)
            session.add(
                LeaseHistoryORM(
                    event_id=LeaseHistoryRecord(
                        deployment_id=assignment.deployment_id,
                        workload_id=assignment.workload_id,
                        hotkey=assignment.hotkey,
                        node_id=assignment.node_id,
                        status=assignment.status,
                        observed_at=assignment.assigned_at,
                    ).event_id,
                    deployment_id=assignment.deployment_id,
                    workload_id=assignment.workload_id,
                    hotkey=assignment.hotkey,
                    node_id=assignment.node_id,
                    status=assignment.status,
                    reason=None,
                    observed_at=assignment.assigned_at,
                )
            )
            server_id = self._resolve_server_id(session, assignment.hotkey, assignment.node_id)
            session.add(
                PlacementORM(
                    placement_id=PlacementRecord(
                        deployment_id=assignment.deployment_id,
                        workload_id=assignment.workload_id,
                        hotkey=assignment.hotkey,
                        server_id=server_id,
                        node_id=assignment.node_id,
                        status=assignment.status,
                    ).placement_id,
                    deployment_id=assignment.deployment_id,
                    workload_id=assignment.workload_id,
                    hotkey=assignment.hotkey,
                    server_id=server_id,
                    node_id=assignment.node_id,
                    status=assignment.status,
                    reason=None,
                    created_at=assignment.assigned_at,
                    updated_at=assignment.assigned_at,
                )
            )
        return assignment

    def list_assignments(self, hotkey: str | None = None, statuses: list[str] | None = None) -> list[LeaseAssignment]:
        with session_scope(self.session_factory) as session:
            stmt = select(LeaseAssignmentORM)
            if hotkey:
                stmt = stmt.where(LeaseAssignmentORM.hotkey == hotkey)
            if statuses:
                stmt = stmt.where(LeaseAssignmentORM.status.in_(statuses))
            rows = session.scalars(stmt).all()
            return [self._to_assignment(row) for row in rows]

    def update_assignment_status(self, deployment_id: str, status: str, reason: str | None = None) -> LeaseAssignment | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(LeaseAssignmentORM).where(LeaseAssignmentORM.deployment_id == deployment_id))
            if row is None:
                return None
            row.status = status
            session.add(row)
            placement = session.scalar(
                select(PlacementORM)
                .where(PlacementORM.deployment_id == deployment_id)
                .order_by(PlacementORM.created_at.desc())
            )
            if placement is not None:
                placement.status = status
                placement.reason = reason
                placement.updated_at = datetime.now(UTC)
                session.add(placement)
            observed_at = datetime.now(UTC)
            session.add(
                LeaseHistoryORM(
                    event_id=LeaseHistoryRecord(
                        deployment_id=row.deployment_id,
                        workload_id=row.workload_id,
                        hotkey=row.hotkey,
                        node_id=row.node_id,
                        status=status,
                        reason=reason,
                        observed_at=observed_at,
                    ).event_id,
                    deployment_id=row.deployment_id,
                    workload_id=row.workload_id,
                    hotkey=row.hotkey,
                    node_id=row.node_id,
                    status=status,
                    reason=reason,
                    observed_at=observed_at,
                )
            )
            session.flush()
            session.refresh(row)
            return self._to_assignment(row)

    def update_placement_status(
        self,
        deployment_id: str,
        status: str,
        *,
        reason: str | None = None,
        increment_failure: bool = False,
        cooldown_until: datetime | None = None,
    ) -> PlacementRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(PlacementORM)
                .where(PlacementORM.deployment_id == deployment_id)
                .order_by(PlacementORM.created_at.desc())
            )
            if row is None:
                return None
            row.status = status
            row.reason = reason
            if increment_failure:
                row.failure_count += 1
            if cooldown_until is not None:
                row.cooldown_until = cooldown_until
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            session.refresh(row)
            return PlacementRecord(
                placement_id=row.placement_id,
                deployment_id=row.deployment_id,
                workload_id=row.workload_id,
                hotkey=row.hotkey,
                server_id=row.server_id,
                node_id=row.node_id,
                status=row.status,
                reason=row.reason,
                failure_count=row.failure_count,
                cooldown_until=row.cooldown_until,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def add_usage_record(self, record: UsageRecord) -> UsageRecord:
        with session_scope(self.session_factory) as session:
            row = UsageRecordORM(
                deployment_id=record.deployment_id,
                workload_id=record.workload_id,
                hotkey=record.hotkey,
                request_count=record.request_count,
                streamed_request_count=record.streamed_request_count,
                stream_chunk_count=record.stream_chunk_count,
                compute_seconds=record.compute_seconds,
                latency_ms_p95=record.latency_ms_p95,
                occupancy_seconds=record.occupancy_seconds,
                measured_at=record.measured_at,
            )
            session.add(row)
        return record

    def add_invocation_record(self, record: InvocationRecord) -> InvocationRecord:
        with session_scope(self.session_factory) as session:
            row = InvocationRecordORM(
                invocation_id=record.invocation_id,
                request_id=record.request_id,
                deployment_id=record.deployment_id,
                workload_id=record.workload_id,
                hotkey=record.hotkey,
                model=record.model,
                api_key_id=record.api_key_id,
                routed_host=record.routed_host,
                resolution_basis=record.resolution_basis,
                routing_reason=record.routing_reason,
                stream=record.stream,
                status=record.status,
                error_class=record.error_class,
                latency_ms=record.latency_ms,
                message_count=record.message_count,
                created_at=record.created_at,
            )
            session.add(row)
        return record

    def list_usage_records(self) -> list[UsageRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(UsageRecordORM)).all()
            return [self._to_usage_record(row) for row in rows]

    def get_invocation_record(self, invocation_id: str) -> InvocationRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(InvocationRecordORM, invocation_id)
            return self._to_invocation_record(row) if row is not None else None

    def list_invocation_records(self, limit: int | None = None) -> list[InvocationRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(InvocationRecordORM).order_by(InvocationRecordORM.created_at.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [self._to_invocation_record(row) for row in rows]

    def last_invocation_at(self, deployment_id: str) -> datetime | None:
        """Return the timestamp of the most recent invocation for a deployment,
        or None if the deployment has never been invoked. Used by the idle-kill
        loop to decide whether a private-endpoint inference deployment should
        be auto-suspended."""
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(InvocationRecordORM)
                .where(InvocationRecordORM.deployment_id == deployment_id)
                .order_by(InvocationRecordORM.created_at.desc())
                .limit(1)
            )
            return row.created_at if row is not None else None

    def add_deployment_event(self, event: DeploymentStatusUpdate) -> DeploymentStatusUpdate:
        with session_scope(self.session_factory) as session:
            row = DeploymentEventORM(
                deployment_id=event.deployment_id,
                payload=event.model_dump(mode="json"),
                observed_at=event.observed_at,
            )
            session.add(row)
        return event

    def list_deployment_events(self, deployment_id: str | None = None) -> list[dict]:
        with session_scope(self.session_factory) as session:
            stmt = select(DeploymentEventORM)
            if deployment_id:
                stmt = stmt.where(DeploymentEventORM.deployment_id == deployment_id)
            rows = session.scalars(stmt.order_by(DeploymentEventORM.observed_at.asc())).all()
            return [row.payload for row in rows]

    def list_servers(self) -> list[ServerRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(ServerORM).order_by(ServerORM.observed_at.desc(), ServerORM.server_id.asc())).all()
            return [
                ServerRecord(
                    server_id=row.server_id,
                    hotkey=row.hotkey,
                    hostname=row.hostname,
                    api_base_url=row.api_base_url,
                    validator_url=row.validator_url,
                    observed_at=row.observed_at,
                )
                for row in rows
            ]

    def get_server_by_hotkey(self, hotkey: str) -> ServerRecord | None:
        """Return the most recently observed server for a given miner hotkey."""
        with session_scope(self.session_factory) as session:
            row = session.scalars(
                select(ServerORM)
                .where(ServerORM.hotkey == hotkey)
                .order_by(ServerORM.observed_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            return ServerRecord(
                server_id=row.server_id,
                hotkey=row.hotkey,
                hostname=row.hostname,
                api_base_url=row.api_base_url,
                validator_url=row.validator_url,
                observed_at=row.observed_at,
            )

    def list_nodes(self) -> list[NodeCapability]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(NodeInventoryORM).order_by(NodeInventoryORM.observed_at.desc())).all()
            return [NodeCapability(**row.payload) for row in rows]

    def list_builds(self) -> list[dict]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(BuildORM).order_by(BuildORM.created_at.desc())).all()
            return [
                {
                    "build_id": row.build_id,
                    "image": row.image,
                    "status": row.status,
                    "artifact_uri": row.artifact_uri,
                    "artifact_digest": row.artifact_digest,
                }
                for row in rows
            ]

    def list_capacity_history(self, limit: int | None = None) -> list[CapacityHistoryRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(CapacityHistoryORM).order_by(CapacityHistoryORM.observed_at.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [
                CapacityHistoryRecord(
                    history_id=row.history_id,
                    hotkey=row.hotkey,
                    server_id=row.server_id,
                    node_id=row.node_id,
                    available_gpus=row.available_gpus,
                    total_gpus=row.total_gpus,
                    observed_at=row.observed_at,
                )
                for row in rows
            ]

    def list_placements(self, limit: int | None = None) -> list[PlacementRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(PlacementORM).order_by(PlacementORM.updated_at.desc(), PlacementORM.created_at.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [
                PlacementRecord(
                    placement_id=row.placement_id,
                    deployment_id=row.deployment_id,
                    workload_id=row.workload_id,
                    hotkey=row.hotkey,
                    server_id=row.server_id,
                    node_id=row.node_id,
                    status=row.status,
                    reason=row.reason,
                    failure_count=row.failure_count,
                    cooldown_until=row.cooldown_until,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    def list_lease_history(self, limit: int | None = None) -> list[LeaseHistoryRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(LeaseHistoryORM).order_by(LeaseHistoryORM.observed_at.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [
                LeaseHistoryRecord(
                    event_id=row.event_id,
                    deployment_id=row.deployment_id,
                    workload_id=row.workload_id,
                    hotkey=row.hotkey,
                    node_id=row.node_id,
                    status=row.status,
                    reason=row.reason,
                    observed_at=row.observed_at,
                )
                for row in rows
            ]

    @staticmethod
    def _to_registration(row: MinerORM) -> MinerRegistration:
        return MinerRegistration(
            hotkey=row.hotkey,
            payout_address=row.payout_address,
            api_base_url=row.api_base_url,
            validator_url=row.validator_url,
            drained=row.drained,
            auth_secret=row.auth_secret,
            supported_workload_kinds=row.supported_workload_kinds,
        )

    @staticmethod
    def _to_heartbeat(row: HeartbeatORM) -> Heartbeat:
        return Heartbeat(
            hotkey=row.hotkey,
            healthy=row.healthy,
            active_deployments=row.active_deployments,
            active_leases=row.active_leases,
            observed_at=row.observed_at,
        )

    @staticmethod
    def _to_capacity(row: CapacityORM) -> CapacityUpdate:
        return CapacityUpdate(hotkey=row.hotkey, nodes=row.nodes, observed_at=row.observed_at)

    @staticmethod
    def _to_workload(row: WorkloadORM) -> WorkloadSpec:
        return WorkloadSpec(
            workload_id=row.workload_id,
            owner_user_id=row.owner_user_id,
            name=row.name,
            image=row.image,
            display_name=row.display_name,
            readme=row.readme,
            logo_uri=row.logo_uri,
            tags=row.tags or [],
            workload_alias=row.workload_alias,
            ingress_host=row.ingress_host,
            kind=row.kind,
            security_tier=row.security_tier,
            pricing_class=row.pricing_class,
            requirements=row.requirements,
            runtime=row.runtime,
            lifecycle=row.lifecycle or {},
            public=row.public,
            metadata=row.metadata_json or {},
            created_at=row.created_at,
        )

    @staticmethod
    def _to_deployment(row: DeploymentORM) -> DeploymentRecord:
        return DeploymentRecord(
            deployment_id=row.deployment_id,
            workload_id=row.workload_id,
            owner_user_id=row.owner_user_id,
            hotkey=row.hotkey,
            node_id=row.node_id,
            state=row.state,
            requested_instances=row.requested_instances,
            ready_instances=row.ready_instances,
            endpoint=row.endpoint,
            ssh_private_key=row.ssh_private_key,
            port_mappings=_deserialize_port_mappings(row.port_mappings),
            hourly_rate_cents=row.hourly_rate_cents if row.hourly_rate_cents is not None else 10,
            deployment_fee_usd=row.deployment_fee_usd,
            fee_acknowledged=row.fee_acknowledged,
            warmup_state=row.warmup_state,
            last_error=row.last_error,
            failure_class=row.failure_class,
            last_retry_reason=row.last_retry_reason,
            retry_count=row.retry_count,
            retry_exhausted=row.retry_exhausted,
            health_check_failures=row.health_check_failures,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_assignment(row: LeaseAssignmentORM) -> LeaseAssignment:
        return LeaseAssignment(
            assignment_id=row.assignment_id,
            deployment_id=row.deployment_id,
            workload_id=row.workload_id,
            hotkey=row.hotkey,
            node_id=row.node_id,
            assigned_at=row.assigned_at,
            expires_at=row.expires_at,
            status=row.status,
        )

    @staticmethod
    def _to_usage_record(row: UsageRecordORM) -> UsageRecord:
        return UsageRecord(
            deployment_id=row.deployment_id,
            workload_id=row.workload_id,
            hotkey=row.hotkey,
            request_count=row.request_count,
            streamed_request_count=row.streamed_request_count,
            stream_chunk_count=row.stream_chunk_count,
            compute_seconds=row.compute_seconds,
            latency_ms_p95=row.latency_ms_p95,
            occupancy_seconds=row.occupancy_seconds,
            measured_at=row.measured_at,
        )

    @staticmethod
    def _to_invocation_record(row: InvocationRecordORM) -> InvocationRecord:
        return InvocationRecord(
            invocation_id=row.invocation_id,
            request_id=row.request_id,
            deployment_id=row.deployment_id,
            workload_id=row.workload_id,
            hotkey=row.hotkey,
            model=row.model,
            api_key_id=row.api_key_id,
            routed_host=row.routed_host,
            resolution_basis=row.resolution_basis,
            routing_reason=row.routing_reason,
            stream=row.stream,
            status=row.status,
            error_class=row.error_class,
            latency_ms=row.latency_ms,
            message_count=row.message_count,
            created_at=row.created_at,
        )

    @staticmethod
    def _normalize_node(hotkey: str, node: NodeCapability, observed_at: datetime) -> NodeCapability:
        server_id = node.server_id or f"{hotkey}-server"
        hostname = node.hostname or f"{server_id}.greencompute.local"
        return node.model_copy(
            update={"hotkey": hotkey, "server_id": server_id, "hostname": hostname, "observed_at": observed_at}
        )

    @staticmethod
    def _resolve_server_id(session: Session, hotkey: str, node_id: str) -> str | None:
        inventory = session.get(NodeInventoryORM, node_id)
        if inventory is not None:
            return inventory.server_id
        server = session.scalar(select(ServerORM).where(ServerORM.hotkey == hotkey))
        return server.server_id if server is not None else None

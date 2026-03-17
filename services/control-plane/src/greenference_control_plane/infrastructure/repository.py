from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.db import needs_bootstrap
from greenference_persistence.orm import (
    CapacityHistoryORM,
    CapacityORM,
    DeploymentEventORM,
    DeploymentORM,
    HeartbeatORM,
    InvocationRecordORM,
    LeaseAssignmentORM,
    MinerORM,
    NodeInventoryORM,
    PlacementORM,
    ServerORM,
    UsageRecordORM,
    WorkloadORM,
)
from greenference_protocol import (
    CapacityHistoryRecord,
    CapacityUpdate,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    Heartbeat,
    InvocationRecord,
    LeaseAssignment,
    MinerRegistration,
    NodeCapability,
    PlacementRecord,
    ServerRecord,
    UsageRecord,
    WorkloadSpec,
)


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
            normalized_nodes = [self._normalize_node(update.hotkey, node) for node in update.nodes]
            row.nodes = [node.model_dump(mode="json") for node in normalized_nodes]
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
            row.name = workload.name
            row.image = workload.image
            row.kind = workload.kind.value
            row.security_tier = workload.security_tier.value
            row.pricing_class = workload.pricing_class
            row.requirements = workload.requirements.model_dump(mode="json")
            row.public = workload.public
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

    def list_workloads(self) -> list[WorkloadSpec]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(WorkloadORM)).all()
            return [self._to_workload(row) for row in rows]

    def create_deployment(self, deployment: DeploymentRecord) -> DeploymentRecord:
        with session_scope(self.session_factory) as session:
            row = DeploymentORM(
                deployment_id=deployment.deployment_id,
                workload_id=deployment.workload_id,
                hotkey=deployment.hotkey,
                node_id=deployment.node_id,
                state=deployment.state.value,
                requested_instances=deployment.requested_instances,
                ready_instances=deployment.ready_instances,
                endpoint=deployment.endpoint,
                last_error=deployment.last_error,
                retry_count=deployment.retry_count,
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
            row.hotkey = deployment.hotkey
            row.node_id = deployment.node_id
            row.state = deployment.state.value
            row.requested_instances = deployment.requested_instances
            row.ready_instances = deployment.ready_instances
            row.endpoint = deployment.endpoint
            row.last_error = deployment.last_error
            row.retry_count = deployment.retry_count
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
            session.flush()
            session.refresh(row)
            return self._to_assignment(row)

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

    def list_nodes(self) -> list[NodeCapability]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(NodeInventoryORM).order_by(NodeInventoryORM.observed_at.desc())).all()
            return [NodeCapability(**row.payload) for row in rows]

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
                    created_at=row.created_at,
                    updated_at=row.updated_at,
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
            name=row.name,
            image=row.image,
            kind=row.kind,
            security_tier=row.security_tier,
            pricing_class=row.pricing_class,
            requirements=row.requirements,
            public=row.public,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_deployment(row: DeploymentORM) -> DeploymentRecord:
        return DeploymentRecord(
            deployment_id=row.deployment_id,
            workload_id=row.workload_id,
            hotkey=row.hotkey,
            node_id=row.node_id,
            state=row.state,
            requested_instances=row.requested_instances,
            ready_instances=row.ready_instances,
            endpoint=row.endpoint,
            last_error=row.last_error,
            retry_count=row.retry_count,
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
            stream=row.stream,
            status=row.status,
            error_class=row.error_class,
            latency_ms=row.latency_ms,
            message_count=row.message_count,
            created_at=row.created_at,
        )

    @staticmethod
    def _normalize_node(hotkey: str, node: NodeCapability) -> NodeCapability:
        server_id = node.server_id or f"{hotkey}-server"
        hostname = node.hostname or f"{server_id}.greenference.local"
        return node.model_copy(update={"hotkey": hotkey, "server_id": server_id, "hostname": hostname})

    @staticmethod
    def _resolve_server_id(session: Session, hotkey: str, node_id: str) -> str | None:
        inventory = session.get(NodeInventoryORM, node_id)
        if inventory is not None:
            return inventory.server_id
        server = session.scalar(select(ServerORM).where(ServerORM.hotkey == hotkey))
        return server.server_id if server is not None else None

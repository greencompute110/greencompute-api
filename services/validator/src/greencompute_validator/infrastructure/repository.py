from __future__ import annotations

from sqlalchemy import select

from greencompute_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greencompute_persistence.db import needs_bootstrap
from greencompute_persistence.orm import (
    AuditReportORM,
    CatalogSubmissionORM,
    DeploymentORM,
    GreenEnergyApplicationORM,
    GreenEnergyAttachmentORM,
    LeaseAssignmentORM,
    MinerWhitelistORM,
    ModelCatalogORM,
    ProbeChallengeORM,
    ProbeResultORM,
    ScoreCardHistoryORM,
    ScoreCardORM,
    ValidatorCapabilityORM,
    WeightSnapshotORM,
    WorkloadORM,
)
from greencompute_protocol import (
    AuditReport,
    CatalogSubmission,
    GreenEnergyApplication,
    GreenEnergyAttachment,
    MinerWhitelistEntry,
    ModelCatalogEntry,
    NodeCapability,
    ProbeChallenge,
    ProbeResult,
    ScoreCard,
    WeightSnapshot,
)


class ValidatorRepository:
    def __init__(self, database_url: str | None = None, bootstrap: bool | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    # ──────────────────────────────────────────────────────────────────
    # ValidatorCapabilityORM is keyed by `hotkey` (one row per miner) but
    # supports multi-node-per-hotkey by nesting nodes inside the payload
    # JSON: payload = {"nodes": {node_id: <NodeCapability dict>, ...}}.
    #
    # Why: GPU-infra subnets like Lium/Chutes operate one logical "miner"
    # (a single Bittensor hotkey identity) that controls many physical
    # boxes. Without this nesting, every box's heartbeat would silently
    # overwrite the others' entries since the row is keyed by hotkey.
    #
    # Back-compat: `get_capability(hotkey)` and `list_capabilities()` still
    # return one NodeCapability per hotkey (a representative node) so
    # existing callers don't break. New per-node methods are below.
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _payload_to_nodes(payload: dict | None) -> list[NodeCapability]:
        """Parse stored payload into the per-node NodeCapability list.
        Handles both new shape ({"nodes": {...}}) and legacy single-node."""
        if not payload:
            return []
        if "nodes" in payload and isinstance(payload["nodes"], dict):
            return [NodeCapability(**n) for n in payload["nodes"].values()]
        # Legacy: payload was a single NodeCapability dict
        try:
            return [NodeCapability(**payload)]
        except Exception:
            return []

    def upsert_capability(self, capability: NodeCapability) -> NodeCapability:
        with session_scope(self.session_factory) as session:
            row = session.get(ValidatorCapabilityORM, capability.hotkey) or ValidatorCapabilityORM(
                hotkey=capability.hotkey, payload={"nodes": {}}
            )
            payload = dict(row.payload or {})
            nodes = dict(payload.get("nodes") or {})
            # Migrate legacy single-payload rows to multi-node shape on first write
            if not nodes and payload and "node_id" in payload:
                try:
                    legacy = NodeCapability(**payload)
                    nodes[legacy.node_id] = legacy.model_dump(mode="json")
                except Exception:
                    pass
            nodes[capability.node_id] = capability.model_dump(mode="json")
            row.payload = {"nodes": nodes}
            session.add(row)
        return capability

    def get_capability(self, hotkey: str) -> NodeCapability | None:
        """Return one representative NodeCapability for this hotkey.

        Back-compat wrapper for callers that don't care about multi-node.
        For multi-node-aware code, use `get_node_capabilities` instead."""
        nodes = self.get_node_capabilities(hotkey)
        return nodes[0] if nodes else None

    def get_node_capabilities(self, hotkey: str) -> list[NodeCapability]:
        with session_scope(self.session_factory) as session:
            row = session.get(ValidatorCapabilityORM, hotkey)
            return self._payload_to_nodes(row.payload) if row else []

    def list_capabilities(self) -> dict[str, NodeCapability]:
        """Back-compat: hotkey → one representative NodeCapability."""
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(ValidatorCapabilityORM)).all()
            out: dict[str, NodeCapability] = {}
            for row in rows:
                nodes = self._payload_to_nodes(row.payload)
                if nodes:
                    out[row.hotkey] = nodes[0]
            return out

    def list_node_capabilities(self) -> dict[str, list[NodeCapability]]:
        """Multi-node view: hotkey → list of NodeCapability (one per node)."""
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(ValidatorCapabilityORM)).all()
            return {row.hotkey: self._payload_to_nodes(row.payload) for row in rows}

    def sync_from_control_plane(self) -> int:
        """Mirror miners from control-plane's node_inventory → validator_capabilities.
        Aggregates multiple rows-per-hotkey from node_inventory into a single
        row in validator_capabilities, with all that hotkey's nodes nested
        under payload["nodes"]. Returns count of node rows ingested."""
        from greencompute_persistence.orm import NodeInventoryORM

        updated = 0
        # Group node_inventory rows by hotkey first, then write one
        # consolidated row per hotkey.
        by_hotkey: dict[str, dict[str, dict]] = {}
        with session_scope(self.session_factory) as session:
            nodes = session.scalars(select(NodeInventoryORM)).all()
            for node in nodes:
                payload = dict(node.payload or {})
                node_id = payload.get("node_id") or node.node_id
                by_hotkey.setdefault(node.hotkey, {})[node_id] = payload
                updated += 1

            for hotkey, nodes_dict in by_hotkey.items():
                cap_row = session.get(ValidatorCapabilityORM, hotkey)
                if cap_row is None:
                    cap_row = ValidatorCapabilityORM(
                        hotkey=hotkey, payload={"nodes": nodes_dict}
                    )
                else:
                    cap_row.payload = {"nodes": nodes_dict}
                session.add(cap_row)
        return updated

    def save_challenge(self, challenge: ProbeChallenge) -> ProbeChallenge:
        with session_scope(self.session_factory) as session:
            row = ProbeChallengeORM(
                challenge_id=challenge.challenge_id,
                hotkey=challenge.hotkey,
                node_id=challenge.node_id,
                kind=challenge.kind,
                payload=challenge.payload,
                created_at=challenge.created_at,
            )
            session.add(row)
        return challenge

    def get_challenge(self, challenge_id: str) -> ProbeChallenge | None:
        with session_scope(self.session_factory) as session:
            row = session.get(ProbeChallengeORM, challenge_id)
            if row is None:
                return None
            return ProbeChallenge(
                challenge_id=row.challenge_id,
                hotkey=row.hotkey,
                node_id=row.node_id,
                kind=row.kind,
                payload=row.payload,
                created_at=row.created_at,
            )

    def get_result(self, challenge_id: str, hotkey: str) -> ProbeResult | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(ProbeResultORM).where(
                    ProbeResultORM.challenge_id == challenge_id,
                    ProbeResultORM.hotkey == hotkey,
                )
            )
            if row is None:
                return None
            return ProbeResult(
                challenge_id=row.challenge_id,
                hotkey=row.hotkey,
                node_id=row.node_id,
                latency_ms=row.latency_ms,
                throughput=row.throughput,
                success=row.success,
                benchmark_signature=row.benchmark_signature,
                proxy_suspected=row.proxy_suspected,
                readiness_failures=row.readiness_failures,
                observed_at=row.observed_at,
            )

    def add_result(self, result: ProbeResult) -> ProbeResult:
        with session_scope(self.session_factory) as session:
            row = ProbeResultORM(
                challenge_id=result.challenge_id,
                hotkey=result.hotkey,
                node_id=result.node_id,
                latency_ms=result.latency_ms,
                throughput=result.throughput,
                success=result.success,
                benchmark_signature=result.benchmark_signature,
                proxy_suspected=result.proxy_suspected,
                readiness_failures=result.readiness_failures,
                observed_at=result.observed_at,
            )
            session.add(row)
        return result

    def list_results(self, hotkey: str | None = None) -> list[ProbeResult]:
        with session_scope(self.session_factory) as session:
            stmt = select(ProbeResultORM)
            if hotkey is not None:
                stmt = stmt.where(ProbeResultORM.hotkey == hotkey)
            rows = session.scalars(stmt).all()
            return [
                ProbeResult(
                    challenge_id=row.challenge_id,
                    hotkey=row.hotkey,
                    node_id=row.node_id,
                    latency_ms=row.latency_ms,
                    throughput=row.throughput,
                    success=row.success,
                    benchmark_signature=row.benchmark_signature,
                    proxy_suspected=row.proxy_suspected,
                    readiness_failures=row.readiness_failures,
                    observed_at=row.observed_at,
                )
                for row in rows
            ]

    def save_scorecard(self, scorecard: ScoreCard) -> ScoreCard:
        with session_scope(self.session_factory) as session:
            row = session.get(ScoreCardORM, scorecard.hotkey) or ScoreCardORM(hotkey=scorecard.hotkey)
            row.capacity_weight = scorecard.capacity_weight
            row.reliability_score = scorecard.reliability_score
            row.performance_score = scorecard.performance_score
            row.security_score = scorecard.security_score
            row.fraud_penalty = scorecard.fraud_penalty
            row.utilization_score = scorecard.utilization_score
            row.rental_revenue_bonus = scorecard.rental_revenue_bonus
            row.final_score = scorecard.final_score
            row.computed_at = scorecard.computed_at
            session.add(row)
        return scorecard

    def list_scorecards(self) -> dict[str, ScoreCard]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(ScoreCardORM)).all()
            return {
                row.hotkey: ScoreCard(
                    hotkey=row.hotkey,
                    capacity_weight=row.capacity_weight,
                    reliability_score=row.reliability_score,
                    performance_score=row.performance_score,
                    security_score=row.security_score,
                    fraud_penalty=row.fraud_penalty,
                    utilization_score=row.utilization_score,
                    rental_revenue_bonus=row.rental_revenue_bonus,
                    final_score=row.final_score,
                    computed_at=row.computed_at,
                )
                for row in rows
            }

    def save_snapshot(self, snapshot: WeightSnapshot) -> WeightSnapshot:
        with session_scope(self.session_factory) as session:
            row = WeightSnapshotORM(
                snapshot_id=snapshot.snapshot_id,
                netuid=snapshot.netuid,
                weights=snapshot.weights,
                created_at=snapshot.created_at,
            )
            session.add(row)
        return snapshot

    def list_snapshots(self, netuid: int | None = None) -> list[WeightSnapshot]:
        with session_scope(self.session_factory) as session:
            stmt = select(WeightSnapshotORM)
            if netuid is not None:
                stmt = stmt.where(WeightSnapshotORM.netuid == netuid)
            rows = session.scalars(stmt).all()
            return [
                WeightSnapshot(
                    snapshot_id=row.snapshot_id,
                    netuid=row.netuid,
                    weights=row.weights,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    # --- Miner whitelist ---

    def add_whitelist_entry(self, entry: MinerWhitelistEntry) -> MinerWhitelistEntry:
        with session_scope(self.session_factory) as session:
            row = session.get(MinerWhitelistORM, entry.hotkey) or MinerWhitelistORM(hotkey=entry.hotkey)
            row.label = entry.label
            row.energy_source = entry.energy_source
            row.notes = entry.notes
            row.approved_at = entry.approved_at
            session.add(row)
        return entry

    def remove_whitelist_entry(self, hotkey: str) -> bool:
        with session_scope(self.session_factory) as session:
            row = session.get(MinerWhitelistORM, hotkey)
            if row is None:
                return False
            session.delete(row)
            return True

    def get_whitelist_entry(self, hotkey: str) -> MinerWhitelistEntry | None:
        with session_scope(self.session_factory) as session:
            row = session.get(MinerWhitelistORM, hotkey)
            if row is None:
                return None
            return MinerWhitelistEntry(
                hotkey=row.hotkey,
                label=row.label,
                energy_source=row.energy_source,
                notes=row.notes or "",
                approved_at=row.approved_at,
            )

    def list_whitelist(self) -> list[MinerWhitelistEntry]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(MinerWhitelistORM)).all()
            return [
                MinerWhitelistEntry(
                    hotkey=row.hotkey,
                    label=row.label,
                    energy_source=row.energy_source,
                    notes=row.notes or "",
                    approved_at=row.approved_at,
                )
                for row in rows
            ]

    def is_whitelisted(self, hotkey: str) -> bool:
        with session_scope(self.session_factory) as session:
            return session.get(MinerWhitelistORM, hotkey) is not None

    # --- Green-energy applications ---

    def _app_from_orm(self, row: GreenEnergyApplicationORM) -> GreenEnergyApplication:
        return GreenEnergyApplication(
            application_id=row.application_id,
            hotkey=row.hotkey,
            signature=row.signature,
            organization=row.organization,
            energy_source=row.energy_source,
            description=row.description or "",
            status=row.status,
            reviewer_notes=row.reviewer_notes or "",
            submitted_at=row.submitted_at,
            reviewed_at=row.reviewed_at,
        )

    def _attachment_from_orm(self, row: GreenEnergyAttachmentORM) -> GreenEnergyAttachment:
        return GreenEnergyAttachment(
            attachment_id=row.attachment_id,
            application_id=row.application_id,
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            data_b64=row.data_b64,
            uploaded_at=row.uploaded_at,
        )

    def create_application(self, app: GreenEnergyApplication) -> GreenEnergyApplication:
        with session_scope(self.session_factory) as session:
            row = GreenEnergyApplicationORM(
                application_id=app.application_id,
                hotkey=app.hotkey,
                signature=app.signature,
                organization=app.organization,
                energy_source=app.energy_source,
                description=app.description,
                status=app.status,
                submitted_at=app.submitted_at,
            )
            session.add(row)
        return app

    def add_attachment(self, att: GreenEnergyAttachment) -> GreenEnergyAttachment:
        with session_scope(self.session_factory) as session:
            row = GreenEnergyAttachmentORM(
                attachment_id=att.attachment_id,
                application_id=att.application_id,
                filename=att.filename,
                content_type=att.content_type,
                size_bytes=att.size_bytes,
                data_b64=att.data_b64,
                uploaded_at=att.uploaded_at,
            )
            session.add(row)
        return att

    def list_applications(self, status: str | None = None) -> list[GreenEnergyApplication]:
        with session_scope(self.session_factory) as session:
            q = select(GreenEnergyApplicationORM).order_by(GreenEnergyApplicationORM.submitted_at.desc())
            if status:
                q = q.where(GreenEnergyApplicationORM.status == status)
            rows = session.scalars(q).all()
            return [self._app_from_orm(r) for r in rows]

    def get_application(self, application_id: str) -> GreenEnergyApplication | None:
        with session_scope(self.session_factory) as session:
            row = session.get(GreenEnergyApplicationORM, application_id)
            return self._app_from_orm(row) if row else None

    def list_applications_by_hotkey(self, hotkey: str) -> list[GreenEnergyApplication]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(GreenEnergyApplicationORM)
                .where(GreenEnergyApplicationORM.hotkey == hotkey)
                .order_by(GreenEnergyApplicationORM.submitted_at.desc())
            ).all()
            return [self._app_from_orm(r) for r in rows]

    def list_attachments(self, application_id: str) -> list[GreenEnergyAttachment]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(GreenEnergyAttachmentORM).where(
                    GreenEnergyAttachmentORM.application_id == application_id
                )
            ).all()
            return [self._attachment_from_orm(r) for r in rows]

    def get_attachment(self, attachment_id: str) -> GreenEnergyAttachment | None:
        with session_scope(self.session_factory) as session:
            row = session.get(GreenEnergyAttachmentORM, attachment_id)
            return self._attachment_from_orm(row) if row else None

    def update_application_status(
        self, application_id: str, status: str, reviewer_notes: str = ""
    ) -> GreenEnergyApplication | None:
        from datetime import UTC, datetime

        with session_scope(self.session_factory) as session:
            row = session.get(GreenEnergyApplicationORM, application_id)
            if row is None:
                return None
            row.status = status
            row.reviewer_notes = reviewer_notes
            row.reviewed_at = datetime.now(UTC)
            session.add(row)
            return self._app_from_orm(row)

    # ------------------------------------------------------------------
    # Model catalog + submissions — Chutes-style shared inference pool
    # ------------------------------------------------------------------

    @staticmethod
    def _catalog_from_orm(row: ModelCatalogORM) -> ModelCatalogEntry:
        return ModelCatalogEntry(
            model_id=row.model_id,
            display_name=row.display_name or "",
            hf_repo=row.hf_repo or "",
            template=row.template or "vllm",
            min_vram_gb_per_gpu=row.min_vram_gb_per_gpu or 24,
            gpu_count=row.gpu_count or 1,
            max_model_len=row.max_model_len,
            visibility=row.visibility or "public",
            min_replicas=row.min_replicas if row.min_replicas is not None else 1,
            max_replicas=row.max_replicas,
            admin_notes=row.admin_notes or "",
            created_by_hotkey=row.created_by_hotkey,
            created_at=row.created_at,
        )

    @staticmethod
    def _submission_from_orm(row: CatalogSubmissionORM) -> CatalogSubmission:
        return CatalogSubmission(
            submission_id=row.submission_id,
            model_id=row.model_id,
            hotkey=row.hotkey or "",
            signature=row.signature or "",
            display_name=row.display_name or "",
            hf_repo=row.hf_repo or "",
            template=row.template or "vllm",
            min_vram_gb_per_gpu=row.min_vram_gb_per_gpu or 24,
            gpu_count=row.gpu_count or 1,
            max_model_len=row.max_model_len,
            rationale=row.rationale or "",
            status=row.status or "pending",
            reviewer_notes=row.reviewer_notes or "",
            submitted_at=row.submitted_at,
            reviewed_at=row.reviewed_at,
        )

    def upsert_catalog_entry(self, entry: ModelCatalogEntry) -> ModelCatalogEntry:
        with session_scope(self.session_factory) as session:
            row = session.get(ModelCatalogORM, entry.model_id)
            if row is None:
                row = ModelCatalogORM(model_id=entry.model_id)
            row.display_name = entry.display_name
            row.hf_repo = entry.hf_repo
            row.template = entry.template
            row.min_vram_gb_per_gpu = entry.min_vram_gb_per_gpu
            row.gpu_count = entry.gpu_count
            row.max_model_len = entry.max_model_len
            row.visibility = entry.visibility
            row.min_replicas = entry.min_replicas
            row.max_replicas = entry.max_replicas
            row.admin_notes = entry.admin_notes
            row.created_by_hotkey = entry.created_by_hotkey
            session.add(row)
        return entry

    def get_catalog_entry(self, model_id: str) -> ModelCatalogEntry | None:
        with session_scope(self.session_factory) as session:
            row = session.get(ModelCatalogORM, model_id)
            return self._catalog_from_orm(row) if row else None

    def list_catalog_entries(self, visibility: str | None = None) -> list[ModelCatalogEntry]:
        with session_scope(self.session_factory) as session:
            q = select(ModelCatalogORM).order_by(ModelCatalogORM.created_at.desc())
            if visibility:
                q = q.where(ModelCatalogORM.visibility == visibility)
            rows = session.scalars(q).all()
            return [self._catalog_from_orm(r) for r in rows]

    def delete_catalog_entry(self, model_id: str) -> bool:
        with session_scope(self.session_factory) as session:
            row = session.get(ModelCatalogORM, model_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def create_catalog_submission(self, sub: CatalogSubmission) -> CatalogSubmission:
        with session_scope(self.session_factory) as session:
            row = CatalogSubmissionORM(
                submission_id=sub.submission_id,
                model_id=sub.model_id,
                hotkey=sub.hotkey,
                signature=sub.signature,
                display_name=sub.display_name,
                hf_repo=sub.hf_repo,
                template=sub.template,
                min_vram_gb_per_gpu=sub.min_vram_gb_per_gpu,
                gpu_count=sub.gpu_count,
                max_model_len=sub.max_model_len,
                rationale=sub.rationale,
                status=sub.status,
                submitted_at=sub.submitted_at,
            )
            session.add(row)
        return sub

    def get_catalog_submission(self, submission_id: str) -> CatalogSubmission | None:
        with session_scope(self.session_factory) as session:
            row = session.get(CatalogSubmissionORM, submission_id)
            return self._submission_from_orm(row) if row else None

    def list_catalog_submissions_by_hotkey(self, hotkey: str) -> list[CatalogSubmission]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(CatalogSubmissionORM)
                .where(CatalogSubmissionORM.hotkey == hotkey)
                .order_by(CatalogSubmissionORM.submitted_at.desc())
            ).all()
            return [self._submission_from_orm(r) for r in rows]

    def list_catalog_submissions(self, status: str | None = None) -> list[CatalogSubmission]:
        with session_scope(self.session_factory) as session:
            q = select(CatalogSubmissionORM).order_by(CatalogSubmissionORM.submitted_at.desc())
            if status:
                q = q.where(CatalogSubmissionORM.status == status)
            rows = session.scalars(q).all()
            return [self._submission_from_orm(r) for r in rows]

    def update_catalog_submission_status(
        self, submission_id: str, status: str, reviewer_notes: str = ""
    ) -> CatalogSubmission | None:
        from datetime import UTC, datetime

        with session_scope(self.session_factory) as session:
            row = session.get(CatalogSubmissionORM, submission_id)
            if row is None:
                return None
            row.status = status
            row.reviewer_notes = reviewer_notes
            row.reviewed_at = datetime.now(UTC)
            session.add(row)
            return self._submission_from_orm(row)

    # --- Flux-managed catalog deployments (Phase 2D) --------------------
    # Validator reconciles DeploymentORM rows directly so miners pick up
    # catalog replicas through their existing sync_leases poll. No new
    # HTTP path between miner and validator is needed.

    def get_catalog_workload_id(self, model_id: str) -> str | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(WorkloadORM).where(WorkloadORM.name == model_id)
            )
            return row.workload_id if row else None

    def list_flux_deployments(self, hotkey: str) -> list[dict]:
        """Return [(deployment_id, workload_id, model_id, state)] for live
        Flux-managed deployments on this miner. Excludes terminated (the
        reconciler treats terminated as 'no current replica')."""
        return self._list_flux_deployments(hotkey, include_terminated=False)

    def list_flux_deployments_incl_terminated(self, hotkey: str) -> list[dict]:
        """Same as list_flux_deployments but includes recently terminated
        rows. Used to populate failure cooldown — a recently-terminated
        (miner, model) pair blocks re-scheduling for a cooldown window."""
        return self._list_flux_deployments(hotkey, include_terminated=True)

    def _list_flux_deployments(self, hotkey: str, *, include_terminated: bool) -> list[dict]:
        with session_scope(self.session_factory) as session:
            stmt = (
                select(
                    DeploymentORM.deployment_id,
                    DeploymentORM.workload_id,
                    DeploymentORM.state,
                    DeploymentORM.updated_at,
                    WorkloadORM.metadata_json,
                )
                .join(WorkloadORM, WorkloadORM.workload_id == DeploymentORM.workload_id)
                .where(DeploymentORM.hotkey == hotkey)
            )
            if not include_terminated:
                stmt = stmt.where(DeploymentORM.state != "terminated")
            rows = session.execute(stmt).all()
            out: list[dict] = []
            for dep_id, wl_id, state, updated_at, meta in rows:
                meta = meta or {}
                if meta.get("managed_by") != "flux":
                    continue
                out.append({
                    "deployment_id": dep_id,
                    "workload_id": wl_id,
                    "model_id": meta.get("catalog_model_id"),
                    "state": state,
                    "updated_at": updated_at,
                })
            return out

    def create_flux_deployment(
        self,
        *,
        hotkey: str,
        node_id: str,
        workload_id: str,
    ) -> str:
        """Insert a DeploymentORM + LeaseAssignmentORM pinned to this miner.
        The miner picks it up on its next list_leases poll. No owner_user_id
        because catalog replicas are fleet-owned, not user-owned."""
        from datetime import UTC, datetime
        from uuid import uuid4

        deployment_id = str(uuid4())
        assignment_id = str(uuid4())
        now = datetime.now(UTC)
        with session_scope(self.session_factory) as session:
            session.add(DeploymentORM(
                deployment_id=deployment_id,
                workload_id=workload_id,
                owner_user_id=None,
                hotkey=hotkey,
                node_id=node_id,
                state="scheduled",
                requested_instances=1,
                ready_instances=0,
                hourly_rate_cents=0,
                metering_remainder_mcents=0,
                deployment_fee_usd=0.0,
                fee_acknowledged=True,
                warmup_state="pending",
                created_at=now,
                updated_at=now,
            ))
            session.add(LeaseAssignmentORM(
                assignment_id=assignment_id,
                deployment_id=deployment_id,
                workload_id=workload_id,
                hotkey=hotkey,
                node_id=node_id,
                assigned_at=now,
                expires_at=None,
                status="assigned",
            ))
        return deployment_id

    def read_demand_windows(self, model_id: str, now: "datetime | None" = None) -> dict:
        """Return rpm_10m / rpm_1h for one model, read off the per-minute
        stats table. Missing rows are treated as zero."""
        from datetime import UTC, datetime as _dt, timedelta

        from sqlalchemy import func

        from greencompute_persistence.orm import InferenceDemandStatsORM

        now = now or _dt.now(UTC)
        cutoff_10 = now - timedelta(minutes=10)
        cutoff_60 = now - timedelta(minutes=60)
        with session_scope(self.session_factory) as session:
            total_10 = session.scalar(
                select(func.coalesce(func.sum(InferenceDemandStatsORM.invocations), 0))
                .where(InferenceDemandStatsORM.model_id == model_id)
                .where(InferenceDemandStatsORM.window_start >= cutoff_10)
            ) or 0
            total_60 = session.scalar(
                select(func.coalesce(func.sum(InferenceDemandStatsORM.invocations), 0))
                .where(InferenceDemandStatsORM.model_id == model_id)
                .where(InferenceDemandStatsORM.window_start >= cutoff_60)
            ) or 0
        return {
            "rpm_10m": float(total_10) / 10.0,
            "rpm_1h": float(total_60) / 60.0,
        }

    def prune_demand_stats(self, retention_hours: int = 48) -> int:
        """Drop demand rows older than `retention_hours`. Returns row count."""
        from datetime import UTC, datetime as _dt, timedelta

        from greencompute_persistence.orm import InferenceDemandStatsORM

        cutoff = _dt.now(UTC) - timedelta(hours=retention_hours)
        with session_scope(self.session_factory) as session:
            result = session.execute(
                InferenceDemandStatsORM.__table__.delete().where(
                    InferenceDemandStatsORM.window_start < cutoff
                )
            )
            return result.rowcount or 0

    def terminate_flux_deployment(self, deployment_id: str) -> bool:
        from datetime import UTC, datetime

        with session_scope(self.session_factory) as session:
            dep = session.get(DeploymentORM, deployment_id)
            if dep is None or dep.state in ("terminated", "failed"):
                return False
            dep.state = "terminated"
            dep.updated_at = datetime.now(UTC)
            session.add(dep)
            lease = session.scalar(
                select(LeaseAssignmentORM).where(
                    LeaseAssignmentORM.deployment_id == deployment_id
                )
            )
            if lease is not None:
                lease.status = "terminated"
                session.add(lease)
            return True

    # --- Audit persistence (chutes-style per-epoch reports) -------------

    def save_scorecard_history(
        self,
        *,
        epoch_id: str,
        snapshot_id: str | None,
        scorecards: dict[str, ScoreCard],
    ) -> int:
        """Append one row per (hotkey, epoch) to scorecard_history. Called
        at epoch boundary alongside weight publishing. Returns row count."""
        from datetime import UTC, datetime

        rows = 0
        with session_scope(self.session_factory) as session:
            for hotkey, sc in scorecards.items():
                session.add(ScoreCardHistoryORM(
                    epoch_id=epoch_id,
                    snapshot_id=snapshot_id,
                    hotkey=hotkey,
                    capacity_weight=sc.capacity_weight,
                    reliability_score=sc.reliability_score,
                    performance_score=sc.performance_score,
                    security_score=sc.security_score,
                    fraud_penalty=sc.fraud_penalty,
                    utilization_score=sc.utilization_score,
                    rental_revenue_bonus=sc.rental_revenue_bonus,
                    final_score=sc.final_score,
                    computed_at=datetime.now(UTC),
                ))
                rows += 1
        return rows

    def save_audit_report(self, report: AuditReport) -> AuditReport:
        """Upsert an AuditReportORM row by epoch_id."""
        with session_scope(self.session_factory) as session:
            row = session.get(AuditReportORM, report.epoch_id) or AuditReportORM(
                epoch_id=report.epoch_id
            )
            row.netuid = report.netuid
            row.epoch_start_block = report.epoch_start_block
            row.epoch_end_block = report.epoch_end_block
            row.report_json = report.report_json
            row.report_sha256 = report.report_sha256
            row.signature = report.signature
            row.signer_hotkey = report.signer_hotkey
            row.chain_commitment_tx = report.chain_commitment_tx
            row.created_at = report.created_at
            session.add(row)
        return report

    @staticmethod
    def _audit_report_from_orm(row: AuditReportORM) -> AuditReport:
        return AuditReport(
            epoch_id=row.epoch_id,
            netuid=row.netuid,
            epoch_start_block=row.epoch_start_block,
            epoch_end_block=row.epoch_end_block,
            report_json=row.report_json,
            report_sha256=row.report_sha256,
            signature=row.signature,
            signer_hotkey=row.signer_hotkey,
            chain_commitment_tx=row.chain_commitment_tx,
            created_at=row.created_at,
        )

    def get_audit_report(self, epoch_id: str) -> AuditReport | None:
        with session_scope(self.session_factory) as session:
            row = session.get(AuditReportORM, epoch_id)
            return self._audit_report_from_orm(row) if row else None

    def list_audit_reports(self, limit: int = 100, offset: int = 0) -> list[AuditReport]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(AuditReportORM)
                .order_by(AuditReportORM.epoch_end_block.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [self._audit_report_from_orm(r) for r in rows]

    def list_probe_challenges_in_block_range(
        self, start_block: int, end_block: int
    ) -> list[ProbeChallenge]:
        """Not actually filtered by block since we don't persist block number
        per probe — we filter by timestamp range estimated from block times.
        Caller passes already-resolved timestamps via `list_probe_challenges_since`."""
        raise NotImplementedError("use list_probe_challenges_since instead")

    def list_probe_challenges_since(self, since, until):
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(ProbeChallengeORM)
                .where(ProbeChallengeORM.created_at >= since)
                .where(ProbeChallengeORM.created_at < until)
                .order_by(ProbeChallengeORM.created_at.asc())
            ).all()
            return [
                ProbeChallenge(
                    challenge_id=r.challenge_id,
                    hotkey=r.hotkey,
                    node_id=r.node_id,
                    kind=r.kind,
                    payload=r.payload or {},
                    created_at=r.created_at,
                )
                for r in rows
            ]

    def list_probe_results_since(self, since, until):
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(ProbeResultORM)
                .where(ProbeResultORM.observed_at >= since)
                .where(ProbeResultORM.observed_at < until)
                .order_by(ProbeResultORM.observed_at.asc())
            ).all()
            return [
                ProbeResult(
                    challenge_id=r.challenge_id,
                    hotkey=r.hotkey,
                    node_id=r.node_id,
                    latency_ms=r.latency_ms,
                    throughput=r.throughput,
                    success=r.success,
                    benchmark_signature=r.benchmark_signature,
                    proxy_suspected=r.proxy_suspected,
                    readiness_failures=r.readiness_failures,
                    prompt_sha256=r.prompt_sha256,
                    response_sha256=r.response_sha256,
                    observed_at=r.observed_at,
                )
                for r in rows
            ]

    def list_weight_snapshots_in_block_range(
        self, netuid: int, start, end
    ) -> list[WeightSnapshot]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(WeightSnapshotORM)
                .where(WeightSnapshotORM.netuid == netuid)
                .where(WeightSnapshotORM.created_at >= start)
                .where(WeightSnapshotORM.created_at < end)
                .order_by(WeightSnapshotORM.created_at.asc())
            ).all()
            return [
                WeightSnapshot(
                    snapshot_id=r.snapshot_id,
                    netuid=r.netuid,
                    weights=r.weights,
                    created_at=r.created_at,
                )
                for r in rows
            ]

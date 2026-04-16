from __future__ import annotations

from sqlalchemy import select

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.db import needs_bootstrap
from greenference_persistence.orm import (
    GreenEnergyApplicationORM,
    GreenEnergyAttachmentORM,
    MinerWhitelistORM,
    ProbeChallengeORM,
    ProbeResultORM,
    ScoreCardORM,
    ValidatorCapabilityORM,
    WeightSnapshotORM,
)
from greenference_protocol import (
    GreenEnergyApplication,
    GreenEnergyAttachment,
    MinerWhitelistEntry,
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

    def upsert_capability(self, capability: NodeCapability) -> NodeCapability:
        with session_scope(self.session_factory) as session:
            row = session.get(ValidatorCapabilityORM, capability.hotkey) or ValidatorCapabilityORM(
                hotkey=capability.hotkey
            )
            row.payload = capability.model_dump(mode="json")
            session.add(row)
        return capability

    def get_capability(self, hotkey: str) -> NodeCapability | None:
        with session_scope(self.session_factory) as session:
            row = session.get(ValidatorCapabilityORM, hotkey)
            return NodeCapability(**row.payload) if row else None

    def list_capabilities(self) -> dict[str, NodeCapability]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(ValidatorCapabilityORM)).all()
            return {row.hotkey: NodeCapability(**row.payload) for row in rows}

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

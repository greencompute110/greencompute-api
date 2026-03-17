from __future__ import annotations

from sqlalchemy import select

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.orm import (
    ProbeChallengeORM,
    ProbeResultORM,
    ScoreCardORM,
    ValidatorCapabilityORM,
    WeightSnapshotORM,
)
from greenference_protocol import NodeCapability, ProbeChallenge, ProbeResult, ScoreCard, WeightSnapshot


class ValidatorRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
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

    def list_results(self, hotkey: str) -> list[ProbeResult]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(ProbeResultORM).where(ProbeResultORM.hotkey == hotkey)).all()
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

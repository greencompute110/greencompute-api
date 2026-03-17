from __future__ import annotations

from greenference_persistence import WorkflowEventRepository
from greenference_protocol import NodeCapability, ProbeChallenge, ProbeResult, ScoreCard, WeightSnapshot
from greenference_validator.domain.scoring import ScoreEngine
from greenference_validator.infrastructure.repository import ValidatorRepository


class UnknownCapabilityError(KeyError):
    pass


class UnknownProbeChallengeError(KeyError):
    pass


class InvalidProbeResultError(ValueError):
    pass


class ValidatorService:
    def __init__(
        self,
        repository: ValidatorRepository | None = None,
        workflow_repository: WorkflowEventRepository | None = None,
    ) -> None:
        self.repository = repository or ValidatorRepository()
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.repository.engine,
            session_factory=self.repository.session_factory,
        )
        self.scoring = ScoreEngine()

    def register_capability(self, capability: NodeCapability) -> NodeCapability:
        return self.repository.upsert_capability(capability)

    def create_probe(self, hotkey: str, node_id: str, kind: str = "latency") -> ProbeChallenge:
        capability = self.repository.get_capability(hotkey)
        if capability is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={hotkey}")
        if capability.node_id != node_id:
            raise InvalidProbeResultError(f"node mismatch for hotkey={hotkey}: expected={capability.node_id}")
        challenge = ProbeChallenge(hotkey=hotkey, node_id=node_id, kind=kind)
        return self.repository.save_challenge(challenge)

    def submit_probe_result(self, result: ProbeResult) -> ScoreCard:
        challenge = self.repository.get_challenge(result.challenge_id)
        if challenge is None:
            raise UnknownProbeChallengeError(f"challenge not found: {result.challenge_id}")
        if challenge.hotkey != result.hotkey or challenge.node_id != result.node_id:
            raise InvalidProbeResultError(f"challenge mismatch for hotkey={result.hotkey} node={result.node_id}")
        if self.repository.get_result(result.challenge_id, result.hotkey) is not None:
            raise InvalidProbeResultError(f"duplicate result for challenge={result.challenge_id} hotkey={result.hotkey}")

        capability = self.repository.get_capability(result.hotkey)
        if capability is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={result.hotkey}")

        self.repository.add_result(result)
        scorecard = self.scoring.compute_scorecard(capability, self.repository.list_results(result.hotkey))
        saved = self.repository.save_scorecard(scorecard)
        self.workflow_repository.publish(
            "probe.result.recorded",
            {
                "challenge_id": result.challenge_id,
                "hotkey": result.hotkey,
                "node_id": result.node_id,
                "final_score": saved.final_score,
            },
        )
        return saved

    def publish_weight_snapshot(self, netuid: int = 64) -> WeightSnapshot:
        scorecards: dict[str, ScoreCard] = {}
        for hotkey, capability in sorted(self.repository.list_capabilities().items()):
            results = self.repository.list_results(hotkey)
            if not results:
                continue
            scorecard = self.scoring.compute_scorecard(capability, results)
            scorecards[hotkey] = self.repository.save_scorecard(scorecard)
        weights = {
            hotkey: scorecard.final_score
            for hotkey, scorecard in sorted(scorecards.items())
        }
        snapshot = WeightSnapshot(netuid=netuid, weights=weights)
        saved = self.repository.save_snapshot(snapshot)
        self.workflow_repository.publish(
            "validator.weights.published",
            {
                "snapshot_id": saved.snapshot_id,
                "netuid": saved.netuid,
                "weights": saved.weights,
            },
        )
        return saved


service = ValidatorService()

from __future__ import annotations

from greenference_protocol import NodeCapability, ProbeChallenge, ProbeResult, WeightSnapshot
from greenference_validator.domain.scoring import ScoreEngine
from greenference_validator.infrastructure.repository import ValidatorRepository


class ValidatorService:
    def __init__(self, repository: ValidatorRepository | None = None) -> None:
        self.repository = repository or ValidatorRepository()
        self.scoring = ScoreEngine()

    def register_capability(self, capability: NodeCapability) -> NodeCapability:
        return self.repository.upsert_capability(capability)

    def create_probe(self, hotkey: str, node_id: str, kind: str = "latency") -> ProbeChallenge:
        challenge = ProbeChallenge(hotkey=hotkey, node_id=node_id, kind=kind)
        return self.repository.save_challenge(challenge)

    def submit_probe_result(self, result: ProbeResult):
        self.repository.add_result(result)
        capability = self.repository.get_capability(result.hotkey)
        if capability is None:
            raise KeyError(f"capability not found for hotkey={result.hotkey}")
        scorecard = self.scoring.compute_scorecard(capability, self.repository.list_results(result.hotkey))
        return self.repository.save_scorecard(scorecard)

    def publish_weight_snapshot(self, netuid: int = 64) -> WeightSnapshot:
        weights = {
            hotkey: scorecard.final_score
            for hotkey, scorecard in sorted(self.repository.list_scorecards().items())
        }
        snapshot = WeightSnapshot(netuid=netuid, weights=weights)
        return self.repository.save_snapshot(snapshot)


service = ValidatorService()

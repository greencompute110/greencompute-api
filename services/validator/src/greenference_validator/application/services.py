from __future__ import annotations

import logging

from greenference_persistence import SubjectBus, WorkflowEventRepository, create_subject_bus, get_metrics_store
from greenference_persistence.runtime import load_runtime_settings
from greenference_protocol import (
    FluxRebalanceEvent,
    FluxState,
    NodeCapability,
    ProbeChallenge,
    ProbeResult,
    RentalWaitEstimate,
    ScoreCard,
    WeightSnapshot,
)
from greenference_validator.config import settings as validator_settings
from greenference_validator.domain.demand import DemandCollector
from greenference_validator.domain.flux import FluxOrchestrator
from greenference_validator.domain.scoring import ScoreEngine
from greenference_validator.domain.wait_estimator import WaitEstimator
from greenference_validator.infrastructure.repository import ValidatorRepository

logger = logging.getLogger(__name__)


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
        bus: SubjectBus | None = None,
    ) -> None:
        self.repository = repository or ValidatorRepository()
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.repository.engine,
            session_factory=self.repository.session_factory,
        )
        runtime_settings = load_runtime_settings("greenference-validator")
        self.bus = bus or create_subject_bus(
            engine=self.workflow_repository.engine,
            session_factory=self.workflow_repository.session_factory,
            workflow_repository=self.workflow_repository,
            nats_url=runtime_settings.nats_url,
            transport=runtime_settings.bus_transport,
        )
        self.scoring = ScoreEngine()
        self.metrics = get_metrics_store("greenference-validator")
        self.flux = FluxOrchestrator(
            inference_floor_pct=validator_settings.flux_inference_floor_pct,
            rental_floor_pct=validator_settings.flux_rental_floor_pct,
        )
        self.demand = DemandCollector()
        self.wait_estimator = WaitEstimator()
        self._flux_states: dict[str, FluxState] = {}

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
        self.bus.publish(
            "probe.result.recorded",
            {
                "challenge_id": result.challenge_id,
                "hotkey": result.hotkey,
                "node_id": result.node_id,
                "final_score": saved.final_score,
            },
        )
        self.metrics.increment("probe.result.recorded")
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
        self.bus.publish(
            "validator.weights.published",
            {
                "snapshot_id": saved.snapshot_id,
                "netuid": saved.netuid,
                "weights": saved.weights,
            },
        )
        self.metrics.increment("weights.published")
        return saved

    def process_pending_events(self, limit: int = 10) -> list[dict]:
        events = self.bus.claim_pending(
            "validator-worker",
            ["probe.result.recorded", "validator.weights.published"],
            limit=limit,
        )
        processed: list[dict] = []
        for event in events:
            if event.subject == "probe.result.recorded":
                self.bus.mark_completed(event.delivery_id)
                self.metrics.increment("probe.result.delivered")
                processed.append({"subject": event.subject, "hotkey": event.payload["hotkey"]})
                continue
            if event.subject == "validator.weights.published":
                self.bus.mark_completed(event.delivery_id)
                self.metrics.increment("weights.delivered")
                processed.append({"subject": event.subject, "snapshot_id": event.payload["snapshot_id"]})
                continue
            self.bus.mark_failed(event.delivery_id, f"unsupported workflow subject={event.subject}")
        self.metrics.set_gauge(
            "workflow.pending.validator",
            float(
                len(
                    self.bus.list_deliveries(
                        consumer="validator-worker",
                        subjects=["probe.result.recorded", "validator.weights.published"],
                        statuses=["pending"],
                    )
                )
            ),
        )
        return processed


    # --- Flux orchestrator ---

    def get_flux_state(self, hotkey: str) -> FluxState | None:
        return self._flux_states.get(hotkey)

    def init_flux_state(self, hotkey: str, node_id: str, total_gpus: int) -> FluxState:
        """Initialize or update a miner's Flux state when capacity is registered."""
        existing = self._flux_states.get(hotkey)
        if existing and existing.total_gpus == total_gpus:
            return existing
        state = FluxState(
            hotkey=hotkey,
            node_id=node_id,
            total_gpus=total_gpus,
            idle_gpus=total_gpus,
            inference_floor_pct=validator_settings.flux_inference_floor_pct,
            rental_floor_pct=validator_settings.flux_rental_floor_pct,
        )
        self._flux_states[hotkey] = state
        return state

    def rebalance_miner(self, hotkey: str) -> tuple[FluxState, list[FluxRebalanceEvent]]:
        """Run Flux rebalance for a single miner."""
        state = self._flux_states.get(hotkey)
        if state is None:
            return FluxState(hotkey=hotkey, node_id="", total_gpus=0), []
        # Inject latest demand scores
        state = state.model_copy(update={
            "inference_demand_score": self.demand.inference_score(hotkey),
            "rental_demand_score": self.demand.rental_score(hotkey),
        })
        new_state, events = self.flux.rebalance(state)
        self._flux_states[hotkey] = new_state
        self.metrics.increment("flux.rebalance", len(events))
        return new_state, events

    def rebalance_all_miners(self) -> dict[str, FluxState]:
        """Rebalance all tracked miners. Called from the worker loop."""
        results: dict[str, FluxState] = {}
        for hotkey in list(self._flux_states):
            new_state, _ = self.rebalance_miner(hotkey)
            results[hotkey] = new_state
        return results

    def estimate_rental_wait(self, deployment_id: str, hotkey: str) -> RentalWaitEstimate:
        """Estimate wait time for a rental deployment on a specific miner."""
        state = self._flux_states.get(hotkey)
        if state is None:
            return RentalWaitEstimate(
                deployment_id=deployment_id,
                estimated_wait_seconds=0.0,
                position_in_queue=0,
            )
        self.wait_estimator.enqueue(deployment_id)
        return self.wait_estimator.estimate(deployment_id, state)


service = ValidatorService()

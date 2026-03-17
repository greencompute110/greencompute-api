from fastapi import APIRouter

from greenference_protocol import NodeCapability, ProbeResult
from greenference_validator.application.services import service

router = APIRouter()


@router.post("/validator/v1/capabilities", response_model=NodeCapability)
def register_capability(payload: NodeCapability) -> NodeCapability:
    return service.register_capability(payload)


@router.post("/validator/v1/probes/{hotkey}/{node_id}")
def create_probe(hotkey: str, node_id: str, kind: str = "latency") -> dict:
    return service.create_probe(hotkey=hotkey, node_id=node_id, kind=kind).model_dump(mode="json")


@router.post("/validator/v1/probes/results")
def submit_probe_result(payload: ProbeResult) -> dict:
    return service.submit_probe_result(payload).model_dump(mode="json")


@router.get("/validator/v1/scores")
def list_scores() -> dict[str, dict]:
    return {
        hotkey: scorecard.model_dump(mode="json")
        for hotkey, scorecard in service.repository.list_scorecards().items()
    }


@router.post("/validator/v1/weights")
def publish_weights(netuid: int = 64) -> dict:
    return service.publish_weight_snapshot(netuid=netuid).model_dump(mode="json")

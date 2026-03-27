from fastapi import APIRouter, Header, HTTPException

from greenference_persistence import get_metrics_store
from greenference_protocol import NodeCapability, ProbeResult
from greenference_validator.application.services import (
    InvalidProbeResultError,
    UnknownCapabilityError,
    UnknownProbeChallengeError,
    service,
)
from greenference_validator.transport.security import require_admin_api_key, require_miner_request

router = APIRouter()
metrics = get_metrics_store("greenference-validator")


@router.post("/validator/v1/capabilities", response_model=NodeCapability)
def register_capability(
    payload: NodeCapability,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> NodeCapability:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
    )
    return service.register_capability(payload)


@router.post("/validator/v1/probes/{hotkey}/{node_id}")
def create_probe(
    hotkey: str,
    node_id: str,
    kind: str = "latency",
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.create_probe(hotkey=hotkey, node_id=node_id, kind=kind).model_dump(mode="json")
    except UnknownCapabilityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidProbeResultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/validator/v1/probes/results")
def submit_probe_result(
    payload: ProbeResult,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> dict:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
    )
    try:
        return service.submit_probe_result(payload).model_dump(mode="json")
    except UnknownProbeChallengeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnknownCapabilityError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidProbeResultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/validator/v1/scores")
def list_scores(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, dict]:
    require_admin_api_key(authorization, x_api_key)
    return {
        hotkey: scorecard.model_dump(mode="json")
        for hotkey, scorecard in service.repository.list_scorecards().items()
    }


@router.post("/validator/v1/weights")
def publish_weights(
    netuid: int = 64,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    return service.publish_weight_snapshot(netuid=netuid).model_dump(mode="json")


@router.get("/validator/v1/debug/results")
def debug_results(
    hotkey: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [result.model_dump(mode="json") for result in service.repository.list_results(hotkey)]


@router.get("/validator/v1/metrics")
def validator_metrics(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    metrics.set_gauge("probe.results.total", float(len(service.repository.list_results())))
    metrics.set_gauge("scorecards.total", float(len(service.repository.list_scorecards())))
    return metrics.snapshot()


# --- Flux orchestrator endpoints ---


@router.get("/validator/v1/flux/{hotkey}")
def get_flux_state(
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    state = service.get_flux_state(hotkey)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no flux state for hotkey={hotkey}")
    return state.model_dump(mode="json")


@router.post("/validator/v1/flux/rebalance")
def flux_rebalance(
    hotkey: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    if hotkey:
        state, events = service.rebalance_miner(hotkey)
        return {
            "state": state.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in events],
        }
    results = service.rebalance_all_miners()
    return {
        hotkey: state.model_dump(mode="json")
        for hotkey, state in results.items()
    }


@router.get("/validator/v1/flux/wait-estimate/{deployment_id}")
def flux_wait_estimate(
    deployment_id: str,
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    estimate = service.estimate_rental_wait(deployment_id, hotkey)
    return estimate.model_dump(mode="json")

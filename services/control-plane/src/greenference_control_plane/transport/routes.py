from fastapi import APIRouter, HTTPException

from greenference_protocol import CapacityUpdate, DeploymentStatusUpdate, Heartbeat, MinerRegistration
from greenference_control_plane.application.services import service

router = APIRouter()


@router.post("/miner/v1/register", response_model=MinerRegistration)
def register_miner(payload: MinerRegistration) -> MinerRegistration:
    return service.register_miner(payload)


@router.post("/miner/v1/heartbeat", response_model=Heartbeat)
def heartbeat(payload: Heartbeat) -> Heartbeat:
    return service.record_heartbeat(payload)


@router.post("/miner/v1/capacity", response_model=CapacityUpdate)
def capacity(payload: CapacityUpdate) -> CapacityUpdate:
    return service.update_capacity(payload)


@router.get("/miner/v1/leases/{hotkey}")
def list_leases(hotkey: str) -> list[dict]:
    return [lease.model_dump(mode="json") for lease in service.list_leases(hotkey)]


@router.post("/miner/v1/deployments/{deployment_id}/status")
def deployment_status(deployment_id: str, payload: DeploymentStatusUpdate) -> dict:
    if payload.deployment_id != deployment_id:
        raise HTTPException(status_code=400, detail="deployment id mismatch")
    deployment = service.update_deployment_status(payload)
    return deployment.model_dump(mode="json")


@router.get("/platform/v1/usage")
def usage_summary() -> dict[str, dict[str, float]]:
    return service.usage_summary()


@router.post("/platform/v1/events/process")
def process_events(limit: int = 10) -> dict[str, list]:
    return service.process_pending_events(limit=limit)

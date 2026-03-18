from fastapi import APIRouter, Header, HTTPException

from greenference_persistence import get_metrics_store
from greenference_protocol import CapacityUpdate, DeploymentStatusUpdate, Heartbeat, MinerRegistration
from greenference_control_plane.application.services import service
from greenference_control_plane.transport.security import require_admin_api_key, require_miner_request

router = APIRouter()
metrics = get_metrics_store("greenference-control-plane")


@router.post("/miner/v1/register", response_model=MinerRegistration)
def register_miner(
    payload: MinerRegistration,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> MinerRegistration:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
        allow_unregistered=True,
        registration_secret=payload.auth_secret,
    )
    return service.register_miner(payload)


@router.post("/miner/v1/heartbeat", response_model=Heartbeat)
def heartbeat(
    payload: Heartbeat,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> Heartbeat:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
    )
    return service.record_heartbeat(payload)


@router.post("/miner/v1/capacity", response_model=CapacityUpdate)
def capacity(
    payload: CapacityUpdate,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> CapacityUpdate:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
    )
    return service.update_capacity(payload)


@router.get("/miner/v1/leases/{hotkey}")
def list_leases(
    hotkey: str,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> list[dict]:
    require_miner_request(
        hotkey,
        b"",
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
    )
    return [lease.model_dump(mode="json") for lease in service.list_leases(hotkey)]


@router.post("/miner/v1/deployments/{deployment_id}/status")
def deployment_status(
    deployment_id: str,
    payload: DeploymentStatusUpdate,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
) -> dict:
    if payload.deployment_id != deployment_id:
        raise HTTPException(status_code=400, detail="deployment id mismatch")
    deployment = service.repository.get_deployment(deployment_id)
    if deployment is None or deployment.hotkey is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    require_miner_request(
        deployment.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
    )
    saved = service.update_deployment_status(payload)
    return saved.model_dump(mode="json")


@router.get("/platform/v1/usage")
def usage_summary(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, dict[str, float]]:
    require_admin_api_key(authorization, x_api_key)
    return service.usage_summary()


@router.post("/platform/v1/events/process")
def process_events(
    limit: int = 10,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, list]:
    require_admin_api_key(authorization, x_api_key)
    return service.process_pending_events(limit=limit)


@router.get("/platform/v1/debug/workflows")
def debug_workflows(
    subject: str | None = None,
    event_status: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    subjects = [subject] if subject else None
    statuses = [event_status] if event_status else None
    return [event.model_dump(mode="json") for event in service.workflow_repository.list_events(subjects, statuses)]


@router.get("/platform/v1/debug/deployments")
def debug_deployments(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [deployment.model_dump(mode="json") for deployment in service.list_deployments()]


@router.get("/platform/v1/debug/leases")
def debug_leases(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [lease.model_dump(mode="json") for lease in service.repository.list_assignments()]


@router.get("/platform/v1/debug/deployment-events")
def debug_deployment_events(
    deployment_id: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.repository.list_deployment_events(deployment_id=deployment_id)


@router.get("/platform/v1/debug/miners")
def debug_miners(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.miner_health_report()


@router.get("/platform/v1/debug/reassignments")
def debug_reassignments(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.reassignment_history()


@router.get("/platform/v1/debug/stuck-deployments")
def debug_stuck_deployments(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.stuck_deployments_report()


@router.get("/platform/v1/debug/servers")
def debug_servers(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [server.model_dump(mode="json") for server in service.list_servers()]


@router.get("/platform/v1/debug/nodes")
def debug_nodes(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [node.model_dump(mode="json") for node in service.list_nodes()]


@router.get("/platform/v1/debug/capacity-history")
def debug_capacity_history(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [record.model_dump(mode="json") for record in service.list_capacity_history(limit=limit)]


@router.get("/platform/v1/debug/placements")
def debug_placements(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [record.model_dump(mode="json") for record in service.list_placements(limit=limit)]


@router.get("/platform/v1/debug/lease-history")
def debug_lease_history(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [record.model_dump(mode="json") for record in service.list_lease_history(limit=limit)]


@router.get("/platform/v1/debug/deployment-retries")
def debug_deployment_retries(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.deployment_retry_report()


@router.get("/platform/v1/debug/placement-exclusions")
def debug_placement_exclusions(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.placement_exclusion_report()


@router.get("/platform/v1/debug/deployment-failures")
def debug_deployment_failures(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.deployment_failure_report()


@router.get("/platform/v1/debug/miner-drift")
def debug_miner_drift(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    return service.miner_drift_report()


@router.get("/platform/v1/debug/status")
def debug_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    return service.operator_status()


@router.post("/platform/v1/debug/deployments/{deployment_id}/requeue")
def requeue_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.requeue_deployment(deployment_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/v1/debug/deployments/{deployment_id}/fail")
def fail_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.fail_deployment(deployment_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/v1/debug/deployments/{deployment_id}/cleanup")
def cleanup_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.cleanup_deployment(deployment_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/v1/debug/miners/{hotkey}/drain")
def drain_miner(
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.drain_miner(hotkey).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/v1/debug/miners/{hotkey}/undrain")
def undrain_miner(
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.undrain_miner(hotkey).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/platform/v1/debug/workers")
def debug_workers(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    consumers = {
        "builder-worker": "greenference-builder",
        "control-plane-worker": "greenference-control-plane",
        "validator-worker": "greenference-validator",
    }
    reports: list[dict] = []
    for consumer, service_name in consumers.items():
        deliveries = service.bus.list_deliveries(consumer=consumer)
        breakdown = {
            "pending": len(service.bus.list_deliveries(consumer=consumer, statuses=["pending"])),
            "processing": len(service.bus.list_deliveries(consumer=consumer, statuses=["processing"])),
            "completed": len(service.bus.list_deliveries(consumer=consumer, statuses=["completed"])),
            "failed": len(service.bus.list_deliveries(consumer=consumer, statuses=["failed"])),
        }
        reports.append(
            {
                "consumer": consumer,
                "service": service_name,
                "bus_transport": service.bus.active_transport,
                "backlog": breakdown["pending"] + breakdown["processing"],
                "status_breakdown": breakdown,
                "max_attempts": max((item.attempts for item in deliveries), default=0),
                "oldest_available_at": (
                    min((item.available_at for item in deliveries), default=None)
                ),
            }
        )
    return reports


@router.get("/platform/v1/debug/event-deliveries")
def debug_event_deliveries(
    consumer: str | None = None,
    subject: str | None = None,
    delivery_status: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    subjects = [subject] if subject else None
    statuses = [delivery_status] if delivery_status else None
    return [
        delivery.model_dump(mode="json")
        for delivery in service.bus.list_deliveries(
            consumer=consumer,
            subjects=subjects,
            statuses=statuses,
        )
    ]


@router.get("/platform/v1/metrics")
def platform_metrics(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    metrics.set_gauge(
        "leases.active",
        float(len(service.repository.list_assignments(statuses=["assigned", "activating", "active"]))),
    )
    metrics.set_gauge(
        "deployments.total",
        float(len(service.repository.list_deployments())),
    )
    metrics.set_gauge(
        "miners.unhealthy",
        float(len([miner for miner in service.miner_health_report() if miner["status"] != "healthy"])),
    )
    metrics.set_gauge("servers.total", float(len(service.list_servers())))
    metrics.set_gauge("nodes.total", float(len(service.list_nodes())))
    metrics.set_gauge("placements.total", float(len(service.list_placements(limit=1000))))
    metrics.set_gauge(
        "deployments.stuck",
        float(len(service.stuck_deployments_report())),
    )
    return metrics.snapshot()

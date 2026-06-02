import json
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from greencompute_persistence import get_metrics_store
from greencompute_protocol import CapacityUpdate, DeploymentState, DeploymentStatusUpdate, Heartbeat, MinerRegistration
from greencompute_control_plane.application.services import service
from greencompute_control_plane.domain.state import InvalidDeploymentTransition
from greencompute_control_plane.transport.security import require_admin_api_key, require_miner_request

router = APIRouter()


def _feature_disabled(feature: str) -> None:
    raise HTTPException(
        status_code=501,
        detail={
            "status": "disabled",
            "feature": feature,
            "reason": "not implemented in greencompute-api",
        },
    )
metrics = get_metrics_store("greencompute-control-plane")


@router.post("/miner/v1/register", response_model=MinerRegistration)
def register_miner(
    payload: MinerRegistration,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
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
        x_miner_auth_mode=x_miner_auth_mode,
    )
    return service.register_miner(payload)


@router.post("/miner/v1/heartbeat", response_model=Heartbeat)
def heartbeat(
    payload: Heartbeat,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> Heartbeat:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
        x_miner_auth_mode=x_miner_auth_mode,
    )
    return service.record_heartbeat(payload)


@router.post("/miner/v1/capacity", response_model=CapacityUpdate)
def capacity(
    payload: CapacityUpdate,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> CapacityUpdate:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
        x_miner_auth_mode=x_miner_auth_mode,
    )
    return service.update_capacity(payload)


@router.get("/miner/v1/deployments/{deployment_id}")
def get_deployment(
    deployment_id: str,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> dict:
    deployment = service.repository.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    hotkey = deployment.hotkey or (x_miner_hotkey or "")
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    return deployment.model_dump(mode="json")


@router.get("/miner/v1/workloads/{workload_id}")
def get_workload(
    workload_id: str,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> dict:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    workload = service.repository.get_workload(workload_id)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")
    return workload.model_dump(mode="json")


@router.get("/miner/v1/leases/{hotkey}")
def list_leases(
    hotkey: str,
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> list[dict]:
    require_miner_request(
        hotkey,
        b"",
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
        x_miner_auth_mode=x_miner_auth_mode,
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
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
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
        x_miner_auth_mode=x_miner_auth_mode,
    )
    try:
        saved = service.update_deployment_status(payload)
    except InvalidDeploymentTransition as exc:
        # Illegal transition from a live (non-terminal) state — surface as a
        # non-retryable 409 so the miner stops retrying, never a 500. Terminal
        # states are absorbed inside update_deployment_status (returns no-op).
        raise HTTPException(status_code=409, detail=str(exc))
    return saved.model_dump(mode="json")


def _sse_stream(items: list):
    for item in items:
        payload = item if isinstance(item, dict) else item.model_dump(mode="json")
        yield f"data: {json.dumps(payload)}\n\n"
    yield "data: NO_ITEMS\n\n"


@router.get("/miner/v1/workloads")
def miner_stream_workloads(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> StreamingResponse:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    workloads = service.list_workloads()
    items = [{"workload_id": w.workload_id, "name": w.name, "image": w.image, "kind": w.kind.value} for w in workloads]
    return StreamingResponse(
        _sse_stream(items),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/miner/v1/images")
def miner_stream_images(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> StreamingResponse:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    builds = service.repository.list_builds()
    items = [{"build_id": b["build_id"], "image": b["image"], "status": b["status"]} for b in builds]
    return StreamingResponse(
        _sse_stream(items),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/miner/v1/nodes")
def miner_stream_nodes(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> StreamingResponse:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    nodes = service.list_nodes()
    items = [
        {"node_id": n.node_id, "hotkey": n.hotkey, "payload": n.model_dump(mode="json")}
        for n in nodes
    ]
    return StreamingResponse(
        _sse_stream(items),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/miner/v1/instances")
def miner_stream_instances(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> StreamingResponse:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    deployments = service.list_deployments()
    items = [d.model_dump(mode="json") for d in deployments]
    return StreamingResponse(
        _sse_stream(items),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/miner/v1/jobs")
def miner_list_jobs(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> list[dict]:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    _feature_disabled("miner_jobs")


@router.get("/miner/v1/inventory")
def miner_inventory(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> dict:
    hk = x_miner_hotkey or ""
    require_miner_request(hk, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    workloads = service.list_workloads()
    deployments = service.list_deployments()
    nodes = service.list_nodes()
    return {
        "workloads": [w.model_dump(mode="json") for w in workloads],
        "deployments": [d.model_dump(mode="json") for d in deployments],
        "nodes": [n.model_dump(mode="json") for n in nodes],
    }


@router.get("/miner/v1/metrics")
def miner_stream_metrics(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> StreamingResponse:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    usage = service.usage_summary()
    items = [{"deployment_id": k, **v} for k, v in usage.items()]
    return StreamingResponse(
        _sse_stream(items),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/miner/v1/active_instances")
def miner_active_instances(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> list[dict]:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    deployments = [d for d in service.list_deployments() if d.state.value == "ready"]
    return [d.model_dump(mode="json") for d in deployments]


@router.get("/miner/v1/stats")
def miner_stats(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> dict:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    return {"deployments": len(service.list_deployments()), "leases": len(service.repository.list_assignments())}


@router.get("/miner/v1/scores")
def miner_scores(
    x_miner_hotkey: str | None = Header(default=None, alias="X-Miner-Hotkey"),
    x_miner_signature: str | None = Header(default=None, alias="X-Miner-Signature"),
    x_miner_nonce: str | None = Header(default=None, alias="X-Miner-Nonce"),
    x_miner_timestamp: str | None = Header(default=None, alias="X-Miner-Timestamp"),
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> dict:
    hotkey = x_miner_hotkey or ""
    require_miner_request(hotkey, b"", x_miner_hotkey, x_miner_signature, x_miner_nonce, x_miner_timestamp, x_miner_auth_mode=x_miner_auth_mode)
    return {}


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


@router.get("/platform/v1/servers/{hotkey}")
def get_server(
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Internal server lookup — used by the gateway to find a miner's
    api_base_url for request relay (e.g. live pod stats)."""
    require_admin_api_key(authorization, x_api_key)
    server = service.get_server_by_hotkey(hotkey)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return server.model_dump(mode="json")


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


@router.get("/platform/v1/debug/routing-eligibility")
def debug_routing_eligibility(
    workload_id: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return service.routing_eligibility_report(workload_id=workload_id)


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


@router.get("/platform/v1/debug/fleet")
def debug_fleet_orchestration(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    return service.fleet_orchestration_report()


@router.get("/platform/v1/debug/scheduling")
def debug_scheduling(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Show why scheduling might fail: nodes, staleness, pending events."""
    require_admin_api_key(authorization, x_api_key)
    from greencompute_control_plane.config import settings as cp_settings
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    all_nodes = service.repository.list_nodes()
    nodes_report = []
    for node in all_nodes:
        miner = service.repository.get_miner(node.hotkey)
        heartbeat = service.repository.get_heartbeat(node.hotkey)
        skip_reasons = []
        if miner is not None and miner.drained:
            skip_reasons.append("drained")
        if heartbeat and not heartbeat.healthy:
            skip_reasons.append("unhealthy_heartbeat")
        if not heartbeat:
            skip_reasons.append("no_heartbeat")
        if service._is_node_stale(node):
            age = None
            if node.observed_at:
                raw = node.observed_at
                if isinstance(raw, str):
                    from datetime import datetime as dt
                    raw = dt.fromisoformat(raw.replace("Z", "+00:00"))
                age = (now - raw).total_seconds()
            skip_reasons.append(f"stale(age={age:.0f}s, max={cp_settings.node_inventory_timeout_seconds}s)" if age else "stale(no_observed_at)")
        if service._is_server_for_node_stale(node):
            skip_reasons.append("server_stale")
        if service._is_node_in_cooldown(node.node_id):
            skip_reasons.append("cooldown")
        nodes_report.append({
            "node_id": node.node_id,
            "hotkey": node.hotkey,
            "gpu_model": node.gpu_model,
            "available_gpus": node.available_gpus,
            "gpu_count": node.gpu_count,
            "vram_gb_per_gpu": node.vram_gb_per_gpu,
            "cpu_cores": node.cpu_cores,
            "memory_gb": node.memory_gb,
            "observed_at": str(node.observed_at) if node.observed_at else None,
            "eligible": len(skip_reasons) == 0,
            "skip_reasons": skip_reasons,
        })
    pending_events = service.bus.list_deliveries(
        consumer="control-plane-worker",
        subjects=["deployment.requested"],
        statuses=["pending", "processing"],
    )
    pending_deployments = [
        d.model_dump(mode="json")
        for d in service.list_deployments()
        if d.state in (DeploymentState.PENDING,)
    ]
    return {
        "registered_nodes": len(all_nodes),
        "eligible_nodes": sum(1 for n in nodes_report if n["eligible"]),
        "nodes": nodes_report,
        "pending_deployment_events": len(pending_events),
        "pending_deployments": pending_deployments,
        "config": {
            "node_inventory_timeout_seconds": cp_settings.node_inventory_timeout_seconds,
            "server_observed_timeout_seconds": cp_settings.server_observed_timeout_seconds,
            "deployment_request_retry_limit": cp_settings.deployment_request_retry_limit,
            "deployment_request_retry_delay_seconds": cp_settings.deployment_request_retry_delay_seconds,
            "default_lease_ttl_seconds": cp_settings.default_lease_ttl_seconds,
        },
    }


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


@router.post("/platform/v1/debug/deployments/{deployment_id}/resume")
def resume_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """ORCH-H3: pod-safe resume — flips a SUSPENDED deployment's lease back to
    'active' and re-acquires its GPU reservation so the node-agent `docker
    start`s the SAME container (no destroy-and-recreate). 409 if the deployment
    isn't suspended or its node lacks capacity to re-acquire the hold."""
    require_admin_api_key(authorization, x_api_key)
    try:
        return service.resume_deployment(deployment_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
        "builder-worker": "greencompute-builder",
        "control-plane-worker": "greencompute-control-plane",
        "validator-worker": "greencompute-validator",
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
                "recovery": service.recovery_status() if consumer == "control-plane-worker" else None,
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


@router.get("/platform/v1/gpu-pool")
def gpu_pool() -> list[dict]:
    """Public endpoint: aggregated GPU availability for the rental page.

    If an admin set a `gpu_capacity_overrides` row for a GPU class, the
    override REPLACES the measured count — sales uses this to advertise
    contracted cluster size before miners are fully live. Without an
    override, falls back to the measured count. Reads overrides via raw
    SQL so the control-plane doesn't have to import gateway code.
    """
    from collections import defaultdict
    from sqlalchemy import text

    all_nodes = service.repository.list_nodes()
    buckets: dict[str, dict] = defaultdict(lambda: {
        "gpu_model": "",
        "vram_gb_per_gpu": 0,
        "total_gpus": 0,
        "available_gpus": 0,
        "node_count": 0,
        "overridden": False,
    })
    for node in all_nodes:
        if service._is_node_stale(node):
            continue
        key = (node.gpu_model or "unknown").lower()
        b = buckets[key]
        b["gpu_model"] = node.gpu_model or "unknown"
        b["vram_gb_per_gpu"] = max(b["vram_gb_per_gpu"], node.vram_gb_per_gpu or 0)
        b["total_gpus"] += node.gpu_count or 0
        b["available_gpus"] += node.available_gpus or 0
        b["node_count"] += 1

    # Merge admin overrides. Wrapped so a missing table (migration not run
    # yet) silently falls through to measured counts.
    try:
        with service.repository.session_factory() as session:
            rows = session.execute(
                text(
                    "SELECT gpu_model, total_gpus, available_gpus, note "
                    "FROM gpu_capacity_overrides"
                )
            ).all()
            for row in rows:
                key = (row.gpu_model or "unknown").lower()
                b = buckets[key]
                if not b["gpu_model"]:
                    # No live nodes for this class yet — surface the
                    # contracted hardware anyway so sales can pre-advertise.
                    b["gpu_model"] = row.gpu_model or "unknown"
                b["total_gpus"] = int(row.total_gpus or 0)
                b["available_gpus"] = int(row.available_gpus or 0)
                b["overridden"] = True
                if row.note:
                    b["note"] = row.note
    except Exception:
        pass

    return sorted(buckets.values(), key=lambda b: -b["available_gpus"])

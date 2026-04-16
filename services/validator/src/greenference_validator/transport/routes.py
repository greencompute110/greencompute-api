import base64

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from greenference_persistence import get_metrics_store
from greenference_protocol import (
    GreenEnergyApplication,
    GreenEnergyAttachment,
    MinerWhitelistEntry,
    NodeCapability,
    ProbeResult,
)
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
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> NodeCapability:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
        x_miner_auth_mode=x_miner_auth_mode,
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
    x_miner_auth_mode: str | None = Header(default=None, alias="X-Miner-Auth-Mode"),
) -> dict:
    require_miner_request(
        payload.hotkey,
        payload.model_dump_json().encode(),
        x_miner_hotkey,
        x_miner_signature,
        x_miner_nonce,
        x_miner_timestamp,
        x_miner_auth_mode=x_miner_auth_mode,
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


# --- Bittensor chain endpoints ---


@router.get("/validator/v1/metagraph")
def get_metagraph(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    return {
        "size": service.metagraph.size,
        "last_synced_at": service.metagraph.last_synced_at.isoformat() if service.metagraph.last_synced_at else None,
        "entries": [e.model_dump(mode="json") for e in service.metagraph.list_entries()],
    }


@router.post("/validator/v1/metagraph/sync")
def sync_metagraph(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    try:
        entries = service.sync_metagraph()
        return {"synced": len(entries)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"chain sync failed: {exc}") from exc


@router.get("/validator/v1/metagraph/{hotkey}")
def check_registration(
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    entry = service.metagraph.get_by_hotkey(hotkey)
    if entry is None:
        return {"registered": False, "hotkey": hotkey}
    return {"registered": True, **entry.model_dump(mode="json")}


# --- Miner whitelist endpoints ---


@router.get("/validator/v1/whitelist")
def list_whitelist(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_admin_api_key(authorization, x_api_key)
    return [e.model_dump(mode="json") for e in service.repository.list_whitelist()]


@router.post("/validator/v1/whitelist", status_code=201)
def add_to_whitelist(
    payload: MinerWhitelistEntry,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    entry = service.repository.add_whitelist_entry(payload)
    return entry.model_dump(mode="json")


@router.delete("/validator/v1/whitelist/{hotkey}")
def remove_from_whitelist(
    hotkey: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    removed = service.repository.remove_whitelist_entry(hotkey)
    if not removed:
        raise HTTPException(status_code=404, detail=f"hotkey {hotkey} not in whitelist")
    return {"removed": hotkey}


# --- Green-energy applications (provider onboarding) ---


@router.post("/validator/v1/applications", status_code=201)
async def submit_application(
    hotkey: str = Form(...),
    signature: str = Form(""),
    organization: str = Form(""),
    energy_source: str = Form(""),
    description: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    """Public endpoint — providers submit green-energy proof here."""
    if not hotkey.strip():
        raise HTTPException(status_code=400, detail="hotkey is required")

    app = GreenEnergyApplication(
        hotkey=hotkey.strip(),
        signature=signature,
        organization=organization,
        energy_source=energy_source,
        description=description,
    )
    service.repository.create_application(app)

    for f in files:
        raw = await f.read()
        att = GreenEnergyAttachment(
            application_id=app.application_id,
            filename=f.filename or "unnamed",
            content_type=f.content_type or "application/octet-stream",
            size_bytes=len(raw),
            data_b64=base64.b64encode(raw).decode(),
        )
        service.repository.add_attachment(att)

    return app.model_dump(mode="json")


@router.get("/validator/v1/applications/status/{hotkey}")
def application_status(hotkey: str) -> list[dict]:
    """Public endpoint — check application status by hotkey."""
    apps = service.repository.list_applications_by_hotkey(hotkey)
    return [
        {
            "application_id": a.application_id,
            "status": a.status,
            "organization": a.organization,
            "energy_source": a.energy_source,
            "reviewer_notes": a.reviewer_notes,
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
        }
        for a in apps
    ]


@router.get("/validator/v1/applications")
def list_applications(
    status: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — list all applications, optionally filtered by status."""
    require_admin_api_key(authorization, x_api_key)
    apps = service.repository.list_applications(status=status)
    result = []
    for app in apps:
        d = app.model_dump(mode="json")
        atts = service.repository.list_attachments(app.application_id)
        d["attachments"] = [
            {
                "attachment_id": a.attachment_id,
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
            }
            for a in atts
        ]
        result.append(d)
    return result


@router.get("/validator/v1/applications/{application_id}/attachments/{attachment_id}")
def download_attachment(
    application_id: str,
    attachment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    """Admin — download a specific attachment."""
    require_admin_api_key(authorization, x_api_key)
    att = service.repository.get_attachment(attachment_id)
    if att is None or att.application_id != application_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    raw = base64.b64decode(att.data_b64)
    return Response(
        content=raw,
        media_type=att.content_type,
        headers={"Content-Disposition": f'attachment; filename="{att.filename}"'},
    )


@router.post("/validator/v1/applications/{application_id}/approve")
def approve_application(
    application_id: str,
    reviewer_notes: str = "",
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — approve application and auto-add hotkey to whitelist."""
    require_admin_api_key(authorization, x_api_key)
    app = service.repository.update_application_status(application_id, "approved", reviewer_notes)
    if app is None:
        raise HTTPException(status_code=404, detail="application not found")

    # Auto-add to whitelist
    entry = MinerWhitelistEntry(
        hotkey=app.hotkey,
        label=app.organization or app.hotkey[:16],
        energy_source=app.energy_source,
        notes=f"Auto-approved from application {application_id}",
    )
    service.repository.add_whitelist_entry(entry)

    return {"status": "approved", "hotkey": app.hotkey, "application_id": application_id}


@router.post("/validator/v1/applications/{application_id}/reject")
def reject_application(
    application_id: str,
    reviewer_notes: str = "",
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — reject an application."""
    require_admin_api_key(authorization, x_api_key)
    app = service.repository.update_application_status(application_id, "rejected", reviewer_notes)
    if app is None:
        raise HTTPException(status_code=404, detail="application not found")
    return {"status": "rejected", "application_id": application_id}

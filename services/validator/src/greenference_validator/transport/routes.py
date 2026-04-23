import base64

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from greenference_persistence import get_metrics_store
from greenference_protocol import (
    CatalogSubmission,
    GreenEnergyApplication,
    GreenEnergyAttachment,
    MinerWhitelistEntry,
    ModelCatalogEntry,
    NodeCapability,
    ProbeResult,
)
from greenference_validator.config import settings as validator_settings
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
    netuid: int | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — publish weight snapshot. netuid defaults to the configured
    GREENFERENCE_BITTENSOR_NETUID (16 on testnet, 110 on mainnet). Pass
    `?netuid=110` to force mainnet publication in a mixed deployment."""
    require_admin_api_key(authorization, x_api_key)
    effective = netuid if netuid is not None else validator_settings.bittensor_netuid
    return service.publish_weight_snapshot(netuid=effective).model_dump(mode="json")


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


@router.get("/validator/v1/flux/dashboard")
def flux_dashboard(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — single-shot snapshot of fleet, catalog pool, and miner summary.
    UI polls this every ~5s."""
    require_admin_api_key(authorization, x_api_key)
    return service.build_flux_dashboard()


@router.get("/validator/v1/flux/demand")
def flux_demand(
    model_id: str | None = None,
    window_minutes: int = 60,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    window_minutes = max(1, min(window_minutes, 60 * 24 * 2))
    return {"rows": service.demand_timeseries(model_id=model_id, window_minutes=window_minutes)}


@router.get("/validator/v1/flux/events")
def flux_events(
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_admin_api_key(authorization, x_api_key)
    limit = max(1, min(limit, 500))
    return {"events": service.flux_events(limit=limit)}


@router.post("/validator/v1/probes/inference/{hotkey}/{model_id}")
def run_inference_probe(
    hotkey: str,
    model_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — fire an inference-verification canary against a specific miner
    hosting the catalog model. Records a ProbeResult that feeds
    reliability / fraud-penalty scoring via the existing pipeline."""
    require_admin_api_key(authorization, x_api_key)
    try:
        result = service.run_inference_canary(hotkey, model_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result.model_dump(mode="json")


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


# --- Model catalog — Chutes-style shared inference pool ---

import re


_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,126}[a-z0-9]$")


def _validate_model_id(model_id: str) -> str:
    """Normalize + sanity-check. Catalog IDs are URL-safe slugs."""
    normalized = model_id.strip().lower()
    if not _MODEL_ID_RE.match(normalized):
        raise HTTPException(
            status_code=400,
            detail="model_id must be lowercase alphanumeric with dashes/dots (2–128 chars)",
        )
    return normalized


def _ensure_catalog_workload(entry: ModelCatalogEntry) -> None:
    """Auto-create or update the canonical WorkloadORM for a catalog entry.

    Keyed by workload.name == model_id. Multiple miners host this single
    workload by each creating their own DeploymentRecord pointing at it.
    The workload carries `metadata.managed_by = "flux"` so billing /
    idle-kill paths can distinguish catalog replicas from user-spun
    private endpoints.
    """
    # Direct DB access — validator and control-plane share the same Postgres;
    # we don't need a cross-service call for a single upsert.
    from sqlalchemy import select as _select
    from greenference_persistence import session_scope as _session_scope
    from greenference_persistence.orm import WorkloadORM

    repo = service.repository
    # vLLM-compatible default image. Miners override via the catalog template
    # if they need diffusion / vision variants.
    default_images = {
        # v0.19.1 with CUDA 13.0 is needed for Blackwell (sm_120 / RTX 5090).
        # Older tags like 0.8.5 crash at EngineCore init on RTX 5090.
        "vllm": "vllm/vllm-openai:v0.19.1-cu130-ubuntu2404",
        "vllm-vision": "vllm/vllm-openai:v0.19.1-cu130-ubuntu2404",
        "diffusion": "greenference/diffusion-server:latest",
    }
    image = default_images.get(entry.template, default_images["vllm"])

    requirements = {
        "gpu_count": entry.gpu_count,
        "min_vram_gb_per_gpu": entry.min_vram_gb_per_gpu,
        "cpu_cores": 4,
        "memory_gb": 16,
        "storage_gb": 200,
        "supported_gpu_models": [],
    }
    runtime = {
        "template": entry.template,
        "model_identifier": entry.hf_repo or entry.model_id,
        "max_model_len": entry.max_model_len,
    }
    metadata_json = {
        "managed_by": "flux",
        "catalog_model_id": entry.model_id,
    }

    with _session_scope(repo.session_factory) as session:
        row = session.scalar(
            _select(WorkloadORM).where(WorkloadORM.name == entry.model_id)
        )
        if row is None:
            row = WorkloadORM(
                workload_id=f"catalog-{entry.model_id}",
                owner_user_id=None,
                name=entry.model_id,
                image=image,
                display_name=entry.display_name or entry.model_id,
                tags=["catalog"],
                workload_alias=entry.model_id,
                kind="inference",
                security_tier="standard",
                pricing_class="standard",
                requirements=requirements,
                runtime=runtime,
                lifecycle={},
                public=(entry.visibility == "public"),
                metadata_json=metadata_json,
            )
        else:
            row.display_name = entry.display_name or entry.model_id
            row.image = image
            row.requirements = requirements
            row.runtime = runtime
            row.public = (entry.visibility == "public")
            row.metadata_json = metadata_json
        session.add(row)


@router.post("/validator/v1/catalog", status_code=201)
def upsert_catalog_entry(
    payload: ModelCatalogEntry,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — directly upsert a catalog entry (bypass submission flow)."""
    require_admin_api_key(authorization, x_api_key)
    payload.model_id = _validate_model_id(payload.model_id)
    service.repository.upsert_catalog_entry(payload)
    _ensure_catalog_workload(payload)
    return payload.model_dump(mode="json")


@router.get("/validator/v1/catalog")
def list_catalog(visibility: str | None = None) -> list[dict]:
    """Public — list catalog entries (optionally filter by visibility)."""
    entries = service.repository.list_catalog_entries(visibility=visibility)
    return [e.model_dump(mode="json") for e in entries]


# NOTE: `/validator/v1/catalog/{model_id}` is defined AFTER the
# `/catalog/submissions*` routes below so FastAPI doesn't capture
# "submissions" as a model_id (first-match routing).


# ====================================================================
# Audit — public endpoints for independent verifiers (greenference-audit)
# ====================================================================

@router.get("/validator/v1/audit/reports")
def list_audit_reports(limit: int = 100, offset: int = 0) -> dict:
    """Public — paginated index of audit reports published by this validator.
    Each entry includes the SHA256 anchored on-chain + its commitment tx."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    reports = service.repository.list_audit_reports(limit=limit, offset=offset)
    return {
        "reports": [
            {
                "epoch_id": r.epoch_id,
                "netuid": r.netuid,
                "epoch_start_block": r.epoch_start_block,
                "epoch_end_block": r.epoch_end_block,
                "report_sha256": r.report_sha256,
                "signer_hotkey": r.signer_hotkey,
                "chain_commitment_tx": r.chain_commitment_tx,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reports
        ],
    }


@router.get("/validator/v1/audit/reports/{epoch_id}")
def get_audit_report(epoch_id: str) -> dict:
    """Public — full signed audit report for one epoch. Auditors
    recompute sha256(canonical_json) and verify it matches both the
    report_sha256 field and the on-chain Commitments entry."""
    report = service.repository.get_audit_report(epoch_id)
    if report is None:
        raise HTTPException(status_code=404, detail="audit report not found")
    return report.model_dump(mode="json")


@router.get("/validator/v1/audit/commitment/{epoch_id}")
def get_audit_commitment(epoch_id: str) -> dict:
    """Public — just the on-chain anchor info (convenience for
    low-bandwidth auditors that only care about the SHA256 + tx)."""
    report = service.repository.get_audit_report(epoch_id)
    if report is None:
        raise HTTPException(status_code=404, detail="audit report not found")
    return {
        "epoch_id": report.epoch_id,
        "report_sha256": report.report_sha256,
        "chain_commitment_tx": report.chain_commitment_tx,
        "signer_hotkey": report.signer_hotkey,
        "signature": report.signature,
    }


@router.get("/validator/v1/audit/hotkey.pub")
def get_audit_hotkey() -> dict:
    """Public — the validator's SS58 hotkey address. Auditors use this to
    verify the ed25519 signature on each audit report. Same hotkey also
    signs set_weights and set_commitment on-chain."""
    hotkey = ""
    try:
        chain = getattr(service, "_chain", None)
        wallet_path = getattr(chain, "wallet_path", None) if chain else None
        if wallet_path:
            from substrateinterface import Keypair as _Keypair
            kp = _Keypair.create_from_uri(wallet_path)
            hotkey = kp.ss58_address
    except Exception:
        pass
    return {"ss58_address": hotkey}


@router.get("/validator/v1/catalog-status")
def catalog_status() -> dict:
    """Public — running replica counts + recent demand per catalog entry.
    Non-admin so /models and landing pages can show 'Hot · 3 miners serving'
    without requiring an admin API key."""
    running_by_model: dict[str, int] = {}
    serving_miners_by_model: dict[str, list[str]] = {}
    for state in service._flux_states.values():
        for model_id, idxs in state.inference_assignments.items():
            if not idxs:
                continue
            running_by_model[model_id] = running_by_model.get(model_id, 0) + 1
            serving_miners_by_model.setdefault(model_id, []).append(state.hotkey)

    rows: list[dict] = []
    for entry in service.repository.list_catalog_entries(visibility="public"):
        windows = service.repository.read_demand_windows(entry.model_id)
        rows.append({
            "model_id": entry.model_id,
            "display_name": entry.display_name,
            "running_replicas": running_by_model.get(entry.model_id, 0),
            "serving_miners_count": len(serving_miners_by_model.get(entry.model_id, [])),
            "rpm_10m": round(windows["rpm_10m"], 2),
            "rpm_1h": round(windows["rpm_1h"], 2),
        })
    return {"catalog": rows}


@router.delete("/validator/v1/catalog/{model_id}")
def delete_catalog_entry(
    model_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — remove a catalog entry. Also drops the canonical workload
    (miners stop hosting it on the next Flux rebalance cycle)."""
    require_admin_api_key(authorization, x_api_key)
    removed = service.repository.delete_catalog_entry(model_id)
    if not removed:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    # Best-effort workload cleanup.
    try:
        from sqlalchemy import delete as _delete
        from greenference_persistence import session_scope as _session_scope
        from greenference_persistence.orm import WorkloadORM

        with _session_scope(service.repository.session_factory) as session:
            session.execute(_delete(WorkloadORM).where(WorkloadORM.name == model_id))
    except Exception:
        pass
    return {"deleted": True, "model_id": model_id}


@router.get("/validator/v1/catalog/submissions/status/{hotkey}")
def catalog_submissions_status(hotkey: str) -> list[dict]:
    """Public — lookup a miner's own catalog submissions by hotkey."""
    subs = service.repository.list_catalog_submissions_by_hotkey(hotkey)
    return [s.model_dump(mode="json") for s in subs]


@router.post("/validator/v1/catalog/submissions", status_code=201)
def submit_catalog(payload: CatalogSubmission) -> dict:
    """Public — a miner (or anyone) proposes a model for the catalog.
    Admin review required; approval triggers catalog-entry + workload creation.
    """
    payload.model_id = _validate_model_id(payload.model_id)
    service.repository.create_catalog_submission(payload)
    return payload.model_dump(mode="json")


@router.get("/validator/v1/catalog/submissions")
def list_catalog_submissions(
    status: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — list catalog submissions, optionally filtered by status."""
    require_admin_api_key(authorization, x_api_key)
    subs = service.repository.list_catalog_submissions(status=status)
    return [s.model_dump(mode="json") for s in subs]


# Defined AFTER /catalog/submissions* routes so FastAPI (first-match) doesn't
# capture "submissions" as a model_id. Must stay below the submissions GET.
@router.get("/validator/v1/catalog/{model_id}")
def get_catalog_entry(model_id: str) -> dict:
    """Public — fetch a single catalog entry."""
    entry = service.repository.get_catalog_entry(model_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    return entry.model_dump(mode="json")


@router.post("/validator/v1/catalog/submissions/{submission_id}/approve")
def approve_catalog_submission(
    submission_id: str,
    reviewer_notes: str = "",
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — approve a submission. Auto-creates the canonical catalog
    entry and its workload; Flux picks it up on the next rebalance cycle."""
    require_admin_api_key(authorization, x_api_key)
    sub = service.repository.update_catalog_submission_status(
        submission_id, "approved", reviewer_notes
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="submission not found")
    entry = ModelCatalogEntry(
        model_id=sub.model_id,
        display_name=sub.display_name or sub.model_id,
        hf_repo=sub.hf_repo,
        template=sub.template,
        min_vram_gb_per_gpu=sub.min_vram_gb_per_gpu,
        gpu_count=sub.gpu_count,
        max_model_len=sub.max_model_len,
        visibility="public",
        created_by_hotkey=sub.hotkey or None,
    )
    service.repository.upsert_catalog_entry(entry)
    _ensure_catalog_workload(entry)
    return {
        "status": "approved",
        "submission_id": submission_id,
        "model_id": sub.model_id,
    }


@router.post("/validator/v1/catalog/submissions/{submission_id}/reject")
def reject_catalog_submission(
    submission_id: str,
    reviewer_notes: str = "",
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — reject a catalog submission."""
    require_admin_api_key(authorization, x_api_key)
    sub = service.repository.update_catalog_submission_status(
        submission_id, "rejected", reviewer_notes
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return {"status": "rejected", "submission_id": submission_id}

import json
import logging
import math
import os
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from greencompute_protocol import (
    APIKeyCreateRequest,
    APIKeySummary,
    BareMetalInquiryCreateRequest,
    BuildContextUploadRequest,
    BuildRequest,
    ChatCompletionRequest,
    CommercialInquiryCreateRequest,
    DeploymentCreateRequest,
    DeploymentUpdateRequest,
    GpuCapacityOverride,
    ProviderServerCreateRequest,
    UserProfileUpdateRequest,
    UserSecretCreateRequest,
    UserRegistrationRequest,
    WorkloadCreateRequest,
    WorkloadShareCreateRequest,
    WorkloadUpdateRequest,
)
from greencompute_gateway.application.services import service
from greencompute_gateway.domain.routing import NoReadyDeploymentError
from greencompute_gateway.infrastructure.inference_client import (
    InferenceBadResponseError,
    InferenceConnectionError,
    InferenceTimeoutError,
    InferenceUpstreamError,
)
from greencompute_gateway.transport.security import (
    current_client_ip,
    enforce_rate_limit,
    metrics,
    require_api_key,
    require_provision_or_admin,
)

router = APIRouter()
log = logging.getLogger(__name__)


# --- Top-up amount bounds (single source of truth for UI + backend) --------
# Keep the default minimum at $1 (don't reject existing low-value flows). The
# UI references MIN_TOPUP_USD so the preset/custom-input minimum can never
# drift from what the backend enforces (BILL-M3). MAX is env-overridable so
# ops can raise it for a known big customer without a redeploy.
MIN_TOPUP_USD = 1.0
try:
    MAX_TOPUP_USD = float(os.environ.get("GREENCOMPUTE_MAX_TOPUP_USD", "50000"))
except (TypeError, ValueError):
    MAX_TOPUP_USD = 50000.0


def _validate_topup_amount(raw) -> float:
    """Validate a user-supplied top-up amount: numeric, finite, within
    [MIN_TOPUP_USD, MAX_TOPUP_USD]. Rounds to cents server-side. Raises
    HTTPException(400) on any violation (NaN/Inf/non-numeric/out-of-range)."""
    try:
        amt = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount_usd must be a number")
    if not math.isfinite(amt):
        raise HTTPException(status_code=400, detail="amount_usd must be finite")
    if amt < MIN_TOPUP_USD:
        raise HTTPException(
            status_code=400, detail=f"amount_usd must be >= {MIN_TOPUP_USD:g}"
        )
    if amt > MAX_TOPUP_USD:
        raise HTTPException(
            status_code=400, detail=f"amount_usd must be <= {MAX_TOPUP_USD:g}"
        )
    return round(amt, 2)


def _audit_client_ip() -> str | None:
    """Best-effort client IP for audit logs, read from the request-context
    ContextVar populated by the middleware."""
    try:
        return current_client_ip.get()
    except LookupError:
        return None


def _feature_disabled(feature: str) -> None:
    raise HTTPException(
        status_code=501,
        detail={
            "status": "disabled",
            "feature": feature,
            "reason": "not implemented in greencompute-api",
        },
    )


@router.post("/platform/api-keys")
def create_api_key(
    payload: APIKeyCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Mint an api key. AUTHENTICATED — anonymous callers are rejected.

    Caller tiers (most → least privileged):
      - env master-admin key: may set an arbitrary user_id and admin=true.
      - GREENCOMPUTE_PROVISION_SECRET (UI server provisioning): may bind the
        key to an explicit user_id, but admin is forced false.
      - a normal user api key: may only mint a key bound to its OWN user_id;
        client-supplied user_id/admin/scopes are ignored.

    Client-supplied `admin`/`user_id`/`scopes` are NEVER trusted from a
    non-admin caller — privilege is derived from the authenticated principal.
    """
    try:
        caller, is_master_admin = require_provision_or_admin(authorization, x_api_key)
    except HTTPException as exc:
        # Not a privileged caller — fall back to self-service: a normal,
        # valid user key may mint a key for its own user_id only. A truly
        # anonymous/invalid caller still gets rejected here.
        if exc.status_code not in (401, 403):
            raise
        api_key = require_api_key(authorization, x_api_key)
        return service.create_api_key(
            payload,
            caller_user_id=api_key.user_id,
            allow_user_id_override=False,  # bound to own user_id
            allow_admin_grant=False,
        ).model_dump(mode="json")
    # Privileged caller. Both the env master admin AND the provision secret may
    # bind the key to an explicit user_id (the UI provisions a key for the
    # newly-registered user). Only the master admin may grant admin=true.
    return service.create_api_key(
        payload,
        caller_user_id=caller.user_id,
        allow_user_id_override=True,
        allow_admin_grant=is_master_admin,
    ).model_dump(mode="json")


@router.get("/platform/api-keys")
def list_api_keys(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    keys = service.list_api_keys(user_id=api_key.user_id, admin=api_key.admin)
    return [
        APIKeySummary(
            key_id=k.key_id,
            name=k.name,
            user_id=k.user_id,
            admin=k.admin,
            scopes=k.scopes,
            created_at=k.created_at,
        ).model_dump(mode="json")
        for k in keys
    ]


@router.get("/platform/api-keys/{key_id}")
def get_api_key(
    key_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    key = service.get_api_key(key_id, user_id=api_key.user_id, admin=api_key.admin)
    if key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    return APIKeySummary(
        key_id=key.key_id,
        name=key.name,
        user_id=key.user_id,
        admin=key.admin,
        scopes=key.scopes,
        created_at=key.created_at,
    ).model_dump(mode="json")


@router.delete("/platform/api-keys/{key_id}")
def delete_api_key(
    key_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        deleted = service.delete_api_key(key_id, user_id=api_key.user_id, admin=api_key.admin)
        return APIKeySummary(
            key_id=deleted.key_id,
            name=deleted.name,
            user_id=deleted.user_id,
            admin=deleted.admin,
            scopes=deleted.scopes,
            created_at=deleted.created_at,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/platform/register")
def register_user(
    payload: UserRegistrationRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Register (or idempotently return) a user. AUTHENTICATED — requires the
    provisioning secret or an admin key (the UI server sends the provision
    secret). register is idempotent-by-email so re-running is safe."""
    require_provision_or_admin(authorization, x_api_key)
    return service.register_user(payload).model_dump(mode="json")


@router.get("/platform/users")
def list_users(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [u.model_dump(mode="json") for u in service.list_users()]


@router.get("/platform/users/{user_id}")
def get_user(
    user_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if not api_key.admin and api_key.user_id != user_id:
        raise HTTPException(status_code=403, detail="user access denied")
    user = service.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user.model_dump(mode="json")


@router.get("/platform/users/{user_id}/balance")
def get_user_balance(
    user_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if not api_key.admin and api_key.user_id != user_id:
        raise HTTPException(status_code=403, detail="user access denied")
    user = service.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return {
        "user_id": user_id,
        "balance_credits": user.balance_credits,
        "balance_usd": round(user.balance_credits / 100.0, 2),
    }


@router.patch("/platform/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserProfileUpdateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if not api_key.admin and api_key.user_id != user_id:
        raise HTTPException(status_code=403, detail="user access denied")
    try:
        return service.update_user_profile(user_id, payload).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/images")
def build_image(
    payload: BuildRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("build_image", api_key.key_id, limit=30, window_seconds=60)
    return service.start_build(payload, owner_user_id=api_key.user_id).model_dump(mode="json")


@router.post("/platform/images/contexts")
def upload_build_context(
    payload: BuildContextUploadRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("upload_build_context", api_key.key_id, limit=30, window_seconds=60)
    return service.upload_build_context(payload).model_dump(mode="json")


@router.get("/platform/images")
def list_images(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        build.model_dump(mode="json")
        for build in service.list_builds(user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/images/{image:path}/history")
def image_history(
    image: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        build.model_dump(mode="json")
        for build in service.list_image_history(image, user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/builds")
def list_build_attempts(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        build.model_dump(mode="json")
        for build in service.list_builds(user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/builds/{build_id}")
def get_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    build = service.get_build(build_id, user_id=api_key.user_id, admin=api_key.admin)
    if build is None:
        raise HTTPException(status_code=404, detail="build not found")
    return build.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/context")
def get_build_context(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    context = service.get_build_context(build_id)
    if context is None:
        raise HTTPException(status_code=404, detail="build context not found")
    return context.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/events")
def get_build_events(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [event.model_dump(mode="json") for event in service.list_build_events(build_id)]


@router.get("/platform/builds/{build_id}/attempts")
def get_build_attempts(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.build_attempts(build_id)


@router.get("/platform/builds/{build_id}/jobs")
def get_build_jobs(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [job.model_dump(mode="json") for job in service.list_build_jobs(build_id)]


@router.get("/platform/builds/{build_id}/jobs/latest")
def get_latest_build_job(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    job = service.get_build_job(build_id)
    if job is None:
        raise HTTPException(status_code=404, detail="build job not found")
    return job.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/jobs/latest/timeline")
def get_latest_build_job_timeline(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    if service.get_build_job(build_id) is None:
        raise HTTPException(status_code=404, detail="build job not found")
    return [entry.model_dump(mode="json") for entry in service.latest_build_job_timeline(build_id)]


@router.post("/platform/builds/{build_id}/jobs/latest/cancel")
def cancel_latest_build_job(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.cancel_latest_build_job(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/builds/{build_id}/jobs/latest/restart")
def restart_latest_build_job(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.restart_latest_build_job(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/platform/builds/recovery/status")
def build_recovery_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.build_recovery_status()


@router.post("/platform/builds/recovery")
def recover_build_jobs(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.recover_build_jobs()


@router.get("/platform/builds/{build_id}/recovery-summary")
def build_recovery_summary(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.build_recovery_summary(build_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/platform/builds/{build_id}/attempts/{attempt}")
def get_build_attempt(
    build_id: str,
    attempt: int,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    record = service.get_build_attempt(build_id, attempt)
    if record is None:
        raise HTTPException(status_code=404, detail="build attempt not found")
    return record.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/logs")
def get_build_logs(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [log.model_dump(mode="json") for log in service.list_build_logs(build_id)]


@router.get("/platform/builds/{build_id}/logs/stream")
def stream_build_logs(
    build_id: str,
    follow: bool = False,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StreamingResponse:
    require_api_key(authorization, x_api_key, admin_required=True)
    return StreamingResponse(
        service.stream_build_logs(build_id, follow=follow),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.post("/platform/builds/{build_id}/retry")
def retry_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.retry_build(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/builds/{build_id}/cleanup")
def cleanup_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.cleanup_build(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/builds/{build_id}/cancel")
def cancel_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.cancel_build(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/platform/workloads")
def create_workload(
    payload: WorkloadCreateRequest | dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("create_workload", api_key.key_id, limit=30, window_seconds=60)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    payload_data = payload if isinstance(payload, dict) else payload.model_dump(mode="json")
    template = payload_data.get("template")
    # NOTE: the route accepts `WorkloadCreateRequest | dict`, so an invalid body
    # (e.g. gpu_count > 16) silently falls back to `dict` at parse time instead
    # of FastAPI's 422 — then re-validating here would raise an *uncaught*
    # ValidationError and surface as a 500. Catch it and return a clean 422 so
    # clients see what they got wrong (this is what made `gpu_count:10` a 500).
    try:
        if template == "vllm":
            from greencompute_gateway.domain.templates import build_vllm_workload
            model = payload_data.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model required for vllm template")
            request = build_vllm_workload(
                model,
                **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
            )
        elif template == "vllm-vision":
            from greencompute_gateway.domain.templates import build_vllm_vision_workload
            model = payload_data.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model required for vllm-vision template")
            request = build_vllm_vision_workload(
                model,
                **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
            )
        elif template == "diffusion":
            from greencompute_gateway.domain.templates import build_diffusion_workload
            model = payload_data.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model required for diffusion template")
            request = build_diffusion_workload(
                model,
                **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
            )
        else:
            request = WorkloadCreateRequest(**payload_data)
    except ValidationError as exc:
        detail = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors(include_url=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from exc
    except ValueError as exc:
        # Template builders raise ValueError (e.g. "model must be org/model
        # format"); without this it would surface as a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redact_workload(service.create_workload(request, api_key.user_id).model_dump(mode="json"))


def _is_secret_meta_key(key: str) -> bool:
    k = key.lower().replace("_", "")
    return any(s in k for s in ("token", "secret", "password", "apikey", "privatekey"))


def _redact_workload(d: dict) -> dict:
    """Strip owner-supplied secrets (HF tokens, API keys, passwords) from a
    workload's metadata before it leaves the gateway — these must never reach
    any client, owner or not. Public keys / ports / other config stay intact.
    (A workload is visible cross-user when public, so an un-redacted hf_token
    would otherwise leak to every authenticated user.)"""
    meta = d.get("metadata")
    if isinstance(meta, dict):
        d["metadata"] = {
            k: ("***redacted***" if _is_secret_meta_key(k) else v)
            for k, v in meta.items()
        }
    return d


@router.get("/platform/workloads")
def list_workloads(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        _redact_workload(workload.model_dump(mode="json"))
        for workload in service.list_workloads(user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/workloads/{workload_id}")
def get_workload(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    workload = service.get_workload(workload_id, user_id=api_key.user_id, admin=api_key.admin)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")
    return _redact_workload(workload.model_dump(mode="json"))


@router.patch("/platform/workloads/{workload_id}")
def update_workload(
    workload_id: str,
    payload: WorkloadUpdateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.update_workload(
            workload_id,
            payload,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/platform/workloads/{workload_id}/shares")
def share_workload(
    workload_id: str,
    payload: WorkloadShareCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.share_workload(
            workload_id,
            payload,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/workloads/{workload_id}")
def delete_workload(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.delete_workload(
            workload_id,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/platform/workloads/{workload_id}/utilization")
def get_workload_utilization(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    workload = service.get_workload(workload_id, user_id=api_key.user_id, admin=api_key.admin)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")
    return service.workload_utilization(workload_id)


@router.get("/platform/workloads/{workload_id}/warmup")
def workload_warmup(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StreamingResponse:
    api_key = require_api_key(authorization, x_api_key)
    workload = service.get_workload(workload_id, user_id=api_key.user_id, admin=api_key.admin)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")

    def _warmup_stream():
        yield f"data: {json.dumps({'workload_id': workload_id, 'status': 'warmup_started'})}\n\n"
        yield f"data: {json.dumps({'workload_id': workload_id, 'status': 'warmup_complete'})}\n\n"

    return StreamingResponse(
        _warmup_stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/platform/workloads/{workload_id}/shares")
def list_workload_shares(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return [
            share.model_dump(mode="json")
            for share in service.list_workload_shares(
                workload_id,
                actor_user_id=api_key.user_id,
                admin=api_key.admin,
            )
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/platform/deployments")
def create_deployment(
    payload: DeploymentCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    from greencompute_gateway.application.services import InsufficientBalanceForRentalError

    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("create_deployment", api_key.key_id, limit=30, window_seconds=60)
    try:
        return service.create_deployment(
            payload,
            user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except InsufficientBalanceForRentalError as exc:
        # 402 Payment Required — UI reads the numeric fields to render a
        # friendly "Add $X to start" prompt.
        raise HTTPException(
            status_code=402,
            detail={
                "message": "insufficient balance to start this rental",
                "required_cents": exc.required_cents,
                "current_cents": exc.current_cents,
                "rate_cents_per_hour": exc.rate_cents_per_hour,
                "gpu_count": exc.gpu_count,
                "requested_instances": exc.requested_instances,
            },
        ) from exc


def _attach_owner_info(rows: list[dict], users_by_id: dict) -> list[dict]:
    """Enrich admin deployment rows with the owner's email + username so a
    sales/admin investigator can identify the customer behind each deployment.
    owner_user_id (a UUID) is already on every row; this adds the
    human-readable fields. Admin-only by caller — never attach owner identity
    to a regular user's own-deployments list."""
    for row in rows:
        owner = users_by_id.get(row.get("owner_user_id"))
        if owner is not None:
            row["owner_email"] = owner.email
            row["owner_username"] = owner.username
    return rows


def _attach_workload_info(rows: list[dict], workloads_by_id: dict) -> list[dict]:
    """Enrich admin deployment rows with the workload's kind + display name so
    the 'who is renting what' admin view can tell rentals (pod/vm) from model
    deployments (inference) at a glance, and show a friendly name instead of
    the workload UUID. Admin-only by caller."""
    for row in rows:
        wl = workloads_by_id.get(row.get("workload_id"))
        if wl is not None:
            row["workload_kind"] = getattr(wl.kind, "value", str(wl.kind))
            row["workload_name"] = wl.display_name or wl.name
    return rows


@router.get("/platform/deployments")
def list_deployments(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    rows = [
        deployment.model_dump(mode="json")
        for deployment in service.list_deployments(user_id=api_key.user_id, admin=api_key.admin)
    ]
    if api_key.admin:
        # Batch-map users + workloads once so each row can show the customer
        # behind it and whether it's a rental (pod/vm) or a model (inference).
        users_by_id = {u.user_id: u for u in service.list_users()}
        _attach_owner_info(rows, users_by_id)
        workloads_by_id = {w.workload_id: w for w in service.list_workloads(admin=True)}
        _attach_workload_info(rows, workloads_by_id)
    return rows


@router.get("/platform/deployments/{deployment_id}")
def get_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    return deployment.model_dump(mode="json")


@router.get("/platform/deployments/{deployment_id}/ssh")
def get_deployment_ssh(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if not deployment.endpoint or not deployment.endpoint.startswith("ssh://"):
        raise HTTPException(status_code=404, detail="SSH not available for this deployment")
    # Parse ssh://user@host:port
    parts = deployment.endpoint.replace("ssh://", "").split("@", 1)
    user = parts[0] if len(parts) == 2 else "root"
    host_port = parts[-1].rsplit(":", 1)
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) == 2 else 22
    # Audit: a long-lived SSH private key is being handed out. Authz already
    # passed (owner/admin), but record WHO pulled it.
    metrics.increment("secret_access.deployment_ssh")
    log.info(
        "secret_access event=deployment_ssh_key deployment_id=%s key_id=%s user_id=%s ip=%s",
        deployment_id,
        api_key.key_id,
        api_key.user_id,
        _audit_client_ip(),
    )
    return {
        "ssh_host": host,
        "ssh_port": port,
        "ssh_username": user,
        "ssh_command": f"ssh {user}@{host} -p {port}",
        "private_key": deployment.ssh_private_key,
    }


def _control_plane_base() -> str:
    """Internal base URL for control-plane HTTP calls. Inside compose we use
    the container hostname; the env var lets ops override for e.g. smoke tests."""
    return os.environ.get("GREENCOMPUTE_CONTROL_PLANE_URL", "http://control-plane:8001").rstrip("/")


def _admin_api_key() -> str | None:
    return os.environ.get("GREENCOMPUTE_ADMIN_API_KEY") or None


def _lookup_miner_base_url(hotkey: str, *, timeout: float = 3.0) -> str | None:
    """Query control-plane for a miner's api_base_url by hotkey."""
    url = f"{_control_plane_base()}/platform/v1/servers/{hotkey}"
    req = urllib_request.Request(url, method="GET")
    admin_key = _admin_api_key()
    if admin_key:
        req.add_header("X-API-Key", admin_key)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode() or "{}")
            base = body.get("api_base_url")
            return base.rstrip("/") if isinstance(base, str) and base else None
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


@router.get("/platform/deployments/{deployment_id}/stats")
def get_deployment_stats(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Live pod stats relay: gateway → control-plane (hotkey → miner URL)
    → miner node-agent's /pods/{id}/stats.

    On any hop failure returns 200 {} so the UI can keep polling without
    flipping to an error state.
    """
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if not deployment.hotkey:
        return {}

    miner_base = _lookup_miner_base_url(deployment.hotkey)
    if not miner_base:
        return {}

    url = f"{miner_base}/pods/{deployment_id}/stats"
    try:
        req = urllib_request.Request(url, method="GET")
        # Authenticate to the node-agent the same way the inference client does,
        # so this relay keeps working once the node :8007 secret is enforced.
        _agent_secret = os.environ.get("GREENCOMPUTE_INFERENCE_AUTH_SECRET")
        if _agent_secret:
            req.add_header("X-Agent-Auth", _agent_secret)
        with urllib_request.urlopen(req, timeout=3.0) as resp:  # noqa: S310
            if resp.status != 200:
                return {}
            body = resp.read().decode() or "{}"
            return json.loads(body)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return {}


@router.patch("/platform/deployments/{deployment_id}")
def update_deployment(
    deployment_id: str,
    payload: DeploymentUpdateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.update_deployment(
            deployment_id,
            payload,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/deployments/{deployment_id}")
def terminate_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.terminate_deployment(
            deployment_id,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/platform/deployments/{deployment_id}/resume")
def resume_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Pod-safe in-place resume of a SUSPENDED deployment — restarts the SAME
    container (preserves the user's saved work) after a 1-hour credit gate.
    Returns the same deployment record (SUSPENDED until the miner re-reports
    READY). 402 if balance can't cover an hour, 409 if the original node is
    full / its placement is gone.
    """
    from greencompute_gateway.application.services import InsufficientBalanceForRentalError

    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("resume_deployment", api_key.key_id, limit=30, window_seconds=60)
    try:
        return service.resume_deployment(
            deployment_id,
            user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        # Not in SUSPENDED state.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InsufficientBalanceForRentalError as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "message": "insufficient balance to resume this deployment",
                "required_cents": exc.required_cents,
                "current_cents": exc.current_cents,
                "rate_cents_per_hour": exc.rate_cents_per_hour,
                "gpu_count": exc.gpu_count,
                "requested_instances": exc.requested_instances,
            },
        ) from exc


@router.post("/platform/secrets")
def create_secret(
    payload: UserSecretCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return service.create_secret(api_key.user_id, payload).model_dump(mode="json")


@router.get("/platform/secrets")
def list_secrets(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return [secret.model_dump(mode="json") for secret in service.list_secrets(api_key.user_id)]


@router.delete("/platform/secrets/{secret_id}")
def delete_secret(
    secret_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.delete_secret(secret_id, user_id=api_key.user_id, admin=api_key.admin).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Commercial inquiries — public /contact-sales lead form
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Prefer X-Forwarded-For (set by nginx) over request.client.host so we
    rate-limit by real client IP, not the proxy."""
    fwd = request.headers.get("x-forwarded-for", "").strip()
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


# Allowed sales-inquiry statuses (free-string column, validated here).
# "attempted" = outreach has been attempted but the contact has NOT yet been
# reached — distinct from "contacted". Order reflects the funnel:
# new → attempted → contacted → won/lost.
_ALLOWED_INQUIRY_STATUSES = {"new", "attempted", "contacted", "won", "lost"}


@router.post("/platform/sales/inquiries", status_code=201)
def create_commercial_inquiry(
    payload: CommercialInquiryCreateRequest,
    request: Request,
) -> dict:
    """Public — no auth. Rate-limited by IP (5 / hour) and honeypotted."""
    enforce_rate_limit("commercial_inquiry", _client_ip(request), limit=10, window_seconds=3600)
    try:
        inquiry = service.submit_commercial_inquiry(
            payload,
            source_ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    # Only return the inquiry id and a friendly status — never leak source_ip
    # or other server-side metadata back to the caller.
    return {"inquiry_id": inquiry.inquiry_id, "status": inquiry.status}


@router.get("/platform/sales/inquiries")
def list_commercial_inquiries(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [
        inquiry.model_dump(mode="json")
        for inquiry in service.list_commercial_inquiries(
            status=status, limit=limit, offset=offset
        )
    ]


@router.patch("/platform/sales/inquiries/{inquiry_id}")
def update_commercial_inquiry(
    inquiry_id: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    # `status` is OPTIONAL: omit it (or send "") to save notes only without
    # changing the funnel stage — lets sales log context before reaching a
    # contact. When present it must be a known status.
    raw_status = body.get("status")
    status = str(raw_status).strip() if raw_status is not None else None
    if not status:
        status = None
    elif status not in _ALLOWED_INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be one of " + "|".join(sorted(_ALLOWED_INQUIRY_STATUSES)),
        )
    notes = body.get("notes")
    if notes is not None:
        notes = str(notes)[:5000]
    if status is None and notes is None:
        raise HTTPException(status_code=400, detail="provide status and/or notes")
    try:
        return service.update_commercial_inquiry_status(
            inquiry_id, status=status, notes=notes
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Bare-metal inquiries — dedicated /rental sales form
# ---------------------------------------------------------------------------


@router.post("/platform/sales/bare-metal-inquiries", status_code=201)
def create_bare_metal_inquiry(
    payload: BareMetalInquiryCreateRequest,
    request: Request,
) -> dict:
    """Public — no auth. Same hardening as commercial inquiries."""
    enforce_rate_limit(
        "bare_metal_inquiry", _client_ip(request), limit=10, window_seconds=3600
    )
    try:
        inquiry = service.submit_bare_metal_inquiry(
            payload,
            source_ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"inquiry_id": inquiry.inquiry_id, "status": inquiry.status}


@router.get("/platform/sales/bare-metal-inquiries")
def list_bare_metal_inquiries(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [
        inq.model_dump(mode="json")
        for inq in service.list_bare_metal_inquiries(
            status=status, limit=limit, offset=offset
        )
    ]


@router.patch("/platform/sales/bare-metal-inquiries/{inquiry_id}")
def update_bare_metal_inquiry(
    inquiry_id: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    # `status` optional — omit (or "") to save review_notes only. See the
    # commercial PATCH above for the rationale.
    raw_status = body.get("status")
    status = str(raw_status).strip() if raw_status is not None else None
    if not status:
        status = None
    elif status not in _ALLOWED_INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be one of " + "|".join(sorted(_ALLOWED_INQUIRY_STATUSES)),
        )
    review_notes = body.get("review_notes")
    if review_notes is not None:
        review_notes = str(review_notes)[:5000]
    if status is None and review_notes is None:
        raise HTTPException(status_code=400, detail="provide status and/or review_notes")
    try:
        return service.update_bare_metal_inquiry_status(
            inquiry_id, status=status, review_notes=review_notes
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# GPU capacity overrides — admin-controlled cluster size for /capacity & /rental
# ---------------------------------------------------------------------------


@router.get("/platform/admin/capacity-overrides")
def list_capacity_overrides(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [
        ov.model_dump(mode="json") for ov in service.list_gpu_capacity_overrides()
    ]


@router.put("/platform/admin/capacity-overrides/{gpu_model}")
def upsert_capacity_override(
    gpu_model: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key, admin_required=True)
    try:
        total = int(body.get("total_gpus") or 0)
        avail = int(body.get("available_gpus") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="total_gpus and available_gpus must be integers") from exc
    if total < 0 or avail < 0:
        raise HTTPException(status_code=400, detail="counts must be non-negative")
    if avail > total:
        raise HTTPException(status_code=400, detail="available_gpus cannot exceed total_gpus")
    override = GpuCapacityOverride(
        gpu_model=gpu_model,
        total_gpus=total,
        available_gpus=avail,
        note=str(body.get("note") or "")[:255],
        updated_by=(api_key.user_id or api_key.name or "admin"),
    )
    saved = service.upsert_gpu_capacity_override(override)
    return saved.model_dump(mode="json")


@router.delete("/platform/admin/capacity-overrides/{gpu_model}")
def delete_capacity_override(
    gpu_model: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    deleted = service.delete_gpu_capacity_override(gpu_model)
    if not deleted:
        raise HTTPException(status_code=404, detail="override not found")
    return {"deleted": True, "gpu_model": gpu_model}


# ---------------------------------------------------------------------------
# Provider server onboarding — UI-driven SSH provisioning of a node-agent
# onto a whitelisted provider's own hardware. The SSH credential is used by
# the background job only and never persisted.
# ---------------------------------------------------------------------------


def _server_owner_view(rec) -> dict:
    """Strip nothing secret here (no creds are stored), but cap the log on
    the wire and present a clean dict."""
    d = rec.model_dump(mode="json")
    return d


@router.post("/platform/provider/servers", status_code=201)
def create_provider_server(
    payload: ProviderServerCreateRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None and not api_key.admin:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    enforce_rate_limit("provider_server_create", api_key.key_id, limit=20, window_seconds=3600)
    try:
        record, inputs = service.create_provider_server(
            payload, user_id=api_key.user_id, admin=api_key.admin,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Provision in the background (paramiko is blocking; BackgroundTasks runs
    # sync callables in the threadpool, off the event loop).
    background_tasks.add_task(service.run_provisioning, inputs)
    return _server_owner_view(record)


@router.get("/platform/provider/servers")
def list_provider_servers(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        _server_owner_view(s)
        for s in service.list_provider_servers(api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/provider/servers/{server_id}")
def get_provider_server(
    server_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return _server_owner_view(
            service.get_provider_server(server_id, user_id=api_key.user_id, admin=api_key.admin)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/provider/servers/{server_id}")
def delete_provider_server(
    server_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        deleted = service.delete_provider_server(server_id, user_id=api_key.user_id, admin=api_key.admin)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"deleted": True, "server_id": deleted.server_id}


@router.post("/v1/chat/completions")
def chat_completions(
    payload: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    host: str | None = Header(default=None, alias="Host"),
) -> dict:
    from greencompute_gateway.application.services import InsufficientBalanceForInferenceError
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("chat_completions", api_key.key_id, limit=60, window_seconds=60)

    # Pre-flight balance gate: require any positive balance. Per-request cost
    # is fractions of a cent for small prompts, so we don't need a large
    # floor — just "not broke". Admin keys bypass. Anonymous (user_id=None)
    # also bypasses so internal / system callers are not blocked.
    if api_key.user_id is not None and not api_key.admin:
        current = BillingRepository().get_balance(api_key.user_id)
        if current <= 0:
            raise HTTPException(
                status_code=402,
                detail={
                    "message": "insufficient balance for inference",
                    "current_cents": current,
                },
            )

    try:
        if payload.stream:
            metrics.increment("invoke.stream")
            return StreamingResponse(
                service.stream_chat_completion(
                    payload,
                    api_key_id=api_key.key_id,
                    user_id=api_key.user_id,
                    routed_host=host,
                    admin=api_key.admin,
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache"},
            )
        response = service.invoke_chat_completion(
            payload,
            api_key_id=api_key.key_id,
            user_id=api_key.user_id,
            routed_host=host,
            admin=api_key.admin,
        ).model_dump(mode="json")
        metrics.increment("invoke.success")
        return response
    except InsufficientBalanceForInferenceError as exc:
        metrics.increment("invoke.failure.insufficient_balance")
        raise HTTPException(
            status_code=402,
            detail={"message": "insufficient balance for inference", "reason": str(exc)},
        ) from exc
    except NoReadyDeploymentError as exc:
        metrics.increment("invoke.failure.no_ready_deployment")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InferenceTimeoutError as exc:
        metrics.increment("invoke.failure.timeout")
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except InferenceConnectionError as exc:
        metrics.increment("invoke.failure.connection")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InferenceBadResponseError as exc:
        metrics.increment("invoke.failure.bad_response")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InferenceUpstreamError as exc:
        metrics.increment("invoke.failure.upstream")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/v1/completions")
def completions(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    host: str | None = Header(default=None, alias="Host"),
) -> dict:
    if not payload.get("model"):
        raise HTTPException(status_code=400, detail="model required")
    try:
        request = ChatCompletionRequest(
            model=payload["model"],
            messages=[{"role": "user", "content": payload.get("prompt", "")}],
            max_tokens=payload.get("max_tokens", 128),
            temperature=payload.get("temperature", 0.7),
            stream=payload.get("stream", False),
        )
    except ValidationError as exc:
        detail = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors(include_url=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from exc
    return chat_completions(request, authorization=authorization, x_api_key=x_api_key, host=host)


@router.post("/v1/embeddings")
def embeddings(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("embeddings", api_key.key_id, limit=60, window_seconds=60)
    text = payload.get("input", "")
    vector = [round(((ord(char) % 32) / 31.0), 6) for char in str(text)[:16]]
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "model": payload.get("model", "greencompute-embedding"),
    }


SUPPORTED_GPU_MODELS = [
    "a100",
    "a100-80gb",
    "h100",
    "h100-80gb",
    "a10",
    "a10g",
    "l40",
    "l40s",
    "rtx-4090",
    "rtx-4080",
    "rtx-3090",
    "rtx-3080",
    "v100",
    "v100-32gb",
    "t4",
    "t4g",
    "m60",
    "k80",
]


@router.get("/platform/nodes/supported")
def list_supported_gpus(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[str]:
    require_api_key(authorization, x_api_key)
    return SUPPORTED_GPU_MODELS


@router.get("/platform/v1/debug/route/{model}")
def debug_route(
    model: str,
    host: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        workload, routing = service.resolve_workload_reference(model, routed_host=host)
        workload_id = workload.workload_id
        deployment = service.control_plane.resolve_ready_deployment(workload_id)
        return {
            "model": model,
            "routing": routing,
            "workload_id": workload_id,
            "workload_alias": workload.workload_alias,
            "ingress_host": workload.ingress_host,
            "deployment": deployment.model_dump(mode="json") if deployment else None,
        }
    except NoReadyDeploymentError as exc:
        return {"model": model, "host": host, "error": str(exc), "deployment": None}


@router.get("/platform/v1/debug/routing-decisions")
def debug_routing_decisions(
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.list_routing_decisions(limit=limit)


@router.get("/platform/v1/metrics")
def platform_metrics(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return metrics.snapshot()


@router.get("/platform/v1/payment/summary")
def payment_summary(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.payment_summary()


@router.get("/platform/v1/invocations")
def list_invocations(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [record.model_dump(mode="json") for record in service.list_invocations(limit=limit)]


@router.get("/platform/v1/invocations/exports/recent")
def export_recent_invocations(
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.export_recent_invocations(limit=limit)


@router.get("/platform/model-aliases")
def list_model_aliases(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    workloads = service.list_workloads(user_id=api_key.user_id, admin=api_key.admin)
    return [
        {"alias": w.workload_alias, "workload_id": w.workload_id}
        for w in workloads
        if w.workload_alias
    ]


@router.post("/platform/model-aliases")
def create_or_update_model_alias(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    alias = payload.get("alias")
    workload_id = payload.get("workload_id")
    if not alias or not workload_id:
        raise HTTPException(status_code=400, detail="alias and workload_id required")
    try:
        from greencompute_protocol import WorkloadUpdateRequest

        return service.update_workload(
            workload_id,
            WorkloadUpdateRequest(workload_alias=alias),
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="alias must be a 1–100 char string") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/model-aliases/{alias}")
def delete_model_alias(
    alias: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    workloads = service.list_workloads(user_id=api_key.user_id, admin=api_key.admin)
    target = next((w for w in workloads if w.workload_alias == alias), None)
    if target is None:
        raise HTTPException(status_code=404, detail="model alias not found")
    try:
        from greencompute_protocol import WorkloadUpdateRequest

        return service.update_workload(
            target.workload_id,
            WorkloadUpdateRequest(clear_workload_alias=True),
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/platform/v1/invocations/{invocation_id}")
def get_invocation(
    invocation_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    record = service.get_invocation(invocation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="invocation not found")
    return record.model_dump(mode="json")


@router.get("/platform/v1/debug/invocation-failures")
def debug_invocation_failures(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.control_plane.invocation_failure_report(limit=limit)


@router.get("/registry/auth")
def registry_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    # The registry password is a shared infra credential. Scope-tighten: only
    # hand it to a user-bound key or an admin — never to an anonymous/system
    # key (user_id None and not admin).
    if api_key.user_id is None and not api_key.admin:
        raise HTTPException(status_code=403, detail="registry auth requires a user-bound api key")
    # Audit: a shared infra credential is being handed out.
    metrics.increment("secret_access.registry_auth")
    log.info(
        "secret_access event=registry_auth key_id=%s user_id=%s ip=%s",
        api_key.key_id,
        api_key.user_id,
        _audit_client_ip(),
    )
    import base64
    from greencompute_persistence.runtime import load_runtime_settings
    settings = load_runtime_settings("greencompute-gateway")
    registry_password = getattr(settings, "registry_password", "greencompute-registry")
    auth_string = base64.b64encode(f":{registry_password}".encode()).decode()
    return {"authenticated": True, "auth_header": f"Basic {auth_string}"}


@router.get("/guess/vllm_config")
def guess_vllm_config(
    model: str = Query(..., description="HuggingFace model id (org/model)"),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    from greencompute_gateway.infrastructure.guesser import analyze_model
    try:
        req = analyze_model(model)
        return req.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/logos")
def upload_logo(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("logos")


@router.get("/logos/{logo_id}.{ext}")
def get_logo(
    logo_id: str,
    ext: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("logos")


@router.get("/bounties")
def list_bounties(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key)
    _feature_disabled("bounties")


@router.post("/audit/miner_data")
def audit_miner_data(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    _feature_disabled("audit")


@router.get("/audit/")
def list_audit(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    _feature_disabled("audit")


@router.get("/audit/download")
def audit_download(
    path: str = Query(""),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    _feature_disabled("audit")


@router.get("/misc/proxy")
def misc_proxy(
    url: str = Query(""),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("misc_proxy")


@router.get("/misc/hf_repo_info")
def misc_hf_repo_info(
    repo: str = Query(""),
    path: str = Query(""),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("misc_hf_repo_info")


@router.get("/e2e/instances/{workload_id}")
def e2e_instances(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key)
    _feature_disabled("e2e_instances")


@router.get("/idp/scopes")
def idp_scopes() -> list[str]:
    _feature_disabled("idp")


@router.get("/idp/authorize")
def idp_authorize(
    client_id: str = Query(""),
    redirect_uri: str = Query(""),
    response_type: str = Query(""),
    scope: str = Query(""),
) -> dict:
    _feature_disabled("idp")


@router.post("/idp/token")
def idp_token(payload: dict) -> dict:
    _feature_disabled("idp")


@router.post("/e2e/invoke")
def e2e_invoke(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("e2e_invoke")


@router.get("/platform/v1/debug/build-failures")
def debug_build_failures(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [build.model_dump(mode="json") for build in service.list_failed_builds()]


# ---------------------------------------------------------------------------
# Billing endpoints
# ---------------------------------------------------------------------------


def _get_billing():
    from greencompute_gateway.application.billing_service import get_billing_service
    return get_billing_service()


@router.get("/platform/billing/balance")
def billing_balance(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return _get_billing().get_balance(api_key.user_id)


@router.get("/platform/billing/ledger")
def billing_ledger(
    limit: int = 50,
    offset: int = 0,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    entries = _get_billing().list_ledger(api_key.user_id, limit=limit, offset=offset)
    return [e.model_dump(mode="json") for e in entries]


@router.post("/platform/billing/topup/stripe")
def billing_topup_stripe(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    amount_usd = _validate_topup_amount(payload.get("amount_usd"))
    try:
        return _get_billing().create_stripe_topup(api_key.user_id, amount_usd)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/platform/billing/topup/crypto")
def billing_topup_crypto(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    currency = payload.get("currency", "").lower()
    allowed = (
        "usdt", "usdc",                           # legacy — single address for either chain
        "usdt-eth", "usdt-base",                  # chain-qualified
        "usdc-eth", "usdc-base",                  # chain-qualified
        "tao",
        # Alpha (subnet token) — auto-credited by scan_alpha in the deposit
        # watcher, which matches SubtensorModule.StakeTransferred events to our
        # deposit coldkey on the configured netuid. Priced via price_feed.
        "alpha",
    )
    if currency not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"currency must be one of: {', '.join(allowed)}",
        )
    amount_usd = _validate_topup_amount(payload.get("amount_usd"))
    return _get_billing().create_crypto_invoice(api_key.user_id, currency, amount_usd)


@router.post("/platform/billing/webhook/stripe")
async def billing_stripe_webhook(request: Request) -> dict:
    """Stripe webhook — called by Stripe when a checkout session completes,
    or when a charge is refunded / disputed (BILL-M1 Part B)."""
    from greencompute_gateway.infrastructure.stripe_client import (
        _payment_intent_id,
        verify_webhook_signature,
    )
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook_signature(payload, sig)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"webhook verification failed: {exc}") from exc
    event_type = event.get("type")
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session_data = event["data"]["object"]
        stripe_session_id = session_data["id"]
        # The completed session carries its PaymentIntent id — record it so a
        # later refund/dispute can map back to this top-up (BILL-M1 Part B).
        payment_intent_id = _payment_intent_id(session_data.get("payment_intent"))
        # Gate the credit on a PAID session. `checkout.session.completed` can
        # fire with payment_status in {'unpaid','no_payment_required'} for
        # async/delayed payment methods — crediting then books money we never
        # captured. Only credit when paid/complete; otherwise leave the session
        # pending for the reconcile job or a later async_payment_succeeded
        # event. confirm_stripe_payment is idempotent (BILL-C2), so a later
        # async event after a borderline 'complete' is safe.
        payment_status = session_data.get("payment_status")
        # MUST be payment_status=='paid'. `status=='complete'` is true for EVERY
        # checkout.session.completed event (incl. unpaid async sessions), so
        # OR-ing it in would credit money we never captured. A delayed payment
        # arrives later as checkout.session.async_payment_succeeded (paid).
        if payment_status == "paid":
            _get_billing().confirm_stripe_payment(
                stripe_session_id, payment_intent_id=payment_intent_id
            )
        else:
            log.info(
                "Stripe %s for session %s not credited (payment_status=%r) — "
                "awaiting capture / reconcile",
                event_type, stripe_session_id, payment_status,
            )
    elif event_type in ("charge.refunded", "charge.dispute.created"):
        # BILL-M1 Part B: reverse a prior top-up on a verified (signature-
        # checked above) refund or dispute. The event's `object` is a Charge
        # (refund) or a Dispute (chargeback); both carry the `payment_intent`,
        # which is our join key back to the originating checkout session — the
        # event does NOT carry our checkout session id. We resolve via the
        # session's recorded payment_intent_id and idempotently debit by the
        # amount actually refunded/disputed, keyed on the Stripe event id so a
        # re-delivered event reverses at most once. If the session can't be
        # resolved (historical session with NULL payment_intent_id), we log +
        # skip for manual reconcile rather than guess.
        obj = event["data"]["object"]
        event_id = event.get("id") or ""
        payment_intent_id = _payment_intent_id(obj.get("payment_intent"))
        if event_type == "charge.refunded":
            # A charge can be partially refunded across several events; reverse
            # the incremental amount of THIS refund. Prefer the most recent
            # refund's amount; fall back to amount_refunded.
            amount_cents = _latest_refund_amount_cents(obj)
        else:  # charge.dispute.created — the Dispute object carries `amount`
            amount_cents = int(obj.get("amount") or 0)
        try:
            _get_billing().reverse_stripe_topup_for_event(
                event_id=event_id,
                payment_intent_id=payment_intent_id,
                amount_cents=amount_cents,
                reason=("refund" if event_type == "charge.refunded" else "dispute"),
            )
        except Exception:
            # Never 200-swallow a genuine processing failure silently into a
            # lost reversal: log loudly and let Stripe retry (we re-raise as a
            # 500 so the event is redelivered; the reversal is idempotent).
            log.exception(
                "Stripe %s reversal failed for event %s", event_type, event_id
            )
            raise HTTPException(status_code=500, detail="reversal processing failed")
    return {"received": True}


def _latest_refund_amount_cents(charge: dict) -> int:
    """Best-effort amount (in cents) for the most recent refund on a Charge.

    `charge.refunded` fires once per refund. Stripe includes a `refunds` list
    (data ordered newest-first) whose latest entry is this refund's amount; if
    that's unavailable we fall back to the charge's cumulative
    `amount_refunded`. Both are integer minor units (cents) already.
    """
    refunds = charge.get("refunds")
    if isinstance(refunds, dict):
        data = refunds.get("data")
        if isinstance(data, list) and data:
            amt = data[0].get("amount")
            if amt is not None:
                return int(amt)
    return int(charge.get("amount_refunded") or 0)


@router.post("/platform/billing/crypto/{invoice_id}/report-tx")
def billing_report_crypto_tx(
    invoice_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """User-side — attach a tx hash to an invoice after sending funds.
    Does NOT credit the user; admin still has to verify and confirm. The
    hash is stored on the invoice so the admin UI has a starting point.
    """
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    tx_hash = (payload.get("tx_hash") or "").strip()
    if not tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required")
    result = _get_billing().repo.report_invoice_tx_hash(
        invoice_id=invoice_id,
        user_id=api_key.user_id,
        tx_hash=tx_hash,
    )
    if result is None:
        # Either invoice doesn't exist or caller doesn't own it — same response
        # either way so we don't leak invoice IDs.
        raise HTTPException(status_code=404, detail="invoice not found")
    return {
        "invoice_id": result.invoice_id,
        "status": result.status,
        "tx_hash": result.tx_hash,
    }


@router.post("/platform/billing/crypto/{invoice_id}/confirm")
def billing_confirm_crypto(
    invoice_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — confirm a crypto deposit."""
    require_api_key(authorization, x_api_key, admin_required=True)
    tx_hash = payload.get("tx_hash", "")
    if not tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required")
    result = _get_billing().confirm_crypto_deposit(invoice_id, tx_hash)
    if result is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if result.get("refused"):
        raise HTTPException(
            status_code=409,
            detail=f"invoice is {result.get('status')} and can no longer be credited",
        )
    return result


@router.post("/platform/billing/crypto/{invoice_id}/reject")
def billing_reject_crypto(
    invoice_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — reject a crypto invoice. Does NOT credit the user. Marks
    status as 'rejected' so it drops out of the pending queue. Idempotent.
    Rejecting a confirmed invoice returns 409 — use admin debit to reverse
    that flow instead.
    """
    require_api_key(authorization, x_api_key, admin_required=True)
    result = _get_billing().repo.reject_crypto_invoice(invoice_id)
    if result is None:
        # Either doesn't exist or already confirmed — both look like 409/404.
        # Using 404 for simplicity; admin can refresh to see current state.
        raise HTTPException(
            status_code=404,
            detail="invoice not found or already confirmed",
        )
    return {"rejected": True, "invoice_id": result.invoice_id}


@router.get("/platform/billing/crypto/invoices")
def billing_list_crypto_invoices(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """User-side — the caller's own crypto top-up invoices, newest first, so
    they can track status (pending / confirmed / expired / rejected) after
    generating one. Scoped to the api key's user_id."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return [
        inv.model_dump(mode="json")
        for inv in _get_billing().repo.list_crypto_invoices(api_key.user_id)
    ]


@router.get("/platform/billing/admin/crypto/invoices")
def billing_admin_list_crypto_invoices(
    status: str | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — list crypto invoices (optionally filtered by status) with the
    user's email/username joined in so the admin can tell who each deposit
    is for at a glance. Used by the admin billing UI."""
    require_api_key(authorization, x_api_key, admin_required=True)
    return _get_billing().repo.list_all_crypto_invoices_for_admin(
        status=status,
        limit=max(1, min(limit, 500)),
    )


@router.get("/platform/billing/admin/miner-revenue")
def billing_admin_miner_revenue(
    window_hours: int = 168,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — miner revenue accrued from successful inference requests.
    Feeds the admin /flux dashboard revenue tile."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository
    rows = BillingRepository().aggregate_miner_revenue(window_hours=window_hours)
    total_cents = sum(r["cents_earned"] for r in rows)
    return {
        "window_hours": window_hours,
        "total_cents": total_cents,
        "rows": rows,
    }


@router.get("/platform/billing/bonus-rates")
def billing_bonus_rates() -> dict:
    """Public — returns the bonus rates for each payment method."""
    from greencompute_gateway.application.billing_service import BONUS_RATES
    return {k: f"+{int(v*100)}%" for k, v in BONUS_RATES.items()}


# ---------------------------------------------------------------------------
# Admin commercial analytics
#
# All routes here require an admin API key. Aggregations are bounded by
# `days` (capped server-side in AnalyticsRepository) so curling silly windows
# can't take the DB down. Each panel has its own endpoint — UI fetches in
# parallel so one slow query doesn't block the rest.
# ---------------------------------------------------------------------------


def _analytics_repo():
    """Cached per-process instance — same DB engine reuse pattern as
    BillingRepository elsewhere in this file."""
    from greencompute_gateway.infrastructure.analytics_repository import (
        AnalyticsRepository,
    )

    return AnalyticsRepository()


@router.get("/platform/admin/analytics/revenue-by-miner")
def analytics_revenue_by_miner(
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    rows = _analytics_repo().revenue_by_miner(days=days, limit=limit)
    return {
        "window_days": days,
        "total_cents_earned": sum(r["cents_earned"] for r in rows),
        "rows": rows,
    }


@router.get("/platform/admin/analytics/top-renters")
def analytics_top_renters(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=25, ge=1, le=200),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    rows = _analytics_repo().top_renters(days=days, limit=limit)
    return {
        "window_days": days,
        "total_cents_spent": sum(r["cents_spent"] for r in rows),
        "rows": rows,
    }


@router.get("/platform/admin/analytics/gross-revenue")
def analytics_gross_revenue(
    days: int = Query(default=30, ge=1, le=365),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return _analytics_repo().gross_revenue(days=days)


@router.get("/platform/admin/analytics/active-rentals")
def analytics_active_rentals(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return _analytics_repo().active_rentals()


@router.post("/platform/billing/admin/credit")
def billing_admin_credit(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — manually credit a user's balance (for testing or manual adjustments)."""
    require_api_key(authorization, x_api_key, admin_required=True)
    user_id = payload.get("user_id")
    amount_usd = payload.get("amount_usd")
    description = payload.get("description", "Manual admin credit")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not amount_usd or amount_usd <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    amount_cents = int(round(float(amount_usd) * 100))
    billing = _get_billing()
    entry = billing.repo.credit_user(
        user_id=user_id,
        amount_cents=amount_cents,
        kind="topup",
        description=description,
    )
    return {
        "credited": True,
        "user_id": user_id,
        "amount_cents": amount_cents,
        "balance_after": entry.balance_after,
        "balance_usd": round(entry.balance_after / 100.0, 2),
    }


@router.post("/platform/billing/admin/debit")
def billing_admin_debit(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — manually debit a user's balance."""
    require_api_key(authorization, x_api_key, admin_required=True)
    user_id = payload.get("user_id")
    amount_usd = payload.get("amount_usd")
    description = payload.get("description", "Manual admin debit")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not amount_usd or amount_usd <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    amount_cents = int(round(float(amount_usd) * 100))
    billing = _get_billing()
    # Admin claw-back is trusted and may push the balance negative — this is
    # the fraud/chargeback case where the user already spent the funds, so a
    # strict debit (which would 400) is exactly wrong here. Usage/inference
    # debits stay strict (they call debit_user without allow_negative).
    entry = billing.repo.debit_user(
        user_id=user_id,
        amount_cents=amount_cents,
        kind="refund",
        description=description,
        allow_negative=True,
    )
    return {
        "debited": True,
        "user_id": user_id,
        "amount_cents": amount_cents,
        "balance_after": entry.balance_after,
        "balance_usd": round(entry.balance_after / 100.0, 2),
    }

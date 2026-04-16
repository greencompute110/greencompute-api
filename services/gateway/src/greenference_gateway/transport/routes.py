import json
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from greenference_protocol import (
    APIKeyCreateRequest,
    APIKeySummary,
    BuildContextUploadRequest,
    BuildRequest,
    ChatCompletionRequest,
    DeploymentCreateRequest,
    DeploymentUpdateRequest,
    UserProfileUpdateRequest,
    UserSecretCreateRequest,
    UserRegistrationRequest,
    WorkloadCreateRequest,
    WorkloadShareCreateRequest,
    WorkloadUpdateRequest,
)
from greenference_gateway.application.services import service
from greenference_gateway.domain.routing import NoReadyDeploymentError
from greenference_gateway.infrastructure.inference_client import (
    InferenceBadResponseError,
    InferenceConnectionError,
    InferenceTimeoutError,
    InferenceUpstreamError,
)
from greenference_gateway.transport.security import enforce_rate_limit, metrics, require_api_key

router = APIRouter()


def _feature_disabled(feature: str) -> None:
    raise HTTPException(
        status_code=501,
        detail={
            "status": "disabled",
            "feature": feature,
            "reason": "not implemented in greenference-api",
        },
    )


@router.post("/platform/api-keys")
def create_api_key(payload: APIKeyCreateRequest) -> dict:
    return service.create_api_key(payload).model_dump(mode="json")


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
def register_user(payload: UserRegistrationRequest) -> dict:
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
    if template == "vllm":
        from greenference_gateway.domain.templates import build_vllm_workload
        model = payload_data.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="model required for vllm template")
        request = build_vllm_workload(
            model,
            **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
        )
    elif template == "vllm-vision":
        from greenference_gateway.domain.templates import build_vllm_vision_workload
        model = payload_data.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="model required for vllm-vision template")
        request = build_vllm_vision_workload(
            model,
            **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
        )
    elif template == "diffusion":
        from greenference_gateway.domain.templates import build_diffusion_workload
        model = payload_data.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="model required for diffusion template")
        request = build_diffusion_workload(
            model,
            **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
        )
    else:
        request = WorkloadCreateRequest(**payload_data)
    return service.create_workload(request, api_key.user_id).model_dump(mode="json")


@router.get("/platform/workloads")
def list_workloads(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        workload.model_dump(mode="json")
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
    return workload.model_dump(mode="json")


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


@router.get("/platform/deployments")
def list_deployments(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        deployment.model_dump(mode="json")
        for deployment in service.list_deployments(user_id=api_key.user_id, admin=api_key.admin)
    ]


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
    return {
        "ssh_host": host,
        "ssh_port": port,
        "ssh_username": user,
        "ssh_command": f"ssh {user}@{host} -p {port}",
        "private_key": deployment.ssh_private_key,
    }


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


@router.post("/v1/chat/completions")
def chat_completions(
    payload: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    host: str | None = Header(default=None, alias="Host"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("chat_completions", api_key.key_id, limit=60, window_seconds=60)
    try:
        if payload.stream:
            metrics.increment("invoke.stream")
            return StreamingResponse(
                service.stream_chat_completion(payload, api_key_id=api_key.key_id, routed_host=host),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache"},
            )
        response = service.invoke_chat_completion(
            payload,
            api_key_id=api_key.key_id,
            routed_host=host,
        ).model_dump(mode="json")
        metrics.increment("invoke.success")
        return response
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
    request = ChatCompletionRequest(
        model=payload["model"],
        messages=[{"role": "user", "content": payload.get("prompt", "")}],
        max_tokens=payload.get("max_tokens", 128),
        temperature=payload.get("temperature", 0.7),
        stream=payload.get("stream", False),
    )
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
        "model": payload.get("model", "greenference-embedding"),
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
        from greenference_protocol import WorkloadUpdateRequest

        return service.update_workload(
            workload_id,
            WorkloadUpdateRequest(workload_alias=alias),
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
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
        from greenference_protocol import WorkloadUpdateRequest

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
    require_api_key(authorization, x_api_key)
    import base64
    from greenference_persistence.runtime import load_runtime_settings
    settings = load_runtime_settings("greenference-gateway")
    registry_password = getattr(settings, "registry_password", "greenference-registry")
    auth_string = base64.b64encode(f":{registry_password}".encode()).decode()
    return {"authenticated": True, "auth_header": f"Basic {auth_string}"}


@router.get("/guess/vllm_config")
def guess_vllm_config(
    model: str = Query(..., description="HuggingFace model id (org/model)"),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    from greenference_gateway.infrastructure.guesser import analyze_model
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
    from greenference_gateway.application.billing_service import get_billing_service
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
    amount_usd = payload.get("amount_usd")
    if not amount_usd or amount_usd < 1:
        raise HTTPException(status_code=400, detail="amount_usd must be >= 1")
    try:
        return _get_billing().create_stripe_topup(api_key.user_id, float(amount_usd))
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
    amount_usd = payload.get("amount_usd")
    if currency not in ("usdt", "usdc", "tao", "alpha"):
        raise HTTPException(status_code=400, detail="currency must be one of: usdt, usdc, tao, alpha")
    if not amount_usd or amount_usd < 1:
        raise HTTPException(status_code=400, detail="amount_usd must be >= 1")
    return _get_billing().create_crypto_invoice(api_key.user_id, currency, float(amount_usd))


@router.post("/platform/billing/webhook/stripe")
async def billing_stripe_webhook(request: Request) -> dict:
    """Stripe webhook — called by Stripe when a checkout session completes."""
    from greenference_gateway.infrastructure.stripe_client import verify_webhook_signature
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook_signature(payload, sig)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"webhook verification failed: {exc}") from exc
    if event.get("type") == "checkout.session.completed":
        session_data = event["data"]["object"]
        stripe_session_id = session_data["id"]
        _get_billing().confirm_stripe_payment(stripe_session_id)
    return {"received": True}


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
    return result


@router.get("/platform/billing/bonus-rates")
def billing_bonus_rates() -> dict:
    """Public — returns the bonus rates for each payment method."""
    from greenference_gateway.application.billing_service import BONUS_RATES
    return {k: f"+{int(v*100)}%" for k, v in BONUS_RATES.items()}


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
    from greenference_gateway.infrastructure.billing_repository import InsufficientBalanceError
    try:
        entry = billing.repo.debit_user(
            user_id=user_id,
            amount_cents=amount_cents,
            kind="refund",
            description=description,
        )
    except InsufficientBalanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "debited": True,
        "user_id": user_id,
        "amount_cents": amount_cents,
        "balance_after": entry.balance_after,
        "balance_usd": round(entry.balance_after / 100.0, 2),
    }

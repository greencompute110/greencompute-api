from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from greenference_protocol import (
    APIKeyCreateRequest,
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


@router.post("/platform/api-keys")
def create_api_key(payload: APIKeyCreateRequest) -> dict:
    return service.create_api_key(payload).model_dump(mode="json")


@router.post("/platform/register")
def register_user(payload: UserRegistrationRequest) -> dict:
    return service.register_user(payload).model_dump(mode="json")


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
    payload: WorkloadCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("create_workload", api_key.key_id, limit=30, window_seconds=60)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return service.create_workload(payload, api_key.user_id).model_dump(mode="json")


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


@router.get("/platform/v1/debug/build-failures")
def debug_build_failures(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [build.model_dump(mode="json") for build in service.list_failed_builds()]

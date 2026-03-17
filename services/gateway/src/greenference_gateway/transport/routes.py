from fastapi import APIRouter, HTTPException

from greenference_protocol import (
    APIKeyCreateRequest,
    BuildRequest,
    ChatCompletionRequest,
    DeploymentCreateRequest,
    UserRegistrationRequest,
    WorkloadCreateRequest,
)
from greenference_gateway.application.services import service
from greenference_gateway.domain.routing import NoReadyDeploymentError
from greenference_gateway.infrastructure.inference_client import InferenceUpstreamError

router = APIRouter()


@router.post("/platform/api-keys")
def create_api_key(payload: APIKeyCreateRequest) -> dict:
    return service.create_api_key(payload).model_dump(mode="json")


@router.post("/platform/register")
def register_user(payload: UserRegistrationRequest) -> dict:
    return service.register_user(payload).model_dump(mode="json")


@router.post("/platform/images")
def build_image(payload: BuildRequest) -> dict:
    return service.start_build(payload).model_dump(mode="json")


@router.get("/platform/images")
def list_images() -> list[dict]:
    return [build.model_dump(mode="json") for build in service.list_builds()]


@router.post("/platform/workloads")
def create_workload(payload: WorkloadCreateRequest) -> dict:
    return service.create_workload(payload).model_dump(mode="json")


@router.get("/platform/workloads")
def list_workloads() -> list[dict]:
    return [workload.model_dump(mode="json") for workload in service.list_workloads()]


@router.post("/platform/deployments")
def create_deployment(payload: DeploymentCreateRequest) -> dict:
    return service.create_deployment(payload).model_dump(mode="json")


@router.get("/platform/deployments")
def list_deployments() -> list[dict]:
    return [deployment.model_dump(mode="json") for deployment in service.list_deployments()]


@router.post("/v1/chat/completions")
def chat_completions(payload: ChatCompletionRequest) -> dict:
    try:
        return service.invoke_chat_completion(payload).model_dump(mode="json")
    except NoReadyDeploymentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InferenceUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/v1/completions")
def completions(payload: dict) -> dict:
    request = ChatCompletionRequest(
        model=payload["model"],
        messages=[{"role": "user", "content": payload.get("prompt", "")}],
        max_tokens=payload.get("max_tokens", 128),
        temperature=payload.get("temperature", 0.7),
    )
    return chat_completions(request)


@router.post("/v1/embeddings")
def embeddings(payload: dict) -> dict:
    text = payload.get("input", "")
    vector = [round(((ord(char) % 32) / 31.0), 6) for char in str(text)[:16]]
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "model": payload.get("model", "greenference-embedding"),
    }

from __future__ import annotations

import secrets
from collections.abc import Iterator
from json import loads

from greenference_builder.application.services import BuilderService, service as default_builder_service
from greenference_control_plane.application.services import (
    ControlPlaneService,
    service as default_control_plane_service,
)
from greenference_protocol import (
    APIKeyCreateRequest,
    APIKeyRecord,
    BuildRecord,
    BuildRequest,
    ChatCompletionRequest,
    DeploymentCreateRequest,
    DeploymentRecord,
    UserRecord,
    UserRegistrationRequest,
    UsageRecord,
    WorkloadCreateRequest,
    WorkloadSpec,
)
from greenference_gateway.domain.routing import NoReadyDeploymentError
from greenference_gateway.infrastructure.inference_client import HttpInferenceClient
from greenference_gateway.infrastructure.repository import GatewayRepository


class GatewayService:
    def __init__(
        self,
        repository: GatewayRepository | None = None,
        control_plane: ControlPlaneService | None = None,
        builder: BuilderService | None = None,
        inference_client: HttpInferenceClient | None = None,
    ) -> None:
        self.repository = repository or GatewayRepository()
        self.control_plane = control_plane or default_control_plane_service
        self.builder = builder or default_builder_service
        self.inference_client = inference_client or HttpInferenceClient()

    def register_user(self, request: UserRegistrationRequest) -> UserRecord:
        user = UserRecord(username=request.username, email=request.email)
        return self.repository.save_user(user)

    def create_api_key(self, request: APIKeyCreateRequest) -> APIKeyRecord:
        api_key = APIKeyRecord(
            name=request.name,
            user_id=request.user_id,
            admin=request.admin,
            scopes=request.scopes,
            secret=f"gk_{secrets.token_urlsafe(24)}",
        )
        return self.repository.save_api_key(api_key)

    def start_build(self, request: BuildRequest) -> BuildRecord:
        return self.builder.start_build(request)

    def list_builds(self) -> list[BuildRecord]:
        return self.builder.list_builds()

    def create_workload(self, request: WorkloadCreateRequest) -> WorkloadSpec:
        workload = WorkloadSpec(**request.model_dump())
        return self.control_plane.upsert_workload(workload)

    def list_workloads(self) -> list[WorkloadSpec]:
        return self.control_plane.list_workloads()

    def create_deployment(self, request: DeploymentCreateRequest | dict) -> DeploymentRecord:
        payload = request if isinstance(request, DeploymentCreateRequest) else DeploymentCreateRequest(**request)
        return self.control_plane.create_deployment(payload)

    def list_deployments(self) -> list[DeploymentRecord]:
        return self.control_plane.list_deployments()

    def invoke_chat_completion(self, request: ChatCompletionRequest):
        deployment = self._resolve_deployment_for_request(request)
        try:
            response = self.inference_client.invoke_chat_completion(deployment, request)
        except RuntimeError as exc:
            self.control_plane.record_deployment_health_failure(deployment.deployment_id, str(exc))
            raise
        self.control_plane.clear_deployment_health_failures(deployment.deployment_id)
        self._record_usage(deployment, stream=False, stream_chunk_count=0)
        return response

    def stream_chat_completion(self, request: ChatCompletionRequest) -> Iterator[str]:
        deployment = self._resolve_deployment_for_request(request)
        chunk_count = 0
        try:
            for line in self.inference_client.stream_chat_completion(deployment, request):
                stripped = line.strip()
                if stripped.startswith("data: ") and stripped != "data: [DONE]":
                    payload = loads(stripped[6:])
                    choices = payload.get("choices", [])
                    if choices and choices[0].get("delta", {}).get("content"):
                        chunk_count += 1
                yield line
        except RuntimeError as exc:
            self.control_plane.record_deployment_health_failure(deployment.deployment_id, str(exc))
            raise
        self.control_plane.clear_deployment_health_failures(deployment.deployment_id)
        self._record_usage(deployment, stream=True, stream_chunk_count=chunk_count)

    def _resolve_deployment_for_request(self, request: ChatCompletionRequest) -> DeploymentRecord:
        workload_id = self._resolve_workload_id(request.model)
        candidates = [
            deployment
            for deployment in self.control_plane.list_ready_deployments(workload_id)
            if deployment.endpoint is not None
        ]
        if not candidates:
            raise NoReadyDeploymentError(f"no ready deployment for model={request.model}")

        for deployment in candidates:
            if not self.inference_client.check_deployment_health(deployment):
                self.control_plane.record_deployment_health_failure(
                    deployment.deployment_id,
                    f"deployment endpoint unhealthy: {deployment.endpoint}",
                )
                continue
            return deployment

        raise NoReadyDeploymentError(f"no healthy deployment available for model={request.model}")

    def _record_usage(self, deployment: DeploymentRecord, *, stream: bool, stream_chunk_count: int) -> None:
        self.control_plane.record_usage(
            UsageRecord(
                deployment_id=deployment.deployment_id,
                workload_id=deployment.workload_id,
                hotkey=deployment.hotkey or "unknown",
                request_count=0 if stream else 1,
                streamed_request_count=1 if stream else 0,
                stream_chunk_count=stream_chunk_count,
                compute_seconds=0.25,
                latency_ms_p95=42.0,
                occupancy_seconds=0.25,
            )
        )

    def _resolve_workload_id(self, model: str) -> str:
        workload = self.control_plane.repository.get_workload(model)
        if workload is not None:
            return workload.workload_id
        named = self.control_plane.find_workload_by_name(model)
        if named is not None:
            return named.workload_id
        for workload in self.control_plane.list_workloads():
            if workload.name == model:
                return workload.workload_id
        raise NoReadyDeploymentError(f"unknown model={model}")


service = GatewayService()

from __future__ import annotations

import secrets

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
    UsageRecord,
    WorkloadCreateRequest,
    WorkloadSpec,
)
from greenference_gateway.domain.routing import InferenceRouter, NoReadyDeploymentError
from greenference_gateway.infrastructure.repository import GatewayRepository


class GatewayService:
    def __init__(
        self,
        repository: GatewayRepository | None = None,
        control_plane: ControlPlaneService | None = None,
        builder: BuilderService | None = None,
    ) -> None:
        self.repository = repository or GatewayRepository()
        self.control_plane = control_plane or default_control_plane_service
        self.builder = builder or default_builder_service
        self.router = InferenceRouter()

    def create_api_key(self, request: APIKeyCreateRequest) -> APIKeyRecord:
        api_key = APIKeyRecord(
            name=request.name,
            admin=request.admin,
            scopes=request.scopes,
            secret=f"gk_{secrets.token_urlsafe(24)}",
        )
        self.repository.api_keys[api_key.key_id] = api_key
        return api_key

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
        workload_id = self._resolve_workload_id(request.model)
        deployment = self.control_plane.resolve_ready_deployment(workload_id)
        if deployment is None:
            raise NoReadyDeploymentError(f"no ready deployment for model={request.model}")
        response = self.router.render_chat_response(request, deployment)
        self.control_plane.record_usage(
            UsageRecord(
                deployment_id=deployment.deployment_id,
                workload_id=deployment.workload_id,
                hotkey=deployment.hotkey or "unknown",
                request_count=1,
                compute_seconds=0.25,
                latency_ms_p95=42.0,
                occupancy_seconds=0.25,
            )
        )
        return response

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

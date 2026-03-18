from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from json import loads
from time import perf_counter
from uuid import uuid4
from urllib.parse import urlsplit

from greenference_builder.application.services import BuilderService, service as default_builder_service
from greenference_control_plane.application.services import (
    ControlPlaneService,
    service as default_control_plane_service,
)
from greenference_protocol import (
    APIKeyCreateRequest,
    BuildAttemptRecord,
    APIKeyRecord,
    BuildContextRecord,
    BuildEventRecord,
    BuildJobRecord,
    BuildLogRecord,
    BuildRecord,
    BuildRequest,
    ChatCompletionRequest,
    DeploymentCreateRequest,
    DeploymentRecord,
    InvocationRecord,
    UserRecord,
    UserRegistrationRequest,
    UsageRecord,
    WorkloadCreateRequest,
    WorkloadSpec,
)
from greenference_gateway.domain.routing import NoReadyDeploymentError
from greenference_gateway.infrastructure.inference_client import HttpInferenceClient
from greenference_gateway.infrastructure.repository import GatewayRepository
from greenference_gateway.transport.security import metrics as gateway_metrics


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
        self.metrics = gateway_metrics

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

    def get_build(self, build_id: str) -> BuildRecord | None:
        return self.builder.get_build(build_id)

    def get_build_context(self, build_id: str) -> BuildContextRecord | None:
        return self.builder.get_build_context(build_id)

    def list_build_events(self, build_id: str) -> list[BuildEventRecord]:
        return self.builder.list_build_events(build_id)

    def list_build_logs(self, build_id: str) -> list[BuildLogRecord]:
        return self.builder.list_build_logs(build_id)

    def stream_build_logs(self, build_id: str, *, follow: bool = False):
        return self.builder.stream_build_logs(build_id, follow=follow)

    def get_build_attempt(self, build_id: str, attempt: int) -> BuildAttemptRecord | None:
        return self.builder.get_build_attempt(build_id, attempt)

    def get_build_job(self, build_id: str, attempt: int | None = None) -> BuildJobRecord | None:
        return self.builder.get_build_job(build_id, attempt=attempt)

    def list_build_jobs(self, build_id: str) -> list[BuildJobRecord]:
        return self.builder.list_build_jobs(build_id)

    def cancel_latest_build_job(self, build_id: str) -> BuildRecord:
        return self.builder.cancel_latest_job(build_id)

    def restart_latest_build_job(self, build_id: str) -> BuildRecord:
        return self.builder.restart_latest_job(build_id)

    def recover_build_jobs(self) -> dict[str, object | None]:
        return self.builder.recover_inflight_jobs()

    def build_recovery_status(self) -> dict[str, object | None]:
        return self.builder.recovery_status()

    def build_attempts(self, build_id: str) -> list[dict]:
        return self.builder.build_attempts(build_id)

    def retry_build(self, build_id: str) -> BuildRecord:
        return self.builder.retry_build(build_id)

    def cleanup_build(self, build_id: str) -> BuildRecord:
        return self.builder.cleanup_build(build_id)

    def cancel_build(self, build_id: str) -> BuildRecord:
        return self.builder.cancel_build(build_id)

    def list_image_history(self, image: str) -> list[BuildRecord]:
        return self.builder.list_image_history(image)

    def list_failed_builds(self) -> list[BuildRecord]:
        return [build for build in self.builder.list_builds() if build.status == "failed"]

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

    def list_invocations(self, limit: int | None = None) -> list[InvocationRecord]:
        return self.control_plane.list_invocations(limit=limit)

    def get_invocation(self, invocation_id: str) -> InvocationRecord | None:
        return self.control_plane.get_invocation(invocation_id)

    def export_recent_invocations(self, limit: int = 50) -> dict:
        return self.control_plane.export_recent_invocations(limit=limit)

    def list_routing_decisions(self, limit: int = 50) -> list[dict]:
        return self.repository.list_routing_decisions(limit=limit)

    def invoke_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key_id: str | None = None,
        routed_host: str | None = None,
    ):
        request_id = str(uuid4())
        started = perf_counter()
        deployment = self._resolve_deployment_for_request(request, routed_host=routed_host)
        try:
            response = self.inference_client.invoke_chat_completion(
                deployment,
                request,
                request_id=request_id,
            )
        except RuntimeError as exc:
            self.control_plane.record_deployment_health_failure(deployment.deployment_id, str(exc))
            self._record_invocation(
                deployment,
                request,
                request_id=request_id,
                api_key_id=api_key_id,
                stream=False,
                started=started,
                status="failed",
                error_class=exc.__class__.__name__,
            )
            raise
        self.control_plane.clear_deployment_health_failures(deployment.deployment_id)
        self._record_usage(deployment, stream=False, stream_chunk_count=0)
        self.metrics.observe("invoke.latency_ms", (perf_counter() - started) * 1000.0)
        response.id = request_id
        self._record_invocation(
            deployment,
            request,
            request_id=request_id,
            api_key_id=api_key_id,
            stream=False,
            started=started,
            status="succeeded",
        )
        return response

    def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key_id: str | None = None,
        routed_host: str | None = None,
    ) -> Iterator[str]:
        request_id = str(uuid4())
        started = perf_counter()
        deployment = self._resolve_deployment_for_request(request, routed_host=routed_host)
        chunk_count = 0
        try:
            for line in self.inference_client.stream_chat_completion(
                deployment,
                request,
                request_id=request_id,
            ):
                stripped = line.strip()
                if stripped.startswith("data: ") and stripped != "data: [DONE]":
                    payload = loads(stripped[6:])
                    choices = payload.get("choices", [])
                    if choices and choices[0].get("delta", {}).get("content"):
                        chunk_count += 1
                yield line
        except RuntimeError as exc:
            self.control_plane.record_deployment_health_failure(deployment.deployment_id, str(exc))
            self._record_invocation(
                deployment,
                request,
                request_id=request_id,
                api_key_id=api_key_id,
                stream=True,
                started=started,
                status="failed",
                error_class=exc.__class__.__name__,
            )
            raise
        self.control_plane.clear_deployment_health_failures(deployment.deployment_id)
        self._record_usage(deployment, stream=True, stream_chunk_count=chunk_count)
        self.metrics.observe("invoke.latency_ms", (perf_counter() - started) * 1000.0)
        self._record_invocation(
            deployment,
            request,
            request_id=request_id,
            api_key_id=api_key_id,
            stream=True,
            started=started,
            status="succeeded",
        )

    def resolve_workload_reference(self, model: str, routed_host: str | None = None) -> tuple[WorkloadSpec, dict]:
        normalized_host = self._normalize_host(routed_host)
        if normalized_host:
            hosted = self.control_plane.find_workload_by_ingress_host(normalized_host)
            if hosted is not None:
                return hosted, {
                    "matched_by": "ingress_host",
                    "host": normalized_host,
                    "model": model,
                }

        workload = self.control_plane.repository.get_workload(model)
        if workload is not None:
            return workload, {"matched_by": "workload_id", "host": normalized_host, "model": model}

        aliased = self.control_plane.find_workload_by_alias(model)
        if aliased is not None:
            return aliased, {"matched_by": "workload_alias", "host": normalized_host, "model": model}

        named = self.control_plane.find_workload_by_name(model)
        if named is not None:
            return named, {"matched_by": "name", "host": normalized_host, "model": model}

        for workload_item in self.control_plane.list_workloads():
            if workload_item.name == model:
                return workload_item, {"matched_by": "name_scan", "host": normalized_host, "model": model}
        raise NoReadyDeploymentError(f"unknown model={model}")

    def _resolve_deployment_for_request(
        self,
        request: ChatCompletionRequest,
        routed_host: str | None = None,
    ) -> DeploymentRecord:
        workload, routing = self.resolve_workload_reference(request.model, routed_host=routed_host)
        workload_id = workload.workload_id
        candidates = [
            deployment
            for deployment in self.control_plane.list_ready_deployments(workload_id)
            if deployment.endpoint is not None
        ]
        if not candidates:
            self.repository.record_routing_decision(
                {
                    **routing,
                    "workload_id": workload_id,
                    "decision": "rejected",
                    "reason": "no_ready_deployment",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            raise NoReadyDeploymentError(f"no ready deployment for model={request.model}")

        for deployment in candidates:
            if not self.inference_client.check_deployment_health(deployment):
                self.control_plane.record_deployment_health_failure(
                    deployment.deployment_id,
                    f"deployment endpoint unhealthy: {deployment.endpoint}",
                )
                self.repository.record_routing_decision(
                    {
                        **routing,
                        "workload_id": workload_id,
                        "deployment_id": deployment.deployment_id,
                        "decision": "skipped",
                        "reason": "endpoint_unhealthy",
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
                continue
            self.repository.record_routing_decision(
                {
                    **routing,
                    "workload_id": workload_id,
                    "deployment_id": deployment.deployment_id,
                    "decision": "selected",
                    "reason": "ready_and_healthy",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            return deployment

        self.repository.record_routing_decision(
            {
                **routing,
                "workload_id": workload_id,
                "decision": "rejected",
                "reason": "no_healthy_deployment",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
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

    def _record_invocation(
        self,
        deployment: DeploymentRecord,
        request: ChatCompletionRequest,
        *,
        request_id: str,
        api_key_id: str | None,
        stream: bool,
        started: float,
        status: str,
        error_class: str | None = None,
    ) -> None:
        self.control_plane.record_invocation(
            InvocationRecord(
                request_id=request_id,
                deployment_id=deployment.deployment_id,
                workload_id=deployment.workload_id,
                hotkey=deployment.hotkey or "unknown",
                model=request.model,
                api_key_id=api_key_id,
                stream=stream,
                status=status,
                error_class=error_class,
                latency_ms=(perf_counter() - started) * 1000.0,
                message_count=len(request.messages),
                created_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def _normalize_host(host: str | None) -> str | None:
        if not isinstance(host, str):
            return None
        stripped = host.strip().lower()
        if not stripped:
            return None
        parsed = urlsplit(f"//{stripped}")
        return parsed.hostname or stripped


service = GatewayService()

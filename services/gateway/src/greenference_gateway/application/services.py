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
    BuildJobCheckpointRecord,
    BuildJobRecord,
    BuildLogRecord,
    BuildRecord,
    BuildRequest,
    ChatCompletionRequest,
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentUpdateRequest,
    InvocationRecord,
    UserRecord,
    UserProfileUpdateRequest,
    UserSecretCreateRequest,
    UserSecretRecord,
    UserRegistrationRequest,
    UsageRecord,
    WorkloadCreateRequest,
    WorkloadShareCreateRequest,
    WorkloadShareRecord,
    WorkloadSpec,
    WorkloadUpdateRequest,
)
from greenference_gateway.domain.routing import NoReadyDeploymentError
from greenference_gateway.infrastructure.inference_client import (
    HttpInferenceClient,
    InferenceBadResponseError,
    InferenceConnectionError,
    InferenceTimeoutError,
)
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

    def get_user(self, user_id: str) -> UserRecord | None:
        return self.repository.get_user(user_id)

    def update_user_profile(self, user_id: str, request: UserProfileUpdateRequest) -> UserRecord:
        user = self.repository.get_user(user_id)
        if user is None:
            raise KeyError(f"user not found: {user_id}")
        if request.email is not None:
            user.email = request.email
        if request.display_name is not None:
            user.display_name = request.display_name
        if request.bio is not None:
            user.bio = request.bio
        if request.website is not None:
            user.website = request.website
        user.metadata = request.metadata
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

    def start_build(self, request: BuildRequest, owner_user_id: str | None = None) -> BuildRecord:
        return self.builder.start_build(request, owner_user_id=owner_user_id)

    def list_builds(self, user_id: str | None = None, *, admin: bool = False) -> list[BuildRecord]:
        builds = self.builder.list_builds()
        if admin:
            return builds
        return [build for build in builds if build.public or build.owner_user_id == user_id]

    def get_build(self, build_id: str, user_id: str | None = None, *, admin: bool = False) -> BuildRecord | None:
        build = self.builder.get_build(build_id)
        if build is None:
            return None
        if admin or build.public or build.owner_user_id == user_id:
            return build
        return None

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

    def latest_build_job_timeline(self, build_id: str) -> list[BuildJobCheckpointRecord]:
        return self.builder.latest_build_job_timeline(build_id)

    def cancel_latest_build_job(self, build_id: str) -> BuildRecord:
        return self.builder.cancel_latest_job(build_id)

    def restart_latest_build_job(self, build_id: str) -> BuildRecord:
        return self.builder.restart_latest_job(build_id)

    def recover_build_jobs(self) -> dict[str, object | None]:
        return self.builder.recover_inflight_jobs()

    def build_recovery_status(self) -> dict[str, object | None]:
        return self.builder.recovery_status()

    def build_recovery_summary(self, build_id: str) -> dict[str, object]:
        build = self.builder.get_build(build_id)
        if build is None:
            raise KeyError(f"build not found: {build_id}")
        return self.builder.latest_build_job_recovery_summary(build_id)

    def build_attempts(self, build_id: str) -> list[dict]:
        return self.builder.build_attempts(build_id)

    def retry_build(self, build_id: str) -> BuildRecord:
        return self.builder.retry_build(build_id)

    def cleanup_build(self, build_id: str) -> BuildRecord:
        return self.builder.cleanup_build(build_id)

    def cancel_build(self, build_id: str) -> BuildRecord:
        return self.builder.cancel_build(build_id)

    def list_image_history(self, image: str, user_id: str | None = None, *, admin: bool = False) -> list[BuildRecord]:
        builds = self.builder.list_image_history(image)
        if admin:
            return builds
        return [build for build in builds if build.public or build.owner_user_id == user_id]

    def list_failed_builds(self) -> list[BuildRecord]:
        return [build for build in self.builder.list_builds() if build.status == "failed"]

    def create_workload(self, request: WorkloadCreateRequest, owner_user_id: str | None = None) -> WorkloadSpec:
        workload = WorkloadSpec(**request.model_dump(), owner_user_id=owner_user_id)
        return self.control_plane.upsert_workload(workload)

    def get_workload(self, workload_id: str, user_id: str | None = None, *, admin: bool = False) -> WorkloadSpec | None:
        workload = self.control_plane.repository.get_workload(workload_id)
        if workload is None:
            return None
        if admin or self._user_can_access_workload(workload, user_id):
            return workload
        return None

    def update_workload(
        self,
        workload_id: str,
        request: WorkloadUpdateRequest,
        *,
        actor_user_id: str | None,
        admin: bool = False,
    ) -> WorkloadSpec:
        workload = self.control_plane.repository.get_workload(workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {workload_id}")
        if not admin and workload.owner_user_id != actor_user_id:
            raise PermissionError(f"workload update denied: {workload_id}")
        if request.display_name is not None:
            workload.display_name = request.display_name
        if request.readme is not None:
            workload.readme = request.readme
        if request.logo_uri is not None:
            workload.logo_uri = request.logo_uri
        if request.tags is not None:
            workload.tags = request.tags
        if request.workload_alias is not None:
            workload.workload_alias = request.workload_alias
        if request.ingress_host is not None:
            workload.ingress_host = request.ingress_host
        if request.pricing_class is not None:
            workload.pricing_class = request.pricing_class
        if request.public is not None:
            workload.public = request.public
        if request.lifecycle is not None:
            workload.lifecycle = request.lifecycle
        return self.control_plane.upsert_workload(workload)

    def list_workloads(self, user_id: str | None = None, *, admin: bool = False) -> list[WorkloadSpec]:
        workloads = self.control_plane.list_workloads()
        if admin:
            return workloads
        shared_ids = set()
        if user_id is not None:
            shared_ids = {share.workload_id for share in self.repository.list_shared_workloads_for_user(user_id)}
        return [
            workload
            for workload in workloads
            if workload.public or workload.owner_user_id == user_id or workload.workload_id in shared_ids
        ]

    def create_deployment(
        self,
        request: DeploymentCreateRequest | dict,
        *,
        user_id: str | None = None,
        admin: bool = False,
    ) -> DeploymentRecord:
        payload = request if isinstance(request, DeploymentCreateRequest) else DeploymentCreateRequest(**request)
        workload = self.control_plane.repository.get_workload(payload.workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {payload.workload_id}")
        if not admin and not self._user_can_access_workload(workload, user_id):
            raise PermissionError(f"workload access denied: {payload.workload_id}")
        return self.control_plane.create_deployment(
            {
                "workload_id": payload.workload_id,
                "requested_instances": payload.requested_instances,
                "accept_fee": payload.accept_fee,
                "owner_user_id": user_id,
            }
        )

    def list_deployments(self, user_id: str | None = None, *, admin: bool = False) -> list[DeploymentRecord]:
        deployments = self.control_plane.list_deployments()
        if admin:
            return deployments
        return [
            deployment
            for deployment in deployments
            if deployment.owner_user_id == user_id or (deployment.owner_user_id is None and user_id is None)
        ]

    def get_deployment(self, deployment_id: str, user_id: str | None = None, *, admin: bool = False) -> DeploymentRecord | None:
        deployment = self.control_plane.repository.get_deployment(deployment_id)
        if deployment is None:
            return None
        if admin or deployment.owner_user_id == user_id or (deployment.owner_user_id is None and user_id is None):
            return deployment
        return None

    def update_deployment(
        self,
        deployment_id: str,
        request: DeploymentUpdateRequest,
        *,
        actor_user_id: str | None,
        admin: bool = False,
    ) -> DeploymentRecord:
        deployment = self.control_plane.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        if not admin and deployment.owner_user_id != actor_user_id:
            raise PermissionError(f"deployment update denied: {deployment_id}")
        return self.control_plane.update_deployment(deployment_id, request)

    def create_secret(self, user_id: str, request: UserSecretCreateRequest) -> UserSecretRecord:
        secret = UserSecretRecord(user_id=user_id, name=request.name, value=request.value)
        return self.repository.save_secret(secret)

    def list_secrets(self, user_id: str) -> list[UserSecretRecord]:
        return self.repository.list_secrets(user_id)

    def delete_secret(self, secret_id: str, *, user_id: str | None, admin: bool = False) -> UserSecretRecord:
        secret = self.repository.get_secret(secret_id)
        if secret is None:
            raise KeyError(f"secret not found: {secret_id}")
        if not admin and secret.user_id != user_id:
            raise PermissionError(f"secret access denied: {secret_id}")
        deleted = self.repository.delete_secret(secret_id)
        if deleted is None:
            raise KeyError(f"secret not found: {secret_id}")
        return deleted

    def share_workload(
        self,
        workload_id: str,
        request: WorkloadShareCreateRequest,
        *,
        actor_user_id: str | None,
        admin: bool = False,
    ) -> WorkloadShareRecord:
        workload = self.control_plane.repository.get_workload(workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {workload_id}")
        if not admin and workload.owner_user_id != actor_user_id:
            raise PermissionError(f"workload share denied: {workload_id}")
        if self.repository.get_user(request.shared_with_user_id) is None:
            raise KeyError(f"user not found: {request.shared_with_user_id}")
        share = WorkloadShareRecord(
            workload_id=workload_id,
            owner_user_id=workload.owner_user_id or "",
            shared_with_user_id=request.shared_with_user_id,
            permission=request.permission,
        )
        return self.repository.save_workload_share(share)

    def list_workload_shares(
        self,
        workload_id: str,
        *,
        actor_user_id: str | None,
        admin: bool = False,
    ) -> list[WorkloadShareRecord]:
        workload = self.control_plane.repository.get_workload(workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {workload_id}")
        if not admin and workload.owner_user_id != actor_user_id:
            raise PermissionError(f"workload share denied: {workload_id}")
        return self.repository.list_workload_shares(workload_id)

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
        deployment, routing = self._select_deployment_for_request(request, routed_host=routed_host)
        try:
            response = self._invoke_upstream_response(deployment, request, request_id=request_id)
        except RuntimeError as exc:
            self._handle_upstream_failure(deployment, routing, exc)
            self._record_invocation(
                deployment,
                request,
                routing=routing,
                request_id=request_id,
                api_key_id=api_key_id,
                stream=False,
                started=started,
                status="failed",
                error_class=self._classify_inference_error(exc),
            )
            raise
        self._handle_upstream_success(deployment, routing)
        self._record_usage(deployment, stream=False, stream_chunk_count=0)
        latency_ms = (perf_counter() - started) * 1000.0
        self.metrics.observe("invoke.latency_ms", latency_ms)
        response.id = request_id
        self._record_invocation(
            deployment,
            request,
            routing=routing,
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
        deployment, routing = self._select_deployment_for_request(request, routed_host=routed_host)
        chunk_count = 0
        try:
            for line in self._invoke_upstream_stream(deployment, request, request_id=request_id):
                stripped = line.strip()
                if stripped.startswith("data: ") and stripped != "data: [DONE]":
                    payload = loads(stripped[6:])
                    choices = payload.get("choices", [])
                    if choices and choices[0].get("delta", {}).get("content"):
                        chunk_count += 1
                yield line
        except RuntimeError as exc:
            self._handle_upstream_failure(deployment, routing, exc)
            self._record_invocation(
                deployment,
                request,
                routing=routing,
                request_id=request_id,
                api_key_id=api_key_id,
                stream=True,
                started=started,
                status="failed",
                error_class=self._classify_inference_error(exc),
            )
            raise
        self._handle_upstream_success(deployment, routing)
        self._record_usage(deployment, stream=True, stream_chunk_count=chunk_count)
        self.metrics.observe("invoke.latency_ms", (perf_counter() - started) * 1000.0)
        self._record_invocation(
            deployment,
            request,
            routing=routing,
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

    def _select_deployment_for_request(
        self,
        request: ChatCompletionRequest,
        routed_host: str | None = None,
    ) -> tuple[DeploymentRecord, dict]:
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
            return deployment, {
                **routing,
                "workload_id": workload_id,
                "selected_deployment_id": deployment.deployment_id,
                "selected_hotkey": deployment.hotkey,
                "routing_reason": "ready_and_healthy",
            }

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

    def _invoke_upstream_response(
        self,
        deployment: DeploymentRecord,
        request: ChatCompletionRequest,
        *,
        request_id: str,
    ):
        return self.inference_client.invoke_chat_completion(deployment, request, request_id=request_id)

    def _invoke_upstream_stream(
        self,
        deployment: DeploymentRecord,
        request: ChatCompletionRequest,
        *,
        request_id: str,
    ) -> Iterator[str]:
        return self.inference_client.stream_chat_completion(deployment, request, request_id=request_id)

    def _handle_upstream_success(self, deployment: DeploymentRecord, routing: dict) -> None:
        self.control_plane.clear_deployment_health_failures(deployment.deployment_id)
        self.repository.record_routing_decision(
            {
                **routing,
                "deployment_id": deployment.deployment_id,
                "decision": "completed",
                "reason": "upstream_succeeded",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    def _handle_upstream_failure(self, deployment: DeploymentRecord, routing: dict, exc: RuntimeError) -> None:
        failure_class = self._classify_inference_error(exc)
        self.control_plane.record_deployment_health_failure(deployment.deployment_id, str(exc))
        self.repository.record_routing_decision(
            {
                **routing,
                "deployment_id": deployment.deployment_id,
                "decision": "failed",
                "reason": failure_class,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    @staticmethod
    def _classify_inference_error(exc: RuntimeError) -> str:
        if isinstance(exc, InferenceTimeoutError):
            return "upstream_timeout"
        if isinstance(exc, InferenceConnectionError):
            return "upstream_connection_failure"
        if isinstance(exc, InferenceBadResponseError):
            return "upstream_bad_response"
        return "upstream_failure"

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
        routing: dict,
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
                routed_host=routing.get("host"),
                resolution_basis=routing.get("matched_by"),
                routing_reason=routing.get("routing_reason"),
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

    def _user_can_access_workload(self, workload: WorkloadSpec, user_id: str | None) -> bool:
        if workload.public:
            return True
        if workload.owner_user_id is None and user_id is None:
            return True
        if user_id is not None and workload.owner_user_id == user_id:
            return True
        if user_id is None:
            return False
        shares = self.repository.list_workload_shares(workload.workload_id)
        return any(share.shared_with_user_id == user_id for share in shares)


service = GatewayService()

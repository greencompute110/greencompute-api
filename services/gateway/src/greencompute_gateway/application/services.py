from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from json import loads
from time import perf_counter
from uuid import uuid4
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

from greencompute_builder.application.services import BuilderService, service as default_builder_service
from greencompute_control_plane.application.services import (
    ControlPlaneService,
    service as default_control_plane_service,
)
from greencompute_protocol import (
    APIKeyCreateRequest,
    BuildAttemptRecord,
    APIKeyRecord,
    DeploymentState,
    BuildContextRecord,
    BuildContextUploadRecord,
    BuildContextUploadRequest,
    BuildEventRecord,
    BuildJobCheckpointRecord,
    BuildJobRecord,
    BuildLogRecord,
    BuildRecord,
    BuildRequest,
    BareMetalInquiryCreateRequest,
    BareMetalInquiryRecord,
    ChatCompletionRequest,
    CommercialInquiryCreateRequest,
    CommercialInquiryRecord,
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentUpdateRequest,
    GpuCapacityOverride,
    InvocationRecord,
    ProviderServerCreateRequest,
    ProviderServerRecord,
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
from greencompute_gateway.domain.routing import NoReadyDeploymentError
from greencompute_gateway.infrastructure.inference_client import (
    HttpInferenceClient,
    InferenceBadResponseError,
    InferenceConnectionError,
    InferenceTimeoutError,
)
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport.security import metrics as gateway_metrics


class InsufficientBalanceForRentalError(Exception):
    """Raised by GatewayService.create_deployment when the caller doesn't
    have enough credits to cover ≥ 1 hour of the requested workload at the
    highest possible GPU rate. The route layer catches this and returns a
    402 Payment Required with the details so the UI can prompt a top-up.
    """

    def __init__(
        self,
        *,
        required_cents: int,
        current_cents: int,
        rate_cents_per_hour: int,
        gpu_count: int,
        requested_instances: int,
    ) -> None:
        self.required_cents = required_cents
        self.current_cents = current_cents
        self.rate_cents_per_hour = rate_cents_per_hour
        self.gpu_count = gpu_count
        self.requested_instances = requested_instances
        super().__init__(
            f"insufficient balance: need {required_cents}¢, have {current_cents}¢ "
            f"(rate {rate_cents_per_hour}¢/GPU/hr × {gpu_count} GPU × {requested_instances} inst × 1 hr)"
        )


class InsufficientBalanceForInferenceError(Exception):
    """Raised when a user attempts a chat completion with zero balance."""

    def __init__(self, *, current_cents: int) -> None:
        self.current_cents = current_cents
        super().__init__(
            f"insufficient balance: inference requires a positive credit balance "
            f"(current: {current_cents}¢)"
        )


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
        self._round_robin_counter = 0
        # Per-deployment EMA of recent chat-completion latency (ms). Populated
        # on every successful invoke; `_select_healthy_deployments` sorts by
        # it so consistently faster miners get preferred. Missing entries
        # default to 0 → new deployments rank first, which we want (it lets
        # the scheduler learn their latency quickly).
        self._deployment_latency_ema: dict[str, float] = {}

    def register_user(self, request: UserRegistrationRequest) -> UserRecord:
        # Idempotent: if a user with this email already exists, return it.
        # The UI's provisionGreenfKey flow retries registration on every new
        # OAuth signup, and email collisions would otherwise 500 out — which
        # breaks the sign-in flow via the JWT callback.
        if request.email:
            existing = self.repository.get_user_by_email(request.email)
            if existing is not None:
                return existing
        # `username` carries a UNIQUE index but the UI sends the user's DISPLAY
        # NAME, which is not unique — two people called "Mark" collided and the
        # INSERT 500'd, leaving the user with no platform account + a null
        # greenfApiKey (which then black-screened them on the client). Resolve
        # to a free username before inserting so a common name never blocks
        # signup.
        username = self._unique_username(request.username, request.email)
        user = UserRecord(username=username, email=request.email)
        return self.repository.save_user(user)

    def _unique_username(self, desired: str | None, email: str | None) -> str:
        """Pick a username not already taken (ix_users_username is UNIQUE).
        Preference order: the display name as given → the email local-part
        (usually unique + meaningful) → numbered variants → a uuid suffix."""
        base = (desired or "").strip()
        local = email.split("@")[0].strip() if email else ""
        candidates: list[str] = []
        if base:
            candidates.append(base)
        if local and local != base:
            candidates.append(local)
        seed = base or local or "user"
        candidates.extend(f"{seed}{i}" for i in range(2, 100))
        for candidate in candidates:
            if self.repository.get_user_by_username(candidate) is None:
                return candidate
        return f"{seed}-{uuid4().hex[:8]}"

    def get_user(self, user_id: str) -> UserRecord | None:
        return self.repository.get_user(user_id)

    def list_users(self) -> list[UserRecord]:
        return self.repository.list_users()

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

    def create_api_key(
        self,
        request: APIKeyCreateRequest,
        *,
        caller_user_id: str | None = None,
        caller_admin: bool | None = None,
        allow_user_id_override: bool | None = None,
        allow_admin_grant: bool = False,
    ) -> APIKeyRecord:
        """Mint an api key, deriving privilege from the authenticated caller
        rather than trusting the wire.

        - ``allow_user_id_override``: when True the client-supplied
          ``user_id``/``scopes`` are honored (master admin AND the provisioning
          principal both set this — the UI provisions a key for an explicit
          user_id). When False the key is bound to ``caller_user_id``
          (self-service) and client scopes are dropped.
        - ``allow_admin_grant``: only when True may the new key be
          ``admin=True`` (master admin only — the provision secret must NOT be
          able to mint admin keys).

        Caller tiers map to flags like this:
          - env master admin:  override=True,  admin_grant=True
          - provision secret:   override=True,  admin_grant=False
          - self-service user:  override=False, admin_grant=False

        Backward-compatibility: when neither ``caller_admin`` nor
        ``allow_user_id_override`` is supplied (the legacy in-process call
        shape, e.g. integration fixtures that already vetted the request) the
        request fields are honored verbatim, matching the original behavior.
        All HTTP entrypoints pass these explicitly so the wire is never trusted
        from a non-admin caller.
        """
        # Derive override from the legacy caller_admin flag when not given, so
        # existing call sites keep working.
        if allow_user_id_override is None:
            allow_user_id_override = caller_admin

        if allow_user_id_override is None:
            # Legacy trusted in-process caller — honor the request as-is.
            user_id = request.user_id
            scopes = request.scopes
            admin = request.admin
        elif allow_user_id_override:
            # Master admin or provisioning principal — explicit user_id allowed.
            user_id = request.user_id
            scopes = request.scopes
            admin = bool(request.admin) and allow_admin_grant
        else:
            # Self-service: bind to the authenticated principal, drop scopes,
            # never admin — even if the body asked for admin=true / another user.
            user_id = caller_user_id
            scopes = []
            admin = False
        api_key = APIKeyRecord(
            name=request.name,
            user_id=user_id,
            admin=admin,
            scopes=scopes,
            secret=f"gk_{secrets.token_urlsafe(24)}",
        )
        return self.repository.save_api_key(api_key)

    def list_api_keys(self, user_id: str | None = None, *, admin: bool = False) -> list[APIKeyRecord]:
        keys = self.repository.list_api_keys(user_id=None if admin else user_id)
        if admin:
            return keys
        return [k for k in keys if k.user_id == user_id]

    def get_api_key(self, key_id: str, user_id: str | None = None, *, admin: bool = False) -> APIKeyRecord | None:
        key = self.repository.get_api_key(key_id)
        if key is None:
            return None
        if admin or key.user_id == user_id:
            return key
        return None

    def delete_api_key(self, key_id: str, *, user_id: str | None, admin: bool = False) -> APIKeyRecord:
        key = self.repository.get_api_key(key_id)
        if key is None:
            raise KeyError(f"api key not found: {key_id}")
        if not admin and key.user_id != user_id:
            raise PermissionError(f"api key delete denied: {key_id}")
        deleted = self.repository.delete_api_key(key_id)
        if deleted is None:
            raise KeyError(f"api key not found: {key_id}")
        return deleted

    def start_build(self, request: BuildRequest, owner_user_id: str | None = None) -> BuildRecord:
        return self.builder.start_build(request, owner_user_id=owner_user_id)

    def upload_build_context(self, request: BuildContextUploadRequest) -> BuildContextUploadRecord:
        return self.builder.upload_build_context(request)

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
        if request.clear_workload_alias:
            workload.workload_alias = None
        elif request.workload_alias is not None:
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

        # Pre-flight balance check: require enough credits for at least 1 hour
        # of runtime at the highest possible rate among the user's requested
        # GPU models. Using MAX (worst case) guarantees the user has enough
        # balance regardless of which GPU they actually get scheduled on.
        # Admin deployments bypass the check.
        if not admin and user_id is not None:
            from greencompute_protocol import rate_for_gpu
            from greencompute_gateway.infrastructure.billing_repository import BillingRepository

            supported = workload.requirements.supported_gpu_models or []
            if supported:
                max_rate_cents_per_hour = max(
                    (rate_for_gpu(m) for m in supported), default=10
                )
            else:
                # No GPU constraint set — be conservative and use the top rate
                # in our table so users need balance for the priciest option.
                from greencompute_protocol import GPU_RATE_CENTS_PER_HOUR, LEGACY_FALLBACK_CENTS_PER_HOUR
                max_rate_cents_per_hour = max(
                    list(GPU_RATE_CENTS_PER_HOUR.values()) + [LEGACY_FALLBACK_CENTS_PER_HOUR]
                )

            gpu_count = workload.requirements.gpu_count or 1
            # The gate mirrors what metering will actually charge: one
            # placed instance (the scheduler never scales out
            # requested_instances — see meter_usage's billed_instances).
            # Gating on requested_instances would 402 users for balance
            # they'll never be billed.
            required_cents = max_rate_cents_per_hour * gpu_count
            current_cents = BillingRepository().get_balance(user_id)
            if current_cents < required_cents:
                # Raised as ValueError so the route layer can catch and return
                # 402 with the detail payload — see create_deployment route.
                raise InsufficientBalanceForRentalError(
                    required_cents=required_cents,
                    current_cents=current_cents,
                    rate_cents_per_hour=max_rate_cents_per_hour,
                    gpu_count=gpu_count,
                    requested_instances=1,
                )

        return self.control_plane.create_deployment(
            {
                "workload_id": payload.workload_id,
                "requested_instances": payload.requested_instances,
                "accept_fee": payload.accept_fee,
                "save_on_exhaustion": payload.save_on_exhaustion,
                "owner_user_id": user_id,
            }
        )

    def resume_deployment(
        self,
        deployment_id: str,
        *,
        user_id: str | None = None,
        admin: bool = False,
    ) -> DeploymentRecord:
        """Pod-safe IN-PLACE resume of a SUSPENDED deployment — restarts the
        SAME container so the user's preserved work comes back (no destroy &
        recreate, no lost disk).

        Flow:
          1. Load the suspended deployment; ownership check; must be SUSPENDED.
          2. Credit gate (skipped for admin): require >= 1 hour of runtime at
             the deployment's LOCKED rate. Any accrued storage hold already
             shows as a NEGATIVE balance, so a top-up nets against it
             automatically — we just require the resulting balance to cover an
             hour before the meter restarts.
          3. Hand to the control-plane's in-place resume: re-acquire the GPU
             hold on the original (hotkey, node) and `docker start` the SAME
             container. It raises ValueError (→ 409) if the original node is
             full or its placement is gone — surfaced rather than silently
             rebuilding, which would lose the saved work.
        """
        old = self.control_plane.repository.get_deployment(deployment_id)
        if old is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        if not admin and old.owner_user_id != user_id:
            raise PermissionError(f"deployment access denied: {deployment_id}")
        if old.state != DeploymentState.SUSPENDED:
            raise ValueError(
                f"deployment {deployment_id} is not suspended "
                f"(current state: {old.state.value})"
            )

        if not admin and user_id is not None:
            workload = self.control_plane.repository.get_workload(old.workload_id)
            gpu_count = (workload.requirements.gpu_count if workload else 1) or 1
            # One hour at the billed quantity — a single placed instance
            # (matches meter_usage's billed_instances, not requested_instances).
            required_cents = old.hourly_rate_cents * gpu_count
            from greencompute_gateway.infrastructure.billing_repository import BillingRepository

            current_cents = BillingRepository().get_balance(user_id)
            if current_cents < required_cents:
                # Raised as InsufficientBalanceForRentalError so the route
                # returns 402 with the settle/top-up detail.
                raise InsufficientBalanceForRentalError(
                    required_cents=required_cents,
                    current_cents=current_cents,
                    rate_cents_per_hour=old.hourly_rate_cents,
                    gpu_count=gpu_count,
                    requested_instances=1,
                )

        # In-place restart of the SAME container — preserves the saved work.
        return self.control_plane.resume_deployment(deployment_id)

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

    def terminate_deployment(
        self,
        deployment_id: str,
        *,
        actor_user_id: str | None,
        admin: bool = False,
    ) -> DeploymentRecord:
        deployment = self.control_plane.repository.get_deployment(deployment_id)
        if deployment is None:
            raise KeyError(f"deployment not found: {deployment_id}")
        if not admin and deployment.owner_user_id != actor_user_id:
            raise PermissionError(f"deployment terminate denied: {deployment_id}")
        return self.control_plane.cleanup_deployment(deployment_id, reason="user terminated")

    # --- Commercial inquiries (public /contact-sales) ----------------------

    def submit_commercial_inquiry(
        self,
        request: CommercialInquiryCreateRequest,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> CommercialInquiryRecord:
        """Public — anyone can POST. Best-effort spam filter + webhook ping.

        - Honeypot: bots fill `website`; real users leave it blank. Silent
          accept-then-drop so spammers don't probe the rule.
        - Rate limit: max 5 inquiries from the same IP per hour.
        - Webhook: fire-and-forget Slack/Discord ping with the lead summary.
        """
        if request.website:
            # Honeypot tripped. Return a fake-success record so the bot
            # doesn't learn it failed.
            return CommercialInquiryRecord(email=request.email or "honeypot@spam")
        if source_ip:
            recent = self.repository.count_commercial_inquiries_from_ip_since(
                source_ip, since_seconds=3600
            )
            if recent >= 5:
                raise PermissionError("rate limit exceeded for commercial inquiries")
        inquiry = CommercialInquiryRecord(
            name=request.name,
            email=request.email,
            company=request.company,
            gpu_count=request.gpu_count,
            duration=request.duration,
            deployment_date=request.deployment_date,
            budget=request.budget,
            use_case=request.use_case,
            discord=request.discord,
            phone=request.phone,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        saved = self.repository.save_commercial_inquiry(inquiry)
        try:
            from greencompute_gateway.infrastructure.notifications import (
                notify_commercial_inquiry,
            )

            notify_commercial_inquiry(saved)
        except Exception:
            # Notification is best-effort; never let it block the lead.
            log.exception("sales webhook dispatch failed for %s", saved.inquiry_id)
        return saved

    def list_commercial_inquiries(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[CommercialInquiryRecord]:
        return self.repository.list_commercial_inquiries(
            status=status, limit=limit, offset=offset
        )

    def update_commercial_inquiry_status(
        self, inquiry_id: str, *, status: str | None = None, notes: str | None = None
    ) -> CommercialInquiryRecord:
        updated = self.repository.update_commercial_inquiry_status(
            inquiry_id, status=status, notes=notes
        )
        if updated is None:
            raise KeyError(f"inquiry not found: {inquiry_id}")
        return updated

    # --- Bare-metal inquiries (dedicated /rental sales form) ---------------

    def submit_bare_metal_inquiry(
        self,
        request: BareMetalInquiryCreateRequest,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> BareMetalInquiryRecord:
        """Public — same hardening as the commercial form (honeypot + IP rate
        limit). Persists, then fires a webhook with a [Bare-metal] prefix."""
        if request.website:
            return BareMetalInquiryRecord(email=request.email or "honeypot@spam")
        if source_ip:
            recent = self.repository.count_bare_metal_inquiries_from_ip_since(
                source_ip, since_seconds=3600
            )
            if recent >= 5:
                raise PermissionError("rate limit exceeded for bare-metal inquiries")
        inquiry = BareMetalInquiryRecord(
            name=request.name,
            email=request.email,
            company=request.company,
            card_type=request.card_type,
            node_count=request.node_count,
            required_vram_gb=request.required_vram_gb,
            storage_gb_per_node=request.storage_gb_per_node,
            work_type=request.work_type,
            deployment_date=request.deployment_date,
            duration=request.duration,
            notes=request.notes,
            discord=request.discord,
            phone=request.phone,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        saved = self.repository.save_bare_metal_inquiry(inquiry)
        try:
            from greencompute_gateway.infrastructure.notifications import (
                notify_bare_metal_inquiry,
            )

            notify_bare_metal_inquiry(saved)
        except Exception:
            log.exception("bare-metal webhook dispatch failed for %s", saved.inquiry_id)
        return saved

    def list_bare_metal_inquiries(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[BareMetalInquiryRecord]:
        return self.repository.list_bare_metal_inquiries(
            status=status, limit=limit, offset=offset
        )

    def update_bare_metal_inquiry_status(
        self, inquiry_id: str, *, status: str | None = None, review_notes: str | None = None
    ) -> BareMetalInquiryRecord:
        updated = self.repository.update_bare_metal_inquiry_status(
            inquiry_id, status=status, review_notes=review_notes
        )
        if updated is None:
            raise KeyError(f"bare-metal inquiry not found: {inquiry_id}")
        return updated

    # --- GPU capacity overrides (admin-controlled cluster size) ------------

    def list_gpu_capacity_overrides(self) -> list[GpuCapacityOverride]:
        return self.repository.list_gpu_capacity_overrides()

    def upsert_gpu_capacity_override(
        self, override: GpuCapacityOverride
    ) -> GpuCapacityOverride:
        return self.repository.upsert_gpu_capacity_override(override)

    def delete_gpu_capacity_override(self, gpu_model: str) -> bool:
        return self.repository.delete_gpu_capacity_override(gpu_model)

    # --- Provider server onboarding ----------------------------------------

    def _hotkey_whitelisted(self, hotkey: str) -> bool:
        from greencompute_persistence import session_scope
        from greencompute_persistence.orm import MinerWhitelistORM

        with session_scope(self.repository.session_factory) as session:
            return session.get(MinerWhitelistORM, hotkey) is not None

    def create_provider_server(
        self,
        request: ProviderServerCreateRequest,
        *,
        user_id: str | None,
        admin: bool = False,
    ) -> tuple[ProviderServerRecord, "ProvisionInputs"]:  # noqa: F821
        """Validate + persist a pending server record and build the (non-
        persisted) provisioning inputs. The route layer kicks off the actual
        SSH provisioning in a background task. Raises ValueError/PermissionError
        which the route maps to 400/403."""
        from greencompute_gateway.infrastructure.server_provisioner import (
            ProvisionInputs,
            resolve_and_guard_host,
        )

        hotkey = request.hotkey.strip()
        if not hotkey:
            raise ValueError("hotkey is required")
        # Reject control chars (CR/LF) in any field that flows into the node
        # .env, so a malicious value can't inject extra GREENCOMPUTE_* lines.
        # Fail fast at the API edge (ValueError -> 400) in addition to the
        # render-time guard (defense in depth).
        def _no_ctrl(value: str | None, field: str) -> None:
            v = value or ""
            if any(c in v for c in "\r\n") or any(ord(c) < 32 and c != "\t" for c in v):
                raise ValueError(f"{field} contains invalid control characters")

        _no_ctrl(request.hotkey, "hotkey")
        _no_ctrl(request.payout_address, "payout_address")
        _no_ctrl(request.label, "label")
        _no_ctrl(request.hf_token, "hf_token")
        _no_ctrl(request.ssh_user, "ssh_user")
        # Gate: the hotkey must be whitelisted (admins bypass for testing).
        if not admin and not self._hotkey_whitelisted(hotkey):
            raise PermissionError(
                f"hotkey {hotkey[:10]}… is not whitelisted — submit a provider "
                f"application and get approved before onboarding servers"
            )
        if not request.ssh_password.strip() and not request.ssh_private_key.strip():
            raise ValueError("provide an SSH password or private key")
        # SSRF guard — resolve ssh_host and reject private/loopback/link-local/
        # multicast/reserved/unspecified destinations BEFORE persisting or
        # queuing, so the API returns 400 synchronously and no provider_servers
        # row is left in `provisioning`. Re-checked again right before connect
        # (server_provisioner._SSH) to defeat DNS rebinding.
        resolve_and_guard_host(request.ssh_host.strip(), request.ssh_port)

        # Unique node_id (PK on node_inventory). Slugify the label + random.
        slug = re.sub(r"[^a-z0-9]+", "-", (request.label or "node").lower()).strip("-") or "node"
        node_id = f"{slug[:32]}-{secrets.token_hex(3)}"

        record = ProviderServerRecord(
            owner_user_id=user_id,
            hotkey=hotkey,
            payout_address=request.payout_address.strip(),
            label=request.label.strip(),
            ssh_host=request.ssh_host.strip(),
            ssh_port=request.ssh_port,
            ssh_user=request.ssh_user.strip() or "root",
            node_id=node_id,
            status="pending",
        )
        self.repository.save_provider_server(record)

        inputs = ProvisionInputs(
            server_id=record.server_id,
            hotkey=hotkey,
            payout_address=record.payout_address,
            label=record.label,
            ssh_host=record.ssh_host,
            ssh_port=record.ssh_port,
            ssh_user=record.ssh_user,
            ssh_password=request.ssh_password,
            ssh_private_key=request.ssh_private_key,
            hf_token=request.hf_token,
            node_id=node_id,
        )
        return record, inputs

    def run_provisioning(self, inputs: "ProvisionInputs") -> None:  # noqa: F821
        """Background entry — runs the SSH provisioning and streams progress
        into the server record. Never raises (called from BackgroundTasks)."""
        from greencompute_gateway.infrastructure.server_provisioner import provision_server

        sid = inputs.server_id

        def on_log(chunk: str) -> None:
            try:
                self.repository.update_provider_server_status(sid, append_log=chunk)
            except Exception:
                log.exception("provisioning log append failed for %s", sid)

        def on_status(status: str) -> None:
            try:
                self.repository.update_provider_server_status(sid, status=status)
            except Exception:
                log.exception("provisioning status update failed for %s", sid)

        result = provision_server(inputs, on_log, on_status)
        if result.ok:
            self.repository.update_provider_server_status(
                sid,
                status="running",
                last_error="",
                public_ip=result.public_ip,
                hardware={
                    "gpu_model": result.gpu_model,
                    "gpu_count": result.gpu_count,
                    "vram_gb_per_gpu": result.vram_gb_per_gpu,
                    "cpu_cores": result.cpu_cores,
                    "memory_gb": result.memory_gb,
                },
            )
        else:
            self.repository.update_provider_server_status(
                sid, status="failed", last_error=result.error,
            )

    def list_provider_servers(self, user_id: str | None, *, admin: bool = False) -> list[ProviderServerRecord]:
        return self.repository.list_provider_servers(user_id, admin=admin)

    def get_provider_server(self, server_id: str, *, user_id: str | None, admin: bool = False) -> ProviderServerRecord:
        rec = self.repository.get_provider_server(server_id)
        if rec is None:
            raise KeyError(f"server not found: {server_id}")
        if not admin and rec.owner_user_id != user_id:
            raise PermissionError("server access denied")
        return rec

    def delete_provider_server(self, server_id: str, *, user_id: str | None, admin: bool = False) -> ProviderServerRecord:
        rec = self.repository.get_provider_server(server_id)
        if rec is None:
            raise KeyError(f"server not found: {server_id}")
        if not admin and rec.owner_user_id != user_id:
            raise PermissionError("server access denied")
        deleted = self.repository.delete_provider_server(server_id)
        if deleted is None:
            raise KeyError(f"server not found: {server_id}")
        return deleted

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

    def delete_workload(
        self,
        workload_id: str,
        *,
        actor_user_id: str | None,
        admin: bool = False,
    ) -> WorkloadSpec:
        workload = self.control_plane.repository.get_workload(workload_id)
        if workload is None:
            raise KeyError(f"workload not found: {workload_id}")
        if not admin and workload.owner_user_id != actor_user_id:
            raise PermissionError(f"workload delete denied: {workload_id}")
        deleted = self.control_plane.repository.delete_workload(workload_id)
        if deleted is None:
            raise KeyError(f"workload not found: {workload_id}")
        return deleted

    def list_invocations(self, limit: int | None = None) -> list[InvocationRecord]:
        return self.control_plane.list_invocations(limit=limit)

    def get_invocation(self, invocation_id: str) -> InvocationRecord | None:
        return self.control_plane.get_invocation(invocation_id)

    def export_recent_invocations(self, limit: int = 50) -> dict:
        return self.control_plane.export_recent_invocations(limit=limit)

    def payment_summary(self) -> dict:
        usage = self.control_plane.usage_summary()
        return {
            "usage": usage,
            "revenue_usd": 0.0,
            "tao_total": 0.0,
            "fmv_usd_per_tao": 0.0,
            "pricing": {"compute_unit_usd": 0.0001, "inference_per_token_usd": 0.000001},
        }

    def workload_utilization(self, workload_id: str) -> dict:
        usage = self.control_plane.usage_summary()
        deployments = self.control_plane.list_deployments()
        workload_deployment_ids = {d.deployment_id for d in deployments if d.workload_id == workload_id}
        request_count = 0.0
        compute_seconds = 0.0
        occupancy_seconds = 0.0
        for dep_id in workload_deployment_ids:
            dep_usage = usage.get(dep_id, {})
            request_count += dep_usage.get("requests", 0) + dep_usage.get("streamed_requests", 0)
            compute_seconds += dep_usage.get("compute_seconds", 0.0)
            occupancy_seconds += dep_usage.get("occupancy_seconds", 0.0)
        return {
            "workload_id": workload_id,
            "request_count": int(request_count),
            "compute_seconds": compute_seconds,
            "occupancy_seconds": occupancy_seconds,
        }

    def list_routing_decisions(self, limit: int = 50) -> list[dict]:
        return self.repository.list_routing_decisions(limit=limit)

    def invoke_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key_id: str | None = None,
        user_id: str | None = None,
        routed_host: str | None = None,
        admin: bool = False,
    ):
        request_id = str(uuid4())
        started = perf_counter()
        candidates, routing = self._select_healthy_deployments(
            request, routed_host=routed_host, user_id=user_id, admin=admin
        )
        last_exc: RuntimeError | None = None
        for deployment in candidates:
            try:
                response = self._invoke_upstream_response(deployment, request, request_id=request_id)
            except RuntimeError as exc:
                last_exc = exc
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
                continue
            self._handle_upstream_success(deployment, routing)
            self._record_usage(deployment, stream=False, stream_chunk_count=0)
            latency_ms = (perf_counter() - started) * 1000.0
            self.metrics.observe("invoke.latency_ms", latency_ms)
            self._track_latency_ema(deployment.deployment_id, latency_ms)
            response.id = request_id
            # Miner-reported usage is clamped to the request's plausible
            # bounds before it feeds billing, accrual, or demand stats.
            billed_prompt = billed_completion = 0
            if response.usage is not None:
                billed_prompt, billed_completion = self._clamp_reported_usage(
                    request,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                    deployment=deployment,
                )
            # Record demand stats for every successful call (admin + anon
            # included). Gives Flux a signal even when the caller doesn't pay.
            if response.usage is not None:
                try:
                    from greencompute_gateway.infrastructure.billing_repository import BillingRepository
                    BillingRepository().record_demand_tick(
                        model_id=request.model,
                        prompt_tokens=billed_prompt,
                        completion_tokens=billed_completion,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    self.metrics.increment("inference.demand.record_failed")

            # Per-token charge: only if miner returned a usage block AND the
            # caller is a real user (admin / anonymous is free by policy).
            if user_id and response.usage is not None:
                self._charge_inference_tokens(
                    user_id=user_id,
                    reference_id=request_id,
                    model=request.model,
                    prompt_tokens=billed_prompt,
                    completion_tokens=billed_completion,
                    deployment=deployment,
                )
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
        raise last_exc  # type: ignore[misc]

    def stream_chat_completion(
        self,
        request: ChatCompletionRequest,
        api_key_id: str | None = None,
        user_id: str | None = None,
        routed_host: str | None = None,
        admin: bool = False,
    ) -> Iterator[str]:
        request_id = str(uuid4())
        started = perf_counter()
        candidates, routing = self._select_healthy_deployments(
            request, routed_host=routed_host, user_id=user_id, admin=admin
        )
        last_exc: RuntimeError | None = None
        for deployment in candidates:
            chunk_count = 0
            # Accumulated usage from the final OpenAI streaming chunk (which
            # only arrives when stream_options.include_usage is True —
            # injected by the node-agent automatically).
            stream_usage: dict | None = None
            try:
                for line in self._invoke_upstream_stream(deployment, request, request_id=request_id):
                    stripped = line.strip()
                    if stripped.startswith("data: ") and stripped != "data: [DONE]":
                        payload = loads(stripped[6:])
                        choices = payload.get("choices", [])
                        if choices and choices[0].get("delta", {}).get("content"):
                            chunk_count += 1
                        # OpenAI streams emit usage ONLY in the final chunk
                        # (choices=[] at that point, usage populated).
                        if isinstance(payload.get("usage"), dict):
                            stream_usage = payload["usage"]
                    yield line
            except RuntimeError as exc:
                last_exc = exc
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
                # Only retry if no chunks have been yielded yet
                if chunk_count > 0:
                    raise
                continue
            self._handle_upstream_success(deployment, routing)
            self._record_usage(deployment, stream=True, stream_chunk_count=chunk_count)
            # Miner-reported usage from the final chunk is clamped to the
            # request's plausible bounds before billing/accrual/demand.
            billed_prompt = billed_completion = 0
            if stream_usage:
                billed_prompt, billed_completion = self._clamp_reported_usage(
                    request,
                    int(stream_usage.get("prompt_tokens", 0) or 0),
                    int(stream_usage.get("completion_tokens", 0) or 0),
                    deployment=deployment,
                )
            # Per-token charge — requires usage from final chunk. If it's
            # missing (older vLLM or user override of stream_options), skip
            # the debit rather than guess at token counts. Acceptable for
            # MVP; upgrade path is a tokenizer-based fallback count.
            if user_id and stream_usage:
                self._charge_inference_tokens(
                    user_id=user_id,
                    reference_id=request_id,
                    model=request.model,
                    prompt_tokens=billed_prompt,
                    completion_tokens=billed_completion,
                    deployment=deployment,
                )
            stream_latency_ms = (perf_counter() - started) * 1000.0
            self.metrics.observe("invoke.latency_ms", stream_latency_ms)
            self._track_latency_ema(deployment.deployment_id, stream_latency_ms)
            if stream_usage:
                try:
                    from greencompute_gateway.infrastructure.billing_repository import BillingRepository
                    BillingRepository().record_demand_tick(
                        model_id=request.model,
                        prompt_tokens=billed_prompt,
                        completion_tokens=billed_completion,
                        latency_ms=stream_latency_ms,
                    )
                except Exception:
                    self.metrics.increment("inference.demand.record_failed")
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
            return
        if last_exc is not None:
            raise last_exc

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

    def _track_latency_ema(self, deployment_id: str, latency_ms: float, alpha: float = 0.3) -> None:
        """EMA of recent chat-completion latencies. Alpha=0.3 means a single
        slow sample shifts the score but transient spikes don't dominate."""
        prev = self._deployment_latency_ema.get(deployment_id)
        if prev is None:
            self._deployment_latency_ema[deployment_id] = latency_ms
        else:
            self._deployment_latency_ema[deployment_id] = (alpha * latency_ms) + ((1 - alpha) * prev)

    def _select_healthy_deployments(
        self,
        request: ChatCompletionRequest,
        routed_host: str | None = None,
        *,
        user_id: str | None = None,
        admin: bool = False,
    ) -> tuple[list[DeploymentRecord], dict]:
        """Return all healthy deployments in round-robin order, plus routing metadata."""
        workload, routing = self.resolve_workload_reference(request.model, routed_host=routed_host)
        # Authorization: a caller may only invoke a workload they own, one shared
        # with them, or a public one. Otherwise report it as an unknown model so
        # a private workload's existence is not enumerable via its id/alias/name.
        if not admin and not self._user_can_access_workload(workload, user_id):
            raise NoReadyDeploymentError(f"unknown model={request.model}")
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

        healthy: list[DeploymentRecord] = []
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
            healthy.append(deployment)

        if not healthy:
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

        # Latency-aware: sort healthy deployments by EMA latency ascending
        # (faster miners first). Deployments without a latency sample keep
        # their index stable via 0.0 — they get probed first, we learn
        # their latency, then the scheduler sorts them accordingly. After
        # sorting, still rotate to balance load across the top-N.
        healthy.sort(key=lambda d: self._deployment_latency_ema.get(d.deployment_id, 0.0))

        # Round-robin: rotate the (latency-sorted) healthy list so each request starts at a different deployment
        offset = self._round_robin_counter % len(healthy)
        self._round_robin_counter += 1
        ordered = healthy[offset:] + healthy[:offset]

        routing_out = {
            **routing,
            "workload_id": workload_id,
            "selected_deployment_id": ordered[0].deployment_id,
            "selected_hotkey": ordered[0].hotkey,
            "routing_reason": "ready_and_healthy",
            "candidate_count": len(ordered),
        }
        return ordered, routing_out

    def _select_deployment_for_request(
        self,
        request: ChatCompletionRequest,
        routed_host: str | None = None,
    ) -> tuple[DeploymentRecord, dict]:
        """Select a single deployment (backwards compat). Prefers round-robin."""
        candidates, routing = self._select_healthy_deployments(request, routed_host=routed_host)
        return candidates[0], routing

    def _rewrite_model_for_upstream(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionRequest:
        """Rewrite request.model from workload name to the actual HF model identifier."""
        workload, _ = self.resolve_workload_reference(request.model)
        if workload.runtime and workload.runtime.model_identifier:
            return request.model_copy(update={"model": workload.runtime.model_identifier})
        return request

    def _invoke_upstream_response(
        self,
        deployment: DeploymentRecord,
        request: ChatCompletionRequest,
        *,
        request_id: str,
    ):
        upstream_request = self._rewrite_model_for_upstream(request)
        return self.inference_client.invoke_chat_completion(deployment, upstream_request, request_id=request_id)

    def _invoke_upstream_stream(
        self,
        deployment: DeploymentRecord,
        request: ChatCompletionRequest,
        *,
        request_id: str,
    ) -> Iterator[str]:
        upstream_request = self._rewrite_model_for_upstream(request)
        return self.inference_client.stream_chat_completion(deployment, upstream_request, request_id=request_id)

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

    # Bounds for billable token counts. The miner's self-reported usage is
    # the ONLY usage signal we have, and the miner profits from it (accrual)
    # while the caller pays for it — so it is never trusted raw. Completion
    # is capped by what the caller asked for; prompt by an upper bound
    # derived from the request itself: a text token is never shorter than a
    # character, plus generous allowances for chat-template overhead and
    # multimodal image tokens. Legitimate usage stays under these bounds;
    # a 10^9-token report gets clamped to the request's plausible maximum.
    _BILLED_COMPLETION_TOKENS_CEILING = 131_072
    _BILLED_PROMPT_TOKENS_PER_IMAGE = 16_384
    _BILLED_PROMPT_TOKENS_PER_MESSAGE = 32
    _BILLED_PROMPT_TOKENS_BASE = 256

    def _clamp_reported_usage(
        self,
        request: ChatCompletionRequest,
        prompt_tokens: int,
        completion_tokens: int,
        deployment: DeploymentRecord | None = None,
    ) -> tuple[int, int]:
        """Clamp miner-reported token counts to what this request could
        plausibly have produced, before any money moves. Returns the
        (billable_prompt_tokens, billable_completion_tokens) pair used for
        the user debit, the miner accrual, and the demand signal — so an
        inflated report can neither drain the caller nor pay the miner.
        Clamps are flagged (metric + log) so a consistently-inflating miner
        shows up in ops."""
        reported_prompt = max(int(prompt_tokens or 0), 0)
        reported_completion = max(int(completion_tokens or 0), 0)

        chars = 0
        images = 0
        for message in request.messages:
            if isinstance(message.content, str):
                chars += len(message.content)
            else:
                for block in message.content:
                    if block.text:
                        chars += len(block.text)
                    if block.type == "image_url":
                        images += 1
        prompt_bound = (
            chars
            + images * self._BILLED_PROMPT_TOKENS_PER_IMAGE
            + len(request.messages) * self._BILLED_PROMPT_TOKENS_PER_MESSAGE
            + self._BILLED_PROMPT_TOKENS_BASE
        )

        completion_bound = self._BILLED_COMPLETION_TOKENS_CEILING
        requested_max = request.max_tokens
        # OpenAI's newer field name, passed through via extra=allow and
        # honored by vLLM — take whichever cap the caller set higher.
        max_completion_tokens = (request.model_extra or {}).get("max_completion_tokens")
        if isinstance(max_completion_tokens, int) and max_completion_tokens > 0:
            requested_max = max(requested_max or 0, max_completion_tokens)
        if requested_max:
            completion_bound = min(requested_max, completion_bound)

        billed_prompt = min(reported_prompt, prompt_bound)
        billed_completion = min(reported_completion, completion_bound)
        if billed_prompt < reported_prompt or billed_completion < reported_completion:
            self.metrics.increment("inference.usage.clamped")
            log.warning(
                "Implausible miner-reported usage clamped: model=%s hotkey=%s "
                "prompt %d→%d (bound %d) completion %d→%d (bound %d)",
                request.model,
                deployment.hotkey if deployment else None,
                reported_prompt,
                billed_prompt,
                prompt_bound,
                reported_completion,
                billed_completion,
                completion_bound,
            )
        return billed_prompt, billed_completion

    def _charge_inference_tokens(
        self,
        *,
        user_id: str,
        reference_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        deployment: DeploymentRecord | None = None,
    ) -> None:
        """Debit a user's balance for one chat-completion call + record a
        per-miner accrual row.

        The accrual table is **reporting-only** — it does not create a
        liability. Miners earn from TAO emissions via the subnet's weight
        snapshot (publish_weight_snapshot). The `cents_earned` column is
        kept so a future miner dashboard can show "you served N requests
        worth ~$X equivalent this week." If you later want to run direct
        fiat payouts, a separate batch job signs transactions with the
        treasury wallet and reads these rows.

        Uses the shared `inference_cost_cents` helper so the UI's estimate
        matches the actual charge. We DRAIN the user's balance to zero via
        `debit_user_to_floor` (never raises, never goes negative) instead of
        the old all-or-nothing debit that took NOTHING when the balance
        couldn't cover the full cost — that gave the user "keep your cent AND
        get the request free". Now the remaining balance is always captured and
        the pre-flight `<=0` gate blocks the NEXT request. A shortfall (the
        single overdrawn request) is recorded as a metric, not swallowed
        silently. By the time we get here the response has already been yielded,
        so we never raise.
        """
        from greencompute_protocol import inference_cost_cents
        from greencompute_gateway.infrastructure.billing_repository import (
            BillingRepository,
        )

        cents = inference_cost_cents(prompt_tokens, completion_tokens)
        billing = BillingRepository()
        try:
            result = billing.debit_user_to_floor(
                user_id=user_id,
                amount_cents=cents,
                kind="inference",
                reference_id=reference_id,
                description=(
                    f"Inference {model}: "
                    f"{prompt_tokens} in + {completion_tokens} out tokens"
                ),
            )
        except Exception:
            self.metrics.increment("inference.debit.failed")
            return
        if result.get("shortfall_cents", 0) > 0:
            # The user couldn't fully cover this one request; we drained what
            # they had. The pre-flight gate blocks subsequent requests.
            self.metrics.increment("inference.debit.shortfall")

        # Debit succeeded → accrue the miner's cut. Accrual captures the
        # full revenue for now; payout split + on-chain transfer is a later
        # admin job (see plan §2E out-of-scope notes).
        if deployment and deployment.hotkey:
            try:
                billing.accrue_miner_payout(
                    hotkey=deployment.hotkey,
                    deployment_id=deployment.deployment_id,
                    workload_id=deployment.workload_id,
                    request_id=reference_id,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cents_earned=cents,
                )
                self.metrics.increment("inference.accrual.recorded")
            except Exception:
                self.metrics.increment("inference.accrual.failed")

        # Demand tick is recorded by the caller (invoke_chat_completion /
        # stream_chat_completion) so admin + anonymous invocations also
        # contribute the signal Flux needs.

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

import pytest
from fastapi import HTTPException

from greencompute_builder.application.services import BuilderService
from greencompute_builder.infrastructure.repository import BuilderRepository
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_control_plane.transport import routes as control_plane_routes
from greencompute_control_plane.transport import security as control_plane_security
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes as gateway_routes
from greencompute_gateway.transport import security as gateway_security
from greencompute_persistence import CredentialStore, FixedWindowRateLimiter, WorkflowEventRepository
from greencompute_protocol import (
    APIKeyCreateRequest,
    BuildRequest,
    CapacityUpdate,
    DeploymentCreateRequest,
    DeploymentState,
    DeploymentStatusUpdate,
    Heartbeat,
    InvocationRecord,
    MinerRegistration,
    NodeCapability,
    ProbeResult,
    UserRegistrationRequest,
    WorkloadCreateRequest,
    WorkloadSpec,
    sign_payload,
)
from greencompute_validator.application.services import ValidatorService
from greencompute_validator.infrastructure.repository import ValidatorRepository
from greencompute_validator.transport import routes as validator_routes
from greencompute_validator.transport import security as validator_security


def _seed_keys(repository: GatewayRepository) -> tuple[str, str]:
    gateway = GatewayService(repository=repository)
    user = gateway.register_user(UserRegistrationRequest(username="alice", email="alice@example.com"))
    user_key = gateway.create_api_key(APIKeyCreateRequest(name="user", user_id=user.user_id))
    admin_key = gateway.create_api_key(APIKeyCreateRequest(name="admin", user_id=user.user_id, admin=True))
    return user_key.secret, admin_key.secret


def _miner_headers(hotkey: str, secret: str, body: bytes) -> dict:
    """The headers a miner sends, plus the exact body it signed (routes verify
    the signature over the raw request body)."""
    signed = sign_payload(secret=secret, actor_id=hotkey, body=body)
    return {
        "raw_body": body,
        "x_miner_hotkey": hotkey,
        "x_miner_signature": signed.signature,
        "x_miner_nonce": signed.nonce,
        "x_miner_timestamp": str(signed.timestamp),
        "x_miner_auth_mode": None,
    }


def test_gateway_routes_require_api_key_and_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    builder_repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    gateway_service = GatewayService(
        repository=gateway_repository,
        builder=BuilderService(builder_repository, workflow_repository=workflow_repository),
        control_plane=ControlPlaneService(control_repository, workflow_repository=workflow_repository),
    )
    user_secret, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(gateway_routes, "service", gateway_service)
    monkeypatch.setattr(
        gateway_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(gateway_security, "rate_limiter", FixedWindowRateLimiter())

    with pytest.raises(HTTPException) as missing:
        gateway_routes.build_image(BuildRequest(image="greenference/echo:latest", context_uri="s3://ctx.zip"))
    assert missing.value.status_code == 401

    build = gateway_routes.build_image(
        BuildRequest(image="greenference/echo:latest", context_uri="s3://ctx.zip"),
        authorization=f"Bearer {user_secret}",
    )
    assert build["status"] == "accepted"

    for _ in range(60):
        payload = gateway_routes.embeddings(
            {"input": "hello", "model": "test-embedding"},
            authorization=f"Bearer {admin_secret}",
        )
        assert payload["object"] == "list"

    with pytest.raises(HTTPException) as limited:
        gateway_routes.embeddings(
            {"input": "hello", "model": "test-embedding"},
            authorization=f"Bearer {admin_secret}",
        )
    assert limited.value.status_code == 429


def test_gateway_admin_routes_expose_build_and_invocation_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    builder_repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    gateway_service = GatewayService(
        repository=gateway_repository,
        builder=BuilderService(builder_repository, workflow_repository=workflow_repository),
        control_plane=ControlPlaneService(control_repository, workflow_repository=workflow_repository),
    )
    user_secret, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(gateway_routes, "service", gateway_service)
    monkeypatch.setattr(
        gateway_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(gateway_security, "rate_limiter", FixedWindowRateLimiter())

    build = gateway_routes.build_image(
        BuildRequest(image="greenference/echo:latest", context_uri="s3://ctx.zip"),
        authorization=f"Bearer {user_secret}",
    )
    gateway_service.builder.process_pending_events()
    gateway_service.control_plane.record_invocation(
        InvocationRecord(
            deployment_id="dep-1",
            workload_id="wl-1",
            hotkey="miner-a",
            model="greenference/echo",
            api_key_id="key-1",
            status="succeeded",
            latency_ms=12.5,
            message_count=1,
        )
    )
    gateway_service.control_plane.process_pending_events()

    build_history = gateway_routes.image_history(
        "greenference/echo:latest",
        authorization=f"Bearer {admin_secret}",
    )
    build_record = gateway_routes.get_build(
        build["build_id"],
        authorization=f"Bearer {admin_secret}",
    )
    build_context = gateway_routes.get_build_context(
        build["build_id"],
        authorization=f"Bearer {admin_secret}",
    )
    build_events = gateway_routes.get_build_events(
        build["build_id"],
        authorization=f"Bearer {admin_secret}",
    )
    build_attempts = gateway_routes.get_build_attempts(
        build["build_id"],
        authorization=f"Bearer {admin_secret}",
    )
    build_attempt = gateway_routes.get_build_attempt(
        build["build_id"],
        1,
        authorization=f"Bearer {admin_secret}",
    )
    build_logs = gateway_routes.get_build_logs(
        build["build_id"],
        authorization=f"Bearer {admin_secret}",
    )
    invocation_records = gateway_routes.list_invocations(authorization=f"Bearer {admin_secret}")
    invocation_export = gateway_routes.export_recent_invocations(authorization=f"Bearer {admin_secret}")

    assert len(build_history) == 1
    assert build_record["status"] == "published"
    assert build_record["artifact_digest"] is not None
    assert build_context["normalized_context_uri"] == "s3://ctx.zip"
    assert build_context["staged_context_uri"] == (
        f"s3://greencompute-build-artifacts/contexts/{build['build_id']}/context.tar.gz"
    )
    assert build_record["registry_manifest_uri"] == f"{build_record['artifact_uri']}@{build_record['artifact_digest']}"
    assert build_record["executor_name"] == "simulated-buildkit"
    assert build_attempts[0]["attempt"] == 1
    assert build_attempt["attempt"] == 1
    assert any(log["stage"] == "publishing" for log in build_logs)
    assert [event["stage"] for event in build_events] == ["accepted", "job_started", "staging", "building", "publishing"]
    assert len(invocation_records) == 1
    assert invocation_records[0]["latency_ms"] == 12.5
    assert invocation_records[0]["resolution_basis"] is None
    assert invocation_export["summary"]["count"] == 1


def test_gateway_debug_routes_expose_alias_and_host_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    builder_repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    gateway_service = GatewayService(
        repository=gateway_repository,
        builder=BuilderService(builder_repository, workflow_repository=workflow_repository),
        control_plane=ControlPlaneService(control_repository, workflow_repository=workflow_repository),
    )
    user_secret, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(gateway_routes, "service", gateway_service)
    monkeypatch.setattr(
        gateway_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(gateway_security, "rate_limiter", FixedWindowRateLimiter())

    workload = gateway_routes.create_workload(
        WorkloadCreateRequest(
            name="route-model",
            workload_alias="route-alias",
            ingress_host="route.greenference.local",
            image="greenference/echo:latest",
            requirements={"gpu_count": 1},
        ),
        authorization=f"Bearer {user_secret}",
    )

    route = gateway_routes.debug_route(
        "route-alias",
        host="route.greenference.local",
        authorization=f"Bearer {admin_secret}",
    )
    decisions = gateway_routes.debug_routing_decisions(authorization=f"Bearer {admin_secret}")

    assert route["workload_id"] == workload["workload_id"]
    assert route["workload_alias"] == "route-alias"
    assert route["ingress_host"] == "route.greenference.local"
    assert route["routing"]["matched_by"] == "ingress_host"
    assert decisions == []


def test_gateway_placeholder_routes_are_explicitly_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    builder_repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    gateway_service = GatewayService(
        repository=gateway_repository,
        builder=BuilderService(builder_repository, workflow_repository=workflow_repository),
        control_plane=ControlPlaneService(control_repository, workflow_repository=workflow_repository),
    )
    user_secret, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(gateway_routes, "service", gateway_service)
    monkeypatch.setattr(
        gateway_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(gateway_security, "rate_limiter", FixedWindowRateLimiter())

    disabled_calls = [
        lambda: gateway_routes.upload_logo(authorization=f"Bearer {user_secret}"),
        lambda: gateway_routes.get_logo("logo-1", "png", authorization=f"Bearer {user_secret}"),
        lambda: gateway_routes.list_bounties(authorization=f"Bearer {user_secret}"),
        lambda: gateway_routes.audit_miner_data({"miner_id": "miner-a"}, authorization=f"Bearer {admin_secret}"),
        lambda: gateway_routes.list_audit(authorization=f"Bearer {admin_secret}"),
        lambda: gateway_routes.audit_download("audit/miner-a", authorization=f"Bearer {admin_secret}"),
        lambda: gateway_routes.misc_proxy("https://scoredata.me/test", authorization=f"Bearer {user_secret}"),
        lambda: gateway_routes.misc_hf_repo_info("org/model", "README.md", authorization=f"Bearer {user_secret}"),
        lambda: gateway_routes.e2e_instances("workload-1", authorization=f"Bearer {user_secret}"),
        gateway_routes.idp_scopes,
        lambda: gateway_routes.idp_authorize("client", "https://app/cb", "code", "openid"),
        lambda: gateway_routes.idp_token({"grant_type": "authorization_code"}),
        lambda: gateway_routes.e2e_invoke({"ciphertext": "abc"}, authorization=f"Bearer {user_secret}"),
    ]

    for call in disabled_calls:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 501
        assert exc.value.detail["status"] == "disabled"


def test_control_plane_routes_require_miner_header_and_expose_debug_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test is about signatures and replay, not the onboarding whitelist
    # (added later, on by default), which would refuse the test miner.
    from greencompute_control_plane.config import settings as cp_settings

    monkeypatch.setattr(cp_settings, "whitelist_enabled", False)
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)
    monkeypatch.setattr(
        control_plane_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )

    registration = MinerRegistration(
        hotkey="miner-a",
        payout_address="5Fminer",
        auth_secret="miner-a-secret",
        api_base_url="http://miner-a.local",
        validator_url="http://validator.local",
    )
    registration_headers = _miner_headers("miner-a", registration.auth_secret, registration.model_dump_json().encode())

    with pytest.raises(HTTPException) as mismatch:
        control_plane_routes.register_miner(registration, **(registration_headers | {"x_miner_hotkey": "miner-b"}))
    assert mismatch.value.status_code == 403

    control_plane_routes.register_miner(registration, **registration_headers)
    heartbeat = Heartbeat(hotkey="miner-a", healthy=True)
    control_plane_routes.heartbeat(
        heartbeat,
        **_miner_headers("miner-a", registration.auth_secret, heartbeat.model_dump_json().encode()),
    )
    capacity = CapacityUpdate(
        hotkey="miner-a",
        nodes=[
            NodeCapability(
                hotkey="miner-a",
                node_id="node-a",
                gpu_model="a100",
                gpu_count=1,
                available_gpus=1,
                vram_gb_per_gpu=80,
                cpu_cores=32,
                memory_gb=128,
            )
        ],
    )
    control_plane_routes.capacity(
        capacity,
        **_miner_headers("miner-a", registration.auth_secret, capacity.model_dump_json().encode()),
    )
    replay_headers = _miner_headers("miner-a", registration.auth_secret, heartbeat.model_dump_json().encode())
    control_plane_routes.heartbeat(heartbeat, **replay_headers)
    with pytest.raises(HTTPException) as replay:
        control_plane_routes.heartbeat(heartbeat, **replay_headers)
    assert replay.value.status_code == 401
    service.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="echo-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    workload = service.find_workload_by_name("echo-model")
    assert workload is not None
    deployment = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()

    workflows = control_plane_routes.debug_workflows(authorization=f"Bearer {admin_secret}")
    leases = control_plane_routes.debug_leases(authorization=f"Bearer {admin_secret}")
    workers = control_plane_routes.debug_workers(authorization=f"Bearer {admin_secret}")
    deliveries = control_plane_routes.debug_event_deliveries(authorization=f"Bearer {admin_secret}")
    metrics = control_plane_routes.platform_metrics(authorization=f"Bearer {admin_secret}")
    status = control_plane_routes.debug_status(authorization=f"Bearer {admin_secret}")

    assert deployment.deployment_id in {event["payload"]["deployment_id"] for event in workflows if "deployment_id" in event["payload"]}
    assert len(leases) == 1
    assert any(item["consumer"] == "control-plane-worker" for item in workers)
    assert any(item["subject"] == "deployment.requested" for item in deliveries)
    assert metrics["gauges"]["deployments.total"] >= 1.0
    assert "workers" in status


def test_control_plane_debug_views_expose_unhealthy_miners_and_reassignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)
    monkeypatch.setattr(
        control_plane_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )

    miner_a = MinerRegistration(
        hotkey="miner-a",
        payout_address="5FminerA",
        auth_secret="miner-a-secret",
        api_base_url="http://miner-a.local",
        validator_url="http://validator.local",
    )
    miner_b = MinerRegistration(
        hotkey="miner-b",
        payout_address="5FminerB",
        auth_secret="miner-b-secret",
        api_base_url="http://miner-b.local",
        validator_url="http://validator.local",
    )
    service.register_miner(miner_a)
    service.register_miner(miner_b)
    service.record_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    service.record_heartbeat(Heartbeat(hotkey="miner-b", healthy=True))
    service.update_capacity(
        CapacityUpdate(
            hotkey="miner-a",
            nodes=[
                NodeCapability(
                    hotkey="miner-a",
                    node_id="node-a",
                    gpu_model="a100",
                    gpu_count=1,
                    available_gpus=1,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                    performance_score=1.5,
                )
            ],
        )
    )
    service.update_capacity(
        CapacityUpdate(
            hotkey="miner-b",
            nodes=[
                NodeCapability(
                    hotkey="miner-b",
                    node_id="node-b",
                    gpu_model="a100",
                    gpu_count=1,
                    available_gpus=1,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                    performance_score=1.0,
                )
            ],
        )
    )
    workload = service.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="failover-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()
    service.record_heartbeat(Heartbeat(hotkey="miner-a", healthy=False))
    service.process_unhealthy_miners()
    service.process_pending_events()

    miners = control_plane_routes.debug_miners(authorization=f"Bearer {admin_secret}")
    reassignments = control_plane_routes.debug_reassignments(authorization=f"Bearer {admin_secret}")
    stuck = control_plane_routes.debug_stuck_deployments(authorization=f"Bearer {admin_secret}")
    lease_history = control_plane_routes.debug_lease_history(authorization=f"Bearer {admin_secret}")
    drift = control_plane_routes.debug_miner_drift(authorization=f"Bearer {admin_secret}")
    metrics = control_plane_routes.platform_metrics(authorization=f"Bearer {admin_secret}")

    miner_a_report = next(item for item in miners if item["hotkey"] == "miner-a")
    assert miner_a_report["status"] == "unhealthy"
    assert "unhealthy" in miner_a_report["reason"]
    assert any(item["payload"]["deployment_id"] == deployment.deployment_id for item in reassignments)
    assert any(item["deployment_id"] == deployment.deployment_id for item in lease_history)
    assert any(item["hotkey"] == "miner-a" for item in drift["miners"])
    stuck_deployment = next(item for item in stuck if item["deployment_id"] == deployment.deployment_id)
    assert stuck_deployment["state"] == "scheduled"
    assert "stalled" in stuck_deployment["reason"]
    assert metrics["gauges"]["miners.unhealthy"] >= 1.0


def test_gateway_debug_routes_expose_failed_builds_and_retry_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    builder_repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    gateway_service = GatewayService(
        repository=gateway_repository,
        builder=BuilderService(builder_repository, workflow_repository=workflow_repository),
        control_plane=ControlPlaneService(control_repository, workflow_repository=workflow_repository),
    )
    user_secret, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(gateway_routes, "service", gateway_service)
    monkeypatch.setattr(
        gateway_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(gateway_security, "rate_limiter", FixedWindowRateLimiter())

    build = gateway_routes.build_image(
        BuildRequest(image="greenference/retry:latest", context_uri="s3://greenference/fail-once-object-store/retry.zip"),
        authorization=f"Bearer {user_secret}",
    )
    gateway_service.builder.process_pending_events()
    failed_builds = gateway_routes.debug_build_failures(authorization=f"Bearer {admin_secret}")
    cleaned = gateway_routes.cleanup_build(build["build_id"], authorization=f"Bearer {admin_secret}")
    retried = gateway_routes.retry_build(build["build_id"], authorization=f"Bearer {admin_secret}")
    gateway_service.builder.process_pending_events()
    final_build = gateway_routes.get_build(build["build_id"], authorization=f"Bearer {admin_secret}")

    assert failed_builds == []
    assert cleaned["cleanup_status"] == "completed"
    assert retried["retry_count"] == 1
    assert final_build["status"] == "published"


def test_gateway_build_cancellation_route(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    builder_repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    gateway_service = GatewayService(
        repository=gateway_repository,
        builder=BuilderService(builder_repository, workflow_repository=workflow_repository),
        control_plane=ControlPlaneService(control_repository, workflow_repository=workflow_repository),
    )
    user_secret, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(gateway_routes, "service", gateway_service)
    monkeypatch.setattr(
        gateway_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(gateway_security, "rate_limiter", FixedWindowRateLimiter())

    build = gateway_routes.build_image(
        BuildRequest(image="greenference/cancel:latest", context_uri="s3://greenference/builds/cancel.zip"),
        authorization=f"Bearer {user_secret}",
    )
    cancelled = gateway_routes.cancel_build(build["build_id"], authorization=f"Bearer {admin_secret}")
    gateway_service.builder.process_pending_events()
    logs = gateway_routes.get_build_logs(build["build_id"], authorization=f"Bearer {admin_secret}")

    assert cancelled["status"] == "cancelled"
    assert any(item["stage"] == "cancelled" for item in logs)


def test_control_plane_debug_views_expose_servers_nodes_capacity_and_placements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)
    monkeypatch.setattr(
        control_plane_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )

    service.register_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5FminerA",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    service.record_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    service.update_capacity(
        CapacityUpdate(
            hotkey="miner-a",
            nodes=[
                NodeCapability(
                    hotkey="miner-a",
                    node_id="node-a",
                    server_id="server-a",
                    hostname="gpu-a.internal",
                    gpu_model="a100",
                    gpu_count=2,
                    available_gpus=2,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                )
            ],
        )
    )
    workload = service.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="inventory-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()

    servers = control_plane_routes.debug_servers(authorization=f"Bearer {admin_secret}")
    nodes = control_plane_routes.debug_nodes(authorization=f"Bearer {admin_secret}")
    history = control_plane_routes.debug_capacity_history(authorization=f"Bearer {admin_secret}")
    placements = control_plane_routes.debug_placements(authorization=f"Bearer {admin_secret}")

    assert servers[0]["server_id"] == "server-a"
    assert servers[0]["api_base_url"] == "http://miner-a.local"
    assert nodes[0]["node_id"] == "node-a"
    assert nodes[0]["server_id"] == "server-a"
    assert history[0]["node_id"] == "node-a"
    assert history[0]["total_gpus"] == 2
    assert placements[0]["deployment_id"] == deployment.deployment_id
    assert placements[0]["server_id"] == "server-a"
    assert placements[0]["status"] == "assigned"


def test_control_plane_fleet_debug_view_summarizes_inventory_placements_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)
    monkeypatch.setattr(
        control_plane_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )

    service.register_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5FminerA",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    service.record_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    service.update_capacity(
        CapacityUpdate(
            hotkey="miner-a",
            nodes=[
                NodeCapability(
                    hotkey="miner-a",
                    node_id="node-a",
                    server_id="server-a",
                    hostname="gpu-a.internal",
                    gpu_model="a100",
                    gpu_count=2,
                    available_gpus=2,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                )
            ],
        )
    )
    workload = service.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="fleet-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()
    service.fail_deployment(deployment.deployment_id)

    fleet = control_plane_routes.debug_fleet_orchestration(authorization=f"Bearer {admin_secret}")
    status = control_plane_routes.debug_status(authorization=f"Bearer {admin_secret}")

    assert fleet["servers"]["total"] == 1
    assert fleet["nodes"]["total"] == 1
    assert fleet["placements"]["counts_by_status"]["failed"] >= 1
    assert fleet["placements"]["by_server"]["server-a"] >= 1
    assert fleet["deployments"]["failure_classes"]["deployment_failure"] >= 1
    assert fleet["deployments"]["recent_failures"][0]["deployment_id"] == deployment.deployment_id
    assert status["fleet_orchestration"]["servers"]["total"] == 1
    assert status["fleet_orchestration"]["placements"]["counts_by_status"]["failed"] >= 1


def test_control_plane_placeholder_miner_jobs_route_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from greencompute_control_plane.config import settings as cp_settings

    monkeypatch.setattr(cp_settings, "whitelist_enabled", False)  # not what this test is about
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)

    registration = MinerRegistration(
        hotkey="miner-a",
        payout_address="5Fminer",
        auth_secret="miner-secret",
        api_base_url="http://miner.local",
        validator_url="http://validator.local",
    )
    service.register_miner(registration)
    headers = _miner_headers("miner-a", "miner-secret", b"")
    headers.pop("raw_body")  # a GET: the signature covers an empty body

    with pytest.raises(HTTPException) as exc:
        control_plane_routes.miner_list_jobs(**headers)

    assert exc.value.status_code == 501
    assert exc.value.detail["status"] == "disabled"


def test_control_plane_operator_actions_and_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)
    monkeypatch.setattr(
        control_plane_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )

    service.register_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5FminerA",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    service.record_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    service.update_capacity(
        CapacityUpdate(
            hotkey="miner-a",
            nodes=[
                NodeCapability(
                    hotkey="miner-a",
                    node_id="node-a",
                    server_id="server-a",
                    hostname="gpu-a.internal",
                    gpu_model="a100",
                    gpu_count=1,
                    available_gpus=1,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                )
            ],
        )
    )
    workload = service.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="operator-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()

    drained = control_plane_routes.drain_miner("miner-a", authorization=f"Bearer {admin_secret}")
    exclusions = control_plane_routes.debug_placement_exclusions(authorization=f"Bearer {admin_secret}")
    requeued = control_plane_routes.requeue_deployment(deployment.deployment_id, authorization=f"Bearer {admin_secret}")
    failed = control_plane_routes.fail_deployment(deployment.deployment_id, authorization=f"Bearer {admin_secret}")
    failures = control_plane_routes.debug_deployment_failures(authorization=f"Bearer {admin_secret}")
    undrained = control_plane_routes.undrain_miner("miner-a", authorization=f"Bearer {admin_secret}")

    assert drained["drained"] is True
    assert any(item["reason"] == "miner_drained" for item in exclusions)
    assert requeued["state"] == "pending"
    assert failed["state"] == "failed"
    assert any(item["deployment_id"] == deployment.deployment_id for item in failures)
    assert undrained["drained"] is False


def test_cleanup_deployment_releases_capacity_for_next_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    service = ControlPlaneService(control_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(control_plane_routes, "service", service)
    monkeypatch.setattr(control_plane_security, "service", service)
    monkeypatch.setattr(
        control_plane_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )

    service.register_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5FminerA",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    service.record_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    service.update_capacity(
        CapacityUpdate(
            hotkey="miner-a",
            nodes=[
                NodeCapability(
                    hotkey="miner-a",
                    node_id="node-a",
                    server_id="server-a",
                    hostname="gpu-a.internal",
                    gpu_model="a100",
                    gpu_count=1,
                    available_gpus=1,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                )
            ],
        )
    )
    workload = service.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="cleanup-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )

    first = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()
    service.update_deployment_status(
        DeploymentStatusUpdate(deployment_id=first.deployment_id, state=DeploymentState.PULLING)
    )
    service.update_deployment_status(
        DeploymentStatusUpdate(deployment_id=first.deployment_id, state=DeploymentState.STARTING)
    )
    service.update_deployment_status(
        DeploymentStatusUpdate(
            deployment_id=first.deployment_id,
            state=DeploymentState.READY,
            endpoint="http://miner-a.local/deployments/first",
            ready_instances=1,
        )
    )

    cleaned = control_plane_routes.cleanup_deployment(first.deployment_id, authorization=f"Bearer {admin_secret}")

    second = service.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    service.process_pending_events()
    scheduled = service.repository.get_deployment(second.deployment_id)

    assert cleaned["state"] == "terminated"
    assert cleaned["endpoint"] is None
    assert cleaned["failure_class"] == "operator_cleanup"
    assert scheduled is not None
    assert scheduled.state == DeploymentState.SCHEDULED
    assert scheduled.hotkey == "miner-a"


def test_validator_routes_require_headers_and_expose_probe_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    validator_repository = ValidatorRepository(database_url=shared_db, bootstrap=True)
    gateway_repository = GatewayRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    service = ValidatorService(validator_repository, workflow_repository=workflow_repository)
    _, admin_secret = _seed_keys(gateway_repository)

    monkeypatch.setattr(validator_routes, "service", service)
    monkeypatch.setattr(
        validator_security,
        "credential_store",
        CredentialStore(engine=gateway_repository.engine, session_factory=gateway_repository.session_factory),
    )
    monkeypatch.setattr(validator_security, "control_plane_repository", control_repository)

    registration = MinerRegistration(
        hotkey="miner-a",
        payout_address="5Fminer",
        auth_secret="miner-a-secret",
        api_base_url="http://miner-a.local",
        validator_url="http://validator.local",
    )
    control_repository.upsert_miner(registration)

    capability = NodeCapability(
        hotkey="miner-a",
        node_id="node-a",
        gpu_model="a100",
        gpu_count=1,
        available_gpus=1,
        vram_gb_per_gpu=80,
        cpu_cores=32,
        memory_gb=128,
    )
    with pytest.raises(HTTPException) as missing:
        validator_routes.register_capability(capability, raw_body=capability.model_dump_json().encode())
    assert missing.value.status_code == 401

    validator_routes.register_capability(
        capability,
        **_miner_headers("miner-a", registration.auth_secret, capability.model_dump_json().encode()),
    )
    challenge = validator_routes.create_probe("miner-a", "node-a", authorization=f"Bearer {admin_secret}")
    result_payload = ProbeResult(
        challenge_id=challenge["challenge_id"],
        hotkey="miner-a",
        node_id="node-a",
        latency_ms=100.0,
        throughput=180.0,
        benchmark_signature="sig-1",
    )
    scorecard = validator_routes.submit_probe_result(
        result_payload,
        **_miner_headers("miner-a", registration.auth_secret, result_payload.model_dump_json().encode()),
    )
    results = validator_routes.debug_results(authorization=f"Bearer {admin_secret}")
    metrics = validator_routes.validator_metrics(authorization=f"Bearer {admin_secret}")

    assert scorecard["final_score"] > 0
    assert len(results) == 1
    assert metrics["gauges"]["probe.results.total"] == 1.0

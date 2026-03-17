import pytest
from fastapi import HTTPException

from greenference_builder.application.services import BuilderService
from greenference_builder.infrastructure.repository import BuilderRepository
from greenference_control_plane.application.services import ControlPlaneService
from greenference_control_plane.infrastructure.repository import ControlPlaneRepository
from greenference_control_plane.transport import routes as control_plane_routes
from greenference_control_plane.transport import security as control_plane_security
from greenference_gateway.application.services import GatewayService
from greenference_gateway.infrastructure.repository import GatewayRepository
from greenference_gateway.transport import routes as gateway_routes
from greenference_gateway.transport import security as gateway_security
from greenference_persistence import CredentialStore, FixedWindowRateLimiter, WorkflowEventRepository
from greenference_protocol import (
    APIKeyCreateRequest,
    BuildRequest,
    CapacityUpdate,
    DeploymentCreateRequest,
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
from greenference_validator.application.services import ValidatorService
from greenference_validator.infrastructure.repository import ValidatorRepository
from greenference_validator.transport import routes as validator_routes
from greenference_validator.transport import security as validator_security


def _seed_keys(repository: GatewayRepository) -> tuple[str, str]:
    gateway = GatewayService(repository=repository)
    user = gateway.register_user(UserRegistrationRequest(username="alice", email="alice@example.com"))
    user_key = gateway.create_api_key(APIKeyCreateRequest(name="user", user_id=user.user_id))
    admin_key = gateway.create_api_key(APIKeyCreateRequest(name="admin", user_id=user.user_id, admin=True))
    return user_key.secret, admin_key.secret


def _miner_headers(hotkey: str, secret: str, body: bytes) -> dict[str, str]:
    signed = sign_payload(secret=secret, actor_id=hotkey, body=body)
    return {
        "x_miner_hotkey": hotkey,
        "x_miner_signature": signed.signature,
        "x_miner_nonce": signed.nonce,
        "x_miner_timestamp": str(signed.timestamp),
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
    invocation_records = gateway_routes.list_invocations(authorization=f"Bearer {admin_secret}")
    invocation_export = gateway_routes.export_recent_invocations(authorization=f"Bearer {admin_secret}")

    assert len(build_history) == 1
    assert build_record["status"] == "published"
    assert build_record["artifact_digest"] is not None
    assert len(invocation_records) == 1
    assert invocation_records[0]["latency_ms"] == 12.5
    assert invocation_export["summary"]["count"] == 1


def test_control_plane_routes_require_miner_header_and_expose_debug_state(
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
    metrics = control_plane_routes.platform_metrics(authorization=f"Bearer {admin_secret}")

    assert deployment.deployment_id in {event["payload"]["deployment_id"] for event in workflows if "deployment_id" in event["payload"]}
    assert len(leases) == 1
    assert metrics["gauges"]["deployments.total"] >= 1.0


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
    metrics = control_plane_routes.platform_metrics(authorization=f"Bearer {admin_secret}")

    miner_a_report = next(item for item in miners if item["hotkey"] == "miner-a")
    assert miner_a_report["status"] == "unhealthy"
    assert "unhealthy" in miner_a_report["reason"]
    assert any(item["payload"]["deployment_id"] == deployment.deployment_id for item in reassignments)
    stuck_deployment = next(item for item in stuck if item["deployment_id"] == deployment.deployment_id)
    assert stuck_deployment["state"] == "scheduled"
    assert "stalled" in stuck_deployment["reason"]
    assert metrics["gauges"]["miners.unhealthy"] >= 1.0


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
        validator_routes.register_capability(capability)
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

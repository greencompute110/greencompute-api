from datetime import UTC, datetime, timedelta
import base64

from sqlalchemy import select

from greenference_builder.application.services import BuilderService
from greenference_builder.infrastructure.execution import BuilderExecutionError, S3CompatibleObjectStoreAdapter
from greenference_builder.infrastructure.repository import BuilderRepository
from greenference_control_plane.application.services import ControlPlaneService
from greenference_control_plane.config import settings
from greenference_control_plane.infrastructure.repository import ControlPlaneRepository
from greenference_persistence import RuntimeSettings, SubjectBus, WorkflowEventRepository, session_scope
from greenference_persistence.orm import BusDeliveryORM, WorkflowEventORM
from greenference_protocol import (
    BuildContextRecord,
    BuildContextUploadRequest,
    BuildRequest,
    BuildRecord,
    CapacityUpdate,
    DeploymentCreateRequest,
    Heartbeat,
    MinerRegistration,
    NodeCapability,
    WorkloadCreateRequest,
    WorkloadSpec,
)


def test_builder_processes_accepted_build_events(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    build = builder.start_build(
        BuildRequest(
            image="greenference/echo:latest",
            context_uri="s3://greenference/builds/echo.zip",
        )
    )

    assert build.status == "accepted"
    pending = workflow_repository.list_events(subjects=["build.accepted"], statuses=["pending"])
    assert len(pending) == 1

    processed = builder.process_pending_events(limit=5)
    saved = repository.get_build(build.build_id)
    published_events = workflow_repository.list_events(subjects=["build.published"], statuses=["pending"])

    assert len(processed) == 1
    assert saved is not None
    assert saved.status == "published"
    assert saved.artifact_uri == "oci://registry.greenference.local:5000/greenference/echo:latest"
    assert saved.registry_repository == "greenference/echo"
    assert saved.image_tag == "latest"
    assert saved.artifact_digest is not None
    assert saved.registry_manifest_uri == f"{saved.artifact_uri}@{saved.artifact_digest}"
    assert saved.executor_name == "simulated-buildkit"
    assert len(published_events) == 1


def test_builder_records_failed_builds_and_image_history(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    successful = builder.start_build(
        BuildRequest(
            image="greenference/echo:latest",
            context_uri="s3://greenference/builds/echo.zip",
        )
    )
    failed = builder.start_build(
        BuildRequest(
            image="greenference/echo:latest",
            context_uri="git://greenference/builds/echo.git",
        )
    )

    processed = builder.process_pending_events(limit=10)
    history = builder.list_image_history("greenference/echo:latest")
    failed_record = repository.get_build(failed.build_id)
    failed_events = workflow_repository.list_events(subjects=["build.failed"], statuses=["pending"])

    assert len(processed) == 1
    assert failed_record is not None
    assert failed_record.status == "failed"
    assert failed_record.failure_reason == "unsupported build context scheme: git"
    assert {item.build_id for item in history} == {successful.build_id, failed.build_id}
    assert len(failed_events) == 1


def test_builder_persists_context_and_build_events(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    build = builder.start_build(
        BuildRequest(
            image="greenference/echo:latest",
            context_uri="s3://greenference/builds/echo.zip",
            dockerfile_path="docker/Inference.Dockerfile",
        )
    )
    builder.process_pending_events(limit=5)

    context = builder.get_build_context(build.build_id)
    events = builder.list_build_events(build.build_id)
    saved = builder.get_build(build.build_id)

    assert context is not None
    assert context.source_uri == "s3://greenference/builds/echo.zip"
    assert context.dockerfile_object_uri == "s3://greenference/builds/echo.zip.docker_Inference.Dockerfile"
    assert context.context_digest is not None
    assert context.staged_context_uri == f"s3://greenference-build-artifacts/contexts/{build.build_id}/context.tar.gz"
    assert context.context_manifest_uri == f"s3://greenference-build-artifacts/manifests/{build.build_id}.json"
    assert saved is not None
    assert saved.build_log_uri == f"s3://greenference-build-artifacts/build-logs/{build.build_id}.log"
    assert saved.registry_manifest_uri == f"{saved.artifact_uri}@{saved.artifact_digest}"
    assert saved.executor_name == "simulated-buildkit"
    assert saved.build_duration_seconds is not None
    assert [event.stage for event in events] == ["accepted", "job_started", "staging", "building", "publishing"]


def test_builder_accepts_inline_context_archive(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    build = builder.start_build(
        BuildRequest(
            image="greenference/sdk:latest",
            context_archive_b64=base64.b64encode(b"fake-archive").decode(),
            context_archive_name="sdk-context.zip",
        )
    )
    builder.process_pending_events(limit=5)

    saved = builder.get_build(build.build_id)
    context = builder.get_build_context(build.build_id)

    assert saved is not None
    assert saved.context_uri.startswith("file://")
    assert saved.status == "published"
    assert context is not None
    assert context.source_uri.startswith("file://")


def test_builder_uploads_context_archive_without_starting_build(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    uploaded = builder.upload_build_context(
        BuildContextUploadRequest(
            context_archive_b64=base64.b64encode(b"context-bytes").decode(),
            context_archive_name="sdk-context.zip",
        )
    )

    assert uploaded.context_uri.startswith("file://")
    assert uploaded.archive_name == "sdk-context.zip"
    assert uploaded.size_bytes == len(b"context-bytes")


def test_builder_retry_and_cleanup_recover_transient_failure(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    build = builder.start_build(
        BuildRequest(
            image="greenference/retry:latest",
            context_uri="s3://greenference/fail-once-object-store/retry.zip",
        )
    )
    first_pass = builder.process_pending_events(limit=5)
    failed = builder.get_build(build.build_id)

    assert first_pass == []
    assert failed is not None
    assert failed.status == "staging"
    assert failed.retry_count == 0

    cleaned = builder.cleanup_build(build.build_id)
    retried = builder.retry_build(build.build_id)
    second_pass = builder.process_pending_events(limit=5)
    attempts = builder.build_attempts(build.build_id)
    saved = builder.get_build(build.build_id)

    assert cleaned.cleanup_status == "completed"
    assert retried.retry_count == 1
    assert len(second_pass) == 1
    assert saved is not None
    assert saved.status == "published"
    assert len(attempts) >= 2
    assert attempts[-1]["status"] == "published"


def test_builder_persists_attempts_logs_and_cancellation(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    build = builder.start_build(
        BuildRequest(
            image="greenference/cancel:latest",
            context_uri="s3://greenference/builds/cancel.zip",
        )
    )
    cancelled = builder.cancel_build(build.build_id)
    processed = builder.process_pending_events(limit=5)
    attempt = builder.get_build_attempt(build.build_id, 1)
    logs = builder.list_build_logs(build.build_id)

    assert cancelled.status == "cancelled"
    assert processed == []
    assert attempt is None
    assert any(item.stage == "cancelled" for item in logs)


def test_live_object_store_creates_bucket_on_missing_head() -> None:
    settings = RuntimeSettings(
        service_name="greenference-builder",
        database_url="sqlite+pysqlite:///:memory:",
        build_execution_mode="live",
        object_store_endpoint="http://minio:9000",
        object_store_bucket="greenference-build-artifacts",
    )
    adapter = S3CompatibleObjectStoreAdapter(settings)
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, key: str, body: bytes | None = None, content_type: str | None = None):
        calls.append((method, key))
        if method == "HEAD":
            raise BuilderExecutionError(
                "object store request failed status=404 target=http://minio:9000/greenference-build-artifacts",
                operation="object_store:head",
                failure_class="object_store_failure",
                retryable=False,
            )
        return None

    adapter._request = fake_request  # type: ignore[method-assign]
    context = BuildContextRecord(
        build_id="build-live",
        source_uri="s3://greenference/builds/live.zip",
        normalized_context_uri="s3://greenference/builds/live.zip",
        dockerfile_path="Dockerfile",
        dockerfile_object_uri="s3://greenference/builds/live.zip.Dockerfile",
        context_digest="sha256:test",
    )
    build = BuildRecord(
        build_id="build-live",
        image="greenference/live:latest",
        context_uri=context.source_uri,
        dockerfile_path="Dockerfile",
    )

    staged = adapter.stage_context(build, context)

    assert staged.context.staged_context_uri is not None
    assert calls[:2] == [("HEAD", ""), ("PUT", "")]
    assert ("PUT", f"contexts/{build.build_id}/context.json") in calls


def test_builder_marks_unexpected_runtime_errors_failed(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    monkeypatch.setenv("GREENFERENCE_REGISTRY_URL", "http://registry.greenference.local:5000")
    repository = BuilderRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    builder = BuilderService(repository, workflow_repository=workflow_repository)

    build = builder.start_build(
        BuildRequest(
            image="greenference/runtime-error:latest",
            context_uri="s3://greenference/builds/runtime.zip",
        )
    )

    def explode(build_record: BuildRecord, context_record: BuildContextRecord):
        raise RuntimeError("unexpected stage failure")

    monkeypatch.setattr(builder.object_store, "stage_context", explode)

    processed = builder.process_pending_events(limit=5)
    saved = builder.get_build(build.build_id)
    failed_events = workflow_repository.list_events(subjects=["build.accepted"], statuses=["failed"])

    assert processed == []
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_class == "builder_runtime_error"
    assert saved.retry_exhausted is True
    assert len(failed_events) == 0


def test_control_plane_fails_expired_leases_and_emits_event() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)

    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    repository.upsert_capacity(
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
                )
            ],
        )
    )
    workload = repository.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="timeout-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = control_plane.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    assert deployment.state.value == "pending"

    scheduled = control_plane.process_pending_events()
    assert len(scheduled["deployments"]) == 1
    assignment = repository.list_assignments("miner-a")[0]
    assignment.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    repository.save_assignment(assignment)

    expired = control_plane.process_timeouts()
    failed_events = workflow_repository.list_events(subjects=["deployment.failed"], statuses=["pending"])
    saved = repository.get_deployment(deployment.deployment_id)

    assert len(expired) == 1
    assert saved is not None
    assert saved.state.value == "failed"
    assert saved.last_error == "lease expired before deployment became ready"
    assert len(failed_events) == 1


def test_control_plane_exhausts_scheduler_retries_without_capacity(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)
    monkeypatch.setattr(settings, "deployment_request_retry_limit", 1)

    workload = repository.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="no-capacity-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 4},
            ).model_dump()
        )
    )
    deployment = control_plane.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))

    processed = control_plane.process_pending_events()
    saved = repository.get_deployment(deployment.deployment_id)

    assert processed["deployments"] == []
    assert saved is not None
    assert saved.state.value == "failed"
    assert saved.retry_exhausted is True
    assert saved.failure_class == "scheduler_failure"


def test_duplicate_deployment_request_events_do_not_duplicate_assignments() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)

    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    repository.upsert_capacity(
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
                )
            ],
        )
    )
    workload = repository.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="dedupe-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = control_plane.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))
    workflow_repository.publish(
        "deployment.requested",
        {
            "deployment_id": deployment.deployment_id,
            "workload_id": deployment.workload_id,
            "requested_instances": deployment.requested_instances,
        },
    )

    processed = control_plane.process_pending_events(limit=10)
    assignments = repository.list_assignments("miner-a")

    assert len(processed["deployments"]) == 1
    assert len(assignments) == 1
    assert assignments[0].deployment_id == deployment.deployment_id


def test_unhealthy_miner_requeues_deployment_to_another_miner() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)

    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5FminerA",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-b",
            payout_address="5FminerB",
            auth_secret="miner-b-secret",
            api_base_url="http://miner-b.local",
            validator_url="http://validator.local",
        )
    )
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-b", healthy=True))
    repository.upsert_capacity(
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
    repository.upsert_capacity(
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
    workload = repository.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="failover-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = control_plane.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))

    first_pass = control_plane.process_pending_events()
    scheduled = repository.get_deployment(deployment.deployment_id)
    assert len(first_pass["deployments"]) == 1
    assert scheduled is not None
    assert scheduled.hotkey == "miner-a"

    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=False))
    requeued = control_plane.process_unhealthy_miners()
    assert len(requeued) == 1

    pending = repository.get_deployment(deployment.deployment_id)
    assert pending is not None
    assert pending.state.value == "pending"
    assert pending.hotkey is None
    assert "unhealthy" in (pending.last_error or "")

    second_pass = control_plane.process_pending_events()
    failed_over = repository.get_deployment(deployment.deployment_id)
    assignments = repository.list_assignments(statuses=["assigned"])
    reassigned_events = workflow_repository.list_events(subjects=["deployment.reassigned"], statuses=["pending"])

    assert len(second_pass["deployments"]) == 1
    assert failed_over is not None
    assert failed_over.hotkey == "miner-b"
    assert failed_over.state.value == "scheduled"
    assert len(assignments) == 1
    assert assignments[0].hotkey == "miner-b"
    assert len(reassigned_events) == 1


def test_deployment_request_retries_until_capacity_is_available() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)

    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    workload = repository.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="retry-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = control_plane.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))

    first_pass = control_plane.process_pending_events()
    saved = repository.get_deployment(deployment.deployment_id)
    pending = workflow_repository.list_events(subjects=["deployment.requested"], statuses=["pending"])

    assert first_pass["deployments"] == []
    assert saved is not None
    assert saved.state.value == "pending"
    assert saved.retry_count == 1
    assert len(pending) == 1

    with session_scope(workflow_repository.session_factory) as session:
        event_row = session.get(WorkflowEventORM, pending[0].event_id)
        assert event_row is not None
        event_row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(event_row)
        delivery_row = session.scalar(select(BusDeliveryORM).where(BusDeliveryORM.event_id == pending[0].event_id))
        assert delivery_row is not None
        delivery_row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(delivery_row)

    repository.upsert_capacity(
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
                )
            ],
        )
    )

    second_pass = control_plane.process_pending_events()
    saved = repository.get_deployment(deployment.deployment_id)
    assignments = repository.list_assignments("miner-a")

    assert len(second_pass["deployments"]) == 1
    assert saved is not None
    assert saved.state.value == "scheduled"
    assert saved.retry_count == 1
    assert len(assignments) == 1


def test_deployment_request_fails_after_retry_budget_exhausted() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)

    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    workload = repository.upsert_workload(
        WorkloadSpec(
            **WorkloadCreateRequest(
                name="retry-budget-model",
                image="greenference/echo:latest",
                requirements={"gpu_count": 1},
            ).model_dump()
        )
    )
    deployment = control_plane.create_deployment(DeploymentCreateRequest(workload_id=workload.workload_id))

    for attempt in range(settings.deployment_request_retry_limit):
        processed = control_plane.process_pending_events()
        saved = repository.get_deployment(deployment.deployment_id)
        assert processed["deployments"] == []
        assert saved is not None
        if attempt < settings.deployment_request_retry_limit - 1:
                pending = workflow_repository.list_events(subjects=["deployment.requested"], statuses=["pending"])
                assert len(pending) == 1
                with session_scope(workflow_repository.session_factory) as session:
                    event_row = session.get(WorkflowEventORM, pending[0].event_id)
                    assert event_row is not None
                    event_row.available_at = datetime.now(UTC) - timedelta(seconds=1)
                    session.add(event_row)
                    delivery_row = session.scalar(
                        select(BusDeliveryORM).where(BusDeliveryORM.event_id == pending[0].event_id)
                    )
                    assert delivery_row is not None
                    delivery_row.available_at = datetime.now(UTC) - timedelta(seconds=1)
                    session.add(delivery_row)

    failed = repository.get_deployment(deployment.deployment_id)
    failed_events = workflow_repository.list_events(subjects=["deployment.requested"], statuses=["failed"])

    assert failed is not None
    assert failed.state.value == "failed"
    assert failed.retry_count == settings.deployment_request_retry_limit
    assert "after" in (failed.last_error or "")
    assert len(failed_events) == 1


def test_builder_uses_durable_bus_deliveries() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    bus = SubjectBus(database_url=shared_db, bootstrap=True, workflow_repository=workflow_repository)
    builder = BuilderService(
        BuilderRepository(database_url=shared_db, bootstrap=True),
        workflow_repository=workflow_repository,
        bus=bus,
    )

    build = builder.start_build(
        BuildRequest(
            image="greenference/echo:bus",
            context_uri="s3://greenference/builds/bus.zip",
        )
    )
    deliveries = bus.list_deliveries(consumer="builder-worker", subjects=["build.accepted"])
    assert len(deliveries) == 1
    assert deliveries[0].payload["build_id"] == build.build_id

    processed = builder.process_pending_events()
    completed = bus.list_deliveries(
        consumer="builder-worker",
        subjects=["build.accepted"],
        statuses=["completed"],
    )
    assert len(processed) == 1
    assert len(completed) == 1

from datetime import UTC, datetime, timedelta

from greenference_builder.application.services import BuilderService
from greenference_builder.infrastructure.repository import BuilderRepository
from greenference_control_plane.application.services import ControlPlaneService
from greenference_control_plane.infrastructure.repository import ControlPlaneRepository
from greenference_persistence import WorkflowEventRepository
from greenference_protocol import (
    BuildRequest,
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
    assert len(published_events) == 1


def test_control_plane_fails_expired_leases_and_emits_event() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    repository = ControlPlaneRepository(database_url=shared_db, bootstrap=True)
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    control_plane = ControlPlaneService(repository, workflow_repository=workflow_repository)

    repository.upsert_miner(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
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

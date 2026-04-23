import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

import pytest

from greencompute_builder.application.services import BuilderService
from greencompute_builder.infrastructure.repository import BuilderRepository
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure import inference_client as inference_client_module
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_miner_agent.application.services import MinerAgentService
from greencompute_miner_agent.infrastructure.repository import MinerAgentRepository
from greencompute_protocol import (
    BuildRequest,
    CapacityUpdate,
    ChatCompletionRequest,
    Heartbeat,
    MinerRegistration,
    NodeCapability,
    WorkloadCreateRequest,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.status = 200

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.fixture(autouse=True)
def _enable_runtime_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREENCOMPUTE_ALLOW_RUNTIME_FALLBACK", "true")


def _patch_upstream(monkeypatch: pytest.MonkeyPatch, miner: MinerAgentService) -> None:
    def fake_urlopen(target, timeout=None):  # type: ignore[no-untyped-def]
        path = urlparse(target.full_url).path
        if path.endswith("/healthz"):
            deployment_id = path.split("/")[2]
            return _FakeResponse({"status": "ok", "deployment_id": deployment_id})
        if not path.endswith("/v1/chat/completions"):
            raise HTTPError(target.full_url, 404, "not found", hdrs=None, fp=None)
        deployment_id = path.split("/")[2]
        payload = ChatCompletionRequest(**json.loads(target.data.decode()))
        response = miner.serve_chat_completion(deployment_id, payload)
        return _FakeResponse(response.model_dump(mode="json"))

    monkeypatch.setattr(inference_client_module.request, "urlopen", fake_urlopen)


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'greencompute-recovery.db'}"


def test_restart_recovery_preserves_workflows_and_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_url = _db_url(tmp_path)

    control_plane_a = ControlPlaneService(ControlPlaneRepository(database_url=database_url, bootstrap=True))
    builder_a = BuilderService(BuilderRepository(database_url=database_url, bootstrap=True))
    gateway_a = GatewayService(
        GatewayRepository(database_url=database_url, bootstrap=True),
        control_plane=control_plane_a,
        builder=builder_a,
    )

    gateway_a.start_build(
        BuildRequest(
            image="greencompute/recovery:latest",
            context_uri="s3://greenference/builds/recovery.zip",
        )
    )

    builder_b = BuilderService(BuilderRepository(database_url=database_url, bootstrap=False))
    published = builder_b.process_pending_events()
    assert len(published) == 1
    assert published[0].status == "published"

    workload = gateway_a.create_workload(
        WorkloadCreateRequest(
            name="recovery-model",
            image=published[0].image,
            requirements={"gpu_count": 1, "min_vram_gb_per_gpu": 40},
        )
    )
    deployment = gateway_a.create_deployment({"workload_id": workload.workload_id})

    control_plane_b = ControlPlaneService(ControlPlaneRepository(database_url=database_url, bootstrap=False))
    miner = MinerAgentService(
        MinerAgentRepository(state_path=str(tmp_path / "miner-recovery-state.json")),
        control_plane=control_plane_b,
        builder=builder_b,
    )
    _patch_upstream(monkeypatch, miner)

    miner.onboard(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
            auth_secret="miner-a-secret",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    miner.publish_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    miner.publish_capacity(
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
                    performance_score=1.25,
                )
            ],
        )
    )

    scheduled = control_plane_b.process_pending_events()
    assert len(scheduled["deployments"]) == 1
    assert miner.sync_leases("miner-a")
    reconciled = miner.reconcile_once("miner-a")
    assert len(reconciled) == 1

    gateway_b = GatewayService(
        GatewayRepository(database_url=database_url, bootstrap=False),
        control_plane=control_plane_b,
        builder=builder_b,
    )
    response = gateway_b.invoke_chat_completion(
        ChatCompletionRequest(
            model=workload.workload_id,
            messages=[{"role": "user", "content": "recover me"}],
        )
    )
    assert response.content.strip()
    assert response.deployment_id == deployment.deployment_id

    control_plane_c = ControlPlaneService(ControlPlaneRepository(database_url=database_url, bootstrap=False))
    persisted_usage = control_plane_c.process_pending_events()
    summary = control_plane_c.usage_summary()
    saved = control_plane_c.repository.get_deployment(deployment.deployment_id)

    assert len(persisted_usage["usage_records"]) == 1
    assert summary[deployment.deployment_id]["requests"] == 1.0
    assert saved is not None
    assert saved.state.value == "ready"
    assert saved.endpoint == f"http://miner-a.local/deployments/{deployment.deployment_id}"

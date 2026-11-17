"""A 9-GPU pod was accepted at POST /platform/workloads (the docs say 8) and
then failed with a misleading 503 no_capacity (external review, Nov 2026).
A rental runs on one machine, so asking for more GPUs than the largest
registered machine has is a 422 naming the limit, at create."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService, TooManyGpusError
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes
from greencompute_gateway.transport.errors import install_error_handlers
from greencompute_protocol import NodeCapability, WorkloadCreateRequest, WorkloadSpec


def _node(node_id, gpu_model, gpu_count):
    return NodeCapability(hotkey=f"hk-{node_id}", node_id=node_id, gpu_model=gpu_model, gpu_count=gpu_count,
                          available_gpus=gpu_count, vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256)


@pytest.fixture
def world(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=url, bootstrap=True))
    gw = GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True), control_plane=cp)
    nodes = [_node("a", "rtx4090", 8), _node("b", "rtx4090", 4), _node("c", "rtx5090", 2), _node("int", "rtx-a4000", 10)]
    monkeypatch.setattr(cp.repository, "list_nodes", lambda: nodes)
    monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090,5090")
    monkeypatch.setattr(routes, "service", gw)
    key = SimpleNamespace(key_id="k1", user_id="u1", admin=False, scopes=[])
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: key)
    app = FastAPI()
    app.include_router(routes.router)
    install_error_handlers(app)
    return SimpleNamespace(gw=gw, cp=cp, nodes=nodes, key=key, client=TestClient(app))


def _pod(world, gpu_count, models=("rtx4090",)):
    return world.client.post("/platform/workloads", json={
        "name": "job", "image": "pytorch", "kind": "pod",
        "requirements": {"gpu_count": gpu_count, "supported_gpu_models": list(models)}})


def test_the_limit_is_the_largest_machine_of_that_gpu(world):
    assert world.gw.max_gpus_per_machine(["rtx4090"]) == 8
    assert world.gw.max_gpus_per_machine(["RTX 5090"]) == 2
    # An internal family (not publicly rented) never counts, even when asked for.
    assert world.gw.max_gpus_per_machine(["rtx-a4000"]) == 8
    assert world.gw.max_gpus_per_machine([]) == 8


def test_nine_gpus_is_a_422_with_the_limit(world):
    r = _pod(world, 9)
    assert r.status_code == 422, r.text
    err = r.json()["detail"][0]
    assert err["loc"] == ["body", "requirements", "gpu_count"] and err["ctx"]["le"] == 8
    assert "at most 8 GPUs" in err["msg"] and r.json()["error"]["code"] == "validation_error"
    assert _pod(world, 8).status_code == 200
    assert _pod(world, 3, models=("rtx5090",)).status_code == 422  # 5090 boxes here have 2


def test_with_no_machine_registered_the_default_is_8(world, monkeypatch):
    monkeypatch.setattr(world.cp.repository, "list_nodes", lambda: [])
    assert world.gw.max_gpus_per_machine(["rtx4090"]) == 8
    monkeypatch.setenv("GREENCOMPUTE_MAX_GPUS_PER_MACHINE", "4")
    assert world.gw.max_gpus_per_machine(["rtx4090"]) == 4


def test_an_admin_may_ask_for_more(world):
    world.key.admin = True
    assert _pod(world, 10, models=("rtx-a4000",)).status_code == 200


def test_an_old_workload_with_too_many_gpus_is_refused_at_deploy_not_with_503(world):
    wl = world.cp.repository.upsert_workload(WorkloadSpec(**WorkloadCreateRequest(
        name="big", image="pytorch", kind="pod",
        requirements={"gpu_count": 12, "supported_gpu_models": ["rtx4090"]}).model_dump(), owner_user_id="u1"))
    with pytest.raises(TooManyGpusError):
        world.gw.create_deployment({"workload_id": wl.workload_id}, user_id="u1")
    r = world.client.post("/platform/deployments", json={"workload_id": wl.workload_id})
    assert r.status_code == 422 and r.json()["detail"][0]["ctx"]["le"] == 8

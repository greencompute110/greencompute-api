"""One answer to "how many GPUs can be rented right now", and a create that
fails at once when it is zero.

An external API user ran 13 rentals through the API (Oct 2026). The site and
`available_now` said cards were free while every create failed with "no
compatible miner capacity", and each failure took ~10 scheduler retries to
surface. The pool was summing the miners' raw heartbeat over every non-stale
node; the scheduler subtracts committed reservations and skips drained,
unhealthy and cooling-down nodes. Now both read the same function, and the
gateway refuses a create (503 + Retry-After) that the scheduler could not
place this instant.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService, NoCapacityError
from greencompute_gateway.transport import routes
from greencompute_protocol import (
    CapacityUpdate,
    DeploymentCreateRequest,
    Heartbeat,
    MinerRegistration,
    NodeCapability,
    WorkloadCreateRequest,
    WorkloadSpec,
)


def _node(node_id, hotkey="m", gpu_model="rtx4090", gpu_count=8, available=8):
    return NodeCapability(
        hotkey=hotkey, node_id=node_id, gpu_model=gpu_model, gpu_count=gpu_count,
        available_gpus=available, vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256,
    )


def _pod(gpu_count=1, supported=("rtx4090",)):
    return WorkloadSpec(
        **WorkloadCreateRequest(
            name="rental", image="img", kind="pod",
            requirements={
                "gpu_count": gpu_count, "min_vram_gb_per_gpu": 24, "cpu_cores": 1,
                "memory_gb": 1, "supported_gpu_models": list(supported),
            },
        ).model_dump()
    )


def _free(cp):
    return {m: c["available_gpus"] for m, c in cp.capacity_by_model().items()}


def _places(cp, workload):
    return cp.placement_preview(workload)[0]


@pytest.fixture
def cp():
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    svc = ControlPlaneService(repository=repo)
    svc.update_capacity(CapacityUpdate(hotkey="m", nodes=[_node("n1")]))
    svc.record_heartbeat(Heartbeat(hotkey="m", healthy=True))
    return svc


# --- the control-plane's count ----------------------------------------------


def test_a_fresh_live_node_offers_all_its_gpus(cp):
    assert _free(cp) == {"rtx4090": 8}
    assert _places(cp, _pod(gpu_count=8))


def test_committed_reservations_come_off_even_while_the_heartbeat_still_says_free(cp):
    # Placement holds 3 GPUs for a deployment the miner hasn't reported yet, so
    # its heartbeat still says 8. The scheduler would hand out 5; so do we.
    assert cp.repository.reserve_gpus("dep-1", "m", "n1", 3, capacity_gpus=8)
    assert _free(cp) == {"rtx4090": 5}
    assert _places(cp, _pod(gpu_count=5))
    assert not _places(cp, _pod(gpu_count=6))


def test_a_full_node_offers_nothing(cp):
    assert cp.repository.reserve_gpus("dep-1", "m", "n1", 8, capacity_gpus=8)
    assert _free(cp) == {"rtx4090": 0}
    assert not _places(cp, _pod())


@pytest.mark.parametrize("how", ["drained", "unhealthy", "stale_heartbeat", "stale_node"])
def test_nodes_the_scheduler_skips_are_not_counted(cp, how):
    if how == "drained":
        cp.register_miner(MinerRegistration(
            hotkey="m", payout_address="5F", auth_secret="secret-8chars", api_base_url="http://m", validator_url="http://v",
        ))
        cp.drain_miner("m")
    elif how == "unhealthy":
        cp.record_heartbeat(Heartbeat(hotkey="m", healthy=False))
    elif how == "stale_heartbeat":
        cp.record_heartbeat(Heartbeat(hotkey="m", healthy=True, observed_at=datetime.now(UTC) - timedelta(days=1)))
    else:
        cp.update_capacity(CapacityUpdate(hotkey="m", nodes=[_node("n1")], observed_at=datetime.now(UTC) - timedelta(days=1)))
    assert _free(cp).get("rtx4090", 0) == 0
    assert not _places(cp, _pod())


def test_gpu_count_must_fit_on_one_machine(cp):
    # 4 + 4 free across two nodes is not 8 free for an 8-GPU rental.
    cp.update_capacity(CapacityUpdate(hotkey="m", nodes=[_node("n1"), _node("n2")]))
    assert cp.repository.reserve_gpus("d1", "m", "n1", 4, capacity_gpus=8)
    assert cp.repository.reserve_gpus("d2", "m", "n2", 4, capacity_gpus=8)
    assert _free(cp) == {"rtx4090": 8}
    assert cp.capacity_by_model()["rtx4090"]["max_gpus_per_deployment"] == 4
    assert _places(cp, _pod(gpu_count=4))
    assert not _places(cp, _pod(gpu_count=8))


def test_a_drained_miner_is_offline_not_busy(cp):
    # The 400/503 split and the free count come from the same node filter, so
    # a drained miner's GPU disappears from "online" rather than reading as
    # "rented, try again in a minute" forever.
    assert cp.online_gpu_models() == ["rtx4090"]
    cp.register_miner(MinerRegistration(
        hotkey="m", payout_address="5F", auth_secret="secret-8chars", api_base_url="http://m", validator_url="http://v",
    ))
    cp.drain_miner("m")
    assert cp.online_gpu_models() == []
    assert cp.capacity_by_model() == {}


def test_placement_still_uses_the_same_filter(cp):
    # _assign_lease and the public count agree: once placement has taken the
    # last GPU, the count is 0 and the next placement fails.
    wl = cp.repository.upsert_workload(_pod(gpu_count=8))
    assert cp._assign_lease(wl, "dep-1") is not None
    assert _free(cp) == {"rtx4090": 0}
    assert cp._assign_lease(wl, "dep-2") is None


# --- the gateway refuses what can't be placed ---------------------------------


def _gateway(can_place, free, balance_cents=10_000):
    workload = _pod()
    capacity = {m: {"available_gpus": n, "max_gpus_per_deployment": n} for m, n in free.items()}
    cp = SimpleNamespace(
        repository=SimpleNamespace(get_workload=lambda wid: workload),
        online_gpu_models=lambda: ["rtx4090"],
        placement_preview=lambda w: (can_place, capacity),
        create_deployment=lambda body: SimpleNamespace(created=True),
    )
    svc = SimpleNamespace(control_plane=cp, _user_can_access_workload=lambda w, u: True)
    return svc, workload


def test_a_full_fleet_is_refused_at_create_time(monkeypatch):
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    monkeypatch.setattr(BillingRepository, "get_balance", lambda self, uid: 10_000)
    svc, wl = _gateway(can_place=False, free={"rtx4090": 0})
    with pytest.raises(NoCapacityError) as exc:
        GatewayService.create_deployment(svc, {"workload_id": wl.workload_id}, user_id="u1")
    assert exc.value.available_gpus == {"rtx4090": 0}
    assert exc.value.gpu_count == 1
    assert "every rtx4090 is rented right now" in exc.value.message
    assert "nothing was created" in exc.value.message


def test_free_gpus_mean_the_create_goes_through(monkeypatch):
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    monkeypatch.setattr(BillingRepository, "get_balance", lambda self, uid: 10_000)
    svc, wl = _gateway(can_place=True, free={"rtx4090": 3})
    assert GatewayService.create_deployment(svc, {"workload_id": wl.workload_id}, user_id="u1").created


def test_no_balance_is_reported_before_no_capacity(monkeypatch):
    # Top up first, then you're ready the moment a GPU frees up.
    from greencompute_gateway.application.services import InsufficientBalanceForRentalError
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    monkeypatch.setattr(BillingRepository, "get_balance", lambda self, uid: 0)
    svc, wl = _gateway(can_place=False, free={"rtx4090": 0})
    with pytest.raises(InsufficientBalanceForRentalError):
        GatewayService.create_deployment(svc, {"workload_id": wl.workload_id}, user_id="u1")


def test_admins_bypass_the_capacity_check():
    svc, wl = _gateway(can_place=False, free={"rtx4090": 0})
    assert GatewayService.create_deployment(svc, {"workload_id": wl.workload_id}, user_id="admin", admin=True).created


def test_the_message_says_when_the_gpus_exist_but_the_workload_does_not_fit(monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    capacity = {"rtx4090": {"available_gpus": 6, "max_gpus_per_deployment": 4}}
    exc = NoCapacityError(requested=["RTX 4090"], gpu_count=8, capacity=capacity, min_vram_gb=24)
    assert exc.gpu_models == ["rtx4090"]
    assert exc.max_gpus_per_deployment == {"rtx4090": 4}
    assert exc.message.startswith(
        "6 rtx4090 GPU(s) are free, but no single machine can take this workload "
        "(it needs 8 GPU(s) together with 24 GB VRAM each; the most free on one machine is 4)"
    )


def test_the_route_answers_503_with_retry_after(monkeypatch):
    from greencompute_gateway.application.services import GatewayDeploymentCreateRequest

    monkeypatch.setattr(routes, "_enforce_key_limits", lambda *a, **k: None)

    def refuse(*a, **k):
        raise NoCapacityError(requested=["rtx4090"], gpu_count=2,
                              capacity={"rtx4090": {"available_gpus": 0, "max_gpus_per_deployment": 0}})

    monkeypatch.setattr(routes.service, "create_deployment", refuse)
    key = SimpleNamespace(user_id="u1", key_id="k1", admin=False)
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(GatewayDeploymentCreateRequest(workload_id="w"), key)
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "60"}
    detail = exc.value.detail
    assert detail["code"] == "no_capacity"
    assert detail["requested_gpus"] == 2
    assert detail["available_gpus"] == {"rtx4090": 0}
    assert detail["max_gpus_per_deployment"] == {"rtx4090": 0}
    assert detail["retry_after_seconds"] == 60
    assert "nothing was created" in detail["hint"]


def test_a_refused_create_is_not_remembered_as_idempotent(monkeypatch):
    """The 503 creates nothing, so a retry with the same Idempotency-Key must
    run again (and succeed once a GPU is free), not replay the refusal."""
    from fastapi import Response

    from greencompute_gateway.infrastructure.repository import GatewayRepository

    monkeypatch.setattr(routes.service, "repository", GatewayRepository(database_url="sqlite:///:memory:", bootstrap=True))
    key = SimpleNamespace(user_id="u1", key_id="k1", admin=False)
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            raise HTTPException(status_code=503, detail={"code": "no_capacity"})
        return {"deployment_id": "d1"}

    with pytest.raises(HTTPException):
        routes._with_idempotency(key, "create_deployment", "same-key", {"workload_id": "w"}, Response(), run)
    assert routes._with_idempotency(key, "create_deployment", "same-key", {"workload_id": "w"}, Response(), run) == {"deployment_id": "d1"}
    assert len(calls) == 2

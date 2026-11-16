"""Each GPU model is "available", "almost fully rented", "fully rented" or
"on request", worked out from the scheduler's count against the model's live
fleet, and that state is all the public ever sees (review team doc "Demand
and Availability Marketing", Nov 2026): no free count, no fleet size, on the
price list, the GPU pool, the MCP tool, a refused create, an alert's event or
email, or a resume that doesn't fit."""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.domain.availability import (
    ALMOST_FULLY_RENTED,
    AVAILABLE,
    FULLY_RENTED,
    ON_REQUEST,
    availability_of,
    availability_state,
)
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.transport import routes
from greencompute_protocol import (
    CapacityUpdate,
    DeploymentRecord,
    DeploymentState,
    Heartbeat,
    NodeCapability,
    WorkloadCreateRequest,
    WorkloadSpec,
)

COUNTS = {"available_gpus", "max_gpus_per_deployment", "total_gpus", "free_gpus", "fleet_gpus"}


@pytest.fixture(autouse=True)
def default_threshold(monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT", raising=False)


# --- the state ------------------------------------------------------------------


@pytest.mark.parametrize("free, fleet, state", [
    (0, 10, FULLY_RENTED),         # none free
    (1, 10, ALMOST_FULLY_RENTED),
    (2, 10, ALMOST_FULLY_RENTED),  # exactly 20%
    (3, 10, AVAILABLE),            # just above
    (10, 10, AVAILABLE),
    (1, 5, ALMOST_FULLY_RENTED),   # exactly 20% of a small fleet
    (2, 5, AVAILABLE),
    (3, 15, ALMOST_FULLY_RENTED),  # 0.2 * 15 is 3.0000000000000004 in floats
    (4, 15, AVAILABLE),
    (1, 1, AVAILABLE),             # one box with its only GPU free
    (0, 1, FULLY_RENTED),
    (0, 0, ON_REQUEST),            # no fleet: nothing to call rented out
    (3, 0, ON_REQUEST),
])
def test_boundaries(free, fleet, state):
    assert availability_state(free, fleet) == state


def test_a_model_with_no_live_node_is_on_request():
    assert availability_of(None) == ON_REQUEST
    assert availability_of({}) == ON_REQUEST


def test_the_threshold_is_set_by_env(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT", "50")
    assert availability_state(5, 10) == ALMOST_FULLY_RENTED
    assert availability_state(6, 10) == AVAILABLE
    monkeypatch.setenv("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT", "0")
    assert availability_state(1, 10) == AVAILABLE  # off: only none-free is shown
    assert availability_state(0, 10) == FULLY_RENTED


@pytest.mark.parametrize("bad", ["lots", "-5", "250", "nan"])
def test_a_bad_threshold_falls_back_to_20_percent(monkeypatch, bad):
    monkeypatch.setenv("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT", bad)
    assert availability_state(2, 10) == ALMOST_FULLY_RENTED
    assert availability_state(3, 10) == AVAILABLE


# --- from the scheduler's own count -------------------------------------------


def _node(node_id, gpu_model="rtx4090", gpu_count=8, available=8):
    return NodeCapability(
        hotkey="m", node_id=node_id, gpu_model=gpu_model, gpu_count=gpu_count,
        available_gpus=available, vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256,
    )


@pytest.fixture
def cp():
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    svc = ControlPlaneService(repository=repo)
    svc.update_capacity(CapacityUpdate(hotkey="m", nodes=[_node("n1")]))
    svc.update_capacity(CapacityUpdate(hotkey="m", nodes=[_node("n1"), _node("n2")]))
    svc.record_heartbeat(Heartbeat(hotkey="m", healthy=True))
    return svc


def _state(cp, model="rtx4090"):
    return availability_of(cp.capacity_by_model().get(model))


def test_the_fleet_is_what_the_scheduler_would_use(cp):
    assert cp.capacity_by_model()["rtx4090"]["total_gpus"] == 16
    assert _state(cp) == AVAILABLE
    # 13 of 16 placed: 3 free is 18.75%, almost fully rented.
    assert cp.repository.reserve_gpus("d1", "m", "n1", 8, capacity_gpus=8)
    assert cp.repository.reserve_gpus("d2", "m", "n2", 5, capacity_gpus=8)
    assert _state(cp) == ALMOST_FULLY_RENTED
    assert cp.repository.reserve_gpus("d3", "m", "n2", 3, capacity_gpus=8)
    assert _state(cp) == FULLY_RENTED


def test_a_drained_fleet_is_on_request_not_rented_out(cp):
    from greencompute_protocol import MinerRegistration

    cp.register_miner(MinerRegistration(
        hotkey="m", payout_address="5F", auth_secret="secret-8chars", api_base_url="http://m", validator_url="http://v",
    ))
    cp.drain_miner("m")
    assert _state(cp) == ON_REQUEST


# --- the price list -------------------------------------------------------------


@pytest.fixture
def pricing(monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    monkeypatch.setattr(routes, "_public_inference_model_names", lambda: [])

    def priced(capacity):
        monkeypatch.setattr(routes, "_capacity_by_model", lambda: capacity)
        return {g["gpu_model"]: g for g in routes.public_pricing()["gpu_rental"]["gpus"]}

    return priced


@pytest.mark.parametrize("free, state", [(0, FULLY_RENTED), (1, ALMOST_FULLY_RENTED), (2, ALMOST_FULLY_RENTED),
                                         (3, AVAILABLE), (10, AVAILABLE)])
def test_available_now_is_false_exactly_when_fully_rented(pricing, free, state):
    g = pricing({"rtx4090": {"available_gpus": free, "max_gpus_per_deployment": free, "total_gpus": 10}})["rtx4090"]
    assert g["availability"] == state
    assert g["online"] is True
    assert g["available_now"] is (state != FULLY_RENTED)


def test_a_priced_gpu_with_no_fleet_is_on_request(pricing):
    # The 5090 while it has no node taking rentals: never "rented out".
    g = pricing({"rtx4090": {"available_gpus": 8, "max_gpus_per_deployment": 8, "total_gpus": 8}})["rtx5090"]
    assert (g["online"], g["available_now"], g["availability"]) == (False, False, ON_REQUEST)


def test_a_gpu_left_out_of_public_rentals_is_not_listed(pricing, monkeypatch):
    # Production pauses the 5090 this way; the website then shows it as on
    # request itself, whatever its nodes are doing.
    monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090")
    gpus = pricing({"rtx5090": {"available_gpus": 8, "max_gpus_per_deployment": 8, "total_gpus": 8}})
    assert set(gpus) == {"rtx4090"}
    assert gpus["rtx4090"]["availability"] == ON_REQUEST


def test_no_counts_in_the_price_list(pricing):
    gpus = pricing({
        "rtx4090": {"available_gpus": 3, "max_gpus_per_deployment": 2, "total_gpus": 16},
        "rtx5090": {"available_gpus": 0, "max_gpus_per_deployment": 0, "total_gpus": 8},
    })
    for g in gpus.values():
        # Exactly these fields: a new one has to be added here on purpose.
        assert set(g) == {"gpu_model", "online", "available_now", "availability", "vram_gb", "cents_per_gpu_hour",
                          "usd_per_gpu_hour", "usd_per_gpu_hour_paid_in_alpha"}


def test_the_documented_price_list_has_no_counts_either():
    from greencompute_gateway.transport import schemas

    fields = schemas.GpuPrice.model_json_schema()["properties"]
    assert not COUNTS & set(fields)
    assert set(fields["availability"]["enum"]) == {AVAILABLE, ALMOST_FULLY_RENTED, FULLY_RENTED, ON_REQUEST}


def test_the_pool_and_the_price_list_agree(monkeypatch):
    """The website's banners read the pool; agents read the price list. Both
    are the same function of the same count."""
    from greencompute_control_plane.transport import routes as cp_routes

    nodes = [SimpleNamespace(node_id="a", gpu_model="rtx4090", vram_gb_per_gpu=24, gpu_count=8, available_gpus=8),
             SimpleNamespace(node_id="b", gpu_model="rtx4090", vram_gb_per_gpu=24, gpu_count=8, available_gpus=8)]
    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    monkeypatch.setattr(cp_routes.service.repository, "list_nodes", lambda: nodes)
    monkeypatch.setattr(cp_routes.service, "_is_node_stale", lambda node: False)
    monkeypatch.setattr(routes, "_public_inference_model_names", lambda: [])
    monkeypatch.setattr(routes, "_capacity_by_model", lambda: cp_routes.service.capacity_by_model())
    for free_a, free_b in [(8, 8), (2, 1), (0, 3), (0, 0)]:
        snapshot = [(nodes[0], free_a), (nodes[1], free_b)]
        monkeypatch.setattr(cp_routes.service, "schedulable_capacity", lambda s=snapshot: s)
        [pool] = cp_routes.gpu_pool(authorization=None, x_api_key=None)
        price = {g["gpu_model"]: g for g in routes.public_pricing()["gpu_rental"]["gpus"]}["rtx4090"]
        assert pool["availability"] == price["availability"]
        assert price["available_now"] is (pool["availability"] != FULLY_RENTED)


# --- what a customer is told when there's no room --------------------------------


def _pod(gpu_count=1):
    return WorkloadSpec(**WorkloadCreateRequest(
        name="rental", image="img", kind="pod",
        requirements={"gpu_count": gpu_count, "min_vram_gb_per_gpu": 24, "cpu_cores": 1, "memory_gb": 1,
                      "supported_gpu_models": ["rtx4090"]},
    ).model_dump())


def test_a_rental_that_never_found_a_gpu_says_join_the_waitlist(cp, monkeypatch):
    from greencompute_control_plane.config import settings

    assert cp.repository.reserve_gpus("d1", "m", "n1", 8, capacity_gpus=8)
    assert cp.repository.reserve_gpus("d2", "m", "n2", 8, capacity_gpus=8)
    wl = cp.repository.upsert_workload(_pod())
    dep = cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id="u1"))
    monkeypatch.setattr(cp, "bus", SimpleNamespace(mark_failed=lambda *a, **k: None,
                                                   mark_completed=lambda *a, **k: None,
                                                   publish=lambda *a, **k: None))
    event = SimpleNamespace(payload={"deployment_id": dep.deployment_id}, delivery_id="e1",
                            attempts=settings.deployment_request_retry_limit)
    cp._process_deployment_request(event)
    failed = cp.repository.get_deployment(dep.deployment_id)
    assert failed.state == DeploymentState.FAILED
    # The old prefix stays (analytics and the website match on it) ...
    assert failed.last_error.startswith("no compatible miner capacity available after")
    # ... and the customer is told what to do, with no count of what is free.
    assert "Join the waitlist" in failed.last_error and "support@green-compute.com" in failed.last_error
    assert failed.failure_class == "scheduler_failure"


def test_a_resume_that_does_not_fit_gives_no_free_count(cp):
    wl = cp.repository.upsert_workload(_pod(gpu_count=2))
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id="u1", hotkey="m", node_id="n1", state=DeploymentState.SUSPENDED,
    ))
    assert cp.repository.reserve_gpus("other", "m", "n1", 7, capacity_gpus=8)
    with pytest.raises(ValueError) as exc:
        cp.resume_deployment(dep.deployment_id)
    message = str(exc.value)
    assert "insufficient capacity to resume" in message and "support@green-compute.com" in message
    # One GPU is free on that machine; that isn't said.
    assert "available" not in message and "effective" not in message

"""A private model that was serving and then FAILED its health checks is
redeployed at once (a new deployment of the same workload), instead of
staying failed until its owner deletes and redeploys it. Shared catalog
models are Flux's to replace."""
import threading
from datetime import UTC, datetime, timedelta

import pytest
from greencompute_control_plane.application import services as cp_services
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.config import settings
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import UserORM
from greencompute_protocol import (
    CapacityUpdate,
    DeploymentRecord,
    DeploymentState,
    Heartbeat,
    NodeCapability,
    WorkloadKind,
    WorkloadSpec,
)


CLOCK = [1000.0]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Failures only count spread over a window (30 s): each `fail` moves
    this clock on, the way real failures are spread out in time."""
    monkeypatch.setattr(cp_services, "_monotonic", lambda: CLOCK[0])

    def advance(seconds):
        CLOCK[0] += seconds
    return advance


@pytest.fixture
def cp(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'replace.db'}"
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)  # BillingRepository() reads balances from here
    repo = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", email="u1@x.io", balance_credits=1000))
        s.add(UserORM(user_id="broke", username="broke", email="b@x.io", balance_credits=10))
    seen = datetime.now(UTC)  # a machine with room for a replacement
    repo.upsert_heartbeat(Heartbeat(hotkey="hk", healthy=True, observed_at=seen))
    repo.upsert_capacity(CapacityUpdate(hotkey="hk", observed_at=seen, nodes=[NodeCapability(
        hotkey="hk", node_id="n1", gpu_model="rtx4090", gpu_count=8, available_gpus=6, vram_gb_per_gpu=24,
        cpu_cores=64, memory_gb=256, observed_at=seen)]))
    return ControlPlaneService(repository=repo)


def private_model(cp, owner="u1", gpus=1):
    return cp.repository.upsert_workload(WorkloadSpec(
        name="meta-llama-Llama-3.1-8B-Instruct", image="vllm", kind=WorkloadKind.INFERENCE, owner_user_id=owner,
        requirements={"gpu_count": gpus}, runtime={"model_identifier": "meta-llama/Llama-3.1-8B-Instruct"},
    ))


def serving(cp, workload, owner="u1", **fields):
    values = {"ready_at": datetime.now(UTC) - timedelta(minutes=20), "hourly_rate_cents": 40, "hotkey": "hk",
              "node_id": "n1", "name": "my-llama", "created_by_key_id": "k1", **fields}
    return cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id=owner, state=DeploymentState.READY, ready_instances=1,
        **values,
    ))


def fail(cp, deployment):
    """Fail its health checks (spread over the window), then let the
    control-plane worker act on the replacement it asked for."""
    for _ in range(settings.deployment_health_failure_threshold):
        CLOCK[0] += 20
        cp.record_deployment_health_failure(deployment.deployment_id, "upstream timed out for deployment=x")
    cp.process_pending_events()
    return cp.repository.get_deployment(deployment.deployment_id)


def others(cp, workload, deployment):
    return [d for d in cp.repository.list_deployments_for_workload(workload.workload_id)
            if d.deployment_id != deployment.deployment_id]


def test_a_private_model_that_stops_answering_is_redeployed(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    failed = fail(cp, dep)
    assert failed.state == DeploymentState.FAILED and failed.failure_class == "health_check_failure"
    [new] = others(cp, workload, dep)
    assert new.state == DeploymentState.PENDING  # placement takes it from here
    assert (new.owner_user_id, new.name, new.created_by_key_id) == ("u1", "my-llama", "k1")
    assert f"Restarted automatically as deployment {new.deployment_id}" in failed.last_error
    assert cp.repository.get_deployment(dep.deployment_id).last_error == failed.last_error
    assert new.hourly_rate_cents <= 40  # never dearer than what its owner paid


def test_two_requests_failing_at_once_replace_it_once(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    for _ in range(settings.deployment_health_failure_threshold - 1):
        CLOCK[0] += 20
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    CLOCK[0] += 20
    start = threading.Barrier(4)

    def hit():
        start.wait()
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cp.process_pending_events()
    cp.process_pending_events()
    assert len(others(cp, workload, dep)) == 1


def test_one_that_never_served_isnt_replaced(cp):
    """A start problem (wrong model, no room for its context) would only fail again."""
    workload = private_model(cp)
    dep = serving(cp, workload, ready_at=None)
    fail(cp, dep)
    assert others(cp, workload, dep) == []


def test_shared_catalog_models_are_left_to_flux(cp):
    workload = cp.repository.upsert_workload(WorkloadSpec(
        workload_id="catalog-qwen", name="qwen", image="vllm", kind=WorkloadKind.INFERENCE, public=True,
        requirements={"gpu_count": 1}, metadata={"managed_by": "flux", "catalog_model_id": "qwen"}))
    dep = serving(cp, workload, owner=None)
    fail(cp, dep)
    assert others(cp, workload, dep) == []


def test_not_while_another_of_its_deployments_is_live(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    starting = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.STARTING))
    fail(cp, dep)
    assert [d.deployment_id for d in others(cp, workload, dep)] == [starting.deployment_id]


def test_at_most_a_few_times_an_hour_then_its_owner_is_told(cp):
    workload = private_model(cp)
    replaced = 0
    dep = serving(cp, workload)
    for _ in range(settings.private_model_auto_replace_per_hour + 1):
        failed = fail(cp, dep)
        live = [d for d in others(cp, workload, dep) if d.state == DeploymentState.PENDING]
        if not live:
            break
        replaced += 1
        # The replacement comes up and serves, then crashes too.
        cp.repository.update_deployment(live[0].model_copy(update={
            "state": DeploymentState.READY, "ready_at": datetime.now(UTC), "hourly_rate_cents": 40}))
        dep = cp.repository.get_deployment(live[0].deployment_id)
    assert replaced == settings.private_model_auto_replace_per_hour
    assert "Not restarted automatically: it failed 4 times in the last hour" in failed.last_error


def test_not_without_an_hour_of_credit(cp):
    workload = private_model(cp, owner="broke")
    dep = serving(cp, workload, owner="broke")
    failed = fail(cp, dep)
    assert others(cp, workload, dep) == []
    assert "Not restarted automatically: the balance doesn't cover an hour of it" in failed.last_error


def test_the_replacement_gets_what_is_left_of_its_runtime_cap(cp):
    workload = private_model(cp)
    dep = serving(cp, workload, max_runtime_minutes=60, ready_at=datetime.now(UTC) - timedelta(minutes=45))
    fail(cp, dep)
    [new] = others(cp, workload, dep)
    assert new.max_runtime_minutes in (14, 15)


def test_none_once_its_runtime_cap_is_used_up(cp):
    workload = private_model(cp)
    dep = serving(cp, workload, max_runtime_minutes=30, ready_at=datetime.now(UTC) - timedelta(minutes=45))
    fail(cp, dep)
    assert others(cp, workload, dep) == []


def test_it_can_be_turned_off(cp, monkeypatch):
    monkeypatch.setattr(settings, "private_model_auto_replace_per_hour", 0)
    workload = private_model(cp)
    dep = serving(cp, workload)
    fail(cp, dep)
    assert others(cp, workload, dep) == []


# --- review H1: failures must be spread over a window ------------------------------------


def test_failures_at_the_same_moment_count_once(cp, clock):
    workload = private_model(cp)
    dep = serving(cp, workload)
    for _ in range(10):  # ten requests failing together (one brief stall)
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    got = cp.repository.get_deployment(dep.deployment_id)
    assert got.state == DeploymentState.READY and got.health_check_failures == 1


def test_a_15_second_node_restart_with_traffic_fails_nothing(cp, clock):
    """Connection refused for 15 s while a request arrives every second."""
    workload = private_model(cp)
    dep = serving(cp, workload)
    for _ in range(16):
        cp.record_deployment_health_failure(dep.deployment_id, "upstream connection failed")
        clock(1)
    got = cp.repository.get_deployment(dep.deployment_id)
    assert got.state == DeploymentState.READY and got.health_check_failures == 2
    cp.clear_deployment_health_failures(dep.deployment_id)  # back: a request succeeds
    clock(5)
    cp.record_deployment_health_failure(dep.deployment_id, "x")
    assert cp.repository.get_deployment(dep.deployment_id).health_check_failures == 1  # a new streak


def test_failing_for_the_whole_window_fails_it(cp, clock):
    workload = private_model(cp)
    dep = serving(cp, workload)
    started = CLOCK[0]
    for _ in range(40):
        cp.record_deployment_health_failure(dep.deployment_id, "x")
        if cp.repository.get_deployment(dep.deployment_id).state == DeploymentState.FAILED:
            break
        clock(1)
    assert cp.repository.get_deployment(dep.deployment_id).state == DeploymentState.FAILED
    assert CLOCK[0] - started >= settings.deployment_health_failure_window_seconds


# --- review M2: the same checks as a deploy, off the request path ------------------------


def test_the_failing_request_only_asks_for_it(cp):
    """The replacement is made by the control-plane worker, not in the
    request that failed the model."""
    workload = private_model(cp)
    dep = serving(cp, workload)
    for _ in range(settings.deployment_health_failure_threshold):
        CLOCK[0] += 20
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    assert others(cp, workload, dep) == []  # nothing yet: asked for
    cp.process_pending_events()
    assert len(others(cp, workload, dep)) == 1


def test_a_failure_asking_for_it_never_fails_the_caller(cp, monkeypatch):
    workload = private_model(cp)
    dep = serving(cp, workload)

    publish = cp.bus.publish

    def broken(subject, payload):
        if subject == "deployment.replacement_requested":
            raise RuntimeError("bus down")
        return publish(subject, payload)
    monkeypatch.setattr(cp.bus, "publish", broken)
    for _ in range(settings.deployment_health_failure_threshold):
        CLOCK[0] += 20
        out = cp.record_deployment_health_failure(dep.deployment_id, "x")  # must not raise
    assert out.state == DeploymentState.FAILED


@pytest.mark.parametrize("limits, says", [
    ({"max_active_rentals": 1}, "limit on rentals at once is reached"),
    ({"daily_spend_limit_usd": 1.0}, "spend limit for 24 hours is reached"),
])
def test_not_past_the_accounts_own_limits(cp, limits, says):
    from greencompute_persistence.orm import LedgerEntryORM

    workload = private_model(cp)
    dep = serving(cp, workload)
    other = cp.repository.upsert_workload(WorkloadSpec(name="pod", image="ubuntu", kind=WorkloadKind.POD,
                                                       owner_user_id="u1", requirements={"gpu_count": 1}))
    serving(cp, other)  # another rental running
    with session_scope(cp.repository.session_factory) as s:
        user = s.get(UserORM, "u1")
        if "max_active_rentals" in limits:
            user.max_active_rentals = limits["max_active_rentals"]
        else:
            user.daily_spend_limit_cents = int(limits["daily_spend_limit_usd"] * 100)
            s.add(LedgerEntryORM(entry_id="e1", user_id="u1", kind="usage", amount_cents=-500, balance_after=500,
                                 reference_id="r", description="spent", created_at=datetime.now(UTC)))
    failed = fail(cp, dep)
    assert others(cp, workload, dep) == []
    assert says in failed.last_error


def test_not_for_a_blocked_account(cp, monkeypatch):
    from greencompute_gateway.application import sanctions

    monkeypatch.setattr(sanctions, "account_blocked", lambda sf, user: "OFAC")
    workload = private_model(cp)
    dep = serving(cp, workload)
    failed = fail(cp, dep)
    assert others(cp, workload, dep) == []
    assert "the account can't start rentals" in failed.last_error


def test_without_room_it_retries_then_says_so(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    seen = datetime.now(UTC)
    cp.repository.upsert_capacity(CapacityUpdate(hotkey="hk", observed_at=seen, nodes=[NodeCapability(
        hotkey="hk", node_id="n1", gpu_model="rtx4090", gpu_count=8, available_gpus=0, vram_gb_per_gpu=24,
        cpu_cores=64, memory_gb=256, observed_at=seen)]))
    failed = fail(cp, dep)
    assert "Not restarted" not in (failed.last_error or "")  # not yet: it tries again in a moment
    pending = cp.bus.list_deliveries(consumer="control-plane-worker", subjects=["deployment.replacement_requested"],
                                     statuses=["pending"])
    assert len(pending) == 1
    assert cp.replace_failed_private_model(dep.deployment_id, final_attempt=True) is not None
    failed = cp.repository.get_deployment(dep.deployment_id)
    assert "no GPU it fits on is free right now" in failed.last_error and others(cp, workload, dep) == []


# --- review round 2, M2: a replacement is never lost --------------------------------------


def test_a_replacement_left_processing_by_a_restart_is_retried(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    for _ in range(settings.deployment_health_failure_threshold):
        CLOCK[0] += 20
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    # The worker claimed it and then died (a restart) before finishing.
    claimed = cp.bus.claim_pending("control-plane-worker", ["deployment.replacement_requested"], limit=10)
    assert len(claimed) == 1
    cp.recover_inflight_events(stale_after_seconds=0)
    cp.process_pending_events()
    assert len(others(cp, workload, dep)) == 1


def test_when_making_it_keeps_failing_its_owner_is_told(cp, monkeypatch):
    workload = private_model(cp)
    dep = serving(cp, workload)

    def broken(*a, **k):
        raise RuntimeError("database hiccup")
    monkeypatch.setattr(cp, "replace_failed_private_model", broken)
    for _ in range(settings.deployment_health_failure_threshold):
        CLOCK[0] += 20
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    for _ in range(cp._MAX_EVENT_ATTEMPTS):
        _make_due(cp)
        cp.process_pending_events()
    failed = cp.repository.get_deployment(dep.deployment_id)
    assert "Not restarted automatically: making its replacement failed" in failed.last_error


def _make_due(cp):
    """Retries wait (backoff); bring them forward."""
    from greencompute_persistence import session_scope as _scope
    from greencompute_persistence.orm import BusDeliveryORM

    with _scope(cp.repository.session_factory) as s:
        for row in s.query(BusDeliveryORM).all():
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)

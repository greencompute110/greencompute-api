"""Fleets (#13 keep N running, #3/#31 start N as GPUs free up): the fleet loop
fills a target count of rentals from one workload, replaces members that stop
(under the same name), waits when GPUs, credit or key limits run out, never
replaces a paused member, and pauses itself if members keep failing."""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application import fleets
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import UserORM
from greencompute_protocol import (
    APIKeyRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    WorkloadCreateRequest,
    WorkloadSpec,
)


class Capacity:
    """How many more deployments the fake scheduler would place."""

    def __init__(self, free=100):
        self.free = free

    def preview(self, workload):
        ok = self.free > 0
        return ok, {"rtx4090": {"available_gpus": max(self.free, 0), "max_gpus_per_deployment": max(self.free, 0)}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'f.sqlite'}"
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=url, bootstrap=True))
    gw = GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True), control_plane=cp)
    gw._billing_repo = BillingRepository(database_url=url, bootstrap=True)
    with session_scope(gw._billing_repo.session_factory) as s:
        s.add(UserORM(user_id="ann", username="ann", email="ann@x.io", balance_credits=10_000))
    cap = Capacity()
    real_create = cp.create_deployment

    def create(request):
        cap.free -= 1  # a placed rental takes a GPU
        return real_create(request)

    monkeypatch.setattr(cp, "online_gpu_models", lambda: ["rtx4090"])
    monkeypatch.setattr(cp, "placement_preview", cap.preview)
    monkeypatch.setattr(cp, "create_deployment", create)
    wl = cp.repository.upsert_workload(WorkloadSpec(**WorkloadCreateRequest(
        name="miner", image="img", kind="pod",
        requirements={"gpu_count": 1, "min_vram_gb_per_gpu": 24, "cpu_cores": 1, "memory_gb": 1,
                      "supported_gpu_models": ["rtx4090"]},
    ).model_dump()).model_copy(update={"owner_user_id": "ann"}))
    return SimpleNamespace(gw=gw, cp=cp, cap=cap, wl=wl)


def _create(w, **body):
    body = {"workload_id": w.wl.workload_id, "target": 3, "name_prefix": "miner", **body}
    return fleets.create_fleet(w.gw, user_id="ann", key_id=body.pop("key_id", None), admin=False, body=body)


def _view(w, fleet_id):
    return fleets.view(w.gw, fleets.get_fleet(w.gw, fleet_id, "ann"))


def _set(w, deployment_id, state, **kw):
    dep = w.cp.repository.get_deployment(deployment_id)
    dep.state = state
    for k, v in kw.items():
        setattr(dep, k, v)
    w.cp.repository.update_deployment(dep)


def test_a_fleet_fills_its_target_with_named_members(world):
    f = _create(world)
    v = _view(world, f.fleet_id)
    assert sorted(m["name"] for m in v["members"]) == ["miner-01", "miner-02", "miner-03"]
    assert v["status"] == "3 of 3 started; 0 running." and v["state"] == "active"
    assert v["usd_per_hour_at_target"] == 1.2  # 3 × $0.40
    names = {world.cp.repository.get_deployment(m["deployment_id"]).name for m in v["members"]}
    assert names == {"miner-01", "miner-02", "miner-03"}  # what the pods see as GC_DEPLOYMENT_NAME


def test_a_stopped_member_is_replaced_under_the_same_name(world):
    f = _create(world)
    second = next(m for m in _view(world, f.fleet_id)["members"] if m["name"] == "miner-02")
    _set(world, second["deployment_id"], DeploymentState.FAILED)
    fleets.reconcile(world.gw, f.fleet_id)
    live = [m for m in _view(world, f.fleet_id)["members"] if m["state"] not in ("failed", "terminated")]
    assert sorted(m["name"] for m in live) == ["miner-01", "miner-02", "miner-03"]
    assert next(m for m in live if m["name"] == "miner-02")["deployment_id"] != second["deployment_id"]


def test_no_free_gpu_means_wait_and_fill_later(world):
    world.cap.free = 2
    f = _create(world)
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 2
    assert v["status"].startswith("2 of 3 started. Waiting for a free GPU")
    world.cap.free = 5  # GPUs free up
    fleets.reconcile(world.gw, f.fleet_id)
    assert len(_view(world, f.fleet_id)["members"]) == 3


def test_short_credit_means_wait(world):
    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.get(UserORM, "ann").balance_credits = 30  # less than an hour at $0.40
    f = _create(world)
    v = _view(world, f.fleet_id)
    assert v["members"] == [] and "Waiting for credit" in v["status"]


def test_a_paused_member_is_not_replaced(world):
    f = _create(world)
    first = _view(world, f.fleet_id)["members"][0]
    _set(world, first["deployment_id"], DeploymentState.SUSPENDED)
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 3 and v["paused"] == 1


def test_start_n_once_does_not_replace_and_completes(world):
    f = _create(world, target=2, replace=False)
    members = _view(world, f.fleet_id)["members"]
    for m in members:  # both ran, then ended
        _set(world, m["deployment_id"], DeploymentState.TERMINATED, ready_at=fleets._now())
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert v["state"] == "completed" and v["status"] == "Done: all 2 have run."
    assert len(v["members"]) == 2  # nothing new started


def test_start_n_once_still_retries_ones_that_failed_to_start(world):
    f = _create(world, target=2, replace=False)
    first = _view(world, f.fleet_id)["members"][0]
    _set(world, first["deployment_id"], DeploymentState.FAILED, last_error="image pull failed")
    fleets.reconcile(world.gw, f.fleet_id)
    live = [m for m in _view(world, f.fleet_id)["members"] if m["state"] != "failed"]
    assert len(live) == 2


def test_members_that_keep_failing_pause_the_fleet_until_resumed(world):
    f = _create(world, target=1)
    for _ in range(fleets.FAILURES_BEFORE_PAUSE):
        newest = _view(world, f.fleet_id)["members"][0]
        _set(world, newest["deployment_id"], DeploymentState.FAILED, last_error="manifest unknown")
        fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert v["state"] == "paused"
    assert "the last 3 rentals failed before they were ready (manifest unknown)" in v["status"]
    before = len(v["members"])
    fleets.reconcile(world.gw, f.fleet_id)  # paused: nothing new
    assert len(_view(world, f.fleet_id)["members"]) == before
    fleets.update_fleet(world.gw, f.fleet_id, user_id="ann", admin=False, body={"paused": False})
    v = _view(world, f.fleet_id)
    assert v["state"] == "active" and len(v["members"]) == before + 1


def test_lowering_the_target_stops_extras_not_ready_ones_first(world):
    f = _create(world, target=3)
    members = sorted(_view(world, f.fleet_id)["members"], key=lambda m: m["slot"])
    for m in members[:2]:
        _set(world, m["deployment_id"], DeploymentState.READY)  # 01 and 02 running, 03 still starting
    fleets.update_fleet(world.gw, f.fleet_id, user_id="ann", admin=False, body={"target": 2})
    states = {m["name"]: m["state"] for m in _view(world, f.fleet_id)["members"]}
    assert states["miner-03"] == "terminated"
    assert states["miner-01"] == states["miner-02"] == "ready"


def test_the_keys_rental_limit_is_respected(world):
    key = world.gw.repository.save_api_key(APIKeyRecord(name="agent", user_id="ann", max_active_rentals=2))
    f = _create(world, target=3, key_id=key.key_id)
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 2
    assert "may have 2 rentals running at once" in v["status"]


def test_a_deleted_key_pauses_the_fleet(world):
    key = world.gw.repository.save_api_key(APIKeyRecord(name="agent", user_id="ann"))
    f = _create(world, target=1, key_id=key.key_id)
    world.gw.repository.delete_api_key(key.key_id)
    first = _view(world, f.fleet_id)["members"][0]
    _set(world, first["deployment_id"], DeploymentState.FAILED)
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert v["state"] == "paused" and "API key that created this fleet was deleted" in v["status"]


def test_stopping_a_fleet_stops_its_rentals_unless_asked_not_to(world):
    f = _create(world, target=2)
    v = fleets.view(world.gw, fleets.stop_fleet(world.gw, f.fleet_id, user_id="ann", admin=False, keep_running=False))
    assert v["state"] == "stopped" and {m["state"] for m in v["members"]} == {"terminated"}
    g = _create(world, target=1)
    v = fleets.view(world.gw, fleets.stop_fleet(world.gw, g.fleet_id, user_id="ann", admin=False, keep_running=True))
    assert v["state"] == "stopped" and v["members"][0]["state"] == "pending"
    fleets.fleet_once(world.gw)  # stopped fleets are left alone
    assert len(_view(world, g.fleet_id)["members"]) == 1


@pytest.mark.parametrize("body, says", [
    ({"target": 0}, "target must be"),
    ({"target": 65}, "target must be"),
    ({"name_prefix": "-bad"}, "name_prefix"),
    ({"resume_on_topup": True}, "save_on_exhaustion"),
])
def test_bad_requests_are_refused(world, body, says):
    with pytest.raises(fleets.FleetError, match=says):
        _create(world, **body)


def test_models_are_not_fleets(world):
    model = world.cp.repository.upsert_workload(WorkloadSpec(name="llm", image="vllm", kind="inference",
                                                             owner_user_id="ann"))
    with pytest.raises(fleets.FleetError, match="pod workloads"):
        fleets.create_fleet(world.gw, user_id="ann", key_id=None, admin=False,
                            body={"workload_id": model.workload_id, "target": 1})


def test_another_accounts_fleet_is_not_found(world):
    f = _create(world, target=1)
    with pytest.raises(KeyError):
        fleets.get_fleet(world.gw, f.fleet_id, "bob")


def test_fleets_need_the_rentals_scope():
    from greencompute_gateway.domain.scopes import required_scope

    assert required_scope("POST", "/platform/fleets") == "rentals"
    assert required_scope("GET", "/platform/fleets") == "read"


def test_default_prefixes_are_pool_friendly():
    assert fleets.default_prefix("My miner!") == "My-miner"
    assert fleets.default_prefix("__") == "rig"


def test_two_fleets_of_one_workload_never_share_names(world):
    a = fleets.create_fleet(world.gw, user_id="ann", key_id=None, admin=False,
                            body={"workload_id": world.wl.workload_id, "target": 1})
    b = fleets.create_fleet(world.gw, user_id="ann", key_id=None, admin=False,
                            body={"workload_id": world.wl.workload_id, "target": 1})
    assert a.name_prefix != b.name_prefix and a.name_prefix.startswith("miner-")
    with pytest.raises(fleets.FleetError, match="already uses the name_prefix"):
        _create(world, name_prefix=a.name_prefix)


def test_lowering_the_target_of_a_paused_fleet_still_stops_the_extras(world):
    f = _create(world, target=3)
    fleets.update_fleet(world.gw, f.fleet_id, user_id="ann", admin=False, body={"paused": True})
    fleets.update_fleet(world.gw, f.fleet_id, user_id="ann", admin=False, body={"target": 1})
    v = _view(world, f.fleet_id)
    assert v["state"] == "paused"
    assert sum(1 for m in v["members"] if m["state"] not in ("terminated", "failed")) == 1


def test_a_member_that_ran_still_counts_after_its_row_is_gone(world):
    f = _create(world, target=1, replace=False)
    first = _view(world, f.fleet_id)["members"][0]
    _set(world, first["deployment_id"], DeploymentState.READY, ready_at=fleets._now())
    fleets.reconcile(world.gw, f.fleet_id)  # seen ready: recorded as having run
    world.cp.repository.delete_workload  # (rows can disappear with their workload)
    with session_scope(world.gw.repository.session_factory) as s:
        from greencompute_persistence.orm import DeploymentORM

        s.delete(s.get(DeploymentORM, first["deployment_id"]))
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert v["state"] == "completed" and len(v["members"]) == 1  # nothing new started


def test_an_unexpected_error_starting_a_member_is_shown_not_hidden(world, monkeypatch):
    def broken(*a, **k):
        raise ValueError("control plane said no")

    monkeypatch.setattr(world.gw, "create_deployment", broken)
    f = _create(world, target=2)
    v = _view(world, f.fleet_id)
    assert v["state"] == "active"
    assert "Couldn't start a rental (control plane said no)" in v["status"]


def test_the_locks_hold_one_connection_from_lock_to_unlock():
    """On Postgres the advisory lock must be released on the connection that
    took it; a Session returns its connection to the pool on commit."""
    calls = []

    class Conn:
        def __init__(self):
            self.id = object()

        def execute(self, stmt, params):
            calls.append((str(stmt), self.id))
            return SimpleNamespace(scalar=lambda: True)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = Conn()
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: conn)
    with fleets._advisory_lock(engine, 7, wait=True) as got:
        assert got
    assert [c[0].split("(")[0] for c in calls] == ["SELECT pg_advisory_lock", "SELECT pg_advisory_unlock"]
    assert calls[0][1] is calls[1][1]  # same connection


def test_a_lock_not_taken_is_not_released():
    calls = []
    conn = SimpleNamespace(
        execute=lambda stmt, params: calls.append(str(stmt)) or SimpleNamespace(scalar=lambda: False),
        commit=lambda: None,
    )
    conn.__enter__ = lambda: conn
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    class Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    engine.connect = lambda: Ctx()
    with fleets._advisory_lock(engine, 7, wait=False) as got:
        assert got is False
    assert calls == ["SELECT pg_try_advisory_lock(:k)"]


# --- review follow-ups: money and concurrency ---------------------------------


def test_the_fleet_only_starts_what_the_balance_covers_an_hour_of(world):
    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.get(UserORM, "ann").balance_credits = 100  # $1: two hours-of-one-GPU at $0.40, not three
    f = _create(world, target=3)
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 2
    assert "Waiting for credit: another rental needs $0.40" in v["status"]


def test_two_passes_at_once_do_not_start_the_same_members_twice(world):
    import threading

    world.cap.free = 0
    f = _create(world, target=4)  # nothing free: waits
    world.cap.free = 50
    threads = [threading.Thread(target=fleets.reconcile, args=(world.gw, f.fleet_id)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    members = _view(world, f.fleet_id)["members"]
    assert len(members) == 4
    assert sorted(m["slot"] for m in members) == [1, 2, 3, 4]


def test_start_n_once_keeps_names_of_members_that_ran(world):
    f = _create(world, target=2, replace=False)
    for m in _view(world, f.fleet_id)["members"]:
        _set(world, m["deployment_id"], DeploymentState.TERMINATED, ready_at=fleets._now())
    fleets.update_fleet(world.gw, f.fleet_id, user_id="ann", admin=False, body={"target": 3})
    names = sorted(m["name"] for m in _view(world, f.fleet_id)["members"])
    assert names == ["miner-01", "miner-02", "miner-03"]  # not a second miner-01


def test_a_member_started_but_not_recorded_is_adopted(world):
    from greencompute_protocol import DeploymentRecord

    f = _create(world, target=2)
    stray = world.cp.repository.create_deployment(DeploymentRecord(
        workload_id=world.wl.workload_id, owner_user_id="ann", name="miner-05"))
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert stray.deployment_id in {m["deployment_id"] for m in v["members"]}
    # Now three live members for a target of two: the extra (not ready, highest slot) is stopped.
    fleets.reconcile(world.gw, f.fleet_id)
    live = [m for m in _view(world, f.fleet_id)["members"] if m["state"] not in ("terminated", "failed")]
    assert len(live) == 2


def test_create_is_idempotent(world, monkeypatch):
    from fastapi import Response

    from greencompute_gateway.transport import routes, schemas

    monkeypatch.setattr(routes, "service", world.gw)
    monkeypatch.setattr(routes, "require_api_key",
                        lambda *a, **k: SimpleNamespace(user_id="ann", key_id="k1", admin=False))
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    body = schemas.FleetCreate(workload_id=world.wl.workload_id, target=2)
    first = routes.create_fleet(body, Response(), authorization="Bearer k", x_api_key=None, idempotency_key="same")
    replay = Response()
    again = routes.create_fleet(body, replay, authorization="Bearer k", x_api_key=None, idempotency_key="same")
    assert again["fleet_id"] == first["fleet_id"] and replay.headers.get("Idempotent-Replayed") == "true"
    assert len(fleets.list_fleets(world.gw, "ann")) == 1



def test_the_status_says_how_things_stand_now(world):
    f = _create(world, target=2)
    for m in _view(world, f.fleet_id)["members"]:
        _set(world, m["deployment_id"], DeploymentState.READY)
    assert _view(world, f.fleet_id)["status"] == "All 2 running."  # without waiting for the next pass


def test_the_accounts_own_limits_are_respected(world):
    """Limits set for the whole account (GET/PUT /platform/account/limits)
    hold for fleets as for single rentals: the fleet waits and says why."""
    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.get(UserORM, "ann").max_active_rentals = 2
    f = _create(world, target=3)
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 2
    assert "this account may have 2 rentals running at once" in v["status"] and "Account limits" in v["status"]
    with session_scope(world.gw._billing_repo.session_factory) as s:
        user = s.get(UserORM, "ann")
        user.max_active_rentals, user.daily_spend_limit_cents = None, 100
        from datetime import UTC, datetime
        from uuid import uuid4

        from greencompute_persistence.orm import LedgerEntryORM

        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id="ann", amount_cents=-100, balance_after=0,
                             kind="usage", created_at=datetime.now(UTC)))
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 2 and "has spent its daily limit ($1.00" in v["status"]


# --- members paused at a spend limit -------------------------------------------------------


def _ledger(world, cents, *, hours_ago=0.0):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from greencompute_persistence.orm import LedgerEntryORM

    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id="ann", amount_cents=-cents, balance_after=0,
                             kind="usage", created_at=datetime.now(UTC) - timedelta(hours=hours_ago)))


def _age_out_spending(world):
    """Move every charge out of the 24-hour window."""
    from datetime import UTC, datetime, timedelta

    from greencompute_persistence.orm import LedgerEntryORM

    with session_scope(world.gw._billing_repo.session_factory) as s:
        for row in s.query(LedgerEntryORM).filter(LedgerEntryORM.user_id == "ann"):
            row.created_at = datetime.now(UTC) - timedelta(hours=25)


@pytest.fixture
def capped(world, monkeypatch):
    """A fleet of 2, both running, then paused at the account's $5 cap. Resume
    is faked (no node): it records and flips the member to READY."""
    resumed = []

    def fake_resume(deployment_id):
        dep = world.cp.repository.get_deployment(deployment_id)
        dep.state = DeploymentState.READY
        world.cp.repository.update_deployment(dep)
        resumed.append(deployment_id)
        return dep

    monkeypatch.setattr(world.cp, "resume_deployment", fake_resume)
    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.get(UserORM, "ann").daily_spend_limit_cents = 500
    return resumed


def _two_paused(world, *, replace=True):
    f = _create(world, target=2, replace=replace)
    members = _view(world, f.fleet_id)["members"]
    for m in members:
        _set(world, m["deployment_id"], DeploymentState.READY)
    _ledger(world, 500)
    paused = world.cp.stop_rentals_over_account_limits()
    assert sorted(paused) == sorted(m["deployment_id"] for m in members)
    return f, sorted(m["deployment_id"] for m in members)


def test_a_keep_n_running_fleet_resumes_its_members_paused_at_a_spend_limit(world, capped):
    """The reviewer's case: 2 members paused at the cap; once the spending has
    left the window they are resumed, not replaced."""
    f, ids = _two_paused(world)
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert capped == [] and len(v["members"]) == 2  # at the cap: neither resumed nor replaced
    assert "2 paused at a spend limit" in v["status"] and "room for an hour" in v["status"]
    _age_out_spending(world)
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert sorted(capped) == ids and len(v["members"]) == 2  # the same two, back
    assert {m["state"] for m in v["members"]} == {"ready"} and v["status"] == "All 2 running."


def test_no_flapping_a_fleet_waits_until_there_is_room_for_an_hour(world, capped):
    f, ids = _two_paused(world)
    _age_out_spending(world)
    _ledger(world, 450)  # 50c of room: one member (40c/h) fits for an hour, two don't
    fleets.reconcile(world.gw, f.fleet_id)
    assert len(capped) == 1 and capped[0] in ids
    v = _view(world, f.fleet_id)
    assert "1 paused at a spend limit" in v["status"]
    _ledger(world, 60)  # now over: nothing more comes back
    fleets.reconcile(world.gw, f.fleet_id)
    assert len(capped) == 1


def test_a_start_n_once_fleet_leaves_its_paused_members_alone(world, capped):
    f, _ids = _two_paused(world, replace=False)
    _age_out_spending(world)
    fleets.reconcile(world.gw, f.fleet_id)
    v = _view(world, f.fleet_id)
    assert capped == [] and len(v["members"]) == 2
    assert {m["state"] for m in v["members"]} == {"suspended"} and "resume them on My Rentals" in v["status"]


def test_a_fleet_skips_a_member_the_auto_resume_sweep_already_brought_back(world, capped):
    from greencompute_protocol import LeaseAssignment

    f, ids = _two_paused(world)
    _age_out_spending(world)
    world.cp.repository.save_assignment(LeaseAssignment(
        deployment_id=ids[0], workload_id=world.wl.workload_id, hotkey="hk", node_id="n1", status="active"))
    fleets.reconcile(world.gw, f.fleet_id)
    assert ids[0] not in capped  # being resumed already: not resumed twice


@pytest.fixture(autouse=True)
def _fresh_full_counts():
    fleets._FULL_SEEN.clear()
    yield
    fleets._FULL_SEEN.clear()


def _resume_raising(world, monkeypatch, failing: dict):
    """cp.resume_deployment raising failing[deployment_id] (an exception) for
    those, and resuming the rest as the `capped` fixture does."""
    real = world.cp.resume_deployment

    def resume(deployment_id):
        if deployment_id in failing:
            raise failing[deployment_id]
        return real(deployment_id)

    monkeypatch.setattr(world.cp, "resume_deployment", resume)


def _save(world, deployment_id, keep=True):
    dep = world.cp.repository.get_deployment(deployment_id)
    dep.save_on_exhaustion = keep
    world.cp.repository.update_deployment(dep)


def test_a_saved_member_is_never_deleted_for_a_full_machine(world, capped, monkeypatch):
    """The reviewer's probe: a saved member, its machine's report still says
    no room (stale): it is kept, pass after pass, and resumed once the
    machine has room."""
    from greencompute_control_plane.application.services import MachineFull

    f, ids = _two_paused(world)
    _save(world, ids[0])
    _age_out_spending(world)
    failing = {ids[0]: MachineFull(f"insufficient capacity to resume deployment {ids[0]}")}
    _resume_raising(world, monkeypatch, failing)
    monkeypatch.setattr(fleets, "FULL_SECONDS_BEFORE_REPLACING", 0)
    for _ in range(fleets.FULL_PASSES_BEFORE_REPLACING + 2):
        fleets.reconcile(world.gw, f.fleet_id)
    assert world.cp.repository.get_deployment(ids[0]).state == DeploymentState.SUSPENDED  # disk kept
    v = _view(world, f.fleet_id)
    assert len(v["members"]) == 2  # no replacement started
    from greencompute_persistence.orm import FleetORM

    with session_scope(world.gw.repository.session_factory) as s:
        status = s.get(FleetORM, f.fleet_id).status_reason
    assert "1 paused at a spend limit waiting for room on its machine (saved disks are kept" in status
    failing.clear()  # the machine reports room now
    fleets.reconcile(world.gw, f.fleet_id)
    assert ids[0] in capped and world.cp.repository.get_deployment(ids[0]).state == DeploymentState.READY


def test_an_unsaved_member_is_replaced_only_after_its_machine_stays_full(world, capped, monkeypatch):
    from greencompute_control_plane.application.services import MachineFull

    f, ids = _two_paused(world)
    _age_out_spending(world)
    _resume_raising(world, monkeypatch, {ids[0]: MachineFull("insufficient capacity")})
    clock = {"t": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    for _ in range(fleets.FULL_PASSES_BEFORE_REPLACING):
        fleets.reconcile(world.gw, f.fleet_id)  # 3 passes, but within a few seconds
        clock["t"] += 5
    assert world.cp.repository.get_deployment(ids[0]).state == DeploymentState.SUSPENDED
    clock["t"] += fleets.FULL_SECONDS_BEFORE_REPLACING
    fleets.reconcile(world.gw, f.fleet_id)  # full for 3+ passes and 2+ minutes: replaced
    gone = world.cp.repository.get_deployment(ids[0])
    assert gone.state == DeploymentState.TERMINATED and gone.last_error == fleets.REPLACED_REASON
    live = [m for m in _view(world, f.fleet_id)["members"] if m["state"] not in ("terminated", "failed")]
    assert len(live) == 2 and ids[0] not in {m["deployment_id"] for m in live}


def test_a_member_whose_machine_is_gone_is_replaced_saved_or_not(world, capped, monkeypatch):
    """The limit has room, the machine is gone for good: even a saved member
    is replaced (its disk went with the machine), under the same name, with
    the reason in last_error rather than "user terminated"."""
    from greencompute_control_plane.application.services import MachineGone

    f, ids = _two_paused(world)
    _save(world, ids[0])
    _age_out_spending(world)
    _resume_raising(world, monkeypatch, {ids[0]: MachineGone(f"node n1 for deployment {ids[0]} is no longer registered")})
    fleets.reconcile(world.gw, f.fleet_id)
    gone = world.cp.repository.get_deployment(ids[0])
    assert gone.state == DeploymentState.TERMINATED and gone.last_error == fleets.REPLACED_REASON
    v = _view(world, f.fleet_id)
    live = [m for m in v["members"] if m["state"] not in ("terminated", "failed")]
    assert len(live) == 2 and ids[1] in capped
    replacement = next(m for m in live if m["deployment_id"] not in ids)
    assert replacement["name"] == next(m for m in v["members"] if m["deployment_id"] == ids[0])["name"]


def test_a_member_resumed_by_hand_meanwhile_is_not_deleted(world, capped, monkeypatch):
    """Right before deleting, the member and its lease are read again: a
    Resume pressed meanwhile (lease live) wins."""
    from greencompute_control_plane.application.services import MachineGone
    from greencompute_protocol import LeaseAssignment

    f, ids = _two_paused(world)
    _age_out_spending(world)
    world.cp.repository.save_assignment(LeaseAssignment(
        deployment_id=ids[0], workload_id=world.wl.workload_id, hotkey="hk", node_id="n1", status="suspended"))

    def racing(deployment_id):
        world.cp.repository.update_assignment_status(deployment_id, "active", reason="resume requested")  # by hand
        raise MachineGone("no longer registered")

    real = world.cp.resume_deployment
    monkeypatch.setattr(world.cp, "resume_deployment", lambda d: racing(d) if d == ids[0] else real(d))
    fleets.reconcile(world.gw, f.fleet_id)
    assert world.cp.repository.get_deployment(ids[0]).state == DeploymentState.SUSPENDED  # not deleted


def test_a_member_already_being_resumed_is_not_stopped(world, capped, monkeypatch):
    f, ids = _two_paused(world)
    _age_out_spending(world)

    def busy(deployment_id):
        raise ValueError(f"deployment {deployment_id} is already being resumed")

    monkeypatch.setattr(world.gw, "resume_deployment", lambda dep_id, **kw: busy(dep_id))
    fleets.reconcile(world.gw, f.fleet_id)
    assert {world.cp.repository.get_deployment(i).state for i in ids} == {DeploymentState.SUSPENDED}
    assert len(_view(world, f.fleet_id)["members"]) == 2  # nothing stopped, nothing started


def test_deleting_or_expiring_the_capped_key_does_not_lift_its_cap(world, capped):
    """The reviewer's probe: a fleet whose key's limit paused its members;
    the key is deleted (or expires); the window ages out: nothing resumes,
    the fleet pauses as it does for a deleted key."""
    from datetime import UTC, datetime, timedelta

    from greencompute_persistence.credentials import StoredAPIKey

    for how in ("deleted", "expired"):
        key = world.gw.repository.save_api_key(StoredAPIKey(name=f"agent-{how}", user_id="ann",
                                                            daily_spend_limit_usd=1.0))
        f = _create(world, target=2, key_id=key.key_id, name_prefix=f"k{how}")
        members = _view(world, f.fleet_id)["members"]
        for m in members:
            _set(world, m["deployment_id"], DeploymentState.READY)
            _ledger_for(world, m["deployment_id"], 100)
        assert sorted(world.cp.stop_rentals_over_key_limits()) == sorted(m["deployment_id"] for m in members)
        if how == "deleted":
            world.gw.repository.delete_api_key(key.key_id)
        else:
            world.gw.repository.save_api_key(key.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(hours=1)}))
        _age_out_spending(world)
        fleets.reconcile(world.gw, f.fleet_id)
        v = _view(world, f.fleet_id)
        assert capped == [] and v["state"] == "paused" and how.replace("deleted", "was deleted") in v["status"]
        assert {m["state"] for m in v["members"]} == {"suspended"}


def test_a_fleet_resumes_only_as_many_as_the_balance_covers_an_hour_of(world, capped):
    f, ids = _two_paused(world)
    _age_out_spending(world)
    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.get(UserORM, "ann").balance_credits = 50  # an hour of one member (40c), not two
    fleets.reconcile(world.gw, f.fleet_id)
    assert len(capped) == 1


def _ledger_for(world, deployment_id, cents):
    from datetime import UTC, datetime
    from uuid import uuid4

    from greencompute_persistence.orm import LedgerEntryORM

    with session_scope(world.gw._billing_repo.session_factory) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id="ann", amount_cents=-cents, balance_after=0,
                             kind="usage", reference_id=deployment_id, created_at=datetime.now(UTC)))

"""Fixes from an external agent-driven evaluation (2026-10).

A tester gave a fresh AI agent nothing but our homepage link and had it rent
RTX 4090s through the API, then compared the run with Lium. Each test below
pins one thing that agent tripped over. Most of them were invisible to a human
using the dashboard, which is why none of them had coverage.
"""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.domain.scheduler import PlacementPolicy, normalize_gpu_model
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.domain.gpu_catalog import matches_public_gpu, public_gpu_models
from greencompute_gateway.transport.routes import DEPLOYMENT_SECRET_FIELDS, public_pricing
from greencompute_protocol import (
    BuildRecord,
    CapacityUpdate,
    Heartbeat,
    DeploymentRecord,
    NodeCapability,
    WorkloadCreateRequest,
    WorkloadSpec,
)


def _workload(supported=None):
    return WorkloadSpec(
        **WorkloadCreateRequest(
            name="rental",
            image="img",
            requirements={
                "gpu_count": 1,
                "min_vram_gb_per_gpu": 24,
                "cpu_cores": 1,
                "memory_gb": 1,
                "supported_gpu_models": supported or [],
            },
        ).model_dump()
    )


def _node(gpu_model):
    return NodeCapability(
        hotkey="m", node_id=f"n-{gpu_model}", gpu_model=gpu_model, gpu_count=8,
        available_gpus=8, vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256,
    )


# --- 1. GPU names ------------------------------------------------------------


@pytest.mark.parametrize("requested", ["rtx-4090", "RTX 4090", "rtx_4090", "rtx4090"])
def test_any_spelling_of_the_4090_schedules_onto_a_4090_node(requested):
    # The tester's "rtx-4090" -- the exact string our supported-GPU endpoint
    # returned -- never scheduled, because the scheduler compared strings.
    ranked = PlacementPolicy().rank_nodes(_workload([requested]), [_node("rtx4090")])
    assert [r.node.node_id for r in ranked] == ["n-rtx4090"]


def test_a_different_gpu_still_does_not_match():
    ranked = PlacementPolicy().rank_nodes(_workload(["rtx-5090"]), [_node("rtx4090")])
    assert ranked == []


def test_normalizer_agrees_with_billing():
    from greencompute_protocol.billing_rates import _normalize_gpu_model

    for raw in ("RTX 4090", "rtx-4090", "Rtx_5090", "rtx5090"):
        assert normalize_gpu_model(raw) == _normalize_gpu_model(raw)


def test_supported_list_is_the_real_fleet_in_node_spelling(monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    models = public_gpu_models()
    assert models == ["rtx4090", "rtx5090"]
    # Never again advertise cards we have never had.
    assert not {"a100", "h100", "t4", "k80"} & set(models)


def test_internal_only_families_stay_hidden(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090")
    assert public_gpu_models() == ["rtx4090"]


def test_unknown_gpu_is_detected_up_front():
    assert matches_public_gpu(["RTX 4090"])
    assert not matches_public_gpu(["h100", "a100-80gb"])


# --- 2. The price in the API is the price ------------------------------------


@pytest.mark.parametrize(
    "supported,expected",
    [
        (["rtx-4090"], 40),            # one model: exact
        (["rtx4090", "rtx5090"], 70),  # several: the most it can cost
        ([], 70),                      # unconstrained: highest public rate
    ],
)
def test_creation_quotes_the_real_rate_not_a_placeholder(supported, expected):
    # Every deployment used to say hourly_rate_cents=10 at creation and then
    # bill 40 -- an agent budgeting from the response under-estimated by 4x.
    assert ControlPlaneService._quoted_hourly_rate_cents(_workload(supported)) == expected


def test_there_is_no_phantom_deployment_fee():
    # `deployment_fee_usd` used to read 0.3 on every rental; nothing debited it.
    assert ControlPlaneService._estimate_deployment_fee(_workload(["rtx4090"]), 1) == 0.0


# --- 3. No credentials in deployment objects ---------------------------------


def test_deployment_objects_never_carry_the_ssh_private_key():
    d = DeploymentRecord(workload_id="w", ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----")
    dumped = d.model_dump(mode="json", exclude=DEPLOYMENT_SECRET_FIELDS)
    assert "ssh_private_key" not in dumped
    assert "BEGIN OPENSSH" not in str(dumped)


def test_every_deployment_route_uses_the_exclusion():
    # Source-level guard: a new route that dumps a deployment without the
    # exclusion would silently reintroduce the leak.
    import ast
    import inspect

    import greencompute_gateway.transport.routes as routes

    tree = ast.parse(inspect.getsource(routes))
    functions = {fn.name: fn for fn in tree.body if isinstance(fn, ast.FunctionDef)}
    to_check = []
    for fn in functions.values():
        paths = [
            d.args[0].value for d in fn.decorator_list
            if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
            and isinstance(d.args[0].value, str)
        ]
        if not any("/platform/deployments" in p for p in paths):
            continue
        to_check.append(fn)
        # Also the module-level helpers the route hands its work to.
        to_check += [
            functions[c.func.id] for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in functions
        ]
    offenders = []
    for fn in to_check:
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "model_dump":
                if not any(k.arg == "exclude" for k in call.keywords):
                    offenders.append(f"{fn.name}:{call.lineno}")
    assert not offenders, f"deployment dumped without exclude=DEPLOYMENT_SECRET_FIELDS: {offenders}"


# --- 4. Other users' images are not listed -----------------------------------


def _builds_service(builds):
    return SimpleNamespace(builder=SimpleNamespace(
        list_builds=lambda: builds,
        get_build=lambda bid: next((b for b in builds if b.build_id == bid), None),
        list_image_history=lambda image: [b for b in builds if b.image == image],
    ))


def test_a_users_public_flag_does_not_publish_to_everyone():
    mine = BuildRecord(image="me/app", owner_user_id="u1", context_uri="x", dockerfile_path="D")
    theirs = BuildRecord(image="them/probe", owner_user_id="u2", context_uri="x",
                         dockerfile_path="D", public=True)
    svc = _builds_service([mine, theirs])
    assert GatewayService.list_builds(svc, user_id="u1") == [mine]
    assert GatewayService.get_build(svc, theirs.build_id, user_id="u1") is None
    assert GatewayService.list_image_history(svc, "them/probe", user_id="u1") == []
    # Admins still see everything.
    assert len(GatewayService.list_builds(svc, admin=True)) == 2


# --- 5. Public, machine-readable pricing -------------------------------------


def test_pricing_endpoint_matches_what_billing_charges(monkeypatch):
    from greencompute_protocol import rate_for_gpu
    import greencompute_gateway.transport.routes as routes

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    monkeypatch.setattr(routes, "_public_inference_model_names", lambda: [])
    monkeypatch.setattr(routes, "_rentable_now", lambda: ["rtx4090"])
    monkeypatch.setattr(routes, "_capacity_by_model", lambda: {"rtx4090": {"available_gpus": 3, "max_gpus_per_deployment": 3}})
    body = public_pricing()
    gpus = {g["gpu_model"]: g for g in body["gpu_rental"]["gpus"]}
    assert set(gpus) == {"rtx4090", "rtx5090"}
    for name, g in gpus.items():
        assert g["cents_per_gpu_hour"] == rate_for_gpu(name)
        assert g["usd_per_gpu_hour"] == rate_for_gpu(name) / 100
    assert body["gpu_rental"]["deployment_fee_usd"] == 0
    assert body["currency"] == "USD"


# --- 6. Only GPUs a usable node has are online (cold-agent test, 2026-10-09) --
#
# The RTX 5090 cluster stopped reporting 47+ days earlier, but the price table
# still listed the 5090 and an admin capacity override advertised 8 free. A
# 5090 request passed validation and would have sat `pending` forever. "Online"
# is now the scheduler's own node filter (`_schedulable_nodes`): stale,
# drained and unhealthy nodes are all offline, not "busy".


def _online(nodes, env=None, monkeypatch=None):
    if monkeypatch is not None:
        if env is None:
            monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
        else:
            monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", env)
    stub = SimpleNamespace(_schedulable_nodes=lambda: ([SimpleNamespace(gpu_model=g) for g in nodes], len(nodes), {}))
    return ControlPlaneService.online_gpu_models(stub)


def test_a_priced_gpu_with_only_unusable_nodes_is_not_online(monkeypatch):
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from datetime import UTC, datetime, timedelta

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    cp = ControlPlaneService(repository=repo)
    cp.update_capacity(CapacityUpdate(hotkey="a", nodes=[_node("rtx4090")]))
    cp.record_heartbeat(Heartbeat(hotkey="a"))
    cp.update_capacity(CapacityUpdate(hotkey="b", nodes=[_node("rtx5090").model_copy(update={"hotkey": "b"})],
                                      observed_at=datetime.now(UTC) - timedelta(days=47)))
    cp.record_heartbeat(Heartbeat(hotkey="b"))
    assert cp.online_gpu_models() == ["rtx4090"]


def test_a_usable_node_makes_its_gpu_online_whatever_the_spelling(monkeypatch):
    assert _online(["RTX-4090", "rtx 5090"], monkeypatch=monkeypatch) == ["rtx4090", "rtx5090"]


def test_excluding_a_family_hides_it_even_with_live_nodes(monkeypatch):
    # "Pause RTX 5090 rentals" is a config switch, independent of liveness.
    assert _online(["rtx4090", "rtx5090"], env="4090", monkeypatch=monkeypatch) == ["rtx4090"]


def test_internal_only_hardware_is_never_online_publicly(monkeypatch):
    assert _online(["a4000"], monkeypatch=monkeypatch) == []


def _gateway_stub(online, supported):
    workload = _workload(supported)
    cp = SimpleNamespace(
        repository=SimpleNamespace(get_workload=lambda wid: workload),
        online_gpu_models=lambda: online,
        placement_preview=lambda w: (True, {}),
        create_deployment=lambda body: pytest.fail("must be rejected before reaching the control-plane"),
    )
    return SimpleNamespace(control_plane=cp, _user_can_access_workload=lambda w, u: True), workload


def test_an_offline_gpu_is_rejected_up_front_with_what_is_available(monkeypatch):
    from greencompute_gateway.application.services import UnsupportedGPUError

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    stub, wl = _gateway_stub(["rtx4090"], ["rtx5090"])
    with pytest.raises(UnsupportedGPUError) as exc:
        GatewayService.create_deployment(stub, {"workload_id": wl.workload_id}, user_id="u1")
    assert exc.value.available == ["rtx4090"]


def test_unconstrained_quote_uses_the_priciest_live_gpu_not_the_table():
    # No live 5090 -> quoting 70 cents would overstate the cost by 75%.
    assert ControlPlaneService._quoted_hourly_rate_cents(_workload([]), ["rtx4090"]) == 40
    assert ControlPlaneService._quoted_hourly_rate_cents(_workload([]), ["rtx4090", "rtx5090"]) == 70


def test_supported_endpoint_lists_only_rentable_gpus(monkeypatch):
    import greencompute_gateway.transport.routes as routes

    monkeypatch.setattr(routes, "_rentable_now", lambda: ["rtx4090"])
    assert routes.list_supported_gpus() == ["rtx4090"]


def test_pricing_flags_which_gpus_are_available_now(monkeypatch):
    import greencompute_gateway.transport.routes as routes

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    monkeypatch.setattr(routes, "_public_inference_model_names", lambda: [])
    # Two 4090 machines with 3 and 2 free; a 5090 machine online but full.
    monkeypatch.setattr(routes, "_rentable_now", lambda: ["rtx4090", "rtx5090"])
    monkeypatch.setattr(routes, "_capacity_by_model", lambda: {
        "rtx4090": {"available_gpus": 5, "max_gpus_per_deployment": 3},
        "rtx5090": {"available_gpus": 0, "max_gpus_per_deployment": 0},
    })
    gpus = {g["gpu_model"]: g for g in routes.public_pricing()["gpu_rental"]["gpus"]}
    g = gpus["rtx4090"]
    assert (g["online"], g["available_now"], g["available_gpus"], g["max_gpus_per_deployment"]) == (True, True, 5, 3)
    g = gpus["rtx5090"]
    assert (g["online"], g["available_now"], g["available_gpus"], g["max_gpus_per_deployment"]) == (True, False, 0, 0)


# --- 7. Deleting a workload keeps the evidence behind its charges ------------


def test_deleting_a_workload_keeps_its_usage_records():
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_protocol import UsageRecord

    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    wl = repo.upsert_workload(_workload(["rtx4090"]))
    dep = repo.create_deployment(DeploymentRecord(workload_id=wl.workload_id))
    repo.add_usage_record(UsageRecord(deployment_id=dep.deployment_id, workload_id=wl.workload_id, hotkey="m"))

    repo.delete_workload(wl.workload_id)

    assert repo.get_deployment(dep.deployment_id) is None  # history row goes...
    from greencompute_persistence.orm import UsageRecordORM
    from greencompute_persistence import session_scope
    from sqlalchemy import select

    with session_scope(repo.session_factory) as s:
        kept = s.scalars(select(UsageRecordORM).where(UsageRecordORM.deployment_id == dep.deployment_id)).all()
        assert len(kept) == 1  # ...but the usage behind the charge stays


def test_deleting_a_workload_keeps_what_happened_to_its_deployments():
    """The status events and lease history stay, plus a final `deleted`
    snapshot of the deployment row (owner, the card it ran on, rate, fee,
    billing start, times), so a billing question or an incident about a
    deleted model still has an answer. Deleting all of it left 10% of usage
    records and recent charges with no deployment behind them (2026-10-29)."""
    from datetime import UTC, datetime

    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_persistence import session_scope
    from greencompute_persistence.orm import LeaseHistoryORM
    from greencompute_protocol import CapacityUpdate, DeploymentState, DeploymentStatusUpdate, LeaseAssignment
    from sqlalchemy import func, select

    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    repo.upsert_capacity(CapacityUpdate(hotkey="m", nodes=[_node("rtx5090").model_copy(update={"node_id": "n1"})]))
    wl = repo.upsert_workload(_workload(["rtx4090", "rtx5090"]))
    started = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    dep = repo.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id="ann", hotkey="m", node_id="n1",
        hourly_rate_cents=70, state=DeploymentState.TERMINATED, ready_at=started,
    ))
    repo.save_assignment(LeaseAssignment(deployment_id=dep.deployment_id, workload_id=wl.workload_id,
                                         hotkey="m", node_id="n1"))
    for state in (DeploymentState.SCHEDULED, DeploymentState.READY, DeploymentState.TERMINATED):
        repo.add_deployment_event(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=state))
    with session_scope(repo.session_factory) as s:
        history_before = s.scalar(select(func.count()).select_from(LeaseHistoryORM)
                                  .where(LeaseHistoryORM.deployment_id == dep.deployment_id))
    assert history_before >= 1

    repo.delete_workload(wl.workload_id)

    assert repo.get_deployment(dep.deployment_id) is None
    events = repo.list_deployment_events(dep.deployment_id)
    assert [e.get("state") for e in events] == ["scheduled", "ready", "terminated", "deleted"]
    last = events[-1]
    assert last["event"] == "deleted_with_workload" and last["last_state"] == "terminated"
    assert (last["owner_user_id"], last["hourly_rate_cents"], last["node_id"]) == ("ann", 70, "n1")
    assert last["gpu_model"] == "rtx5090"  # the card it ran on, not the first one it allowed
    assert last["allowed_gpu_models"] == ["rtx4090", "rtx5090"]
    assert last["ready_at"].startswith("2026-10-20T09:00") and last["observed_at"]
    with session_scope(repo.session_factory) as s:
        history_after = s.scalar(select(func.count()).select_from(LeaseHistoryORM)
                                 .where(LeaseHistoryORM.deployment_id == dep.deployment_id))
    assert history_after == history_before


def test_deleting_a_workload_with_something_running_stops_it_first(monkeypatch):
    """Over the API, Delete with a model still running behaves like Stop then
    Delete (as the website does): a proper terminate, then the delete."""
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_gateway.infrastructure.repository import GatewayRepository
    from greencompute_protocol import DeploymentState

    url = "sqlite+pysqlite:///:memory:"
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=url, bootstrap=True))
    gw = GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True), control_plane=cp)
    wl = cp.repository.upsert_workload(_workload(["rtx4090"]).model_copy(update={"owner_user_id": "ann"}))
    live = cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id="ann",
                                                            state=DeploymentState.READY))
    done = cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id="ann",
                                                            state=DeploymentState.TERMINATED))
    stopped = []
    real_cleanup = cp.cleanup_deployment
    monkeypatch.setattr(cp, "cleanup_deployment",
                        lambda dep_id, reason="": stopped.append((dep_id, reason)) or real_cleanup(dep_id, reason=reason))
    gw.delete_workload(wl.workload_id, actor_user_id="ann")
    assert stopped == [(live.deployment_id, "workload deleted")]  # the terminated one is left alone
    events = cp.repository.list_deployment_events(live.deployment_id)
    assert events[-1]["state"] == "deleted" and events[-1]["last_state"] == "terminated"
    assert cp.repository.get_deployment(done.deployment_id) is None

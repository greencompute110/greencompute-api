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
    offenders = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        paths = [
            d.args[0].value for d in fn.decorator_list
            if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
            and isinstance(d.args[0].value, str)
        ]
        if not any("/platform/deployments" in p for p in paths):
            continue
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
    body = public_pricing()
    gpus = {g["gpu_model"]: g for g in body["gpu_rental"]["gpus"]}
    assert set(gpus) == {"rtx4090", "rtx5090"}
    for name, g in gpus.items():
        assert g["cents_per_gpu_hour"] == rate_for_gpu(name)
        assert g["usd_per_gpu_hour"] == rate_for_gpu(name) / 100
    assert body["gpu_rental"]["deployment_fee_usd"] == 0
    assert body["currency"] == "USD"


# --- 6. Only GPUs a LIVE node has are rentable (cold-agent test, 2026-10-09) --
#
# The RTX 5090 cluster stopped reporting 47+ days earlier, but the price table
# still listed the 5090 and an admin capacity override advertised 8 free. A
# 5090 request passed validation and would have sat `pending` forever.


def _live_node(gpu, stale=False):
    return SimpleNamespace(gpu_model=gpu, stale=stale)


def _is_stale(n):
    return n.stale


def test_a_priced_gpu_with_only_stale_nodes_is_not_rentable(monkeypatch):
    from greencompute_gateway.domain.gpu_catalog import rentable_gpu_models

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    nodes = [_live_node("rtx4090"), _live_node("rtx5090", stale=True), _live_node("RTX 5090", stale=True)]
    assert rentable_gpu_models(nodes, _is_stale) == ["rtx4090"]


def test_a_live_node_makes_its_gpu_rentable_whatever_the_spelling(monkeypatch):
    from greencompute_gateway.domain.gpu_catalog import rentable_gpu_models

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    nodes = [_live_node("RTX-4090"), _live_node("rtx 5090")]
    assert rentable_gpu_models(nodes, _is_stale) == ["rtx4090", "rtx5090"]


def test_excluding_a_family_hides_it_even_with_live_nodes(monkeypatch):
    # "Pause RTX 5090 rentals" is a config switch, independent of liveness.
    from greencompute_gateway.domain.gpu_catalog import rentable_gpu_models

    monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090")
    nodes = [_live_node("rtx4090"), _live_node("rtx5090")]
    assert rentable_gpu_models(nodes, _is_stale) == ["rtx4090"]


def test_internal_only_hardware_is_never_rentable_publicly(monkeypatch):
    from greencompute_gateway.domain.gpu_catalog import rentable_gpu_models

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    assert rentable_gpu_models([_live_node("a4000")], _is_stale) == []


def _gateway_stub(nodes, supported):
    workload = _workload(supported)
    cp = SimpleNamespace(
        repository=SimpleNamespace(get_workload=lambda wid: workload, list_nodes=lambda: nodes),
        _is_node_stale=_is_stale,
        create_deployment=lambda body: pytest.fail("must be rejected before reaching the control-plane"),
    )
    return SimpleNamespace(control_plane=cp, _user_can_access_workload=lambda w, u: True), workload


def test_a_stale_only_gpu_is_rejected_up_front_with_what_is_available(monkeypatch):
    from greencompute_gateway.application.services import UnsupportedGPUError

    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", raising=False)
    stub, wl = _gateway_stub([_live_node("rtx4090"), _live_node("rtx5090", stale=True)], ["rtx5090"])
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
    monkeypatch.setattr(routes, "_rentable_now", lambda: ["rtx4090"])
    gpus = {g["gpu_model"]: g for g in routes.public_pricing()["gpu_rental"]["gpus"]}
    assert gpus["rtx4090"]["available_now"] is True
    assert gpus["rtx5090"]["available_now"] is False


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

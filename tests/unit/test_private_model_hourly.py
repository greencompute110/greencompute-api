"""Private models are charged by the hour, like GPU rentals (owner decision,
2026-10-19): the GPU's hourly rate × its GPU count, by the minute, only while
READY. Requests to one aren't charged per token, whoever makes them; shared
catalog models still are. A private model that was already running when this
shipped is charged from then on, never back to its ready_at. Running out of
balance pauses it, without a storage charge."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.config import settings
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import (
    GatewayService,
    InsufficientBalanceForRentalError,
)
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.orm import LedgerEntryORM, UserORM
from greencompute_protocol import (
    CapacityUpdate,
    ChatCompletionMessage,
    ChatCompletionRequest,
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    LeaseAssignment,
    NodeCapability,
    WorkloadKind,
    WorkloadSpec,
)
from sqlalchemy import select

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def cp(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'hourly.db'}"
    # meter_usage and the gateway build BillingRepository() from the environment.
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)
    repo = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", email="u1@x.io", balance_credits=1000))
        s.add(UserORM(user_id="u2", username="u2", email="u2@x.io", balance_credits=1000))
    return ControlPlaneService(repository=repo)


def _private_workload(cp, owner="u1", gpus=1):
    return cp.repository.upsert_workload(WorkloadSpec(
        name="meta-llama-Llama-3.1-8B-Instruct", image="vllm", kind=WorkloadKind.INFERENCE, owner_user_id=owner,
        requirements={"gpu_count": gpus},
        runtime={"model_identifier": "meta-llama/Llama-3.1-8B-Instruct"},
    ))


def _catalog_workload(cp):
    return cp.repository.upsert_workload(WorkloadSpec(
        workload_id="catalog-qwen2.5-7b", name="qwen2.5-7b", image="vllm", kind=WorkloadKind.INFERENCE,
        public=True, requirements={"gpu_count": 1}, metadata={"managed_by": "flux", "catalog_model_id": "qwen2.5-7b"},
    ))


def _deployment(cp, workload, state=DeploymentState.READY, owner="u1", rate=40, **fields):
    return cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id=owner, state=state,
        ready_instances=1 if state == DeploymentState.READY else 0, hourly_rate_cents=rate, **fields,
    ))


def _started(cp, workload, owner="u1"):
    """A private model the way it really starts: placed, then reported READY."""
    dep = _deployment(cp, workload, state=DeploymentState.STARTING, owner=owner)
    cp.repository.save_assignment(LeaseAssignment(
        deployment_id=dep.deployment_id, workload_id=workload.workload_id, hotkey="hk", node_id="n1", status="activating",
    ))
    cp.update_deployment_status(DeploymentStatusUpdate(
        deployment_id=dep.deployment_id, state=DeploymentState.READY, ready_instances=1,
    ))
    return dep.deployment_id


def _balance(cp, user="u1"):
    with session_scope(cp.repository.session_factory) as s:
        return s.get(UserORM, user).balance_credits


def _charges(cp, user="u1"):
    with session_scope(cp.repository.session_factory) as s:
        return [
            (e.kind, e.amount_cents, e.reference_id, e.description)
            for e in s.scalars(select(LedgerEntryORM).where(LedgerEntryORM.user_id == user)).all()
        ]


# --- metering --------------------------------------------------------------------


def test_a_ready_private_model_is_charged_by_the_hour(cp):
    dep_id = _started(cp, _private_workload(cp))
    since = cp.repository.get_billable_since(dep_id)
    assert since is not None

    # Ten minutes at $0.40/hr = 6.67¢: 6¢ now, the rest carried to the next charge.
    assert cp.meter_usage(elapsed_seconds=600, now=since + timedelta(minutes=10)) == {dep_id: 6}
    assert _balance(cp) == 994
    assert _charges(cp) == [("usage", -6, dep_id, "Private model Llama-3.1-8B-Instruct · 1 GPU at $0.40/hr")]
    # An hour in all comes to exactly the hourly rate.
    for minute in range(11, 61):
        cp.meter_usage(elapsed_seconds=60, now=since + timedelta(minutes=minute))
    assert _balance(cp) == 960


def test_the_rate_is_the_gpu_rate_times_the_gpu_count(cp):
    dep_id = _started(cp, _private_workload(cp, gpus=2))
    since = cp.repository.get_billable_since(dep_id)
    assert cp.meter_usage(elapsed_seconds=3600, now=since + timedelta(hours=1)) == {dep_id: 80}
    assert _charges(cp)[0][3] == "Private model Llama-3.1-8B-Instruct · 2 GPUs at $0.40/hr each"


def test_the_minute_it_became_ready_in_only_counts_from_then(cp):
    dep_id = _started(cp, _private_workload(cp))
    since = cp.repository.get_billable_since(dep_id)
    # The cycle was a full minute, but it was ready for only the last 15 s
    # of it (it was starting before): 40¢/hr × 15 s = 167 millicents.
    cp.meter_usage(elapsed_seconds=60, now=since + timedelta(seconds=15))
    cp.meter_usage(elapsed_seconds=3600 - 15, now=since + timedelta(hours=1))
    assert _balance(cp) == 960


@pytest.mark.parametrize("state", [
    DeploymentState.PENDING, DeploymentState.SCHEDULED, DeploymentState.PULLING, DeploymentState.STARTING,
    DeploymentState.SUSPENDED, DeploymentState.DRAINING, DeploymentState.FAILED, DeploymentState.TERMINATED,
])
def test_a_private_model_that_isnt_ready_is_not_charged(cp, state):
    _deployment(cp, _private_workload(cp), state=state, billable_since=NOW - timedelta(hours=2))
    assert cp.meter_usage(elapsed_seconds=3600, now=NOW) == {}
    assert _balance(cp) == 1000


def test_an_idle_paused_private_model_stops_being_charged(cp):
    dep_id = _started(cp, _private_workload(cp))
    since = cp.repository.get_billable_since(dep_id)
    assert cp.meter_usage(elapsed_seconds=1800, now=since + timedelta(minutes=30)) == {dep_id: 20}
    # No requests for 30 minutes: the idle check pauses it.
    assert cp.process_idle_inference_deployments(now=datetime.now(UTC) + timedelta(minutes=31)) == [dep_id]
    assert cp.repository.get_deployment(dep_id).state == DeploymentState.SUSPENDED
    assert cp.meter_usage(elapsed_seconds=3600, now=since + timedelta(minutes=90)) == {}
    assert _balance(cp) == 980


def test_shared_catalog_models_are_not_charged_by_the_hour(cp):
    catalog = _catalog_workload(cp)
    _deployment(cp, catalog, owner=None, rate=0)  # a Flux replica: no owner
    _deployment(cp, catalog, owner="u1")  # even one an admin deployed under their account
    assert cp.meter_usage(elapsed_seconds=3600, now=NOW) == {}
    assert _balance(cp) == 1000


# --- no back-charging ---------------------------------------------------------------


def test_a_private_model_running_for_days_is_not_charged_for_the_past(cp):
    """Running since before private models were billed by the hour: no
    billable_since. It is charged from the first metering pass that sees it,
    never from its ready_at three days ago."""
    dep = _deployment(cp, _private_workload(cp), ready_at=NOW - timedelta(days=3))
    assert cp.repository.get_billable_since(dep.deployment_id) is None

    # Even a maximal one-hour cycle charges nothing on that first pass...
    assert cp.meter_usage(elapsed_seconds=3600, now=NOW) == {}
    assert _balance(cp) == 1000
    assert cp.repository.get_billable_since(dep.deployment_id) == NOW

    # ...and from then on only the time since.
    assert cp.meter_usage(elapsed_seconds=3600, now=NOW + timedelta(minutes=30)) == {dep.deployment_id: 20}
    assert _balance(cp) == 980


def test_rentals_running_before_billable_since_existed_are_charged_as_before(cp):
    pod = cp.repository.upsert_workload(WorkloadSpec(name="pod", image="img", kind=WorkloadKind.POD))
    dep = _deployment(cp, pod, ready_at=NOW - timedelta(days=3))
    assert cp.meter_usage(elapsed_seconds=1800, now=NOW) == {dep.deployment_id: 20}
    assert _charges(cp)[0][3] == "GPU usage 1× GPU @ $0.40/hr"


# --- running out of balance ----------------------------------------------------------


def test_running_out_of_balance_pauses_it_without_going_below_zero(cp, monkeypatch):
    monkeypatch.setattr(settings, "pod_storage_billing_enabled", True)
    monkeypatch.setattr(settings, "pod_exhaustion_reaper_enabled", True)
    with session_scope(cp.repository.session_factory) as s:
        s.get(UserORM, "u1").balance_credits = 3
    dep_id = _started(cp, _private_workload(cp))
    since = cp.repository.get_billable_since(dep_id)

    # An hour is 40¢; the 3¢ there are taken, and it is paused.
    assert cp.meter_usage(elapsed_seconds=3600, now=since + timedelta(hours=1)) == {dep_id: 3}
    assert _balance(cp) == 0
    paused = cp.repository.get_deployment(dep_id)
    assert paused.state == DeploymentState.SUSPENDED
    assert "Insufficient balance" in paused.last_error
    # A model keeps no state: no storage charge, no deletion deadline.
    assert cp.meter_pod_storage() == {}
    assert cp.reap_exhausted_pods(now=datetime.now(UTC) + timedelta(days=60)) == []
    assert _balance(cp) == 0


def test_resuming_a_paused_private_model_needs_an_hour_of_credit(cp):
    with session_scope(cp.repository.session_factory) as s:
        s.get(UserORM, "u1").balance_credits = 10
    wl = _private_workload(cp)
    dep = _deployment(cp, wl, state=DeploymentState.SUSPENDED, hotkey="hk", node_id="n1")
    gw = GatewayService(control_plane=cp)
    with pytest.raises(InsufficientBalanceForRentalError) as exc:
        gw.resume_deployment(dep.deployment_id, user_id="u1")
    assert exc.value.private_model and exc.value.required_cents == 40


# --- starting one ------------------------------------------------------------------


def test_starting_one_needs_an_hour_at_its_rate_and_says_so(cp):
    with session_scope(cp.repository.session_factory) as s:
        s.get(UserORM, "u1").balance_credits = 10
    wl = _private_workload(cp)
    gw = GatewayService(control_plane=cp)
    with pytest.raises(InsufficientBalanceForRentalError) as exc:
        gw.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id), user_id="u1")
    assert exc.value.private_model
    detail = routes._hour_of_credit_detail(exc.value, "start")
    rate = exc.value.rate_cents_per_hour
    assert detail["required_cents"] == rate
    assert detail["message"] == (
        f"insufficient balance to start this private model: it costs ${rate / 100:.2f}/hour "
        f"(1 GPU at ${rate / 100:.2f}/hr) while it runs, billed by the minute, and starting it needs "
        f"at least one hour (${rate / 100:.2f}) of credit; your balance is $0.10"
    )


def test_the_locked_rate_is_never_above_the_quote(cp, monkeypatch):
    """A model with no GPU constraint is quoted the dearest live public GPU
    ($0.40 with only the 4090 offered), and placement can't raise that by
    landing it on a card that isn't offered."""
    seen = datetime.now(UTC)
    cp.repository.upsert_capacity(CapacityUpdate(hotkey="hk", observed_at=seen, nodes=[NodeCapability(
        hotkey="hk", node_id="n5090", gpu_model="rtx5090", gpu_count=8, available_gpus=8,
        vram_gb_per_gpu=32, cpu_cores=64, memory_gb=256, observed_at=seen,
    )]))
    dep = cp.create_deployment(DeploymentCreateRequest(workload_id=_private_workload(cp).workload_id))
    cp.repository.update_deployment(dep.model_copy(update={"hourly_rate_cents": 40}))  # quoted for a 4090
    monkeypatch.setattr(cp, "_assign_lease", lambda workload, deployment_id: LeaseAssignment(
        deployment_id=deployment_id, workload_id=workload.workload_id, hotkey="hk", node_id="n5090",
    ))
    cp.process_pending_events()
    placed = cp.repository.get_deployment(dep.deployment_id)
    assert placed.state == DeploymentState.SCHEDULED and placed.hourly_rate_cents == 40


# --- no per-token charge -------------------------------------------------------------


def _chat(model):
    return ChatCompletionRequest(model=model, messages=[ChatCompletionMessage(role="user", content="hi")])


def test_requests_to_a_private_model_are_not_charged_per_token(cp):
    gw = GatewayService(control_plane=cp)
    private = _deployment(cp, _private_workload(cp), hotkey="hk")
    catalog = _deployment(cp, _catalog_workload(cp), owner=None, hotkey="hk")
    for caller in ("u1", "u2"):  # the owner, or anyone it's shared with
        gw._charge_inference_tokens(
            user_id=caller, reference_id=f"r-{caller}", model="m", prompt_tokens=50_000, completion_tokens=50_000,
            deployment=private,
        )
    assert _charges(cp, "u1") == [] and _charges(cp, "u2") == []
    # The shared catalog is still charged per token.
    gw._charge_inference_tokens(
        user_id="u2", reference_id="r-cat", model="qwen2.5-7b", prompt_tokens=50_000, completion_tokens=50_000,
        deployment=catalog,
    )
    assert [c[0] for c in _charges(cp, "u2")] == ["inference"] and _balance(cp, "u2") < 1000


@pytest.mark.parametrize("stream", [False, True])
def test_no_balance_hold_for_a_private_model_but_still_for_the_catalog(cp, stream):
    gw = GatewayService(control_plane=cp)
    private = _deployment(cp, _private_workload(cp))
    catalog = _deployment(cp, _catalog_workload(cp), owner=None)
    reserved = []
    gw._reserve_inference_budget = lambda user_id, ref, request: reserved.append(user_id)
    gw._release_inference_budget = lambda ref: None
    gw._invoke_from_candidates = lambda *a, **k: "RESP"
    gw._stream_from_candidates = lambda *a, **k: iter(["data: x\n"])
    call = gw.stream_chat_completion if stream else gw.invoke_chat_completion

    for target in (private, catalog):
        gw._select_healthy_deployments = lambda *a, _t=target, **k: ([_t], {"host": None})
        result = call(_chat("m"), user_id="u2")
        if stream:
            list(result)
    assert reserved == ["u2"]  # the catalog request only


def test_an_empty_balance_can_still_call_a_private_model(cp):
    gw = GatewayService(control_plane=cp)
    wl = _private_workload(cp)
    _deployment(cp, wl)
    _deployment(cp, _catalog_workload(cp), owner=None)
    assert gw.bills_per_token(_chat(wl.workload_id), user_id="u1") is False
    assert gw.bills_per_token(_chat("catalog-qwen2.5-7b"), user_id="u1") is True
    assert gw.bills_per_token(_chat("no-such-model"), user_id="u1") is True
    # Someone else's private model is answered as an unknown one would be.
    gw.repository = SimpleNamespace(list_workload_shares=lambda workload_id: [])
    assert gw.bills_per_token(_chat(wl.workload_id), user_id="u2") is True

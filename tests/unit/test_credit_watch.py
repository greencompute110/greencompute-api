"""Credit watch: the forecast behind "your credit lasts about N hours", the
low-credit emails (once under 24 h, once under 2 h, re-armed by a top-up), and
auto-resume of pods flagged resume_on_topup once the balance covers an hour
again. External API user, Oct 2026: "suspended cards sit idle until someone
notices".
"""
from datetime import UTC, datetime, timedelta

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application import credit_watch as cw
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.orm import UserORM
from greencompute_protocol import (
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    DeploymentUpdateRequest,
    LeaseAssignment,
    WorkloadSpec,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=url, bootstrap=True))
    gw = GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True), control_plane=cp)
    billing = BillingRepository(database_url=url, bootstrap=True)
    with session_scope(billing.session_factory) as s:
        s.add(UserORM(user_id="ann", username="ann", email="ann@x.io", balance_credits=0))
        s.add(UserORM(user_id="bo", username="bo", email="bo@x.io", balance_credits=0))
    # The gateway's resume gate builds its own BillingRepository(): point it at this DB.
    original = BillingRepository.get_balance
    monkeypatch.setattr(BillingRepository, "get_balance", lambda self, uid: original(billing, uid))
    return gw, cp, billing


def _set_balance(billing, user_id, cents):
    with session_scope(billing.session_factory) as s:
        s.get(UserORM, user_id).balance_credits = cents


def _pod(cp, owner, name="train", gpu_count=1, kind="pod"):
    return cp.repository.upsert_workload(WorkloadSpec(
        name=name, image="ubuntu", kind=kind, owner_user_id=owner,
        requirements={"gpu_count": gpu_count, "min_vram_gb_per_gpu": 24, "cpu_cores": 1, "memory_gb": 1,
                      "supported_gpu_models": ["rtx4090"]},
    ))


def _deployment(cp, workload, owner, *, state, rate=40, resume_on_topup=False, last_error=None, suspended_at=None,
                suspended_reason=None):
    return cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id=owner, state=state, hourly_rate_cents=rate,
        resume_on_topup=resume_on_topup, last_error=last_error, suspended_at=suspended_at, ready_instances=1,
        suspended_reason=suspended_reason,
    ))


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, session_factory, user_id, kind, fields, *, event_id):
        self.sent.append((user_id, kind, fields, event_id))


# --- forecast ------------------------------------------------------------------


def test_forecast_adds_up_running_rentals_and_private_models_only(world):
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)                       # 1× 40
    _deployment(cp, _pod(cp, "ann", "big", gpu_count=2), "ann", state=DeploymentState.READY)  # 2× 40
    _deployment(cp, _pod(cp, "ann", "paused"), "ann", state=DeploymentState.SUSPENDED)        # not running
    # A shared catalog model: run by the platform (no owner), paid per token.
    shared = cp.repository.upsert_workload(WorkloadSpec(name="shared-llm", image="vllm", kind="inference", public=True))
    _deployment(cp, shared, None, state=DeploymentState.READY)
    _set_balance(billing, "ann", 240)
    fc = cw.forecast("ann", control_plane=cp, balance_cents=240, now=NOW)
    assert fc["burn_cents_per_hour"] == 120
    assert fc["hours_left"] == 2.0
    assert fc["runs_out_at"] == (NOW + timedelta(hours=2)).isoformat()
    assert sorted(r["label"] for r in fc["running"]) == ["big (2× rtx4090)", "train (1× rtx4090)"]


def test_nothing_running_means_no_run_out_time(world):
    gw, cp, billing = world
    fc = cw.forecast("ann", control_plane=cp, balance_cents=500, now=NOW)
    assert (fc["burn_cents_per_hour"], fc["hours_left"], fc["runs_out_at"], fc["running"]) == (0, None, None, [])


def test_the_route_answers_for_the_keys_owner(world, monkeypatch):
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)
    _set_balance(billing, "ann", 80)
    monkeypatch.setattr(routes, "service", gw)
    monkeypatch.setattr(cw, "_billing_repo", lambda: billing)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: type("K", (), {"user_id": "ann", "admin": False})())
    body = routes.billing_forecast(authorization="Bearer k", x_api_key=None)
    assert (body["balance_cents"], body["burn_cents_per_hour"], body["hours_left"]) == (80, 40, 2.0)


# --- low-credit emails ---------------------------------------------------------


def _sweep(cp, billing, mail, hours_from_now=0):
    return cw.low_credit_once(now=NOW + timedelta(hours=hours_from_now), control_plane=cp, billing_repo=billing, notify=mail)


def test_warned_once_under_24h_and_once_more_under_2h_then_rearmed_by_a_topup(world):
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)  # 40 c/h
    mail = Mail()

    _set_balance(billing, "ann", 40 * 30)  # 30 h left: fine
    assert _sweep(cp, billing, mail) == []

    _set_balance(billing, "ann", 40 * 10)  # 10 h left: first warning
    assert _sweep(cp, billing, mail, 1) == ["ann"]
    assert _sweep(cp, billing, mail, 2) == []  # not again at this level
    uid, kind, fields, event_id = mail.sent[-1]
    assert (uid, kind, fields["level_hours"], fields["hours_left"]) == ("ann", "low_credit", 24, 10.0)
    assert fields["running"] == ["train (1× rtx4090)"] and fields["burn_usd_per_hour"] == 0.4

    _set_balance(billing, "ann", 40)  # 1 h left: the tighter level, once
    assert _sweep(cp, billing, mail, 3) == ["ann"]
    assert mail.sent[-1][2]["level_hours"] == 2
    assert _sweep(cp, billing, mail, 4) == []

    _set_balance(billing, "ann", 40 * 48)  # topped up: re-armed
    assert _sweep(cp, billing, mail, 5) == []
    _set_balance(billing, "ann", 40 * 5)  # runs down again: warned again
    assert _sweep(cp, billing, mail, 6) == ["ann"]
    assert mail.sent[-1][2]["level_hours"] == 24
    assert len({s[3] for s in mail.sent}) == len(mail.sent)  # every email its own event id


def test_a_balance_going_slightly_up_within_a_level_does_not_warn_again(world):
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)
    mail = Mail()
    _set_balance(billing, "ann", 40)      # 1 h: level 2
    assert _sweep(cp, billing, mail) == ["ann"]
    _set_balance(billing, "ann", 40 * 10)  # 10 h: still under 24, already told at a tighter level
    assert _sweep(cp, billing, mail, 1) == []


def test_nobody_is_warned_for_someone_elses_deployments(world):
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)
    _set_balance(billing, "ann", 40)
    _set_balance(billing, "bo", 0)  # bo runs nothing
    mail = Mail()
    assert _sweep(cp, billing, mail) == ["ann"]


# --- auto-resume after a top-up -----------------------------------------------


def _suspended_for_balance(cp, owner, name, *, resume_on_topup, when, rate=40, gpu_count=1):
    return _deployment(
        cp, _pod(cp, owner, name, gpu_count=gpu_count), owner, state=DeploymentState.SUSPENDED, rate=rate,
        resume_on_topup=resume_on_topup, last_error="Insufficient balance — top up your credits to resume",
        suspended_at=when, suspended_reason="balance",
    )


@pytest.fixture
def resumes(monkeypatch, world):
    """Resume in place without a node: records the order, flips the state."""
    gw, cp, billing = world
    order = []

    def fake_resume(deployment_id):
        dep = cp.repository.get_deployment(deployment_id)
        dep.state = DeploymentState.READY
        cp.repository.update_deployment(dep)
        order.append(deployment_id)
        return dep

    monkeypatch.setattr(cp, "resume_deployment", fake_resume)
    return order


def test_flagged_pods_come_back_oldest_first_once_the_balance_covers_an_hour(world, resumes):
    gw, cp, billing = world
    older = _suspended_for_balance(cp, "ann", "older", resume_on_topup=True, when=NOW - timedelta(hours=5))
    newer = _suspended_for_balance(cp, "ann", "newer", resume_on_topup=True, when=NOW - timedelta(hours=1))
    _set_balance(billing, "ann", 40)  # one hour of ONE pod
    mail = Mail()
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) == [older.deployment_id]
    assert cp.repository.get_deployment(newer.deployment_id).state == DeploymentState.SUSPENDED
    uid, kind, fields, _ = mail.sent[-1]
    assert (uid, kind, fields["count"], fields["names"]) == ("ann", "deployments_resumed", 1, ["older (1× rtx4090)"])

    _set_balance(billing, "ann", 80)
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) == [newer.deployment_id]


def test_pods_not_flagged_or_not_suspended_for_balance_are_left_alone(world, resumes):
    gw, cp, billing = world
    _suspended_for_balance(cp, "ann", "manual", resume_on_topup=False, when=NOW)
    idle = _deployment(cp, _pod(cp, "ann", "idle-model", kind="inference"), "ann", state=DeploymentState.SUSPENDED,
                       resume_on_topup=True, last_error="idle — no invocations in 30 min", suspended_reason="idle")
    _set_balance(billing, "ann", 10_000)
    mail = Mail()
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) == []
    assert resumes == [] and mail.sent == []
    assert cp.repository.get_deployment(idle.deployment_id).state == DeploymentState.SUSPENDED


def test_a_flagged_pod_waits_while_the_balance_is_short(world, resumes):
    gw, cp, billing = world
    _suspended_for_balance(cp, "ann", "big", resume_on_topup=True, when=NOW, gpu_count=2)  # needs 80
    _set_balance(billing, "ann", 79)
    mail = Mail()
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) == []
    assert mail.sent == []


def test_a_full_machine_is_skipped_not_fatal(world, monkeypatch):
    gw, cp, billing = world
    stuck = _suspended_for_balance(cp, "ann", "stuck", resume_on_topup=True, when=NOW - timedelta(hours=2))
    fine = _suspended_for_balance(cp, "bo", "fine", resume_on_topup=True, when=NOW)
    _set_balance(billing, "ann", 1000)
    _set_balance(billing, "bo", 1000)

    def resume(deployment_id):
        if deployment_id == stuck.deployment_id:
            raise ValueError("original node is full")
        dep = cp.repository.get_deployment(deployment_id)
        dep.state = DeploymentState.READY
        return cp.repository.update_deployment(dep)

    monkeypatch.setattr(cp, "resume_deployment", resume)
    mail = Mail()
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) == [fine.deployment_id]
    assert [s[0] for s in mail.sent] == ["bo"]


# --- the flag itself -----------------------------------------------------------


def test_the_flag_is_off_unless_asked_and_survives_the_database(world):
    gw, cp, billing = world
    wl = _pod(cp, "ann")
    plain = cp.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id))
    opted = cp.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id, resume_on_topup=True))
    assert cp.repository.get_deployment(plain.deployment_id).resume_on_topup is False
    assert cp.repository.get_deployment(opted.deployment_id).resume_on_topup is True
    cp.update_deployment(plain.deployment_id, DeploymentUpdateRequest(resume_on_topup=True))
    assert cp.repository.get_deployment(plain.deployment_id).resume_on_topup is True
    cp.update_deployment(plain.deployment_id, DeploymentUpdateRequest(fee_acknowledged=True))  # untouched by other edits
    assert cp.repository.get_deployment(plain.deployment_id).resume_on_topup is True


def test_the_gateway_passes_the_flag_through_on_create(world, monkeypatch):
    gw, cp, billing = world
    wl = _pod(cp, "ann")
    _set_balance(billing, "ann", 10_000)
    monkeypatch.setattr(cp, "online_gpu_models", lambda: ["rtx4090"])
    monkeypatch.setattr(cp, "placement_preview", lambda w: (True, {}))
    dep = gw.create_deployment(
        {"workload_id": wl.workload_id, "resume_on_topup": True, "save_on_exhaustion": True}, user_id="ann")
    assert cp.repository.get_deployment(dep.deployment_id).resume_on_topup is True


def test_a_pod_still_coming_back_counts_against_the_balance(world, monkeypatch):
    """Resume leaves the row SUSPENDED (reason "resuming") until the machine
    reports it running, which takes minutes. The next sweep must not spend the
    same balance on a sibling."""
    gw, cp, billing = world
    first = _suspended_for_balance(cp, "ann", "first", resume_on_topup=True, when=NOW - timedelta(hours=2))
    second = _suspended_for_balance(cp, "ann", "second", resume_on_topup=True, when=NOW)
    _set_balance(billing, "ann", 40)

    def real_shape_resume(deployment_id):
        # As ControlPlaneService.resume_deployment leaves things: the row stays
        # SUSPENDED (reason intact) and its lease is active again.
        dep = cp.repository.get_deployment(deployment_id)
        dep.last_error = None
        cp.repository.save_assignment(LeaseAssignment(
            deployment_id=deployment_id, workload_id=dep.workload_id, hotkey="m", node_id="n1", status="active"))
        return cp.repository.update_deployment(dep)

    monkeypatch.setattr(cp, "resume_deployment", real_shape_resume)
    mail = Mail()
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) == [first.deployment_id]
    assert cw.auto_resume_once(now=NOW + timedelta(minutes=1), service=gw, billing_repo=billing, notify=mail) == []
    assert cp.repository.get_deployment(second.deployment_id).state == DeploymentState.SUSPENDED


def test_the_machines_suspended_re_report_keeps_the_reason_and_the_text(world):
    """What happens in production: the meter suspends with a reason, the node
    agent stops the container and re-reports SUSPENDED with no text."""
    gw, cp, billing = world
    dep = _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)
    cp.update_deployment_status(DeploymentStatusUpdate(
        deployment_id=dep.deployment_id, state=DeploymentState.SUSPENDED,
        error="Insufficient balance — top up your credits to resume", suspended_reason="balance"))
    cp.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=DeploymentState.SUSPENDED))
    row = cp.repository.get_deployment(dep.deployment_id)
    assert row.suspended_reason == "balance"
    assert row.last_error.startswith("Insufficient balance")
    cp.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=DeploymentState.READY))
    row = cp.repository.get_deployment(dep.deployment_id)
    assert (row.suspended_reason, row.suspended_at) == (None, None)


def test_a_deployment_on_a_silent_machine_burns_nothing(world, monkeypatch):
    # Kept, not billed, while its machine is offline (meter_usage); the
    # forecast must not warn about money that isn't moving.
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)
    monkeypatch.setattr(cp, "_box_silent", lambda dep, now: True)
    fc = cw.forecast("ann", control_plane=cp, balance_cents=40, now=NOW)
    assert (fc["burn_cents_per_hour"], fc["hours_left"]) == (0, None)
    assert fc["running"][0]["offline"] is True
    assert cw.low_credit_once(now=NOW, control_plane=cp, billing_repo=billing, notify=Mail()) == []


def test_a_row_without_a_rate_is_forecast_at_what_the_meter_charges(world):
    gw, cp, billing = world
    _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY, rate=0)
    assert cw.forecast("ann", control_plane=cp, balance_cents=100, now=NOW)["burn_cents_per_hour"] == 10


def test_resume_on_topup_needs_a_saved_pod():
    from pydantic import ValidationError

    from greencompute_gateway.application.services import GatewayDeploymentCreateRequest

    with pytest.raises(ValidationError, match="save_on_exhaustion"):
        GatewayDeploymentCreateRequest(workload_id="w", resume_on_topup=True)
    assert GatewayDeploymentCreateRequest(workload_id="w", resume_on_topup=True, save_on_exhaustion=True).resume_on_topup
    assert GatewayDeploymentCreateRequest.__doc__.startswith("POST /platform/deployments")


def test_patch_cannot_flag_an_unsaved_pod(world):
    gw, cp, billing = world
    wl = _pod(cp, "ann")
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id="ann", state=DeploymentState.SUSPENDED, save_on_exhaustion=False))
    with pytest.raises(ValueError, match="save_on_exhaustion"):
        gw.update_deployment(dep.deployment_id, DeploymentUpdateRequest(resume_on_topup=True), actor_user_id="ann")
    saved = cp.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id="ann", state=DeploymentState.SUSPENDED, save_on_exhaustion=True))
    gw.update_deployment(saved.deployment_id, DeploymentUpdateRequest(resume_on_topup=True), actor_user_id="ann")
    assert cp.repository.get_deployment(saved.deployment_id).resume_on_topup is True


def test_a_machine_cannot_say_why_a_deployment_is_suspended(world, monkeypatch):
    """A miner's status report never carries the reason: only the meter and
    the idle sweep set it, or a miner could drive auto-resume from outside."""
    from greencompute_control_plane.transport import routes as cp_routes

    gw, cp, billing = world
    dep = _deployment(cp, _pod(cp, "ann"), "ann", state=DeploymentState.READY)
    dep.hotkey = "m"
    cp.repository.update_deployment(dep)
    monkeypatch.setattr(cp_routes, "service", cp)
    monkeypatch.setattr(cp_routes, "require_miner_request", lambda *a, **k: None)
    status_route = next(r for r in cp_routes.router.routes if r.path.endswith("/status") and "POST" in r.methods)
    status_route.endpoint(
        deployment_id=dep.deployment_id,
        payload=DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=DeploymentState.SUSPENDED,
                                       suspended_reason="balance"),
        x_miner_hotkey=None, x_miner_signature=None, x_miner_nonce=None, x_miner_timestamp=None, x_miner_auth_mode=None,
    )
    assert cp.repository.get_deployment(dep.deployment_id).suspended_reason is None


def test_a_resume_email_is_not_followed_by_a_low_credit_email_saying_the_same(world, resumes):
    gw, cp, billing = world
    _suspended_for_balance(cp, "ann", "pod", resume_on_topup=True, when=NOW)
    _set_balance(billing, "ann", 50)  # 1.25 h of it
    mail = Mail()
    assert cw.auto_resume_once(now=NOW, service=gw, billing_repo=billing, notify=mail) != []
    assert cw.low_credit_once(now=NOW + timedelta(minutes=2), control_plane=cp, billing_repo=billing, notify=mail) == []
    assert [k for _, k, _, _ in mail.sent] == ["deployments_resumed"]
    _set_balance(billing, "ann", 10)  # dropped further: that is news
    assert cw.low_credit_once(now=NOW + timedelta(minutes=9), control_plane=cp, billing_repo=billing, notify=mail) == []
    # (still level 2, already told); a later top-up above 24 h re-arms as before

"""Saved pods (save_on_exhaustion): deleting one stops its storage charge and
frees its disk, only rentals are billed or reaped as saved pods (an idle
private model was being charged $1/day as one), the API says what a suspended
rental costs and when it goes, and API callers don't opt in by default."""
from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Response

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.config import settings
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayDeploymentCreateRequest
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.orm import DeploymentORM, UserORM
from greencompute_protocol import (
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    LeaseAssignment,
    WorkloadKind,
    WorkloadSpec,
)


@pytest.fixture
def cp(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'saved.db'}"
    # meter_pod_storage builds BillingRepository() from the environment.
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)
    monkeypatch.setattr(settings, "pod_storage_billing_enabled", True)
    monkeypatch.setattr(settings, "pod_exhaustion_reaper_enabled", True)
    repo = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u", email="u@x.io", balance_credits=0))
    return ControlPlaneService(repository=repo)


def _suspended(svc, kind=WorkloadKind.POD, save=True):
    """A deployment that ran on a node and was then suspended, as metering
    does when the balance runs out (or the idle check, for a model)."""
    wl = svc.repository.upsert_workload(WorkloadSpec(name="job", image="img", kind=kind))
    dep = svc.repository.create_deployment(
        DeploymentRecord(
            workload_id=wl.workload_id,
            owner_user_id="u1",
            hotkey="hk",
            node_id="n1",
            state=DeploymentState.READY,
            save_on_exhaustion=save,
        )
    )
    svc.repository.save_assignment(
        LeaseAssignment(deployment_id=dep.deployment_id, workload_id=wl.workload_id, hotkey="hk", node_id="n1", status="active")
    )
    svc.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=DeploymentState.SUSPENDED))
    return svc.repository.get_deployment(dep.deployment_id), wl


def _one_day_of_storage_due(svc, deployment_id):
    # Storage accrues ~69 millicents a minute; skip ahead to a whole dollar.
    with session_scope(svc.repository.session_factory) as s:
        s.get(DeploymentORM, deployment_id).storage_remainder_mcents = 100_000 - 69


def _balance(svc):
    with session_scope(svc.repository.session_factory) as s:
        return s.get(UserORM, "u1").balance_credits


def test_a_saved_pod_is_billed_storage_while_suspended(cp):
    dep, _ = _suspended(cp)
    _one_day_of_storage_due(cp, dep.deployment_id)
    assert cp.meter_pod_storage() == {dep.deployment_id: 100}
    assert _balance(cp) == -100


def test_deleting_a_saved_pod_stops_the_storage_charge_and_frees_its_disk(cp):
    dep, _ = _suspended(cp)
    assert [lease.status for lease in cp.list_leases("hk")] == ["suspended"]

    # What DELETE /platform/deployments/{id} runs once ownership is checked.
    cp.cleanup_deployment(dep.deployment_id, reason="user terminated")

    assert cp.repository.get_deployment(dep.deployment_id).state == DeploymentState.TERMINATED
    _one_day_of_storage_due(cp, dep.deployment_id)
    assert cp.meter_pod_storage() == {}
    assert _balance(cp) == 0
    # The lease leaves the node's list, so the node-agent removes the stopped
    # container and its disk instead of keeping them.
    assert cp.list_leases("hk") == []
    assert cp.repository.list_assignments(statuses=["terminated"])[0].deployment_id == dep.deployment_id


def test_an_idle_private_model_is_not_billed_or_reaped_as_a_saved_pod(cp):
    dep, wl = _suspended(cp, kind=WorkloadKind.INFERENCE)
    assert dep.save_on_exhaustion and dep.suspended_at is not None
    _one_day_of_storage_due(cp, dep.deployment_id)
    assert cp.meter_pod_storage() == {}
    assert _balance(cp) == 0
    later = dep.suspended_at.replace(tzinfo=UTC) + timedelta(days=60)
    assert cp.reap_exhausted_pods(now=later) == []
    assert cp.suspension_terms(dep, wl) == (0, None)


def test_suspension_terms_follow_the_billing_and_reaper_rules(cp, monkeypatch):
    saved, wl = _suspended(cp, save=True)
    suspended_at = saved.suspended_at.replace(tzinfo=UTC)
    assert cp.suspension_terms(saved, wl) == (100, suspended_at + timedelta(days=30))

    unsaved, wl2 = _suspended(cp, save=False)
    assert cp.suspension_terms(unsaved, wl2) == (0, unsaved.suspended_at.replace(tzinfo=UTC) + timedelta(minutes=15))

    # The reaper deletes at exactly that time.
    assert cp.reap_exhausted_pods(now=suspended_at + timedelta(days=30) - timedelta(seconds=1)) == [unsaved.deployment_id]
    assert cp.reap_exhausted_pods(now=suspended_at + timedelta(days=30, seconds=1)) == [saved.deployment_id]

    running = DeploymentRecord(workload_id=wl.workload_id, state=DeploymentState.READY)
    assert cp.suspension_terms(running, wl) == (0, None)

    monkeypatch.setattr(settings, "pod_storage_billing_enabled", False)
    monkeypatch.setattr(settings, "pod_exhaustion_reaper_enabled", False)
    third, wl3 = _suspended(cp, save=True)
    assert cp.suspension_terms(third, wl3) == (0, None)


def test_the_api_shows_what_a_suspended_rental_costs_and_when_it_goes(cp, monkeypatch):
    dep, _ = _suspended(cp)
    monkeypatch.setattr(routes.service, "control_plane", cp)
    out = routes._deployment_out(dep, admin=False)
    assert out["storage_cents_per_day"] == 100
    assert out["deletes_at"].startswith(str((dep.suspended_at + timedelta(days=30)).date()))

    ready = DeploymentRecord(workload_id=dep.workload_id, state=DeploymentState.READY)
    out = routes._deployment_out(ready, admin=False)
    assert out["storage_cents_per_day"] == 0 and out["deletes_at"] is None


# --- API callers don't save a pod unless they ask -----------------------------


def test_save_on_exhaustion_is_off_unless_asked_for():
    assert GatewayDeploymentCreateRequest(workload_id="w").save_on_exhaustion is False
    assert GatewayDeploymentCreateRequest(workload_id="w", save_on_exhaustion=True).save_on_exhaustion is True
    # Internal callers of the protocol model keep its default.
    assert DeploymentCreateRequest(workload_id="w").save_on_exhaustion is True


def test_post_deployments_without_the_field_does_not_save_the_pod(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    body = spec["paths"]["/platform/deployments"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/GatewayDeploymentCreateRequest"}
    field = spec["components"]["schemas"]["GatewayDeploymentCreateRequest"]["properties"]["save_on_exhaustion"]
    assert field["default"] is False

    seen = []

    def fake_create(payload, *, user_id=None, admin=False, created_by_key_id=None):
        seen.append(payload.save_on_exhaustion)
        return DeploymentRecord(workload_id=payload.workload_id, owner_user_id=user_id)

    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", key_id="k1", admin=False))
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_enforce_key_limits", lambda *a, **k: None)
    monkeypatch.setattr(routes.service, "create_deployment", fake_create)
    for raw in ({"workload_id": "w1"}, {"workload_id": "w1", "save_on_exhaustion": True}):
        routes.create_deployment(
            GatewayDeploymentCreateRequest.model_validate(raw), Response(),
            authorization="Bearer k", x_api_key=None, idempotency_key=None,
        )
    assert seen == [False, True]


def test_a_dict_request_to_the_gateway_service_does_not_save_the_pod(monkeypatch):
    from greencompute_gateway.application.services import GatewayService

    captured = {}
    workload = WorkloadSpec(name="job", image="img", kind=WorkloadKind.POD)
    control_plane = SimpleNamespace(
        repository=SimpleNamespace(get_workload=lambda _id: workload),
        create_deployment=lambda body: captured.update(body) or DeploymentRecord(workload_id=body["workload_id"]),
    )
    gateway = GatewayService.__new__(GatewayService)
    gateway.control_plane = control_plane
    gateway.create_deployment({"workload_id": workload.workload_id}, admin=True)
    assert captured["save_on_exhaustion"] is False

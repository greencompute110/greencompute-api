"""Guardrails for unattended (agent) clients: a runtime cap so a forgotten
rental stops paying, idempotent creates so a retried request can't rent
twice, Retry-After on rate limits, and listing deployments by state."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes, security
from greencompute_protocol import (
    DeploymentCreateRequest,
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    WorkloadSpec,
)


# --- runtime cap and ready_at --------------------------------------------------


@pytest.fixture
def cp():
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    svc = ControlPlaneService(repository=repo)
    wl = repo.upsert_workload(WorkloadSpec(name="job", image="pytorch", kind="pod"))
    return svc, wl


def _make_ready(svc, deployment_id):
    for state in (DeploymentState.SCHEDULED, DeploymentState.READY):
        svc.update_deployment_status(DeploymentStatusUpdate(deployment_id=deployment_id, state=state))
    return svc.repository.get_deployment(deployment_id)


def test_ready_at_is_set_once_when_billing_starts(cp):
    svc, wl = cp
    dep = svc.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id))
    assert dep.ready_at is None
    ready = _make_ready(svc, dep.deployment_id)
    assert ready.ready_at is not None
    # A repeated READY report from the miner must not move the start time.
    svc.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=DeploymentState.READY))
    assert svc.repository.get_deployment(dep.deployment_id).ready_at == ready.ready_at


def test_runtime_cap_stops_the_deployment_at_the_limit_and_not_before(cp):
    svc, wl = cp
    dep = svc.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id, max_runtime_minutes=30))
    ready = _make_ready(svc, dep.deployment_id)
    started = ready.ready_at if ready.ready_at.tzinfo else ready.ready_at.replace(tzinfo=UTC)

    assert svc.stop_expired_runtimes(now=started + timedelta(minutes=29)) == []
    assert svc.stop_expired_runtimes(now=started + timedelta(minutes=30, seconds=1)) == [dep.deployment_id]
    stopped = svc.repository.get_deployment(dep.deployment_id)
    assert stopped.state == DeploymentState.TERMINATED
    assert "max_runtime_minutes=30" in stopped.last_error


def test_no_cap_means_no_automatic_stop(cp):
    svc, wl = cp
    dep = svc.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id))
    _make_ready(svc, dep.deployment_id)
    assert svc.stop_expired_runtimes(now=datetime.now(UTC) + timedelta(days=365)) == []


def test_runtime_cap_is_validated():
    with pytest.raises(ValueError):
        DeploymentCreateRequest(workload_id="w", max_runtime_minutes=0)


# --- idempotent creates --------------------------------------------------------


@pytest.fixture
def created(monkeypatch):
    calls = []

    def fake_create(payload, *, user_id=None, admin=False, created_by_key_id=None):
        calls.append(payload)
        return DeploymentRecord(workload_id=payload.workload_id, owner_user_id=user_id)

    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", key_id="k1", admin=False))
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(routes.service, "repository", GatewayRepository(database_url="sqlite:///:memory:", bootstrap=True))
    monkeypatch.setattr(routes.service, "create_deployment", fake_create)
    return calls


def _create(workload_id="w1", key="retry-1"):
    response = Response()
    body = routes.create_deployment(
        DeploymentCreateRequest(workload_id=workload_id), response,
        authorization="Bearer k", x_api_key=None, idempotency_key=key,
    )
    return body, response


def test_resending_with_the_same_key_returns_the_first_rental(created):
    first, _ = _create()
    second, response = _create()
    assert len(created) == 1
    assert second == first
    assert response.headers.get("Idempotent-Replayed") == "true"


def test_same_key_with_a_different_body_is_refused(created):
    _create(workload_id="w1")
    with pytest.raises(HTTPException) as exc:
        _create(workload_id="w2")
    assert exc.value.status_code == 422
    assert len(created) == 1


def test_a_failed_request_can_be_retried_with_the_same_key(created, monkeypatch):
    def broke(payload, **kw):
        raise KeyError("workload not found")

    original = routes.service.create_deployment
    monkeypatch.setattr(routes.service, "create_deployment", broke)
    with pytest.raises(HTTPException) as exc:
        _create()
    assert exc.value.status_code == 404
    monkeypatch.setattr(routes.service, "create_deployment", original)
    body, response = _create()
    assert body["workload_id"] == "w1" and "Idempotent-Replayed" not in response.headers


def test_without_a_key_every_request_creates(created):
    _create(key=None)
    _create(key=None)
    assert len(created) == 2


def test_keys_are_per_account(created, monkeypatch):
    _create()
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u2", key_id="k2", admin=False))
    _create()
    assert len(created) == 2


# --- Retry-After and state filter ------------------------------------------------


def test_rate_limit_says_when_to_retry():
    for _ in range(2):
        security.enforce_rate_limit("guardrail-test", "k-retry", limit=2, window_seconds=60)
    with pytest.raises(HTTPException) as exc:
        security.enforce_rate_limit("guardrail-test", "k-retry", limit=2, window_seconds=60)
    retry_after = int(exc.value.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert exc.value.detail["retry_after_seconds"] == retry_after
    assert exc.value.detail["limit"] == 2


def test_list_deployments_by_state(monkeypatch):
    rows = [
        DeploymentRecord(workload_id="w", state=DeploymentState.READY, owner_user_id="u1"),
        DeploymentRecord(workload_id="w", state=DeploymentState.TERMINATED, owner_user_id="u1"),
        DeploymentRecord(workload_id="w", state=DeploymentState.FAILED, owner_user_id="u1"),
    ]
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", key_id="k1", admin=False))
    monkeypatch.setattr(routes.service, "list_deployments", lambda **kw: rows)
    states = lambda q: sorted(r["state"] for r in routes.list_deployments(state=q, authorization="x", x_api_key=None))
    assert states(None) == ["failed", "ready", "terminated"]
    assert states("terminated") == ["terminated"]
    assert states("Terminated, failed") == ["failed", "terminated"]
    with pytest.raises(HTTPException) as exc:
        routes.list_deployments(state="stopped", authorization="x", x_api_key=None)
    assert exc.value.status_code == 400

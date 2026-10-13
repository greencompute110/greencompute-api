"""Per-key limits: how many rentals a key may have running and how much it may
spend in any 24 hours, so a leaked or runaway key can't drain the account."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.key_limits import key_active_rentals, key_spend_cents
from greencompute_persistence.orm import APIKeyORM, DeploymentORM, InvocationRecordORM, LedgerEntryORM
from greencompute_protocol import APIKeyCreateRequest, APIKeyRecord, DeploymentCreateRequest, DeploymentState


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'gc.sqlite'}"
    cp_repo = ControlPlaneRepository(database_url=url, bootstrap=True)
    return SimpleNamespace(url=url, cp=cp_repo, sf=cp_repo.session_factory)


def _deployment(sf, key_id, state="ready"):
    dep_id = str(uuid4())
    with session_scope(sf) as s:
        s.add(DeploymentORM(deployment_id=dep_id, workload_id="w", owner_user_id="u1", state=state,
                            requested_instances=1, created_by_key_id=key_id))
    return dep_id


def _charge(sf, cents, ref, kind="usage", when=None):
    with session_scope(sf) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id="u1", amount_cents=-cents, balance_after=0,
                             kind=kind, reference_id=ref, created_at=when or datetime.now(UTC)))


def _inference(sf, key_id, cents):
    request_id = str(uuid4())
    with session_scope(sf) as s:
        s.add(InvocationRecordORM(invocation_id=str(uuid4()), request_id=request_id, deployment_id="d", workload_id="w", hotkey="h", model="m",
                                  api_key_id=key_id))
    _charge(sf, cents, request_id, kind="inference")


def test_spend_adds_rentals_and_inference_for_that_key_only(db):
    d1 = _deployment(db.sf, "k1")
    _charge(db.sf, 100, d1)
    _charge(db.sf, 20, d1, kind="storage")
    _charge(db.sf, 500, d1, when=datetime.now(UTC) - timedelta(hours=30))  # outside the 24h window
    _charge(db.sf, 70, _deployment(db.sf, "k2"))
    _inference(db.sf, "k1", 30)
    _inference(db.sf, "k2", 5)
    assert key_spend_cents(db.sf, "k1") == 150
    assert key_spend_cents(db.sf, "k2") == 75


def test_active_rentals_ignore_finished_ones(db):
    _deployment(db.sf, "k1", "ready")
    _deployment(db.sf, "k1", "pending")
    _deployment(db.sf, "k1", "terminated")
    _deployment(db.sf, "k1", "failed")
    assert key_active_rentals(db.sf, "k1") == 2


def _key(**limits):
    return APIKeyRecord(key_id="k1", name="agent", user_id="u1", secret="s", **limits)


@pytest.fixture
def gateway(db, monkeypatch):
    monkeypatch.setattr(routes.service, "repository", GatewayRepository(database_url=db.url))
    calls = []

    def fake_create(payload, **kw):
        calls.append(kw)
        return SimpleNamespace(model_dump=lambda **_: {"workload_id": payload.workload_id})

    monkeypatch.setattr(routes.service, "create_deployment", fake_create)
    return calls


def test_rental_refused_at_the_running_limit(db, gateway):
    _deployment(db.sf, "k1")
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), _key(max_active_rentals=1))
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "key_limit"
    routes._create_deployment(DeploymentCreateRequest(workload_id="w"), _key(max_active_rentals=2))
    assert gateway[-1]["created_by_key_id"] == "k1"


def test_rental_and_inference_refused_at_the_spend_limit(db, gateway):
    _charge(db.sf, 250, _deployment(db.sf, "k1", "terminated"))
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), _key(daily_spend_limit_usd=2.5))
    assert exc.value.detail["spent_last_24h_usd"] == 2.5
    with pytest.raises(HTTPException):
        routes._enforce_key_limits(_key(daily_spend_limit_usd=2.5))  # what /v1 requests check
    routes._enforce_key_limits(_key(daily_spend_limit_usd=3.0))


def test_new_keys_inherit_limits_and_can_only_tighten_them():
    svc = GatewayService(repository=SimpleNamespace(save_api_key=lambda rec: rec))
    caller = _key(max_active_rentals=2, daily_spend_limit_usd=10.0)
    mint = lambda **kw: svc.create_api_key(APIKeyCreateRequest(name="x", **kw), caller_user_id="u1",
                                           allow_user_id_override=False, caller_limits=caller)
    inherited = mint()
    assert (inherited.max_active_rentals, inherited.daily_spend_limit_usd) == (2, 10.0)
    tighter = mint(max_active_rentals=1, daily_spend_limit_usd=5.0)
    assert (tighter.max_active_rentals, tighter.daily_spend_limit_usd) == (1, 5.0)
    with pytest.raises(PermissionError):
        mint(max_active_rentals=3)
    with pytest.raises(PermissionError):
        mint(daily_spend_limit_usd=20.0)


def test_running_rentals_stop_when_their_key_reaches_its_limit(db):
    svc = ControlPlaneService(repository=db.cp)
    with session_scope(db.sf) as s:
        s.add(APIKeyORM(key_id="k1", user_id="u1", name="agent", admin=False, scopes=[], secret="s1",
                        daily_spend_limit_cents=100))
        s.add(APIKeyORM(key_id="k2", user_id="u1", name="other", admin=False, scopes=[], secret="s2"))
    over = _deployment(db.sf, "k1")
    _charge(db.sf, 130, over)
    unlimited = _deployment(db.sf, "k2")
    _charge(db.sf, 9999, unlimited)

    assert svc.stop_rentals_over_key_limits() == [over]
    assert db.cp.get_deployment(over).state == DeploymentState.TERMINATED
    assert "spend limit of $1.00" in db.cp.get_deployment(over).last_error
    assert db.cp.get_deployment(unlimited).state == DeploymentState.READY

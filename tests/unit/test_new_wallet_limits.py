"""A wallet account in its first week: at most 2 rentals at once and $50 of
spend a day for the whole account, until it is 7 days old or the website has
confirmed an email for it. Email and Google accounts are never limited."""
import types
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from greencompute_gateway.application.services import GatewayService, NewAccountLimitError
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.key_limits import account_spend_cents, new_account_limits, new_account_room
from greencompute_persistence.orm import DeploymentORM, LedgerEntryORM, UserORM
from greencompute_protocol import DeploymentCreateRequest


@pytest.fixture
def sf():
    return GatewayRepository(database_url="sqlite:///:memory:", bootstrap=True).session_factory


def _account(sf, uid="w1", *, signup="wallet", age=timedelta(days=1), verified=False):
    with session_scope(sf) as s:
        s.add(UserORM(user_id=uid, username=uid, email=None, balance_credits=10_000, profile_metadata={},
                      created_at=datetime.now(UTC) - age, signup_method=signup,
                      email_verified_at=datetime.now(UTC) if verified else None))
    return uid


def _rental(sf, uid, state="ready"):
    with session_scope(sf) as s:
        s.add(DeploymentORM(deployment_id=str(uuid4()), workload_id="w", owner_user_id=uid, state=state,
                            requested_instances=1))


def _charge(sf, uid, cents, kind="usage", when=None):
    with session_scope(sf) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=uid, amount_cents=-cents, balance_after=0, kind=kind,
                             created_at=when or datetime.now(UTC)))


def test_who_is_limited(sf):
    assert new_account_limits(sf, _account(sf, "new"))["max_active_rentals"] == 2
    assert new_account_limits(sf, "new")["daily_spend_limit_usd"] == 50
    assert new_account_limits(sf, _account(sf, "week-old", age=timedelta(days=7, minutes=1))) is None
    assert new_account_limits(sf, _account(sf, "confirmed", verified=True)) is None
    assert new_account_limits(sf, _account(sf, "google", signup=None)) is None
    assert new_account_limits(sf, "nobody") is None and new_account_limits(sf, None) is None


def test_the_limits_come_from_the_environment(sf, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_NEW_WALLET_MAX_RENTALS", "5")
    monkeypatch.setenv("GREENCOMPUTE_NEW_WALLET_DAILY_SPEND_USD", "12.5")
    monkeypatch.setenv("GREENCOMPUTE_NEW_WALLET_DAYS", "1")
    uid = _account(sf, age=timedelta(hours=1))
    assert {k: v for k, v in new_account_limits(sf, uid).items() if k != "until"} == {
        "max_active_rentals": 5, "daily_spend_limit_usd": 12.5}
    assert new_account_limits(sf, _account(sf, "older", age=timedelta(hours=25))) is None


def test_rentals_at_once_count_every_key_and_finished_ones_dont(sf):
    uid = _account(sf)
    _rental(sf, uid)
    _rental(sf, uid, "terminated")
    _rental(sf, uid, "failed")
    assert new_account_room(sf, uid)[0] is None
    _rental(sf, uid, "pending")
    why, numbers = new_account_room(sf, uid)
    assert (why, numbers["active_rentals"]) == ("active", 2)


def test_spend_is_the_account_s_last_24_hours_without_staff_clawbacks(sf):
    uid = _account(sf)
    _charge(sf, uid, 3000)
    _charge(sf, uid, 1000, kind="inference")
    _charge(sf, uid, 9000, kind="refund")  # staff taking a payment back isn't spending
    _charge(sf, uid, 9000, when=datetime.now(UTC) - timedelta(hours=25))
    assert account_spend_cents(sf, uid) == 4000
    assert new_account_room(sf, uid)[0] is None
    _charge(sf, uid, 1000, kind="storage")
    why, numbers = new_account_room(sf, uid)
    assert (why, numbers["spent_cents"]) == ("spend", 5000)


def _service(sf, uid):
    workload = types.SimpleNamespace(workload_id="w", owner_user_id=uid, public=False, metadata={},
                                     requirements=types.SimpleNamespace(supported_gpu_models=[], gpu_count=1))
    created = []
    cp = types.SimpleNamespace(
        repository=types.SimpleNamespace(get_workload=lambda _id: workload),
        online_gpu_models=lambda: ["rtx4090"],
        placement_preview=lambda w: (True, {}),
        create_deployment=lambda body: created.append(body) or types.SimpleNamespace(created=True),
    )
    svc = GatewayService(repository=types.SimpleNamespace(session_factory=sf, get_api_key=lambda _k: None),
                         control_plane=cp)
    svc._billing_repo = types.SimpleNamespace(get_balance=lambda _u: 10_000)
    return svc, created


def test_create_deployment_holds_a_new_wallet_account_to_its_limits(sf):
    uid = _account(sf)
    svc, created = _service(sf, uid)
    svc.create_deployment({"workload_id": "w"}, user_id=uid, created_by_key_id="k1")
    _rental(sf, uid)
    _rental(sf, uid)
    with pytest.raises(NewAccountLimitError) as exc:
        svc.create_deployment({"workload_id": "w"}, user_id=uid, created_by_key_id="k1")
    assert exc.value.reason == "active" and len(created) == 1
    svc.create_deployment({"workload_id": "w"}, user_id=uid, admin=True)  # staff aren't held to it


def test_the_route_says_what_the_limit_is_and_how_it_ends(sf, monkeypatch):
    uid = _account(sf)
    _charge(sf, uid, 5000)
    svc, _ = _service(sf, uid)
    monkeypatch.setattr(routes, "service", svc)
    key = types.SimpleNamespace(user_id=uid, admin=False, key_id="k1", max_active_rentals=None,
                                daily_spend_limit_usd=None)
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), key)
    detail = exc.value.detail
    assert (exc.value.status_code, exc.value.error_code, detail["limit"]) == (403, "new_account_limit", "spend")
    assert detail["daily_spend_limit_usd"] == 50 and detail["spent_last_24h_usd"] == 50.0
    assert "$50.00" in detail["message"] and "first 7 days" in detail["message"]
    assert detail["until"] and "email" in detail["hint"]

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


def test_a_new_wallet_account_s_creates_take_turns_on_postgres_without_waiting():
    """Requests sent at once must not each see room for one more rental, and a
    second one is refused at once rather than parked on a pooled connection
    (15 parked waiters for one free account would stall every customer)."""
    from greencompute_gateway.application.services import NewAccountBusyError, _account_rental_lock

    seen = []
    held = {"now": False}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            name = str(statement).split("(")[0].replace("SELECT ", "")
            seen.append(name)
            got = name == "pg_try_advisory_lock" and not held["now"]
            if got:
                held["now"] = True
            if name == "pg_advisory_unlock":
                held["now"] = False
            return types.SimpleNamespace(scalar=lambda: got)

        def commit(self):
            pass

    engine = types.SimpleNamespace(dialect=types.SimpleNamespace(name="postgresql"), connect=Conn)
    svc = types.SimpleNamespace(repository=types.SimpleNamespace(engine=engine))
    with _account_rental_lock(svc, "w1"):
        seen.append("create")
        with pytest.raises(NewAccountBusyError):  # a second request while the first is inside
            with _account_rental_lock(svc, "w1"):
                seen.append("never")
    assert seen == ["pg_try_advisory_lock", "create", "pg_try_advisory_lock", "pg_advisory_unlock"]
    with _account_rental_lock(svc, None):  # accounts without the limits never lock
        pass
    assert len(seen) == 4


def test_a_busy_new_account_hears_to_send_it_again(monkeypatch):
    from greencompute_gateway.application.services import NewAccountBusyError

    def busy(*a, **k):
        raise NewAccountBusyError("w1")

    monkeypatch.setattr(routes, "service", types.SimpleNamespace(create_deployment=busy))
    key = types.SimpleNamespace(user_id="w1", admin=False, key_id="k1", max_active_rentals=None,
                                daily_spend_limit_usd=None, scopes=[])
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), key)
    assert (exc.value.status_code, exc.value.headers["Retry-After"]) == (409, "2")
    assert "again in a moment" in exc.value.detail


def test_running_rentals_stop_when_a_new_wallet_account_reaches_its_daily_spend(tmp_path):
    """"$50 in 24 hours" holds for rentals already running, not only new ones."""
    from greencompute_control_plane.application.services import ControlPlaneService
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_protocol import DeploymentState

    cp = ControlPlaneRepository(database_url=f"sqlite:///{tmp_path / 'cp.sqlite'}", bootstrap=True)
    sf = cp.session_factory
    over, fine, old = _account(sf, "w-over"), _account(sf, "w-fine"), _account(sf, "w-old", age=timedelta(days=9))
    for uid, cents in ((over, 5000), (fine, 4999), (old, 9999)):
        _rental(sf, uid)
        _charge(sf, uid, cents)
    svc = ControlPlaneService(repository=cp)
    stopped = svc.stop_rentals_over_account_limits()
    assert len(stopped) == 1
    dep = cp.get_deployment(stopped[0])
    assert dep.owner_user_id == over and dep.state == DeploymentState.TERMINATED
    assert "$50.00 in 24 hours" in dep.last_error and "email" in dep.last_error
    assert svc.stop_rentals_over_account_limits() == []  # nothing left over the limit

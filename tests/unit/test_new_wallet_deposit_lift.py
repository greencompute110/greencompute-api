"""A new wallet account's first-week limits (2 rentals at once, $50 of spend
per 24 hours) also end once it has paid in $20 in crypto (owner decision,
2026-11-17): deposits credited from the chain, by the watchers or staff
assigning one, never a staff credit or a card payment. A confirmed email
still ends them, and so does the end of the week."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import NewAccountLimitError
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.key_limits import account_limits, new_account_lift_text, new_account_limits
from greencompute_persistence.orm import CryptoDepositORM, DeploymentORM, LedgerEntryORM, UserORM
from greencompute_protocol import DeploymentCreateRequest, DeploymentState

from test_new_wallet_limits import _service


@pytest.fixture
def db(tmp_path):
    cp = ControlPlaneRepository(database_url=f"sqlite:///{tmp_path / 'lift.sqlite'}", bootstrap=True)
    return SimpleNamespace(cp=cp, sf=cp.session_factory)


def _wallet_account(sf, uid="w1", *, age=timedelta(days=1), verified=False):
    with session_scope(sf) as s:
        s.add(UserORM(user_id=uid, username=uid, email=None, balance_credits=10_000, profile_metadata={},
                      created_at=datetime.now(UTC) - age, signup_method="wallet",
                      email_verified_at=datetime.now(UTC) if verified else None))
    return uid


def _deposit(sf, uid, cents, *, status="credited", currency="tao", matched_by="wallet"):
    """A transfer the deposit watcher logged (and, when credited, the
    ledger top-up it paid)."""
    ref = f"{currency}:{uuid4().hex}:0"
    with session_scope(sf) as s:
        s.add(CryptoDepositORM(deposit_ref=ref, currency=currency, sender="5Sender", deposit_address="5Ours",
                               amount_raw=cents, decimals=2, block_number=1, usd_price=1.0, status=status,
                               matched_by=matched_by, user_id=uid if status == "credited" else None,
                               credited_cents=cents if status == "credited" else None))
        if status == "credited":
            s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=uid, amount_cents=cents, balance_after=0,
                                 kind="topup", deposit_ref=ref, created_at=datetime.now(UTC)))


def _staff_credit(sf, uid, cents, *, deposit_ref=None):
    with session_scope(sf) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=uid, amount_cents=cents, balance_after=0, kind="topup",
                             deposit_ref=deposit_ref, description="Manual admin credit", created_at=datetime.now(UTC)))


def _rental(sf, uid, state="ready"):
    dep_id = str(uuid4())
    with session_scope(sf) as s:
        s.add(DeploymentORM(deployment_id=dep_id, workload_id="w", owner_user_id=uid, state=state,
                            requested_instances=1))
    return dep_id


def _spend(sf, uid, cents):
    with session_scope(sf) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=uid, amount_cents=-cents, balance_after=0, kind="usage",
                             created_at=datetime.now(UTC)))


def test_19_99_in_crypto_still_limited_and_20_lifts_it(db):
    uid = _wallet_account(db.sf)
    limits = new_account_limits(db.sf, uid)
    assert (limits["lifts_after_deposit_usd"], limits["deposited_usd"]) == (20.0, 0.0)
    _deposit(db.sf, uid, 1500, currency="usdc-base")
    _deposit(db.sf, uid, 499)
    assert new_account_limits(db.sf, uid)["deposited_usd"] == 19.99
    _deposit(db.sf, uid, 1)
    assert new_account_limits(db.sf, uid) is None
    assert account_limits(db.sf, uid) is None


def test_staff_credits_card_payments_and_uncredited_transfers_dont_count(db):
    uid = _wallet_account(db.sf)
    _staff_credit(db.sf, uid, 5000)  # an admin credit: kind topup, no deposit
    _staff_credit(db.sf, uid, 5000, deposit_ref=f"stripe:cs_{uuid4().hex}")  # a card payment
    _deposit(db.sf, uid, 5000, status="unmatched")
    _deposit(db.sf, uid, 5000, status="blocked")
    assert new_account_limits(db.sf, uid)["deposited_usd"] == 0.0


def test_a_deposit_staff_assigned_from_the_chain_counts(db):
    uid = _wallet_account(db.sf)
    _deposit(db.sf, uid, 2000, matched_by="admin")
    assert new_account_limits(db.sf, uid) is None


def test_an_email_or_the_end_of_the_week_still_lifts_it(db):
    assert new_account_limits(db.sf, _wallet_account(db.sf, "confirmed", verified=True)) is None
    assert new_account_limits(db.sf, _wallet_account(db.sf, "week-old", age=timedelta(days=7, minutes=1))) is None
    assert new_account_limits(db.sf, _wallet_account(db.sf, "new")) is not None


def test_the_threshold_comes_from_the_environment(db, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_NEW_WALLET_LIFT_DEPOSIT_USD", "100")
    uid = _wallet_account(db.sf)
    _deposit(db.sf, uid, 5000)
    assert new_account_limits(db.sf, uid)["lifts_after_deposit_usd"] == 100.0
    _deposit(db.sf, uid, 5000)
    assert new_account_limits(db.sf, uid) is None


def test_starting_a_rental_sees_the_deposit_at_once(db):
    uid = _wallet_account(db.sf)
    _rental(db.sf, uid)
    _rental(db.sf, uid)
    svc, created = _service(db.sf, uid)
    with pytest.raises(NewAccountLimitError):
        svc.create_deployment({"workload_id": "w"}, user_id=uid, created_by_key_id="k1")
    _deposit(db.sf, uid, 2000)
    svc.create_deployment({"workload_id": "w"}, user_id=uid, created_by_key_id="k1")
    assert len(created) == 1


def test_the_control_plane_pause_sees_the_deposit_and_says_how_to_lift_it(db):
    svc = ControlPlaneService(repository=db.cp)
    svc.bus = SimpleNamespace(publish=lambda subject, data: None)
    paid, short = _wallet_account(db.sf, "paid"), _wallet_account(db.sf, "short")
    running = {uid: _rental(db.sf, uid) for uid in (paid, short)}
    for uid in (paid, short):
        _spend(db.sf, uid, 5000)  # at the $50 first-week limit
    _deposit(db.sf, paid, 2000)
    _deposit(db.sf, short, 500)
    assert svc.stop_rentals_over_account_limits() == [running[short]]
    assert db.cp.get_deployment(running[paid]).state == DeploymentState.READY
    reason = db.cp.get_deployment(running[short]).last_error
    assert reason.startswith("paused: a new wallet account can spend $50.00 in 24 hours in its first week")
    assert "deposit $15.00 more in crypto (TAO, alpha, USDC or USDT) or add an email address" in reason
    assert "or wait until 20" in reason


def test_the_403_says_how_much_more_to_deposit_or_when_it_ends(db, monkeypatch):
    uid = _wallet_account(db.sf)
    _deposit(db.sf, uid, 1250)
    _spend(db.sf, uid, 5000)
    svc, _ = _service(db.sf, uid)
    monkeypatch.setattr(routes, "service", svc)
    key = SimpleNamespace(user_id=uid, admin=False, key_id="k1", max_active_rentals=None, daily_spend_limit_usd=None)
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), key)
    detail = exc.value.detail
    assert (exc.value.error_code, detail["limit"]) == ("new_account_limit", "spend")
    assert (detail["lifts_after_deposit_usd"], detail["deposited_usd"]) == (20.0, 12.5)
    assert detail["message"] == ("New wallet accounts can spend $50.00 in any 24 hours for their first 7 days, "
                                 "until they have paid in $20.00 in crypto")
    assert "deposit $7.50 more in crypto (TAO, alpha, USDC or USDT)" in detail["hint"]
    assert "add an email address on the website (Settings)" in detail["hint"]
    assert detail["until"][:10] in detail["hint"]


def test_the_lift_text():
    until = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    assert new_account_lift_text({"lifts_after_deposit_usd": 20.0, "deposited_usd": 5.5, "until": until}) == (
        "deposit $14.50 more in crypto (TAO, alpha, USDC or USDT) or add an email address on the website "
        "(Settings), or wait until 2026-11-24 10:00 UTC")


def test_get_account_limits_says_what_lifts_them(db, monkeypatch):
    uid = _wallet_account(db.sf)
    _deposit(db.sf, uid, 800)
    monkeypatch.setattr(routes, "service", SimpleNamespace(repository=SimpleNamespace(session_factory=db.sf)))
    new = routes._account_limits_out(uid)["new_account_limits"]
    assert (new["max_active_rentals"], new["daily_spend_limit_usd"]) == (2, 50)
    assert (new["lifts_after_deposit_usd"], new["deposited_usd"]) == (20.0, 8.0)
    assert new["until"]
    _deposit(db.sf, uid, 1200)
    assert routes._account_limits_out(uid)["new_account_limits"] is None

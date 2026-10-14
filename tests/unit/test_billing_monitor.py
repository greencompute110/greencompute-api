"""The billing monitor: catches balances changed without a ledger row, rentals
running uncharged or on an empty balance, charges into debt, credits worth a
look and spend spikes, without repeating itself; plus the daily summary."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from greencompute_gateway.application import billing_monitor as mon
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    DeploymentORM,
    KeyValueStateORM,
    LedgerEntryORM,
    UserORM,
    WorkloadORM,
)

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)


@pytest.fixture
def sf(tmp_path, monkeypatch):
    for name in ("GREENCOMPUTE_MONITOR_LARGE_CREDIT_USD", "GREENCOMPUTE_MONITOR_SPIKE_FLOOR_USD"):
        monkeypatch.delenv(name, raising=False)
    repo = BillingRepository(database_url=f"sqlite:///{tmp_path / 'm.sqlite'}", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        for uid, age in (("alice", 90), ("bob", 90), ("newbie", 1)):
            s.add(UserORM(user_id=uid, username=uid, balance_credits=0, created_at=NOW - timedelta(days=age)))
    return repo.session_factory


def ledger(sf, user, cents, kind="usage", when=NOW, ref=None, deposit_ref=None, description=None):
    """Add a ledger row and move the balance with it, the way billing does."""
    with session_scope(sf) as s:
        u = s.get(UserORM, user)
        u.balance_credits += cents
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=user, amount_cents=cents, balance_after=u.balance_credits,
                             kind=kind, reference_id=ref, deposit_ref=deposit_ref, description=description,
                             created_at=when))


def rental(sf, owner, kind="pod", ready_minutes_ago=30, gpus=1, rate=40):
    wid, did = str(uuid4()), str(uuid4())
    with session_scope(sf) as s:
        s.add(WorkloadORM(workload_id=wid, owner_user_id=owner, name="w", image="img", kind=kind,
                          security_tier="standard", pricing_class="standard", runtime={},
                          requirements={"gpu_count": gpus}))
        s.add(DeploymentORM(deployment_id=did, workload_id=wid, owner_user_id=owner, state="ready",
                            requested_instances=1, hourly_rate_cents=rate,
                            ready_at=NOW - timedelta(minutes=ready_minutes_ago)))
    return did


def run(sf, now=NOW):
    sent = []
    result = mon.run_once(sf, now=now, send=sent.append)
    alerts = [m for m in sent if m.startswith("Billing monitor")]
    return result, "\n".join(alerts)


def checks(result):
    return sorted(f["check"] for f in result["alerted"])


def test_a_healthy_account_raises_nothing(sf):
    ledger(sf, "alice", 5000, kind="topup", deposit_ref="stripe:pi_1", description="Stripe payment $50.00",
           when=NOW - timedelta(days=3))
    did = rental(sf, "alice")
    ledger(sf, "alice", -1, ref=did, when=NOW - timedelta(minutes=2))
    result, alerts = run(sf)
    assert result["alerted"] == [] and alerts == ""


def test_balance_changed_without_a_ledger_row(sf):
    with session_scope(sf) as s:
        s.get(UserORM, "bob").balance_credits = 700
    result, alerts = run(sf)
    assert checks(result) == ["ledger_drift"]
    assert "bob: balance $7.00 but the ledger adds up to $0.00" in alerts
    assert run(sf, NOW + timedelta(minutes=5))[0]["alerted"] == []  # not repeated
    assert checks(run(sf, NOW + timedelta(hours=7))[0]) == ["ledger_drift"]  # still broken: reminded
    with session_scope(sf) as s:
        s.get(UserORM, "bob").balance_credits = 900  # it got worse: reported straight away
    assert checks(run(sf, NOW + timedelta(hours=7, minutes=5))[0]) == ["ledger_drift"]


def test_a_rental_running_without_being_charged(sf):
    ledger(sf, "alice", 5000, kind="topup", deposit_ref="stripe:pi_1", when=NOW - timedelta(days=1))
    stuck = rental(sf, "alice")
    ledger(sf, "alice", -1, ref=stuck, when=NOW - timedelta(minutes=40))
    rental(sf, "alice", ready_minutes_ago=5)  # too new to judge
    rental(sf, "alice", kind="inference")  # charged per token, not hourly
    result, alerts = run(sf)
    assert checks(result) == ["unbilled_rental"]
    assert f"rental {stuck[:8]} is running but last charged 40 min ago" in alerts


def test_a_rental_on_an_empty_balance_is_reported_after_the_grace(sf):
    dep = rental(sf, "bob")
    assert run(sf)[0]["alerted"] == []
    assert run(sf, NOW + timedelta(minutes=20))[0]["alerted"] == []
    result, alerts = run(sf, NOW + timedelta(minutes=31))
    assert checks(result) == ["running_on_empty"]
    assert f"bob: rental {dep[:8]} still running on a $0.00 balance for 31 min" in alerts


def test_only_storage_may_take_a_balance_below_zero(sf):
    ledger(sf, "alice", -300, kind="storage", when=NOW - timedelta(minutes=10))
    ledger(sf, "bob", -50, kind="usage", when=NOW - timedelta(minutes=10))
    result, alerts = run(sf)
    assert checks(result) == ["negative_balance"]
    assert "bob: a usage charge of -$0.50 took the balance to -$0.50" in alerts


def test_credits_worth_a_look_are_reported_once(sf):
    t = NOW - timedelta(minutes=20)
    ledger(sf, "alice", 2500, kind="topup", deposit_ref="stripe:pi_2", description="Stripe payment $25.00", when=t)
    ledger(sf, "alice", 15000, kind="topup", deposit_ref="usdc-base:0xabc:0", description="Crypto deposit", when=t)
    ledger(sf, "bob", 5000, kind="topup", description="Exploit Winnings!", when=t)
    ledger(sf, "bob", -1000, kind="refund", description="Refund", when=t)
    result, alerts = run(sf)
    assert checks(result) == ["large_credit", "manual_credit", "manual_debit"]
    assert "alice: crypto top-up $150.00" in alerts and "bob: manual credit $50.00 (Exploit Winnings!)" in alerts
    assert "Stripe payment $25.00" not in alerts
    assert run(sf, NOW + timedelta(minutes=5))[0]["alerted"] == []


def test_new_account_topping_up_repeatedly(sf):
    for i in range(3):
        ledger(sf, "newbie", 1000, kind="topup", deposit_ref=f"stripe:pi_n{i}", when=NOW - timedelta(hours=3 - i))
    result, alerts = run(sf)
    assert checks(result) == ["repeat_topups"]
    assert "newbie (account 1 day old): 3 top-ups totalling $30.00 in 24h" in alerts


def test_spend_spike_against_the_accounts_usual_rate(sf):
    ledger(sf, "alice", 100_000, kind="topup", deposit_ref="stripe:pi_3", when=NOW - timedelta(days=8))
    ledger(sf, "bob", 100_000, kind="topup", deposit_ref="stripe:pi_4", when=NOW - timedelta(days=8))
    for h in range(2, 7 * 24 + 2):
        ledger(sf, "alice", -100, when=NOW - timedelta(hours=h))  # $1/hr all week
    ledger(sf, "alice", -1200, when=NOW - timedelta(minutes=10))  # $12 in the last hour
    ledger(sf, "bob", -500, when=NOW - timedelta(minutes=10))  # under the $10 floor
    result, alerts = run(sf)
    assert checks(result) == ["spend_spike"]
    assert "alice spent $12.00 in the last hour (usually $1.00/hr)" in alerts


def test_daily_summary_once_a_day(sf):
    yesterday = NOW - timedelta(days=1)
    ledger(sf, "alice", 2500, kind="topup", deposit_ref="stripe:pi_5", when=yesterday)
    ledger(sf, "bob", 1100, kind="topup", deposit_ref="tao:1:2", description="Crypto deposit TAO", when=yesterday)
    ledger(sf, "alice", -400, when=yesterday)
    ledger(sf, "bob", -1200, kind="storage", when=yesterday)  # into debt
    rental(sf, "alice", gpus=2)
    sent = []
    mon.run_once(sf, now=NOW, send=sent.append)
    [daily] = [m for m in sent if m.startswith("Green Compute daily")]
    assert "2026-10-13" in daily
    assert "Top-ups: 2, $36.00 (card $25.00, crypto $11.00, manual $0.00)" in daily
    assert "Spend: $16.00 (saved-pod storage $12.00, GPU rentals $4.00)" in daily
    assert "Running now: 1 rental, 2 GPUs, $0.80/hr" in daily
    assert "storage debt $1.00 across 1 account" in daily
    sent.clear()
    mon.run_once(sf, now=NOW + timedelta(hours=1), send=sent.append)
    assert not [m for m in sent if m.startswith("Green Compute daily")]


def test_snapshot_reports_without_remembering(sf):
    with session_scope(sf) as s:
        s.get(UserORM, "bob").balance_credits = 700
    ledger(sf, "alice", 5000, kind="topup", description="Manual admin credit", when=NOW - timedelta(hours=2))
    snap = mon.snapshot(sf, now=NOW)
    assert [f["check"] for f in snap["problems"]] == ["ledger_drift"]
    assert [f["check"] for f in snap["notices"]] == ["manual_credit"]
    assert snap["today"]["topups"]["manual"] == {"count": 1, "cents": 5000}
    with session_scope(sf) as s:
        assert s.query(KeyValueStateORM).filter(KeyValueStateORM.key.like("monitor:%")).count() == 0

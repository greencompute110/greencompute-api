"""Readable billing (#20 and #37 from an external API user): charges added up
per day and per deployment with names and hours, and monthly statements
whose lines reconcile opening + credits - charges = closing, as JSON or CSV."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application import statements as st
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import LedgerEntryORM, UserORM
from greencompute_protocol import DeploymentRecord, WorkloadCreateRequest, WorkloadSpec


@pytest.fixture
def world(tmp_path):
    url = f"sqlite:///{tmp_path / 'b.sqlite'}"
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=url, bootstrap=True))
    billing = BillingRepository(database_url=url, bootstrap=True)
    with session_scope(billing.session_factory) as s:
        s.add(UserORM(user_id="ann", username="ann", email="ann@x.io", balance_credits=0))
    return cp, billing


class Ledger:
    """Writes ledger rows with chosen times, keeping balance_after right."""

    def __init__(self, billing, user_id="ann"):
        self.billing, self.user_id, self.balance = billing, user_id, 0

    def add(self, at, cents, kind, description, ref=None):
        self.balance += cents
        with session_scope(self.billing.session_factory) as s:
            s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=self.user_id, amount_cents=cents,
                                 balance_after=self.balance, kind=kind, reference_id=ref,
                                 description=description, created_at=at))
            s.get(UserORM, self.user_id).balance_credits = self.balance


def _workload(cp, name, gpu_count=1, kind="pod"):
    return cp.repository.upsert_workload(WorkloadSpec(**WorkloadCreateRequest(
        name=name, image="img", kind=kind,
        requirements={"gpu_count": gpu_count, "min_vram_gb_per_gpu": 24, "cpu_cores": 1, "memory_gb": 1,
                      "supported_gpu_models": ["rtx4090"]},
    ).model_dump()))


def _at(day, hour=12, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def october(world):
    """Oct 2026: $20 top-up on the 10th; 'miner' (2× 4090 at $0.40) runs 3 h on
    the 10th and 1.5 h on the 11th; 'trainer' (1×) runs 2 h on the 11th and is
    then deleted with its workload; a saved pod's storage on the 12th; two
    shared-model requests; a $1 refund on the 12th."""
    cp, billing = world
    miner = cp.repository.create_deployment(DeploymentRecord(workload_id=_workload(cp, "miner", 2).workload_id,
                                                             owner_user_id="ann"))
    trainer_wl = _workload(cp, "trainer", 1)
    trainer = cp.repository.create_deployment(DeploymentRecord(workload_id=trainer_wl.workload_id,
                                                               owner_user_id="ann", node_id="n1"))
    led = Ledger(billing)
    led.add(_at(10, 8), 2000, "topup", "Card top-up $20.00")
    # 2× GPU at $0.40 = 80 c/h: 3 h on the 10th (3 rows of 80 c), 1.5 h on the 11th
    for h in range(3):
        led.add(_at(10, 9 + h), -80, "usage", "GPU usage 2× GPU @ $0.40/hr", miner.deployment_id)
    led.add(_at(11, 1), -80, "usage", "GPU usage 2× GPU @ $0.40/hr", miner.deployment_id)
    led.add(_at(11, 2), -40, "usage", "GPU usage 2× GPU @ $0.40/hr", miner.deployment_id)
    for h in range(2):  # 2 h × 40 c
        led.add(_at(11, 5 + h), -40, "usage", "GPU usage 1× GPU @ $0.40/hr", trainer.deployment_id)
    cp.repository.delete_workload(trainer_wl.workload_id)
    led.add(_at(12, 0), -100, "storage", "Saved-pod storage $1.00/day", miner.deployment_id)
    led.add(_at(12, 3), -1, "inference", "Inference qwen2.5-7b-instruct: 31 in + 2 out tokens", "req-1")
    led.add(_at(12, 4), -1, "inference", "Inference qwen2.5-7b-instruct: 34 in + 32 out tokens", "req-2")
    led.add(_at(12, 9), -100, "refund", "Refund of unused credit")
    return cp, billing, miner, trainer


def test_summary_adds_up_per_day_and_per_deployment(october):
    cp, billing, miner, trainer = october
    s = st.spending_summary(billing, cp, "ann", date(2026, 10, 1), date(2026, 10, 31))
    assert (s["topped_up_usd"], s["charged_usd"], s["adjustments_usd"]) == (20.0, 5.42, -1.0)
    assert (s["opening_balance_usd"], s["closing_balance_usd"]) == (0.0, 13.58)
    days = {d["date"]: d for d in s["days"]}
    assert days["2026-10-10"]["gpu_rentals_usd"] == 2.4 and days["2026-10-10"]["topped_up_usd"] == 20.0
    assert days["2026-10-11"]["charged_usd"] == 2.0
    assert days["2026-10-12"]["storage_usd"] == 1.0 and days["2026-10-12"]["shared_models_usd"] == 0.02
    deps = {d["deployment_id"]: d for d in s["deployments"]}
    m = deps[miner.deployment_id]
    assert (m["name"], m["hours"], m["gpu_usd"], m["storage_usd"], m["total_usd"]) == ("miner", 4.5, 3.6, 1.0, 4.6)
    assert (m["gpu_model"], m["gpu_count"], m["state"]) == ("rtx4090", 2, "running")
    t = deps[trainer.deployment_id]
    assert (t["name"], t["state"], t["hours"]) == ("trainer", "deleted", 2.0)
    assert s["deployments"][0]["deployment_id"] == miner.deployment_id  # most expensive first
    assert (s["shared_models_usd"], s["shared_model_requests"]) == (0.02, 2)


def test_statement_lines_reconcile(october):
    cp, billing, miner, trainer = october
    stmt = st.monthly_statement(billing, cp, "ann", "2026-10")
    assert (stmt["month"], stmt["from"], stmt["to"]) == ("2026-10", "2026-10-01", "2026-10-31")
    assert stmt["opening_balance_cents"] + stmt["credits_cents"] - stmt["charges_cents"] == stmt["closing_balance_cents"]
    assert stmt["charges_by_type_cents"] == {"GPU rental": 440, "Saved-pod storage": 100, "Refund": 100,
                                             "Shared models": 2}
    first = stmt["lines"][0]
    assert (first["date"], first["type"], first["amount_cents"]) == ("2026-10-10", "Top-up", 2000)
    miner_day = next(x for x in stmt["lines"] if x["date"] == "2026-10-10" and x["type"] == "GPU rental")
    assert (miner_day["description"], miner_day["quantity"], miner_day["amount_usd"]) == (
        "miner, 2× rtx4090", "3.0 h", -2.4)
    storage = next(x for x in stmt["lines"] if x["type"] == "Saved-pod storage")
    assert storage["quantity"] == "1 day"
    shared = next(x for x in stmt["lines"] if x["type"] == "Shared models")
    assert (shared["description"], shared["quantity"], shared["deployment_id"], shared["amount_cents"]) == (
        "Requests to shared models", "2 requests", None, -2)
    trainer_line = next(x for x in stmt["lines"] if x["deployment_id"] == trainer.deployment_id)
    assert trainer_line["description"] == "trainer, 1× rtx4090 (deleted)"
    dates = [x["date"] for x in stmt["lines"]]
    assert dates == sorted(dates)


def test_statement_csv_has_balances_lines_and_totals(october):
    cp, billing, *_ = october
    import csv
    import io

    csv_text = st.statement_csv(st.monthly_statement(billing, cp, "ann", "2026-10"))
    rows = [r for r in csv.reader(io.StringIO(csv_text)) if r]
    assert ["2026-10-10", "GPU rental", "miner, 2× rtx4090", "3.0 h"] == rows[5][:4] or any(
        r[:4] == ["2026-10-10", "GPU rental", "miner, 2× rtx4090", "3.0 h"] for r in rows)
    assert rows[2] == ["date", "type", "description", "quantity", "deployment_id", "amount_usd"]
    assert rows[3][1:] == ["Opening balance", "", "", "", "0.00"]
    assert any(r[1] == "Closing balance" and r[-1] == "13.58" for r in rows)
    assert rows[-2] == ["Total added", "", "", "", "", "20.00"] and rows[-1][-1] == "-6.42"


def test_a_month_with_nothing_still_has_its_balances(october):
    cp, billing, *_ = october
    stmt = st.monthly_statement(billing, cp, "ann", "2026-11")
    assert (stmt["lines"], stmt["opening_balance_cents"], stmt["closing_balance_cents"]) == ([], 1358, 1358)


@pytest.mark.parametrize("bad", ["2026-13", "26-10", "2026/10", "october"])
def test_a_bad_month_is_refused(bad):
    with pytest.raises(ValueError, match="YYYY-MM"):
        st.parse_month(bad)


def test_ranges_default_to_30_days_and_are_bounded():
    assert st.parse_range(None, "2026-10-29") == (date(2026, 9, 30), date(2026, 10, 29))
    with pytest.raises(ValueError, match="after"):
        st.parse_range("2026-10-29", "2026-10-01")
    with pytest.raises(ValueError, match="400"):
        st.parse_range("2025-01-01", "2026-10-29")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        st.parse_range("yesterday", None)


def test_december_rolls_into_the_next_year():
    assert st.parse_month("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))


def test_the_csv_route_downloads_a_file(october, monkeypatch):
    from greencompute_gateway.transport import routes

    cp, billing, *_ = october
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="ann", admin=False))
    monkeypatch.setattr(routes, "_get_billing", lambda: SimpleNamespace(repo=billing))
    monkeypatch.setattr(routes.service, "control_plane", cp)
    res = routes.billing_statement(month="2026-10", format="csv", authorization="Bearer k", x_api_key=None)
    assert res.media_type.startswith("text/csv")
    assert res.headers["content-disposition"] == 'attachment; filename="green-compute-statement-2026-10.csv"'
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as bad:
        routes.billing_statement(month="2026-10", format="pdf", authorization="Bearer k", x_api_key=None)
    assert bad.value.status_code == 400



def test_hours_use_each_charges_own_rate_when_a_day_mixes_them(world):
    """A deployment charged at 1 GPU in the morning and 2 GPUs after a
    redeploy: each set of rows divides by its own rate."""
    cp, billing = world
    dep = cp.repository.create_deployment(DeploymentRecord(workload_id=_workload(cp, "mixed").workload_id,
                                                           owner_user_id="ann"))
    led = Ledger(billing)
    led.add(_at(5, 8), -40, "usage", "GPU usage 1× GPU @ $0.40/hr", dep.deployment_id)   # 1 h
    led.add(_at(5, 9), -80, "usage", "GPU usage 2× GPU @ $0.40/hr", dep.deployment_id)   # 1 h
    led.add(_at(5, 10), -5, "usage", "GPU usage for deployment abcd1234", dep.deployment_id)  # legacy text
    s = st.spending_summary(billing, cp, "ann", date(2026, 10, 5), date(2026, 10, 5))
    assert s["deployments"][0]["hours"] == 2.0
    line = st.monthly_statement(billing, cp, "ann", "2026-10")["lines"][0]
    assert line["quantity"] is None  # some rows had no rate: no guessed hours on the statement


def test_balances_follow_the_real_balance(world):
    """Opening and closing come from the account balance less later rows, so
    this month's closing balance is the balance the Billing page shows, even
    when a balance change was made without a ledger row."""
    cp, billing = world
    led = Ledger(billing)
    led.add(_at(3), 500, "topup", "Card top-up $5.00")
    with session_scope(billing.session_factory) as s:
        s.get(UserORM, "ann").balance_credits += 250  # e.g. a manual fix with no ledger row
    stmt = st.monthly_statement(billing, cp, "ann", "2026-10")
    assert (stmt["opening_balance_cents"], stmt["closing_balance_cents"]) == (250, 750)


def test_spreadsheet_formulas_in_names_are_neutralised(world):
    cp, billing = world
    wl = _workload(cp, '=HYPERLINK("http://evil","click")')
    dep = cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id="ann"))
    Ledger(billing).add(_at(6), -40, "usage", "GPU usage 1× GPU @ $0.40/hr", dep.deployment_id)
    text = st.statement_csv(st.monthly_statement(billing, cp, "ann", "2026-10"))
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text
    assert ",-0.40" in text  # amounts stay numbers


def test_the_formats_the_meter_writes_are_the_ones_statements_read():
    from greencompute_control_plane.domain.metering import rental_charge_description

    assert st.cents_per_hour(rental_charge_description(3, 40)) == 120
    assert st.cents_per_hour(rental_charge_description(1, 70)) == 70
    one = ControlPlaneService._private_model_charge_description(None, 1, 40)
    two = ControlPlaneService._private_model_charge_description(None, 2, 40)
    assert (st.cents_per_hour(one), st.cents_per_hour(two)) == (40, 80)


def test_shared_model_requests_are_summed_in_the_query(october):
    cp, billing, *_ = october
    since, until = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    shared = [r for r in billing.spending_rows("ann", since, until)["metered"] if r[1] == "inference"]
    assert len(shared) == 1 and shared[0][0] is None and shared[0][5] == 2  # one group, two requests

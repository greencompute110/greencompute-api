"""The network report const asked subnet 110 to publish: GPUs by type and by
farmer, revenue, buybacks, and farmers' monthly statements.

Covered: nothing is public until published; what counts as revenue (real
payments without bonuses, never staff credit; card chargebacks; excluded
accounts; the counting-from date); GPU-hours and revenue per machine; which
machines are listed, online, hidden and whose; farmers and their labels;
the buyback sum (gamma payments already bought back, treasury spends
count), due only once its period (a quarter by default) has ended;
contracts as a revenue stream and as capacity; the public GPU bands and
shares (rounded down, never exact unless staff choose); statements and
payments; and that every staff route needs an admin key."""
import asyncio
import json
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from greencompute_gateway.application import network_report as report
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_persistence import CredentialStore, FixedWindowRateLimiter, session_scope
from greencompute_persistence.orm import (
    APIKeyORM,
    CryptoInvoiceORM,
    DeploymentORM,
    GammaSpendORM,
    LedgerEntryORM,
    NodeFarmerORM,
    NodeInventoryORM,
    UserORM,
    WorkloadORM,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)   # a Tuesday, in the fourth quarter
JANUARY = datetime(2027, 1, 5, 12, 0, tzinfo=UTC)  # the quarter NOW is in has ended
G = 10**9


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'gc.sqlite'}"
    repo = GatewayRepository(database_url=url, bootstrap=True)
    monkeypatch.setattr(routes.service, "repository", repo)
    monkeypatch.setattr(security, "credential_store", CredentialStore(session_factory=repo.session_factory))
    monkeypatch.setattr(security, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_SERVICE_HOTKEYS", "5CwckYmAV3fET8V9KTKfqds6BnRGACAgofECQqdYfa2Q9rvp")
    monkeypatch.setattr("greencompute_gateway.infrastructure.price_feed.get_price", lambda currency: 400.0)
    report._forget_cache()
    sf = repo.session_factory
    with session_scope(sf) as s:
        for uid in ("cust", "staff-test"):
            s.add(UserORM(user_id=uid, username=uid, email=f"{uid}@x.io", display_name=uid, profile_metadata={},
                          balance_credits=0, created_at=NOW))
        s.add(APIKeyORM(key_id="admin", user_id=None, name="admin", admin=True, scopes=[], secret="gk_admin"))
        s.add(APIKeyORM(key_id="user", user_id="cust", name="user", admin=False, scopes=[], secret="gk_user"))
        for node_id, model, count, seen in (
            ("node-a1", "rtx4090", 8, NOW - timedelta(minutes=2)),
            ("node-a2", "RTX 4090", 8, NOW - timedelta(days=5)),      # silent for 5 days: listed, offline
            ("node-b1", "rtx5090", 8, NOW - timedelta(minutes=1)),
            ("node-old", "a4000", 10, NOW - timedelta(days=160)),     # long gone: not listed
        ):
            s.add(NodeInventoryORM(node_id=node_id, hotkey="hk", server_id="srv",
                                   payload={"gpu_model": model, "gpu_count": count}, observed_at=seen))
        s.add(WorkloadORM(workload_id="w1", owner_user_id="cust", name="w", image="i", kind="pod",
                          security_tier="standard", pricing_class="standard", requirements={}, runtime={},
                          lifecycle={}, metadata_json={}, tags=[]))
        for dep, node in (("dep-a", "node-a1"), ("dep-b", "node-b1")):
            s.add(DeploymentORM(deployment_id=dep, workload_id="w1", owner_user_id="cust", node_id=node,
                                state="ready", hotkey="hk", requested_instances=1))
    return types.SimpleNamespace(sf=sf)


def ledger(env, entry_id, cents, *, kind="topup", ref=None, when=NOW, user="cust", reference=None, description="x"):
    with session_scope(env.sf) as s:
        s.add(LedgerEntryORM(entry_id=entry_id, user_id=user, amount_cents=cents, balance_after=0, kind=kind,
                             deposit_ref=ref, reference_id=reference, description=description, created_at=when))


def call(method, path, body=None, *, key=None, query=b""):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"host", b"api.test")]
    if key:
        headers.append((b"authorization", f"Bearer {key}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query, "headers": headers,
        "client": ("203.0.113.9", 1), "server": ("api.test", 80),
    }
    sent = {"status": None, "body": b"", "headers": {}}
    chunks = [{"type": "http.request", "body": raw, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    call.last_headers = sent["headers"]
    return sent["status"], json.loads(sent["body"] or b"null")


# --- published or not ---------------------------------------------------------------


def test_nothing_is_public_until_published(env):
    assert report.get_settings(env.sf)["published"] is False
    status, body = call("GET", "/platform/report")
    assert status == 404 and body["error"]["code"] == "not_found"
    status, _ = call("PUT", "/platform/admin/report/settings", {"published": True}, key="gk_admin")
    assert status == 200
    status, body = call("GET", "/platform/report")
    assert status == 200 and body["published"] is True and body["netuid"] == 110
    assert call.last_headers.get("access-control-allow-origin") == "*"
    # Unpublishing takes it down at once, cache or not.
    call("PUT", "/platform/admin/report/settings", {"published": False}, key="gk_admin")
    assert call("GET", "/platform/report")[0] == 404


def test_public_report_cache_is_single_flight_and_never_outlives_a_change(env, monkeypatch):
    import threading

    report.update_settings(env.sf, {"published": True})
    builds, real_build = [], report.build
    gate = threading.Event()

    def slow_build(session_factory, **kwargs):
        builds.append(1)
        gate.wait(5)
        return real_build(session_factory, **kwargs)

    monkeypatch.setattr(report, "build", slow_build)
    results = []
    threads = [threading.Thread(target=lambda: results.append(report.public_report(env.sf))) for _ in range(6)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(builds) == 1 and len(results) == 6 and all(r is not None for r in results)
    # A build in flight when the report is unpublished is thrown away.
    report._forget_cache()
    gate.clear()
    out = []
    t = threading.Thread(target=lambda: out.append(report.public_report(env.sf)))
    t.start()
    while len(builds) < 2:
        pass
    report.update_settings(env.sf, {"published": False})
    gate.set()
    t.join(10)
    assert out == [None] and report.public_report(env.sf) is None


def test_staff_routes_need_an_admin_key(env):
    for method, path, body in (
        ("GET", "/platform/admin/report", None),
        ("PUT", "/platform/admin/report/settings", {"published": True}),
        ("POST", "/platform/admin/farmers", {"name": "X"}),
        ("PATCH", "/platform/admin/farmers/f1", {"name": "Y"}),
        ("PUT", "/platform/admin/machines/node-a1", {"hidden": True}),
        ("GET", "/platform/admin/buyback", None),
        ("POST", "/platform/admin/farmers/f1/payments", {"month": "2026-11", "amount_usd": 5}),
        ("DELETE", "/platform/admin/farmer-payments/p1", None),
    ):
        assert call(method, path, body)[0] == 401, path
        assert call(method, path, body, key="gk_user")[0] == 403, path
    assert call("GET", "/platform/admin/farmers/f1/statement", key="gk_user", query=b"month=2026-11")[0] == 403
    assert report.get_settings(env.sf)["published"] is False


def test_contract_routes_need_an_admin_key(env):
    for method, path, body in (
        ("POST", "/platform/admin/contracts", {"name": "X", "starts_on": "2026-11-01"}),
        ("PATCH", "/platform/admin/contracts/c1", {"name": "Y"}),
        ("POST", "/platform/admin/contracts/c1/payments", {"received_on": "2026-11-02", "amount_usd": 5}),
        ("PATCH", "/platform/admin/contract-payments/p1", {"amount_usd": 6}),
        ("DELETE", "/platform/admin/contract-payments/p1", None),
    ):
        assert call(method, path, body)[0] == 401, path
        assert call(method, path, body, key="gk_user")[0] == 403, path
    assert report.list_contracts(env.sf) == []


def test_settings_are_checked(env):
    # What the owner asked for is the default: quarters, bands, no GPU-hours.
    defaults = report.get_settings(env.sf)
    assert (defaults["period"], defaults["gpu_detail"], defaults["show_providers"], defaults["show_gpu_hours"]) == (
        "quarter", "bands", True, False)
    for bad in ({"since": "yesterday"}, {"revenue_basis": "profit"}, {"listed_within_days": 0},
                {"published": "yes"}, {"excluded_user_ids": "cust"}, {"colour": "green"}, {"period": "year"},
                {"period": None}, {"gpu_detail": "rough"}, {"show_providers": "no"}, {"show_gpu_hours": 1}):
        with pytest.raises(report.ReportError):
            report.update_settings(env.sf, bad)
    out = report.update_settings(env.sf, {"since": "2026-10-01", "revenue_basis": "usage", "listed_within_days": 7,
                                          "excluded_user_ids": ["b", "a", "a"], "period": "month",
                                          "gpu_detail": "exact", "show_providers": False, "show_gpu_hours": True})
    assert out["excluded_user_ids"] == ["a", "b"] and report.get_settings(env.sf) == out
    assert (out["period"], out["gpu_detail"], out["show_providers"], out["show_gpu_hours"]) == (
        "month", "exact", False, True)
    status, body = call("PUT", "/platform/admin/report/settings", {"since": "soon"}, key="gk_admin")
    assert status == 400 and "YYYY-MM-DD" in body["detail"]


# --- revenue ------------------------------------------------------------------------


def test_revenue_is_what_customers_paid_without_bonuses_or_staff_credit(env):
    with session_scope(env.sf) as s:
        s.add(CryptoInvoiceORM(invoice_id="inv1", user_id="cust", currency="usdc-base", amount_usd=20.0,
                               amount_crypto=20.0, bonus_pct=0.05, total_credits=2100, deposit_address="0xabc",
                               status="confirmed", created_at=NOW, expires_at=NOW))
    ledger(env, "card", 2000, ref="stripe:cs_1", when=NOW - timedelta(days=1))
    ledger(env, "usdc", 2100, ref="usdc-base:0xabc:1", reference="inv1", when=NOW - timedelta(days=2))  # +5% bonus
    ledger(env, "alpha", 1100, ref="alpha:9:4", when=NOW - timedelta(days=2))        # +10% alpha bonus, no invoice
    ledger(env, "manual", 5000, ref="manual:tao:0xdead", when=NOW - timedelta(days=9))  # staff confirmed a real tx
    ledger(env, "gamma", 700, ref="gamma:110:0:svc:hk:7", when=NOW - timedelta(days=3))
    ledger(env, "gift", 9900, ref=None, when=NOW - timedelta(days=1))                 # staff credit: not revenue
    ledger(env, "cb", -500, kind="chargeback", when=NOW)                              # a card payment reversed
    ledger(env, "ours", 4000, ref="stripe:cs_2", user="staff-test", when=NOW)         # our own test account
    ledger(env, "early", 7700, ref="stripe:cs_0", when=datetime(2026, 10, 30, tzinfo=UTC))  # before the start
    report.update_settings(env.sf, {"excluded_user_ids": ["staff-test"], "period": "week"})
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]  # staff: up to now
    by = {m["method"]: m["usd"] for m in revenue["by_method"]}
    assert by == {"gamma": "7.00", "contract": "0.00", "card": "15.00", "crypto": "80.00"}
    assert [m["method"] for m in revenue["by_method"]] == ["gamma", "contract", "card", "crypto"]
    assert revenue["total_usd"] == "102.00" and revenue["basis"] == "paid" and revenue["through"] == "2026-11-24"
    this_week = revenue["periods"][0]
    assert (this_week["start"], this_week["end"], this_week["complete"], this_week["card_usd"]) == (
        "2026-11-23", "2026-11-29", False, "15.00")
    last_week = revenue["periods"][1]
    assert (last_week["crypto_usd"], last_week["gamma_usd"], last_week["usd"], last_week["complete"]) == (
        "30.00", "7.00", "37.00", True)
    assert last_week["to_buy_back_usd"] == "30.00" and last_week["counted_from"] is None
    assert revenue["days"][0] == {"day": "2026-11-24", "usd": "-5.00"}
    # By quarter, the default: one row, the quarter in progress, counted from the start date.
    report.update_settings(env.sf, {"period": "quarter"})
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert [(q["start"], q["end"], q["counted_from"], q["complete"], q["usd"]) for q in revenue["periods"]] == [
        ("2026-10-01", "2026-12-31", "2026-11-01", False, "102.00")]
    # Counting from October includes the earlier payment.
    report.update_settings(env.sf, {"since": "2026-10-01"})
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["total_usd"] == "179.00"


def test_the_public_report_stops_at_the_last_completed_period(env):
    """By week, NOW being a Tuesday: the public sees money and GPU-hours
    through Sunday the 22nd, totals included, so polling can't show a payment
    as it lands or how many GPUs are rented this minute. That is so whatever
    the period is (public_through "week", the default); "day" runs through
    yesterday, and "period" shows completed periods only."""
    assert report.get_settings(env.sf)["public_through"] == "week"
    report.update_settings(env.sf, {"period": "week", "show_gpu_hours": True})
    ledger(env, "last-week", 3000, ref="usdc-base:0x1:1", when=datetime(2026, 11, 20, 9, tzinfo=UTC))
    ledger(env, "sunday-night", 700, ref="stripe:cs_s", when=datetime(2026, 11, 22, 23, 59, tzinfo=UTC))
    ledger(env, "monday", 4321, ref="usdc-base:0x2:1", when=datetime(2026, 11, 23, 0, 30, tzinfo=UTC))
    ledger(env, "today", 900, ref="stripe:cs_t", when=NOW)
    ledger(env, "use-old", -400, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr",
           when=datetime(2026, 11, 21, tzinfo=UTC))
    ledger(env, "use-now", -800, kind="usage", reference="dep-a", description="GPU usage 2× GPU @ $0.40/hr")
    public = report.build(env.sf, now=NOW)
    revenue = public["revenue"]
    assert revenue["through"] == "2026-11-22" and revenue["total_usd"] == "37.00"
    assert {m["method"]: m["usd"] for m in revenue["by_method"]} == {
        "gamma": "0.00", "contract": "0.00", "card": "7.00", "crypto": "30.00"}
    assert revenue["periods"][0]["start"] == "2026-11-16" and "days" not in revenue
    assert all(w["complete"] for w in revenue["periods"])
    assert revenue["next_figures_on"] == "2026-11-30"      # when the week in progress will show
    assert revenue["compute_delivered"] == {"usd": "4.00", "gpu_hours": "10.0"}
    assert revenue["periods"][0]["gpu_hours"] == "10.0"
    assert public["buybacks"]["to_buy_back_usd"] == "37.00" and public["buybacks"]["next_due"] == "2026-11-30"
    assert public["buybacks"]["accruing_usd"] == "0.00" and "settle_now_usd" not in public["buybacks"]
    assert revenue["public_through"] == "week"
    # GPU-hours and compute charged are off the public report unless staff turn them on.
    report.update_settings(env.sf, {"show_gpu_hours": False})
    quiet = report.build(env.sf, now=NOW)["revenue"]
    assert "compute_delivered" not in quiet
    assert not [k for row in quiet["periods"] for k in row if k in ("gpu_hours", "compute_usd")]
    # Staff see up to now, GPU-hours included, whatever the public setting.
    staff = report.build(env.sf, admin=True, now=NOW)
    assert staff["revenue"]["total_usd"] == "89.21" and staff["revenue"]["compute_delivered"]["gpu_hours"] == "30.0"
    assert staff["revenue"]["next_figures_on"] is None and "public_through" not in staff["revenue"]
    assert (staff["buybacks"]["to_buy_back_usd"], staff["buybacks"]["accruing_usd"]) == ("37.00", "52.21")
    # Before the first completed week there is nothing to show yet.
    report.update_settings(env.sf, {"since": "2026-11-23"})
    early = report.build(env.sf, now=NOW)["revenue"]
    assert (early["through"], early["total_usd"], early["periods"]) == (None, "0.00", [])
    # By quarter, the public still sees through the last completed week: the quarter in progress, so far.
    report.update_settings(env.sf, {"since": "2026-11-01", "period": "quarter"})
    public = report.build(env.sf, now=NOW)
    assert (public["period"], public["revenue"]["through"], public["revenue"]["total_usd"]) == (
        "quarter", "2026-11-22", "37.00")
    assert [(q["start"], q["end"], q["complete"], q["usd"]) for q in public["revenue"]["periods"]] == [
        ("2026-10-01", "2026-12-31", False, "37.00")]
    assert public["revenue"]["next_figures_on"] == "2026-11-30"
    # None of it is due yet: it builds up until the quarter ends.
    assert (public["buybacks"]["to_buy_back_usd"], public["buybacks"]["accruing_usd"],
            public["buybacks"]["next_due"]) == ("0.00", "37.00", "2027-01-01")
    # "day": through yesterday (Monday the 23rd), never today.
    report.update_settings(env.sf, {"public_through": "day"})
    daily = report.build(env.sf, now=NOW)["revenue"]
    assert (daily["through"], daily["total_usd"], daily["next_figures_on"], daily["public_through"]) == (
        "2026-11-23", "80.21", "2026-11-25", "day")
    # "period": nothing of the quarter in progress is public; it all shows once it has ended.
    report.update_settings(env.sf, {"public_through": "period"})
    public = report.build(env.sf, now=NOW)
    assert (public["revenue"]["through"], public["revenue"]["total_usd"]) == (None, "0.00")
    assert public["revenue"]["periods"] == [] and public["revenue"]["next_figures_on"] == "2027-01-01"
    assert (public["buybacks"]["to_buy_back_usd"], public["buybacks"]["accruing_usd"],
            public["buybacks"]["next_due"]) == ("0.00", "0.00", "2027-01-01")
    later = report.build(env.sf, now=JANUARY)
    assert (later["revenue"]["through"], later["revenue"]["total_usd"]) == ("2026-12-31", "89.21")
    assert [(q["start"], q["end"], q["counted_from"], q["complete"], q["usd"], q["to_buy_back_usd"])
            for q in later["revenue"]["periods"]] == [
        ("2026-10-01", "2026-12-31", "2026-11-01", True, "89.21", "89.21")]
    assert (later["buybacks"]["to_buy_back_usd"], later["buybacks"]["next_due"]) == ("89.21", "2027-04-01")


def test_compute_delivered_and_the_usage_basis(env):
    for i in range(10):
        ledger(env, f"a{i}", -40, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr",
               when=NOW - timedelta(hours=i))
    ledger(env, "b0", -280, kind="usage", reference="dep-b", description="GPU usage 2× GPU @ $0.70/hr (final minute)")
    ledger(env, "pm", -80, kind="usage", reference="dep-a", description="Private model Llama · 1 GPU at $0.40/hr")
    ledger(env, "inf", -3, kind="inference", description="Inference m: 1 in + 1 out tokens")
    ledger(env, "sto", -100, kind="storage", reference="dep-a", description="Saved-pod storage $1.00/day")
    ledger(env, "adj", -5000, kind="adjustment", description="Manual admin debit")  # not compute
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert revenue["compute_delivered"] == {"usd": "8.63", "gpu_hours": "16.0"}  # 10 + 4 + 2 GPU-hours
    assert revenue["total_usd"] == "0.00"  # nothing was paid in
    report.update_settings(env.sf, {"revenue_basis": "usage"})
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert revenue["total_usd"] == "8.63" and revenue["basis"] == "usage"
    _, _, by_node = report.compute_delivered(env.sf, NOW - timedelta(days=30))
    assert by_node["node-a1"]["cents"] == 480 and round(by_node["node-a1"]["gpu_hours"], 1) == 12.0
    assert by_node["node-b1"]["cents"] == 280 and round(by_node["node-b1"]["gpu_hours"], 1) == 4.0


# --- GPUs, machines and farmers -----------------------------------------------------


def test_gpus_by_type_and_by_farmer(env):
    a = report.create_farmer(env.sf, name="Acme Hosting")
    b = report.create_farmer(env.sf, name="Nordic GPUs")
    assert (a["public_label"], b["public_label"]) == ("Farmer A", "Farmer B")
    report.assign_machine(env.sf, "node-a1", farmer_id=a["farmer_id"])
    report.assign_machine(env.sf, "node-a2", farmer_id=a["farmer_id"])
    report.update_settings(env.sf, {"gpu_detail": "exact"})
    gpus = report.build(env.sf, now=NOW)["gpus"]
    assert gpus["detail"] == "exact"
    assert (gpus["gpus"], gpus["online_gpus"], gpus["machines"], gpus["machines_online"]) == (24, 16, 3, 2)
    assert (gpus["contract_gpus"], gpus["total_gpus"]) == (0, 24)
    assert [(m["label"], m["gpus"], m["online_gpus"]) for m in gpus["by_model"]] == [
        ("RTX 4090", 16, 8), ("RTX 5090", 8, 8)]
    assert [(f["farmer"], f["gpus"], f["online_gpus"]) for f in gpus["by_farmer"]] == [
        ("Farmer A", 16, 8), ("Unassigned", 8, 8)]
    # Shares are of capacity at list price: 16 x $0.40 against 8 x $0.70.
    assert [f["share_pct"] for f in gpus["by_farmer"]] == [53.3, 46.7]
    assert "usd_per_hour" not in gpus["by_farmer"][0]
    # The public report never carries a farmer's name or id; staff's does.
    assert "Acme" not in json.dumps(report.build(env.sf, now=NOW))
    admin = report.build(env.sf, admin=True, now=NOW)["gpus"]["by_farmer"][0]
    assert admin["name"] == "Acme Hosting" and admin["farmer_id"] == a["farmer_id"]
    assert admin["usd_per_hour"] == "6.40"
    # A hidden machine and one silent past the listing window are left out.
    report.assign_machine(env.sf, "node-b1", hidden=True)
    report.update_settings(env.sf, {"listed_within_days": 3})
    gpus = report.build(env.sf, now=NOW)["gpus"]
    assert (gpus["gpus"], gpus["machines"]) == (8, 1)
    listed = {m["node_id"]: m["listed"] for m in report.machines(env.sf, now=NOW)}
    assert listed == {"node-a1": True, "node-a2": False, "node-b1": False, "node-old": False}


def test_farmers_labels_terms_and_changes(env):
    first = report.create_farmer(env.sf, name="  Acme   Hosting ", terms={"revenue_share_pct": 70})
    assert first["name"] == "Acme Hosting" and first["terms"]["revenue_share_pct"] == 70.0
    for _ in range(26):
        last = report.create_farmer(env.sf, name=f"F{_}")
    assert last["public_label"] == "Farmer AA"
    for bad in ({"name": "Acme Hosting"}, {"name": "New", "public_label": "Farmer A"}, {"name": ""},
                {"name": "T", "terms": {"revenue_share_pct": 140}}, {"name": "T", "terms": {"usd_per_gpu_hour": -1}}):
        with pytest.raises(report.ReportError):
            report.create_farmer(env.sf, **bad)
    changed = report.update_farmer(env.sf, first["farmer_id"], {"public_label": "Provider One", "archived": True})
    assert changed["public_label"] == "Provider One" and changed["archived"] is True
    # Changing one term keeps the others.
    report.update_farmer(env.sf, first["farmer_id"], {"terms": {"usd_per_gpu_hour": 0.25, "monthly_fee_usd": 500}})
    kept = report.update_farmer(env.sf, first["farmer_id"], {"terms": {"revenue_share_pct": 60}})
    assert kept["terms"] == {"usd_per_gpu_hour": 0.25, "revenue_share_pct": 60.0, "monthly_fee_usd": 500.0}
    with pytest.raises(report.ReportError):
        report.create_farmer(env.sf, name={"a": 1})
    with pytest.raises(report.ReportError):
        report.update_farmer(env.sf, first["farmer_id"], {"public_label": "Farmer B"})
    assert report.update_farmer(env.sf, "nope", {"name": "x"}) is None
    assert report.assign_machine(env.sf, "no-such-node", farmer_id=None) is None
    with pytest.raises(report.ReportError):
        report.assign_machine(env.sf, "node-a1", farmer_id="no-such-farmer")
    status, body = call("PUT", "/platform/admin/machines/node-a1", {"farmer_id": first["farmer_id"]}, key="gk_admin")
    assert status == 200 and body["farmer_id"] == first["farmer_id"]
    status, body = call("PUT", "/platform/admin/machines/node-a1", {"farmer_id": None}, key="gk_admin")
    assert status == 200 and body["farmer_id"] is None
    with session_scope(env.sf) as s:
        assert s.get(NodeFarmerORM, "node-a1").farmer_id is None


# --- buybacks -----------------------------------------------------------------------


def _treasury_spend(env, spend_id, gamma, when=NOW):
    with session_scope(env.sf) as s:
        s.add(GammaSpendORM(spend_id=spend_id, netuid=110, subnet_counter=0, service_hotkey="svc", hotkey="tre",
                            kind="treasury", amount_raw=gamma * G, total_raw=gamma * G, block_number=9_577_000,
                            tx_hash="0x" + "ab" * 32, observed_at=when))


def test_buyback_owes_card_and_crypto_revenue_less_what_was_bought_back(env):
    ledger(env, "card", 6000, ref="stripe:cs_1")
    ledger(env, "tao", 4000, ref="tao:9:1")
    ledger(env, "gamma", 2500, ref="gamma:110:0:svc:hk:25")  # bought back when the customer minted it
    with session_scope(env.sf) as s:
        s.add(GammaSpendORM(spend_id="c1", netuid=110, subnet_counter=0, service_hotkey="svc", hotkey="cus",
                            kind="customer", amount_raw=25 * G, total_raw=25 * G, block_number=9_576_000,
                            observed_at=NOW))
    _treasury_spend(env, "b1", 30, NOW - timedelta(days=1))
    _treasury_spend(env, "b0", 500, datetime(2026, 10, 1, tzinfo=UTC))  # before the counting-from date
    # While the quarter runs nothing is due: the $100 builds up, and falls due on 1 January.
    out = report.buyback(env.sf, now=NOW)
    assert (out["period"], out["revenue_usd"], out["to_buy_back_usd"], out["accruing_usd"], out["next_due"]) == (
        "quarter", "125.00", "0.00", "100.00", "2027-01-01")
    assert (out["bought_back_usd"], out["outstanding_usd"], out["commands"], out["tao_to_mint"]) == (
        "30.00", "0.00", [], None)
    # To settle everything up to today anyway: $100 less the $30 already bought back.
    assert (out["settle_now_usd"], out["settle_now_tao"]) == ("70.00", "0.1750")
    assert out["periods"] == [{"start": "2026-10-01", "end": "2026-12-31", "counted_from": "2026-11-01",
                               "complete": False, "revenue_usd": "125.00", "gamma_usd": "25.00",
                               "to_buy_back_usd": "100.00"}]
    public = report.build(env.sf, now=NOW)
    assert not [k for k in (*public["buybacks"], *public["revenue"]) if k.endswith("_cents")]
    assert "next_due_day" not in public["revenue"]
    # This quarter's payments aren't public yet; the buyback already made is (it is on chain).
    assert (public["buybacks"]["to_buy_back_usd"], public["buybacks"]["bought_back_usd"]) == ("0.00", "30.00")
    assert "All gamma ever spent" in public["buybacks"]["on_chain"]["scope"]
    # The quarter has ended: it is due, less what was bought back early.
    out = report.buyback(env.sf, now=JANUARY)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["bought_back_usd"], out["outstanding_usd"]) == (
        "125.00", "100.00", "30.00", "70.00")
    assert (out["accruing_usd"], out["next_due"], out["settle_now_usd"]) == ("0.00", "2027-04-01", "70.00")
    assert out["paid_in_gamma_usd"] == "25.00" and out["tao_usd"] == 400.0 and out["tao_to_mint"] == "0.1750"
    assert out["commands"][0] == "btcli gamma mint --netuid 110 --hotkey gc-buyback --amount-tao 0.1750"
    assert "--service-hotkey 5CwckYmAV3fET8V9KTKfqds6BnRGACAgofECQqdYfa2Q9rvp" in out["commands"][1]
    assert out["on_chain"]["spent_gamma"] == "555"  # every spend here, whenever
    assert [entry["gamma"] for entry in out["log"]] == ["30"]
    assert [(q["start"], q["complete"]) for q in out["periods"]] == [("2027-01-01", False), ("2026-10-01", True)]
    staff = report.build(env.sf, admin=True, now=JANUARY)["buybacks"]
    assert (staff["to_buy_back_usd"], staff["outstanding_usd"], staff["accruing_usd"]) == ("100.00", "70.00", "0.00")
    public = report.build(env.sf, now=JANUARY)["buybacks"]
    assert (public["to_buy_back_usd"], public["outstanding_usd"], public["paid_in_gamma_usd"]) == (
        "100.00", "70.00", "25.00")
    # New revenue in the new quarter builds up beside what is due.
    ledger(env, "jan", 900, ref="stripe:cs_j", when=JANUARY - timedelta(days=1))
    out = report.buyback(env.sf, now=JANUARY)
    assert (out["to_buy_back_usd"], out["outstanding_usd"], out["accruing_usd"], out["settle_now_usd"]) == (
        "100.00", "70.00", "9.00", "79.00")
    # Bought back in full: nothing outstanding, no commands.
    _treasury_spend(env, "b2", 80, JANUARY)
    out = report.buyback(env.sf, now=JANUARY)
    assert out["outstanding_usd"] == "0.00" and out["commands"] == [] and out["tao_to_mint"] is None
    assert out["settle_now_usd"] == "0.00" and out["settle_now_tao"] is None
    # By week the same money falls due after its week.
    report.update_settings(env.sf, {"period": "week"})
    out = report.buyback(env.sf, now=NOW + timedelta(days=7))
    assert (out["to_buy_back_usd"], out["next_due"]) == ("100.00", "2026-12-07")


def test_buyback_without_a_price_gives_no_amount(env, monkeypatch):
    def no_price(currency):
        raise RuntimeError("no endpoint")

    monkeypatch.setattr("greencompute_gateway.infrastructure.price_feed.get_price", no_price)
    ledger(env, "card", 6000, ref="stripe:cs_1")
    out = report.buyback(env.sf, now=JANUARY)
    assert out["outstanding_usd"] == "60.00" and out["tao_usd"] is None and out["commands"] == []
    assert out["settle_now_tao"] is None


def test_on_the_usage_basis_gamma_only_covers_its_own_accounts_usage(env):
    """A hotkey prepays $100 in gamma and uses nothing; a card customer uses
    $50. The $50 is still to buy back: prepaid gamma doesn't cancel what
    others' usage owes."""
    with session_scope(env.sf) as s:
        s.add(UserORM(user_id="gamma-payer", username="gp", email=None, display_name="gp", profile_metadata={},
                      balance_credits=0, created_at=NOW))
    ledger(env, "g", 10_000, ref="gamma:110:0:svc:hk:100", user="gamma-payer")
    ledger(env, "use", -5000, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr")
    report.update_settings(env.sf, {"revenue_basis": "usage"})
    out = report.buyback(env.sf, now=JANUARY)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["paid_in_gamma_usd"]) == ("50.00", "50.00", "0.00")
    # Per period it isn't worked out on this basis: gamma covers usage across periods.
    assert out["periods"][-1]["to_buy_back_usd"] is None
    # Once the gamma payer uses $30 of it, that $30 is revenue already bought back.
    ledger(env, "use-g", -3000, kind="usage", reference="dep-b", description="GPU usage 1× GPU @ $0.70/hr",
           user="gamma-payer")
    out = report.buyback(env.sf, now=JANUARY)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["paid_in_gamma_usd"]) == ("80.00", "50.00", "30.00")
    # In the quarter itself all of it is still building up; the gamma-funded part never falls due.
    out = report.buyback(env.sf, now=NOW)
    assert (out["to_buy_back_usd"], out["accruing_usd"], out["paid_in_gamma_usd"]) == ("0.00", "50.00", "30.00")
    # Usage in the next quarter that gamma paid for doesn't reduce what the last quarter owes.
    ledger(env, "use-g2", -2000, kind="usage", reference="dep-b", description="GPU usage 1× GPU @ $0.70/hr",
           user="gamma-payer", when=JANUARY - timedelta(days=2))
    ledger(env, "use-c2", -700, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr",
           when=JANUARY - timedelta(days=2))
    out = report.buyback(env.sf, now=JANUARY)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["accruing_usd"], out["paid_in_gamma_usd"]) == (
        "107.00", "50.00", "7.00", "50.00")


def test_staff_debits_are_listed_not_netted(env):
    ledger(env, "paid", 1100, ref="usdt-eth:0x1:1")
    ledger(env, "back", -1100, kind="refund", description="Manual admin debit")
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["total_usd"] == "11.00"
    status, body = call("GET", "/platform/admin/report", key="gk_admin")
    assert status == 200
    assert [(a["kind"], a["usd"], a["user_id"]) for a in body["adjustments"]] == [("refund", "11.00", "cust")]


# --- statements ---------------------------------------------------------------------


def test_farmer_statement_and_payments(env):
    farmer = report.create_farmer(env.sf, name="Acme", terms={
        "revenue_share_pct": 50, "usd_per_gpu_hour": 0.10, "monthly_fee_usd": 20})
    other = report.create_farmer(env.sf, name="Nordic")
    report.assign_machine(env.sf, "node-a1", farmer_id=farmer["farmer_id"])
    report.assign_machine(env.sf, "node-b1", farmer_id=other["farmer_id"])
    for i in range(10):  # 10 GPU-hours, $4.00, on Acme's machine in November
        ledger(env, f"a{i}", -40, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr",
               when=NOW - timedelta(hours=i))
    ledger(env, "oct", -400, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr",
           when=datetime(2026, 10, 15, tzinfo=UTC))
    ledger(env, "b", -700, kind="usage", reference="dep-b", description="GPU usage 1× GPU @ $0.70/hr")
    st = report.statement(env.sf, farmer["farmer_id"], "2026-11")
    assert (st["gpu_hours"], st["revenue_usd"]) == ("10.0", "4.00")
    assert st["payout"] == {"revenue_share_usd": "2.00", "per_gpu_hour_usd": "1.00", "monthly_fee_usd": "20.00",
                            "total_usd": "23.00"}
    assert st["machines"] == [{"node_id": "node-a1", "label": "RTX 4090", "gpu_count": 8, "gpu_hours": "10.0",
                               "revenue_usd": "4.00"}]
    assert (st["paid_usd"], st["balance_usd"]) == ("0.00", "23.00")
    status, payment = call("POST", f"/platform/admin/farmers/{farmer['farmer_id']}/payments",
                           {"month": "2026-11", "amount_usd": 15, "reference": "0xpaid"}, key="gk_admin")
    assert status == 200 and payment["amount_usd"] == "15.00"
    status, st = call("GET", f"/platform/admin/farmers/{farmer['farmer_id']}/statement", key="gk_admin",
                      query=b"month=2026-11")
    assert status == 200 and (st["paid_usd"], st["balance_usd"]) == ("15.00", "8.00")
    assert "farmer_payments" not in report.build(env.sf, now=NOW)  # not public unless staff turn it on
    # Our own test accounts are left out of statements, as of the report.
    ledger(env, "ours", -900, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr",
           user="staff-test")
    assert report.statement(env.sf, farmer["farmer_id"], "2026-11")["revenue_usd"] == "13.00"
    report.update_settings(env.sf, {"excluded_user_ids": ["staff-test"]})
    assert report.statement(env.sf, farmer["farmer_id"], "2026-11")["revenue_usd"] == "4.00"
    assert report.build(env.sf, admin=True, now=NOW)["farmer_payments"]["total_usd"] == "15.00"
    # In public only once the public figures have passed the end of the statement month, as for revenue.
    report.update_settings(env.sf, {"show_farmer_payments": True})
    assert report.build(env.sf, now=NOW)["farmer_payments"] == {"total_usd": "0.00", "months": []}
    assert report.build(env.sf, now=JANUARY)["farmer_payments"] == {
        "total_usd": "15.00", "months": [{"month": "2026-11", "usd": "15.00"}]}
    # A statement month shows once the public figures have reached its last day: through the last
    # completed week, that is the Monday after the month's last Sunday... (30 Nov is a Monday: 7 Dec).
    assert report.build(env.sf, now=NOW + timedelta(days=10))["farmer_payments"]["months"] == []
    assert report.build(env.sf, now=NOW + timedelta(days=14))["farmer_payments"]["total_usd"] == "15.00"
    report.update_settings(env.sf, {"public_through": "period"})
    assert report.build(env.sf, now=NOW + timedelta(days=14))["farmer_payments"]["months"] == []
    report.update_settings(env.sf, {"public_through": "week"})
    assert report.statement(env.sf, farmer["farmer_id"], "2026-10")["revenue_usd"] == "4.00"
    assert call("DELETE", f"/platform/admin/farmer-payments/{payment['payment_id']}", key="gk_admin")[0] == 200
    assert call("DELETE", f"/platform/admin/farmer-payments/{payment['payment_id']}", key="gk_admin")[0] == 404
    assert call("GET", "/platform/admin/farmers/nope/statement", key="gk_admin", query=b"month=2026-11")[0] == 404
    assert call("GET", f"/platform/admin/farmers/{farmer['farmer_id']}/statement", key="gk_admin",
                query=b"month=2026-13")[0] == 400
    for bad in ({"month": "2026-11", "amount_usd": 0}, {"month": "Nov", "amount_usd": 5},
                {"month": "2026-11", "amount_usd": "5"}, {"month": "2026-11\n", "amount_usd": 5},
                {"month": "0000-01", "amount_usd": 5}, {"month": 202611, "amount_usd": 5}):
        assert call("POST", f"/platform/admin/farmers/{farmer['farmer_id']}/payments", bad, key="gk_admin")[0] == 400
    for month in (b"0000-01", b"9999-12", b"2026-1"):
        assert call("GET", f"/platform/admin/farmers/{farmer['farmer_id']}/statement", key="gk_admin",
                    query=b"month=" + month)[0] == 400


def test_a_charge_belongs_to_the_machine_that_ran_it(env):
    """A rental moved to another farmer's machine: each charge stays with the
    machine written on it, whatever the rental's node is today."""
    a = report.create_farmer(env.sf, name="Acme", terms={"revenue_share_pct": 50})
    b = report.create_farmer(env.sf, name="Nordic", terms={"revenue_share_pct": 50})
    report.assign_machine(env.sf, "node-a1", farmer_id=a["farmer_id"])
    report.assign_machine(env.sf, "node-b1", farmer_id=b["farmer_id"])
    with session_scope(env.sf) as s:
        for entry_id, node in (("on-a", "node-a1"), ("on-b", "node-b1")):
            s.add(LedgerEntryORM(entry_id=entry_id, user_id="cust", amount_cents=-400, balance_after=0, kind="usage",
                                 reference_id="dep-a", node_id=node, description="GPU usage 1× GPU @ $0.40/hr",
                                 created_at=NOW))
        s.get(DeploymentORM, "dep-a").node_id = None  # requeued, not placed again
    assert report.statement(env.sf, a["farmer_id"], "2026-11")["revenue_usd"] == "4.00"
    assert report.statement(env.sf, b["farmer_id"], "2026-11")["revenue_usd"] == "4.00"


def test_the_meter_writes_the_machine_on_the_charge(env):
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    repo = BillingRepository.__new__(BillingRepository)
    repo.session_factory = env.sf
    with session_scope(env.sf) as s:
        s.get(UserORM, "cust").balance_credits = 500
    out = repo.debit_user_to_floor(user_id="cust", amount_cents=40, kind="usage", reference_id="dep-a",
                                   description="GPU usage 1× GPU @ $0.40/hr", node_id="node-b1")
    assert out == {"debited_cents": 40, "shortfall_cents": 0}
    with session_scope(env.sf) as s:
        row = s.scalar(select(LedgerEntryORM).where(LedgerEntryORM.kind == "usage"))
        assert (row.node_id, row.reference_id, row.amount_cents) == ("node-b1", "dep-a", -40)
    # Without one (inference, older callers) nothing changes.
    repo.debit_user_to_floor(user_id="cust", amount_cents=3, kind="inference", description="Inference m")
    with session_scope(env.sf) as s:
        assert s.scalar(select(LedgerEntryORM).where(LedgerEntryORM.kind == "inference")).node_id is None


def test_admin_report_has_everything_staff_need(env):
    farmer = report.create_farmer(env.sf, name="Acme")
    status, body = call("GET", "/platform/admin/report", key="gk_admin")
    assert status == 200
    assert body["settings"]["published"] is False and set(body["bases"]) == {"paid", "usage"}
    assert {m["node_id"] for m in body["machines"]} == {"node-a1", "node-a2", "node-b1", "node-old"}
    assert body["farmers"][0]["farmer_id"] == farmer["farmer_id"]
    assert body["report"]["gpus"]["by_farmer"][0]["farmer"] == "Unassigned"
    assert body["report"]["gpus"]["detail"] == "exact" and body["contracts"] == []
    # The preview is what the public would get: bands, no names, no days, completed periods only.
    assert "days" not in body["preview"]["revenue"] and "days" in body["report"]["revenue"]
    assert body["preview"]["gpus"]["detail"] == "bands" and "by_farmer" not in body["preview"]["gpus"]
    assert "settle_now_usd" in body["report"]["buybacks"] and "settle_now_usd" not in body["preview"]["buybacks"]
    assert body["preview"]["contracts"] == [] and "contracts" not in body["report"]
    assert (body["settings"]["public_through"], body["settings"]["show_contracts"]) == ("week", True)
    # The public spec lists the report and none of the staff routes.
    app.openapi_schema = None
    paths = app.openapi()["paths"]
    app.openapi_schema = None
    assert "/platform/report" in paths and paths["/platform/report"]["get"]["security"] == []
    assert not [p for p in paths if p.startswith("/platform/admin/")]


# --- how much the public report says about GPUs --------------------------------------


def test_public_gpu_counts_are_bands_rounded_down_never_exact(env):
    a = report.create_farmer(env.sf, name="Acme Hosting")
    report.assign_machine(env.sf, "node-a1", farmer_id=a["farmer_id"])
    report.assign_machine(env.sf, "node-a2", farmer_id=a["farmer_id"])
    public = report.build(env.sf, now=NOW)
    gpus = public["gpus"]
    # 24 listed, 16 online, none under contract.
    assert (gpus["detail"], gpus["total"], gpus["on_demand"], gpus["online"], gpus["under_contract"]) == (
        "bands", "20+", "20+", "10+", None)
    assert gpus["by_model"] == [{"gpu_model": "rtx4090", "label": "RTX 4090", "gpus": "10+"},
                                {"gpu_model": "rtx5090", "label": "RTX 5090", "gpus": "5+"}]
    # Providers by label, with their types and a share rounded down to 5%: 53.3% and 46.7%.
    assert gpus["providers"] == [
        {"provider": "Farmer A", "types": ["RTX 4090"], "share_pct": 50, "share": "50%+"},
        {"provider": "Unassigned", "types": ["RTX 5090"], "share_pct": 45, "share": "45%+"}]
    assert gpus["provider_count"] == 1 and gpus["listed_within_days"] == 30
    # No exact count, machine count or name anywhere in what the public gets.
    text = json.dumps(public)
    for key in ("online_gpus", "machines", "total_gpus", "contract_gpus", "by_farmer", "usd_per_hour", "Acme",
                "_share"):
        assert key not in text, key
    assert not any(isinstance(v, int) and not isinstance(v, bool) for k, v in gpus.items()
                   if k not in ("listed_within_days", "provider_count"))
    # Staff always see the exact counts.
    assert report.build(env.sf, admin=True, now=NOW)["gpus"]["gpus"] == 24
    # Providers can be left out altogether, in either detail.
    report.update_settings(env.sf, {"show_providers": False})
    hidden = report.build(env.sf, now=NOW)["gpus"]
    assert hidden["providers"] == [] and hidden["provider_count"] is None
    report.update_settings(env.sf, {"gpu_detail": "exact"})
    exact = report.build(env.sf, now=NOW)["gpus"]
    assert exact["by_farmer"] == [] and exact["gpus"] == 24
    staff = report.build(env.sf, admin=True, now=NOW)["gpus"]["by_farmer"]
    assert len(staff) == 2 and "_share" not in staff[0]
    report.update_settings(env.sf, {"show_providers": True})
    assert "_share" not in report.build(env.sf, now=NOW)["gpus"]["by_farmer"][0]


def test_bands_never_round_up(env):
    assert [report._band(n) for n in (0, 1, 4, 5, 9, 10, 19, 24, 99, 100, 149, 150, 999, 1000, 2300)] == [
        "0", "1+", "1+", "5+", "5+", "10+", "10+", "20+", "90+", "100+", "100+", "150+", "950+", "1,000+", "2,000+"]
    for n in range(1, 3000):
        floor = int(report._band(n).rstrip("+").replace(",", ""))
        assert 0 < floor <= n
    assert report._share_band(100.0, alone=False) == (100, "100%")
    assert report._share_band(42.0, alone=True) == (100, "100%")
    assert report._share_band(1.0, alone=False) == (0, "under 5%")
    assert report._share_band(98.9, alone=False) == (95, "95%+")
    assert report._share_band(0.0, alone=False) == (0, "0%")
    assert report._share_band(34.999, alone=False) == (30, "30%+")
    # Shares shown never exceed the real ones, so they never add up past 100%.
    for parts in ((1, 1, 1), (2, 1), (5, 3, 1, 1), (97, 2, 1), (1,) * 7):
        shown = [report._share_band(100 * p / sum(parts), alone=False)[0] for p in parts]
        assert sum(shown) <= 100 and all(w <= 100 * p / sum(parts) for w, p in zip(shown, parts)), (parts, shown)


def test_a_lone_provider_shows_all_of_the_capacity(env):
    gpus = report.build(env.sf, now=NOW)["gpus"]
    assert gpus["providers"] == [{"provider": "Unassigned", "types": ["RTX 4090", "RTX 5090"], "share_pct": 100,
                                  "share": "100%"}]
    assert gpus["provider_count"] == 0


# --- contracts ----------------------------------------------------------------------


def test_contracts_are_a_revenue_stream_and_capacity(env):
    farmer = report.create_farmer(env.sf, name="Acme")
    report.assign_machine(env.sf, "node-a1", farmer_id=farmer["farmer_id"])
    status, contract = call("POST", "/platform/admin/contracts", {
        "name": "  Lab   One ", "farmer_id": farmer["farmer_id"], "gpu_model": "RTX 5090", "gpu_count": 40,
        "monthly_usd": 14600, "starts_on": "2026-11-01", "notes": "bare metal",
    }, key="gk_admin")
    assert status == 200
    assert (contract["name"], contract["gpu_model"], contract["gpu_label"], contract["gpu_count"]) == (
        "Lab One", "rtx5090", "RTX 5090", 40)
    assert (contract["monthly_usd"], contract["active"], contract["archived"], contract["received_usd"]) == (
        "14600.00", True, False, "0.00")
    cid = contract["contract_id"]
    # A GPU type we don't list a price for is valued at its contract's own rate: 7300 / 10 / 730 h = $1.00.
    report.create_contract(env.sf, {"name": "H100 lab", "gpu_model": "h100", "gpu_count": 10, "monthly_usd": 7300,
                                    "starts_on": "2026-11-10"})
    # One that has ended, one not started, one archived: none of them is capacity.
    report.create_contract(env.sf, {"name": "Ended", "gpu_model": "rtx4090", "gpu_count": 99,
                                    "starts_on": "2026-01-01", "ends_on": "2026-11-23"})
    report.create_contract(env.sf, {"name": "Later", "gpu_model": "rtx4090", "gpu_count": 99,
                                    "starts_on": "2026-12-01"})
    gone = report.create_contract(env.sf, {"name": "Gone", "gpu_model": "rtx4090", "gpu_count": 99,
                                           "starts_on": "2026-11-01"})
    assert report.update_contract(env.sf, gone["contract_id"], {"archived": True})["active"] is False
    gpus = report.build(env.sf, admin=True, now=NOW)["gpus"]
    assert (gpus["gpus"], gpus["contract_gpus"], gpus["total_gpus"]) == (24, 50, 74)
    assert [(m["label"], m["gpus"], m["contract_gpus"], m["total_gpus"]) for m in gpus["by_model"]] == [
        ("H100", 0, 10, 10), ("RTX 4090", 16, 0, 16), ("RTX 5090", 8, 40, 48)]
    acme = gpus["by_farmer"][0]
    # Acme: 8 x 0.40 + 40 x 0.70 = $31.20 an hour of $50.00 (plus 8 x 0.40 + 8 x 0.70 + 10 x 1.00 unassigned).
    assert (acme["farmer"], acme["gpus"], acme["contract_gpus"], acme["usd_per_hour"], acme["share_pct"]) == (
        "Farmer A", 8, 40, "31.20", 62.4)
    public = report.build(env.sf, now=NOW)["gpus"]
    assert (public["total"], public["on_demand"], public["under_contract"]) == ("70+", "20+", "50+")
    assert [(m["label"], m["gpus"]) for m in public["by_model"]] == [
        ("H100", "10+"), ("RTX 4090", "10+"), ("RTX 5090", "40+")]
    assert public["providers"][0] == {"provider": "Farmer A", "types": ["RTX 4090", "RTX 5090"], "share_pct": 60,
                                      "share": "60%+"}

    # Payments received are revenue on the day they arrived, on either basis, and are bought back.
    status, payment = call("POST", f"/platform/admin/contracts/{cid}/payments",
                           {"received_on": "2026-11-05", "amount_usd": 14600, "reference": "wire 881"},
                           key="gk_admin")
    assert status == 200 and (payment["amount_usd"], payment["received_on"]) == ("14600.00", "2026-11-05")
    report.record_contract_payment(env.sf, cid, received_on="2026-10-20", amount_usd=5000)   # before the start date
    ledger(env, "card", 2000, ref="stripe:cs_1", when=NOW - timedelta(days=1))
    ledger(env, "gamma", 700, ref="gamma:110:0:svc:hk:7", when=NOW - timedelta(days=3))
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert {m["method"]: m["usd"] for m in revenue["by_method"]} == {
        "gamma": "7.00", "contract": "14600.00", "card": "20.00", "crypto": "0.00"}
    quarter = revenue["periods"][0]
    assert (quarter["usd"], quarter["contract_usd"], quarter["to_buy_back_usd"]) == ("14627.00", "14600.00", "14620.00")
    out = report.buyback(env.sf, now=JANUARY)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["paid_in_gamma_usd"]) == ("14627.00", "14620.00", "7.00")
    report.update_settings(env.sf, {"revenue_basis": "usage"})
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["total_usd"] == "14600.00"
    report.update_settings(env.sf, {"revenue_basis": "paid"})
    # The public report, through the last completed week: the payment of the 5th and the gamma of the
    # 21st, not yesterday's card payment.
    public = report.build(env.sf, now=NOW)
    assert (public["revenue"]["total_usd"], public["revenue"]["contracts_usd"], public["revenue"]["normal_usd"]) == (
        "14607.00", "14600.00", "7.00")
    # The contracts are listed by label, with their term, GPU type (no count) and what was received: no name,
    # no notes, no reference, no value. The ended, not-yet-started and archived ones that brought in
    # nothing are left out or marked.
    assert public["contracts"] == [
        {"label": "Contract A", "term_months": None, "starts_on": "2026-11-01", "ends_on": None, "status": "active",
         "gpu_label": "RTX 5090", "gpus": None, "received_usd": "14600.00"},
        {"label": "Contract B", "term_months": None, "starts_on": "2026-11-10", "ends_on": None, "status": "active",
         "gpu_label": "H100", "gpus": None, "received_usd": "0.00"},
    ]
    # Once it has ended, a contract that brought in money stays listed, as ended, under the same label.
    report.update_contract(env.sf, cid, {"ends_on": "2026-11-20"})
    listed = report.build(env.sf, now=NOW)["contracts"]
    assert [(c["label"], c["status"], c["received_usd"]) for c in listed] == [
        ("Contract A", "ended", "14600.00"), ("Contract B", "active", "0.00")]
    report.update_contract(env.sf, cid, {"ends_on": None})
    later = json.dumps(report.build(env.sf, now=JANUARY))
    assert '"contract_usd": "14600.00"' in later
    for private in ("Lab One", "wire 881", "bare metal", "monthly", "value_usd", "contract_id", "farmer_id"):
        assert private not in later, private
    # With exact GPU detail the counts are exact; with the list switched off there is no list at all.
    report.update_settings(env.sf, {"gpu_detail": "exact"})
    assert [c["gpus"] for c in report.build(env.sf, now=NOW)["contracts"]] == [40, 10]
    report.update_settings(env.sf, {"gpu_detail": "bands", "show_contracts": False})
    hidden = report.build(env.sf, now=NOW)
    assert "contracts" not in hidden and hidden["revenue"]["contracts_usd"] == "14600.00"
    report.update_settings(env.sf, {"show_contracts": True})
    # Staff's list: the contract with its payments, newest first, and what it has brought in.
    status, body = call("GET", "/platform/admin/report", key="gk_admin")
    mine = next(c for c in body["contracts"] if c["contract_id"] == cid)
    assert mine["received_usd"] == "19600.00"
    assert [p["received_on"] for p in mine["payments"]] == ["2026-11-05", "2026-10-20"]
    assert [c["name"] for c in body["contracts"]][-1] == "Gone"      # archived ones last
    # Taking a payment back out takes its revenue out.
    assert call("DELETE", f"/platform/admin/contract-payments/{payment['payment_id']}", key="gk_admin")[0] == 200
    assert call("DELETE", f"/platform/admin/contract-payments/{payment['payment_id']}", key="gk_admin")[0] == 404
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["total_usd"] == "27.00"


def test_contracts_are_checked(env):
    good = {"name": "Lab", "starts_on": "2026-11-01"}
    for bad in ({"starts_on": "2026-11-01"}, {"name": "Lab"}, {**good, "starts_on": "soon"},
                {**good, "gpu_count": 4}, {**good, "gpu_model": "rtx5090", "gpu_count": -1},
                {**good, "gpu_count": 2.5, "gpu_model": "x"}, {**good, "monthly_usd": "12000"},
                {**good, "monthly_usd": -5}, {**good, "ends_on": "2026-10-31"}, {**good, "farmer_id": "nope"},
                {**good, "colour": "green"}, {**good, "starts_on": "1999-01-01"}):
        assert call("POST", "/platform/admin/contracts", bad, key="gk_admin")[0] == 400, bad
    status, contract = call("POST", "/platform/admin/contracts", good, key="gk_admin")
    assert status == 200 and (contract["gpu_model"], contract["gpu_count"], contract["monthly_usd"]) == ("", 0, None)
    cid = contract["contract_id"]
    assert call("POST", "/platform/admin/contracts", good, key="gk_admin")[0] == 400       # the name is taken
    other = report.create_contract(env.sf, {"name": "Other", "starts_on": "2026-11-01"})
    assert call("PATCH", f"/platform/admin/contracts/{other['contract_id']}", {"name": "Lab"}, key="gk_admin")[0] == 400
    assert call("PATCH", f"/platform/admin/contracts/{cid}", {"gpu_count": 8}, key="gk_admin")[0] == 400  # which GPU?
    status, changed = call("PATCH", f"/platform/admin/contracts/{cid}",
                           {"gpu_model": "RTX 4090", "gpu_count": 8, "monthly_usd": 2000.5, "ends_on": "2027-10-31"},
                           key="gk_admin")
    assert status == 200 and (changed["gpu_label"], changed["monthly_usd"], changed["ends_on"]) == (
        "RTX 4090", "2000.50", "2027-10-31")
    status, cleared = call("PATCH", f"/platform/admin/contracts/{cid}", {"monthly_usd": None, "ends_on": None,
                                                                         "farmer_id": None}, key="gk_admin")
    assert status == 200 and (cleared["monthly_usd"], cleared["ends_on"], cleared["gpu_count"]) == (None, None, 8)
    assert call("PATCH", f"/platform/admin/contracts/{cid}", {"ends_on": "2026-01-01"}, key="gk_admin")[0] == 400
    assert call("PATCH", "/platform/admin/contracts/nope", {"name": "x"}, key="gk_admin")[0] == 404
    tomorrow_and_more = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    for bad in ({"received_on": "2026-11-05", "amount_usd": 0}, {"received_on": "2026-11-05", "amount_usd": "5"},
                {"amount_usd": 5}, {"received_on": "5 Nov", "amount_usd": 5},
                {"received_on": tomorrow_and_more, "amount_usd": 5}, {"received_on": "2026-11-05", "amount_usd": True}):
        assert call("POST", f"/platform/admin/contracts/{cid}/payments", bad, key="gk_admin")[0] == 400, bad
    assert call("POST", "/platform/admin/contracts/nope/payments", {"received_on": "2026-11-05", "amount_usd": 5},
                key="gk_admin")[0] == 404
    # A payment dated tomorrow (allowed, for time zones) counts from its day on, in totals and rows alike.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    report.record_contract_payment(env.sf, cid, received_on=tomorrow, amount_usd=7)
    today_report = report.build(env.sf, admin=True)["revenue"]
    assert today_report["total_usd"] == "0.00" and sum(float(q["usd"]) for q in today_report["periods"]) == 0
    assert report.build(env.sf, admin=True, now=datetime.now(UTC) + timedelta(days=1))["revenue"]["total_usd"] == "7.00"
    report.delete_contract_payment(env.sf, next(
        p["payment_id"] for c in report.list_contracts(env.sf) for p in c["payments"] if p["received_on"] == tomorrow))
    # An archived contract keeps the revenue it brought in.
    report.record_contract_payment(env.sf, cid, received_on="2026-11-05", amount_usd=100)
    report.update_contract(env.sf, cid, {"archived": True})
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["total_usd"] == "100.00"
    assert report.build(env.sf, admin=True, now=NOW)["gpus"]["contract_gpus"] == 0


# --- the two flows staff see: normal flow by currency, and contracts ------------------


def test_staff_see_the_normal_flow_by_currency_and_contracts_apart(env):
    """Normal flow: what came in through the website, each currency on its
    own, gamma among them but burned. Contracts: the sales team's direct
    deals. The public report keeps to the four streams."""
    with session_scope(env.sf) as s:
        s.add(CryptoInvoiceORM(invoice_id="inv1", user_id="cust", currency="usdc-base", amount_usd=20.0,
                               amount_crypto=20.0, bonus_pct=0.05, total_credits=2100, deposit_address="0xabc",
                               status="confirmed", created_at=NOW, expires_at=NOW))
    day = NOW - timedelta(days=2)
    ledger(env, "card", 2000, ref="stripe:cs_1", when=day)
    ledger(env, "usdc", 2100, ref="usdc-base:0xabc:1", reference="inv1", when=day)      # $20 + 5% bonus
    ledger(env, "usdt", 1500, ref="usdt-eth:0xdef:2", when=day)
    ledger(env, "tao", 4000, ref="tao:9131128:42", when=day)
    ledger(env, "manual", 5000, ref="manual:tao:0xdead", when=day)                      # staff confirmed a TAO tx
    ledger(env, "alpha", 1100, ref="alpha:9:4", when=day)                               # $10 + 10% alpha bonus
    ledger(env, "gamma", 700, ref="gamma:110:0:svc:hk:7", when=day)
    ledger(env, "odd", 300, ref="doge:1:1", when=day)                                   # a crypto we don't name
    ledger(env, "cb", -500, kind="chargeback", when=day)
    contract = report.create_contract(env.sf, {"name": "Lab", "starts_on": "2026-11-01", "term_months": 2})
    report.record_contract_payment(env.sf, contract["contract_id"], received_on="2026-11-10", amount_usd=9000)
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert [(c["currency"], c["label"], c["usd"]) for c in revenue["by_currency"]] == [
        ("card", "Card", "15.00"), ("usdc", "USDC", "20.00"), ("usdt", "USDT", "15.00"), ("tao", "TAO", "90.00"),
        ("alpha", "Alpha", "10.00"), ("gamma", "Gamma", "7.00"), ("other", "Other", "3.00")]
    assert (revenue["total_usd"], revenue["normal_usd"], revenue["contracts_usd"]) == ("9160.00", "160.00", "9000.00")
    # The four streams still add up: crypto is USDC + USDT + TAO + alpha + other.
    assert {m["method"]: m["usd"] for m in revenue["by_method"]} == {
        "gamma": "7.00", "contract": "9000.00", "card": "15.00", "crypto": "138.00"}
    quarter = revenue["periods"][0]
    assert {k: quarter[k] for k in ("usd", "normal_usd", "contract_usd", "card_usd", "usdc_usd", "usdt_usd", "tao_usd",
                                    "alpha_usd", "other_usd", "gamma_usd", "crypto_usd")} == {
        "usd": "9160.00", "normal_usd": "160.00", "contract_usd": "9000.00", "card_usd": "15.00", "usdc_usd": "20.00",
        "usdt_usd": "15.00", "tao_usd": "90.00", "alpha_usd": "10.00", "other_usd": "3.00", "gamma_usd": "7.00",
        "crypto_usd": "138.00"}
    assert quarter["to_buy_back_usd"] == "9153.00"        # everything but the gamma
    # On the usage basis the normal flow is the compute charged; contracts are the same.
    ledger(env, "use", -400, kind="usage", reference="dep-a", description="GPU usage 1× GPU @ $0.40/hr", when=day)
    report.update_settings(env.sf, {"revenue_basis": "usage"})
    usage = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert (usage["total_usd"], usage["normal_usd"], usage["contracts_usd"]) == ("9004.00", "4.00", "9000.00")
    report.update_settings(env.sf, {"revenue_basis": "paid"})
    # The public report shows the same two flows and the same split, through the last completed week
    # (the payments were on Sunday the 22nd), and never single days.
    public = report.build(env.sf, now=NOW)["revenue"]
    assert (public["through"], public["total_usd"], public["normal_usd"], public["contracts_usd"]) == (
        "2026-11-22", "9160.00", "160.00", "9000.00")
    assert public["by_currency"] == revenue["by_currency"] and "days" not in public
    row = public["periods"][0]
    assert (row["complete"], row["normal_usd"], row["usdt_usd"], row["tao_usd"], row["gamma_usd"]) == (
        False, "160.00", "15.00", "90.00", "7.00")
    # A week earlier none of it had happened yet as far as the public could see.
    earlier = report.build(env.sf, now=NOW - timedelta(days=3))["revenue"]
    assert (earlier["through"], earlier["normal_usd"], earlier["contracts_usd"]) == ("2026-11-15", "0.00", "9000.00")


def test_contract_terms_one_month_two_months_a_quarter(env):
    """What the sales team sells: a term in whole months from a start date."""
    status, one = call("POST", "/platform/admin/contracts", {
        "name": "One month", "starts_on": "2026-11-01", "term_months": 1, "monthly_usd": 3000,
        "gpu_model": "rtx5090", "gpu_count": 8}, key="gk_admin")
    assert status == 200
    assert (one["ends_on"], one["term_months"], one["value_usd"], one["outstanding_usd"]) == (
        "2026-11-30", 1, "3000.00", "3000.00")
    two = report.create_contract(env.sf, {"name": "Two months", "starts_on": "2026-11-15", "term_months": 2,
                                          "monthly_usd": 1000})
    assert (two["ends_on"], two["term_months"], two["value_usd"]) == ("2027-01-14", 2, "2000.00")
    quarter = report.create_contract(env.sf, {"name": "A quarter", "starts_on": "2026-12-01", "term_months": 3})
    assert (quarter["ends_on"], quarter["term_months"], quarter["value_usd"], quarter["outstanding_usd"]) == (
        "2027-02-28", 3, None, None)                      # no monthly value: no contract value
    year = report.create_contract(env.sf, {"name": "A year", "starts_on": "2026-01-31", "term_months": 12,
                                           "monthly_usd": 100})
    assert (year["ends_on"], year["term_months"], year["value_usd"]) == ("2027-01-30", 12, "1200.00")
    # A month that is shorter than the start day: 31 January + 1 month ends in February.
    short = report.create_contract(env.sf, {"name": "From the 31st", "starts_on": "2027-01-31", "term_months": 1})
    assert (short["ends_on"], short["term_months"]) == ("2027-02-27", 1)
    # Dates given by hand: a whole number of months is recognised, anything else has no term.
    by_hand = report.create_contract(env.sf, {"name": "By hand", "starts_on": "2026-11-01", "ends_on": "2027-01-31",
                                              "monthly_usd": 500})
    assert (by_hand["term_months"], by_hand["value_usd"]) == (3, "1500.00")
    odd = report.create_contract(env.sf, {"name": "Odd", "starts_on": "2026-11-01", "ends_on": "2026-11-20",
                                          "monthly_usd": 500})
    assert (odd["term_months"], odd["value_usd"], odd["outstanding_usd"]) == (None, None, None)
    open_ended = report.create_contract(env.sf, {"name": "Open", "starts_on": "2026-11-01", "monthly_usd": 500})
    assert (open_ended["ends_on"], open_ended["term_months"], open_ended["value_usd"]) == (None, None, None)
    # What is received comes off what is outstanding, never below zero.
    cid = one["contract_id"]
    report.record_contract_payment(env.sf, cid, received_on="2026-11-02", amount_usd=1200)
    assert report.list_contracts(env.sf)[-1]["name"] and next(
        c for c in report.list_contracts(env.sf) if c["contract_id"] == cid)["outstanding_usd"] == "1800.00"
    report.record_contract_payment(env.sf, cid, received_on="2026-11-20", amount_usd=2500)
    assert next(c for c in report.list_contracts(env.sf) if c["contract_id"] == cid)["outstanding_usd"] == "0.00"
    # Editing: a longer term moves the end date; a new start date with a term moves both.
    status, longer = call("PATCH", f"/platform/admin/contracts/{cid}", {"term_months": 3}, key="gk_admin")
    assert status == 200 and (longer["ends_on"], longer["value_usd"], longer["outstanding_usd"]) == (
        "2027-01-31", "9000.00", "5300.00")
    status, moved = call("PATCH", f"/platform/admin/contracts/{cid}", {"starts_on": "2026-12-10", "term_months": 1},
                         key="gk_admin")
    assert status == 200 and (moved["starts_on"], moved["ends_on"], moved["term_months"]) == (
        "2026-12-10", "2027-01-09", 1)
    status, opened = call("PATCH", f"/platform/admin/contracts/{cid}", {"ends_on": None}, key="gk_admin")
    assert status == 200 and (opened["ends_on"], opened["term_months"], opened["value_usd"]) == (None, None, None)
    for bad in ({"term_months": 0}, {"term_months": 121}, {"term_months": 1.5}, {"term_months": "3"},
                {"term_months": True}, {"term_months": 2, "ends_on": "2027-01-31"}):
        assert call("PATCH", f"/platform/admin/contracts/{cid}", bad, key="gk_admin")[0] == 400, bad
        assert call("POST", "/platform/admin/contracts", {"name": "Bad", "starts_on": "2026-11-01", **bad},
                    key="gk_admin")[0] == 400, bad


def test_a_recorded_contract_payment_can_be_corrected(env):
    contract = report.create_contract(env.sf, {"name": "Lab", "starts_on": "2026-11-01", "term_months": 1})
    cid = contract["contract_id"]
    payment = report.record_contract_payment(env.sf, cid, received_on="2026-11-05", amount_usd=100, reference="A")
    pid = payment["payment_id"]
    status, fixed = call("PATCH", f"/platform/admin/contract-payments/{pid}",
                         {"amount_usd": 150.5, "received_on": "2026-11-06", "reference": "INV-7", "note": "typo"},
                         key="gk_admin")
    assert status == 200 and fixed == {"payment_id": pid, "contract_id": cid, "received_on": "2026-11-06",
                                       "amount_usd": "150.50", "reference": "INV-7", "note": "typo"}
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["contracts_usd"] == "150.50"
    # One field at a time keeps the others.
    status, kept = call("PATCH", f"/platform/admin/contract-payments/{pid}", {"note": ""}, key="gk_admin")
    assert status == 200 and (kept["amount_usd"], kept["reference"], kept["note"]) == ("150.50", "INV-7", "")
    future = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    for bad in ({"amount_usd": 0}, {"amount_usd": "5"}, {"received_on": "5 Nov"}, {"received_on": future},
                {"colour": "green"}, {"received_on": None}):
        assert call("PATCH", f"/platform/admin/contract-payments/{pid}", bad, key="gk_admin")[0] == 400, bad
    assert call("PATCH", "/platform/admin/contract-payments/nope", {"note": "x"}, key="gk_admin")[0] == 404
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["contracts_usd"] == "150.50"

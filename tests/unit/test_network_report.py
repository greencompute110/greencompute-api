"""The network report const asked subnet 110 to publish: GPUs by type and by
farmer, revenue, buybacks, and farmers' monthly statements.

Covered: nothing is public until published; what counts as revenue (real
payments without bonuses, never staff credit; card chargebacks; excluded
accounts; the counting-from date); GPU-hours and revenue per machine; which
machines are listed, online, hidden and whose; farmers and their labels;
the buyback sum (gamma payments already bought back, treasury spends
count); statements and payments; and that every staff route needs an admin
key."""
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

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
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


def test_settings_are_checked(env):
    for bad in ({"since": "yesterday"}, {"revenue_basis": "profit"}, {"listed_within_days": 0},
                {"published": "yes"}, {"excluded_user_ids": "cust"}, {"colour": "green"}):
        with pytest.raises(report.ReportError):
            report.update_settings(env.sf, bad)
    out = report.update_settings(env.sf, {"since": "2026-10-01", "revenue_basis": "usage", "listed_within_days": 7,
                                          "excluded_user_ids": ["b", "a", "a"]})
    assert out["excluded_user_ids"] == ["a", "b"] and report.get_settings(env.sf) == out
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
    report.update_settings(env.sf, {"excluded_user_ids": ["staff-test"]})
    revenue = report.build(env.sf, admin=True, now=NOW)["revenue"]  # staff: up to now
    by = {m["method"]: m["usd"] for m in revenue["by_method"]}
    assert by == {"card": "15.00", "crypto": "80.00", "gamma": "7.00"}
    assert revenue["total_usd"] == "102.00" and revenue["basis"] == "paid" and revenue["through"] == "2026-11-24"
    this_week = revenue["weeks"][0]
    assert this_week["week_start"] == "2026-11-23" and this_week["card_usd"] == "15.00"
    last_week = revenue["weeks"][1]
    assert (last_week["crypto_usd"], last_week["gamma_usd"], last_week["usd"]) == ("30.00", "7.00", "37.00")
    assert revenue["days"][0] == {"day": "2026-11-24", "usd": "-5.00"}
    # Counting from October includes the earlier payment.
    report.update_settings(env.sf, {"since": "2026-10-01"})
    assert report.build(env.sf, admin=True, now=NOW)["revenue"]["total_usd"] == "179.00"


def test_the_public_report_stops_at_the_last_completed_week(env):
    """NOW is a Tuesday: the public sees money and GPU-hours through Sunday
    the 22nd, totals included, so polling can't show a payment as it lands
    or how many GPUs are rented this minute."""
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
    assert {m["method"]: m["usd"] for m in revenue["by_method"]} == {"card": "7.00", "crypto": "30.00", "gamma": "0.00"}
    assert [w["week_start"] for w in revenue["weeks"]][0] == "2026-11-16" and "days" not in revenue
    assert revenue["compute_delivered"] == {"usd": "4.00", "gpu_hours": "10.0"}
    assert public["buybacks"]["to_buy_back_usd"] == "37.00"
    # Staff see up to now.
    staff = report.build(env.sf, admin=True, now=NOW)["revenue"]
    assert staff["total_usd"] == "89.21" and staff["compute_delivered"]["gpu_hours"] == "30.0"
    # Before the first completed week there is nothing to show yet.
    report.update_settings(env.sf, {"since": "2026-11-23"})
    early = report.build(env.sf, now=NOW)["revenue"]
    assert (early["through"], early["total_usd"], early["weeks"]) == (None, "0.00", [])


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
    gpus = report.build(env.sf, now=NOW)["gpus"]
    assert (gpus["gpus"], gpus["online_gpus"], gpus["machines"], gpus["machines_online"]) == (24, 16, 3, 2)
    assert [(m["label"], m["gpus"], m["online_gpus"]) for m in gpus["by_model"]] == [
        ("RTX 4090", 16, 8), ("RTX 5090", 8, 8)]
    assert [(f["farmer"], f["gpus"], f["online_gpus"]) for f in gpus["by_farmer"]] == [
        ("Farmer A", 16, 8), ("Unassigned", 8, 8)]
    # The public report never carries a farmer's name or id; staff's does.
    assert "Acme" not in json.dumps(report.build(env.sf, now=NOW))
    admin = report.build(env.sf, admin=True, now=NOW)["gpus"]["by_farmer"][0]
    assert admin["name"] == "Acme Hosting" and admin["farmer_id"] == a["farmer_id"]
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
    out = report.buyback(env.sf, now=NOW)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["bought_back_usd"], out["outstanding_usd"]) == (
        "125.00", "100.00", "30.00", "70.00")
    assert out["paid_in_gamma_usd"] == "25.00" and out["tao_usd"] == 400.0 and out["tao_to_mint"] == "0.1750"
    assert out["commands"][0] == "btcli gamma mint --netuid 110 --hotkey gc-buyback --amount-tao 0.1750"
    assert "--service-hotkey 5CwckYmAV3fET8V9KTKfqds6BnRGACAgofECQqdYfa2Q9rvp" in out["commands"][1]
    assert out["on_chain"]["spent_gamma"] == "555"  # every spend here, whenever
    assert [entry["gamma"] for entry in out["log"]] == ["30"]
    staff = report.build(env.sf, admin=True, now=NOW)["buybacks"]
    assert (staff["to_buy_back_usd"], staff["outstanding_usd"]) == ("100.00", "70.00")
    public = report.build(env.sf, now=NOW)
    assert not [k for k in (*public["buybacks"], *public["revenue"]) if k.endswith("_cents")]
    # This week's payments aren't public yet; the buyback already made is (it is on chain).
    assert (public["buybacks"]["to_buy_back_usd"], public["buybacks"]["bought_back_usd"]) == ("0.00", "30.00")
    assert "All gamma ever spent" in public["buybacks"]["on_chain"]["scope"]
    # Bought back in full: nothing outstanding, no commands.
    _treasury_spend(env, "b2", 80)
    out = report.buyback(env.sf, now=NOW)
    assert out["outstanding_usd"] == "0.00" and out["commands"] == [] and out["tao_to_mint"] is None


def test_buyback_without_a_price_gives_no_amount(env, monkeypatch):
    def no_price(currency):
        raise RuntimeError("no endpoint")

    monkeypatch.setattr("greencompute_gateway.infrastructure.price_feed.get_price", no_price)
    ledger(env, "card", 6000, ref="stripe:cs_1")
    out = report.buyback(env.sf, now=NOW)
    assert out["outstanding_usd"] == "60.00" and out["tao_usd"] is None and out["commands"] == []


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
    out = report.buyback(env.sf, now=NOW)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["paid_in_gamma_usd"]) == ("50.00", "50.00", "0.00")
    # Once the gamma payer uses $30 of it, that $30 is revenue already bought back.
    ledger(env, "use-g", -3000, kind="usage", reference="dep-b", description="GPU usage 1× GPU @ $0.70/hr",
           user="gamma-payer")
    out = report.buyback(env.sf, now=NOW)
    assert (out["revenue_usd"], out["to_buy_back_usd"], out["paid_in_gamma_usd"]) == ("80.00", "50.00", "30.00")


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
    report.update_settings(env.sf, {"show_farmer_payments": True})
    assert report.build(env.sf, now=NOW)["farmer_payments"] == {
        "total_usd": "15.00", "months": [{"month": "2026-11", "usd": "15.00"}]}
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
    # The preview is what the public would get: no names, no days, completed weeks only.
    assert "days" not in body["preview"]["revenue"] and "days" in body["report"]["revenue"]
    assert "name" not in body["preview"]["gpus"]["by_farmer"][0]
    # The public spec lists the report and none of the staff routes.
    app.openapi_schema = None
    paths = app.openapi()["paths"]
    app.openapi_schema = None
    assert "/platform/report" in paths and paths["/platform/report"]["get"]["security"] == []
    assert not [p for p in paths if p.startswith("/platform/admin/")]

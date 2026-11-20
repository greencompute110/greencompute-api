"""The TLS certificate watch: alerts before a certificate on our own hosts
expires (the 2026-11-20 api/control/validator outage), once a day per host,
not on a single unreachable check, and covers model addresses once their
deployment has been ready for an hour."""
import ssl
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from greencompute_gateway.application import tls_watch as tw
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import DeploymentORM, WorkloadORM

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
API = "api.green-compute.com"


@pytest.fixture
def sf(tmp_path, monkeypatch):
    for name in ("GREENCOMPUTE_TLS_WATCH_HOSTS", "GREENCOMPUTE_TLS_WARN_DAYS"):
        monkeypatch.delenv(name, raising=False)
    return BillingRepository(database_url=f"sqlite:///{tmp_path / 't.sqlite'}", bootstrap=True).session_factory


def model(sf, host, state="ready", ready_minutes_ago=120):
    wid = str(uuid4())
    with session_scope(sf) as s:
        s.add(WorkloadORM(workload_id=wid, owner_user_id="u", name="m", image="img", kind="inference",
                          security_tier="standard", pricing_class="standard", runtime={}, requirements={},
                          ingress_host=host))
        s.add(DeploymentORM(deployment_id=str(uuid4()), workload_id=wid, owner_user_id="u", state=state,
                            requested_instances=1,
                            ready_at=None if ready_minutes_ago is None else NOW - timedelta(minutes=ready_minutes_ago)))


def checker(table):
    """A check() that returns table[host] (default: valid for 60 days)."""
    def check(host):
        return table.get(host, tw.Result(host, tw.OK, expires_at=NOW + timedelta(days=60)))
    return check


def run(sf, table, watch, now=NOW):
    sent = []
    out = tw.run_once(sf, now=now, check=checker(table), send=sent.append, watch=watch)
    return out, sent


# --- check_host ---------------------------------------------------------------

def test_valid_certificate_is_ok_and_one_near_expiry_is_expiring():
    ok = tw.check_host(API, NOW, fetch=lambda h, c: NOW + timedelta(days=60), gateway=lambda: None)
    assert ok.status == tw.OK and ok.expires_at == NOW + timedelta(days=60)
    soon = tw.check_host(API, NOW, fetch=lambda h, c: NOW + timedelta(days=13), gateway=lambda: None)
    assert soon.status == tw.EXPIRING


def test_warn_days_setting(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_TLS_WARN_DAYS", "30")
    assert tw.check_host(API, NOW, fetch=lambda h, c: NOW + timedelta(days=20), gateway=lambda: None).status == tw.EXPIRING
    monkeypatch.setenv("GREENCOMPUTE_TLS_WARN_DAYS", "nonsense")
    assert tw.warn_days() == 14


def test_expired_certificate_is_invalid_and_not_retried():
    calls = []

    def fetch(host, connect):
        calls.append(connect)
        err = ssl.SSLCertVerificationError("certificate verify failed")
        err.verify_message = "certificate has expired"
        raise err

    r = tw.check_host(API, NOW, fetch=fetch, gateway=lambda: "172.18.0.1")
    assert r.status == tw.INVALID and r.detail == "certificate has expired"
    assert calls == [None]


def test_unreachable_directly_retries_through_the_default_gateway():
    calls = []

    def fetch(host, connect):
        calls.append(connect)
        if connect is None:
            raise OSError(113, "No route to host")
        return NOW + timedelta(days=80)

    r = tw.check_host(API, NOW, fetch=fetch, gateway=lambda: "172.18.0.1")
    assert r.status == tw.OK and calls == [None, "172.18.0.1"]


def test_unreachable_both_ways_without_a_gateway():
    def fetch(host, connect):
        raise TimeoutError("timed out")

    r = tw.check_host(API, NOW, fetch=fetch, gateway=lambda: "172.18.0.1")
    assert r.status == tw.UNREACHABLE and "TimeoutError" in r.detail
    r = tw.check_host(API, NOW, fetch=fetch, gateway=lambda: None)
    assert r.status == tw.UNREACHABLE


def test_default_gateway_parses_proc_net_route(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    assert tw.default_gateway(str(route)) == "172.18.0.1"
    assert tw.default_gateway(str(tmp_path / "missing")) is None


# --- run_once -----------------------------------------------------------------

def test_healthy_hosts_send_nothing(sf):
    out, sent = run(sf, {}, tw.Watch())
    assert sent == [] and [r.host for r in out["results"]] == list(tw.DEFAULT_HOSTS)


def test_expiring_and_invalid_alert_once_a_day(sf):
    watch = tw.Watch()
    table = {
        API: tw.Result(API, tw.EXPIRING, expires_at=NOW + timedelta(days=9, hours=3)),
        "control.green-compute.com": tw.Result("control.green-compute.com", tw.INVALID, detail="certificate has expired"),
    }
    _, sent = run(sf, table, watch)
    assert len(sent) == 1
    assert "api.green-compute.com: certificate expires in 9 days (2026-11-29 15:00 UTC)" in sent[0]
    assert "control.green-compute.com: certificate is not valid (certificate has expired)" in sent[0]
    assert "certbot renew --dry-run" in sent[0]
    _, sent = run(sf, table, watch, now=NOW + timedelta(hours=6))
    assert sent == []
    _, sent = run(sf, table, watch, now=NOW + timedelta(hours=25))
    assert len(sent) == 1


def test_recovery_resets_so_a_new_problem_alerts_at_once(sf):
    watch = tw.Watch()
    bad = {API: tw.Result(API, tw.INVALID, detail="x")}
    assert len(run(sf, bad, watch)[1]) == 1
    assert run(sf, {}, watch, now=NOW + timedelta(hours=1))[1] == []
    assert len(run(sf, bad, watch, now=NOW + timedelta(hours=2))[1]) == 1


def test_unreachable_alerts_only_after_three_checks_in_a_row(sf):
    watch = tw.Watch()
    down = {API: tw.Result(API, tw.UNREACHABLE, detail="OSError: no route")}
    assert run(sf, down, watch)[1] == []
    assert run(sf, down, watch, now=NOW + timedelta(hours=6))[1] == []
    _, sent = run(sf, down, watch, now=NOW + timedelta(hours=12))
    assert len(sent) == 1 and "could not check the certificate for 3 checks in a row" in sent[0]
    # one good check resets the streak
    run(sf, {}, watch, now=NOW + timedelta(hours=18))
    assert run(sf, down, watch, now=NOW + timedelta(hours=24))[1] == []


def test_model_addresses_ready_for_an_hour_are_watched(sf):
    model(sf, "llama-3-1-8b-7fb157.inference.green-compute.com")
    model(sf, "new-model-aaaaaa.inference.green-compute.com", ready_minutes_ago=10)
    model(sf, "stopped-bbbbbb.inference.green-compute.com", state="stopped")
    model(sf, "pending-cccccc.inference.green-compute.com", state="pending", ready_minutes_ago=None)
    hosts = tw.watched_hosts(sf, NOW)
    assert hosts == [*tw.DEFAULT_HOSTS, "llama-3-1-8b-7fb157.inference.green-compute.com"]


def test_hosts_setting_replaces_the_defaults(sf, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_TLS_WATCH_HOSTS", " API.example.com , ,b.example.com")
    assert tw.watched_hosts(sf, NOW) == ["api.example.com", "b.example.com"]


# --- review fixes -------------------------------------------------------------

def test_one_bad_host_never_stops_the_others_alerts(sf):
    model(sf, "ok-model-aaaaaa.inference.green-compute.com")
    watch = tw.Watch()
    table = {API: tw.Result(API, tw.INVALID, detail="certificate has expired")}

    def check(host):
        if host.startswith("ok-model"):
            raise UnicodeError("label too long")
        return checker(table)(host)

    sent = []
    out = tw.run_once(sf, now=NOW, check=check, send=sent.append, watch=watch)
    assert len(sent) == 1 and "api.green-compute.com: certificate is not valid" in sent[0]
    bad = next(r for r in out["results"] if r.host.startswith("ok-model"))
    assert bad.status == tw.UNREACHABLE and "UnicodeError" in bad.detail


def test_database_down_still_checks_the_base_hosts():
    def broken():
        raise RuntimeError("database is down")

    sent = []
    out = tw.run_once(broken, now=NOW, check=checker({API: tw.Result(API, tw.INVALID, detail="x")}),
                      send=sent.append, watch=tw.Watch())
    assert [r.host for r in out["results"]] == list(tw.DEFAULT_HOSTS) and len(sent) == 1


def test_a_failed_send_is_retried_next_pass(sf):
    watch = tw.Watch()
    bad = {API: tw.Result(API, tw.INVALID, detail="x")}

    def failing(text):
        raise RuntimeError("smtp down")

    with pytest.raises(RuntimeError):
        tw.run_once(sf, now=NOW, check=checker(bad), send=failing, watch=watch)
    assert len(run(sf, bad, watch, now=NOW + timedelta(minutes=15))[1]) == 1


def test_expiring_then_expired_alerts_at_once(sf):
    watch = tw.Watch()
    assert len(run(sf, {API: tw.Result(API, tw.EXPIRING, expires_at=NOW + timedelta(hours=5))}, watch)[1]) == 1
    _, sent = run(sf, {API: tw.Result(API, tw.INVALID, detail="certificate has expired")}, watch,
                  now=NOW + timedelta(hours=6))
    assert len(sent) == 1 and "certificate has expired" in sent[0]


def test_handshake_drops_are_unreachable_not_invalid():
    calls = []

    def fetch(host, connect):
        calls.append(connect)
        if connect is None:
            raise OSError(113, "No route to host")
        raise ssl.SSLEOFError("EOF occurred in violation of protocol")

    r = tw.check_host(API, NOW, fetch=fetch, gateway=lambda: "172.18.0.1")
    assert r.status == tw.UNREACHABLE and "SSLEOFError" in r.detail and calls == [None, "172.18.0.1"]

    def fetch_then_ok(host, connect):
        if connect is None:
            raise ssl.SSLZeroReturnError("closed")
        return NOW + timedelta(days=50)

    assert tw.check_host(API, NOW, fetch=fetch_then_ok, gateway=lambda: "172.18.0.1").status == tw.OK


def test_unreachable_both_ways_tries_the_gateway_after_a_timeout():
    calls = []

    def fetch(host, connect):
        calls.append(connect)
        raise TimeoutError("timed out")

    assert tw.check_host(API, NOW, fetch=fetch, gateway=lambda: "172.18.0.1").status == tw.UNREACHABLE
    assert calls == [None, "172.18.0.1"]


def test_only_our_zone_is_watched(sf):
    model(sf, "good-123abc.inference.green-compute.com")
    model(sf, "chat.customer-domain.com")
    model(sf, "evil.inference.green-compute.com.attacker.net")
    model(sf, "a.b.inference.green-compute.com")
    model(sf, ("x" * 64) + ".inference.green-compute.com")
    assert tw.model_hosts(sf, NOW) == ["good-123abc.inference.green-compute.com"]


def test_a_model_address_alerts_invalid_only_twice_in_a_row(sf):
    host = "fresh-ddd111.inference.green-compute.com"
    model(sf, host)
    watch = tw.Watch()
    bad = {host: tw.Result(host, tw.INVALID, detail="Hostname mismatch")}
    out, sent = run(sf, bad, watch)
    assert sent == [] and out["recheck_soon"] is True
    _, sent = run(sf, bad, watch, now=NOW + timedelta(minutes=15))
    assert len(sent) == 1 and host in sent[0]


def test_many_failures_are_capped_in_one_message(sf):
    names = [f"m{i:02d}-abc123.inference.green-compute.com" for i in range(30)]
    for n in names:
        model(sf, n)
    watch = tw.Watch()
    table = {n: tw.Result(n, tw.EXPIRING, expires_at=NOW + timedelta(days=3)) for n in names}
    _, sent = run(sf, table, watch)
    assert len(sent) == 1 and sent[0].count("\n! ") == tw.MAX_LINES and "…and 10 more" in sent[0]
    assert len(sent[0]) < 4000


def test_all_ok_does_not_ask_for_a_recheck(sf):
    out, _ = run(sf, {}, tw.Watch())
    assert out["recheck_soon"] is False


def test_loop_disabled_returns_at_once(monkeypatch):
    import asyncio

    monkeypatch.setenv("GREENCOMPUTE_TLS_WATCH_ENABLED", "off")
    asyncio.run(asyncio.wait_for(tw.tls_watch_loop(), timeout=2))


def test_interval_setting(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_TLS_WATCH_INTERVAL_SECONDS", "60")
    assert tw._interval() == 300
    monkeypatch.setenv("GREENCOMPUTE_TLS_WATCH_INTERVAL_SECONDS", "junk")
    assert tw._interval() == 21600

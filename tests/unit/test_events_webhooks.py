"""Account events and webhooks (request #14): every change a client acts on
is recorded with the change itself, readable as a feed and pushed, signed, to
the customer's HTTPS endpoint, in order and at least once, without letting a
webhook URL reach our own network."""
import hashlib
import hmac
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application import webhooks as wh
from greencompute_gateway.domain.scopes import required_scope
from greencompute_gateway.infrastructure import webhook_http
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.orm import AccountEventORM, WebhookORM
from greencompute_protocol import (
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    WorkloadKind,
    WorkloadSpec,
)

LATER = datetime.now(UTC) + timedelta(minutes=1)


@pytest.fixture
def cp(tmp_path):
    repo = ControlPlaneRepository(database_url=f"sqlite+pysqlite:///{tmp_path / 'ev.db'}", bootstrap=True)
    return ControlPlaneService(repository=repo)


def sf(cp):
    return cp.repository.session_factory


def _dep(cp, owner="u1", state=DeploymentState.STARTING, name="rig-01"):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="job", image="img", kind=WorkloadKind.POD, owner_user_id=owner))
    return cp.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id=owner, hotkey="hk", node_id="n1", state=state, name=name))


def _report(cp, dep, state, **kw):
    cp.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=state, **kw))


def _events(cp, user="u1", **kw):
    return wh.list_events(sf(cp), user, **kw)


# --- events are recorded with the change itself ---------------------------------


def test_each_state_a_client_acts_on_is_one_event_with_where_it_came_from(cp):
    dep = _dep(cp)
    _report(cp, dep, DeploymentState.READY, endpoint="ssh://root@h:1")
    _report(cp, dep, DeploymentState.READY)  # a re-report: nothing new
    _report(cp, dep, DeploymentState.SUSPENDED, error="Insufficient balance", suspended_reason="balance")
    _report(cp, dep, DeploymentState.READY)  # resumed
    _report(cp, dep, DeploymentState.TERMINATED, error="deleted")
    page = _events(cp)
    got = [(e["type"], e["data"]["previous_state"]) for e in page["events"]]
    assert got == [
        ("deployment.ready", "starting"),
        ("deployment.suspended", "ready"),
        ("deployment.ready", "suspended"),
        ("deployment.terminated", "ready"),
    ]
    suspended = page["events"][1]["data"]
    assert suspended["suspended_reason"] == "balance" and suspended["error"] == "Insufficient balance"
    assert suspended["name"] == "rig-01" and suspended["deployment_id"] == dep.deployment_id
    assert page["events"][0]["data"]["error"] is None


def test_a_failed_start_says_why(cp):
    dep = _dep(cp)
    _report(cp, dep, DeploymentState.FAILED, error="SSH setup failed: no package manager")
    [event] = _events(cp)["events"]
    assert event["type"] == "deployment.failed"
    assert event["data"]["error"] == "SSH setup failed: no package manager"


def test_steps_in_between_and_ownerless_deployments_make_no_event(cp):
    _report(cp, _dep(cp, state=DeploymentState.PULLING), DeploymentState.STARTING)
    _report(cp, _dep(cp, owner=None), DeploymentState.READY)  # a catalog model
    assert _events(cp)["events"] == []


def test_an_operator_stop_is_an_event_too(cp):
    dep = _dep(cp, state=DeploymentState.READY)
    cp.cleanup_deployment(dep.deployment_id, reason="operator cleanup")
    [event] = _events(cp)["events"]
    assert event["type"] == "deployment.terminated"


def test_the_feed_pages_filters_and_is_private(cp):
    for i in range(3):
        _report(cp, _dep(cp, name=f"rig-0{i}"), DeploymentState.READY)
    _report(cp, _dep(cp, owner="someone-else"), DeploymentState.READY)
    first = _events(cp, limit=2)
    assert len(first["events"]) == 2 and first["has_more"] is True
    second = _events(cp, after=first["next_after"], limit=2)
    assert len(second["events"]) == 1 and second["has_more"] is False
    assert {e["data"]["name"] for e in first["events"] + second["events"]} == {"rig-00", "rig-01", "rig-02"}
    # A filter that matches nothing still moves the cursor past what it read.
    none = _events(cp, types="deployment.failed")
    assert none["events"] == [] and none["next_after"] == second["next_after"]
    with pytest.raises(wh.WebhookError, match="unknown event type"):
        _events(cp, types="deployment.exploded")


def test_each_event_is_written_under_its_customers_lock_so_ids_commit_in_order():
    """A cursor (`after=`) must never pass an id that commits later: on
    Postgres the event's id is taken under a per-customer lock held to commit."""
    from types import SimpleNamespace

    from greencompute_persistence.account_events import add_event

    executed, added = [], []
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda sql, params: executed.append((str(sql), params)),
        flush=lambda: executed.append(("flush", None)),
        add=added.append,
    )
    event = AccountEventORM(user_id="u1", type="deployment.ready", data={})
    add_event(session, event)
    other = AccountEventORM(user_id="u2", type="deployment.ready", data={})
    add_event(session, other)
    lock = "SELECT pg_advisory_xact_lock(:k)"
    # Row changes are written (row locks taken) before the event lock, every time.
    assert [sql for sql, _ in executed] == ["flush", lock, "flush", lock]
    assert executed[1][1] != executed[3][1]  # one lock per customer
    assert added == [event, other]
    add_event(session, None)  # nothing to add, nothing locked
    assert len(executed) == 4


# --- webhooks: setup -----------------------------------------------------------------

PUBLIC = lambda host, port: "93.184.216.34"  # noqa: E731


def test_a_webhook_gets_a_secret_once_and_starts_from_now(cp):
    _report(cp, _dep(cp), DeploymentState.READY)  # history before the webhook
    made = wh.create_webhook(sf(cp), "u1", "https://hooks.example.com/gc", ["deployment.ready"], resolve=PUBLIC)
    assert made["secret"].startswith("whsec_") and made["webhook_id"].startswith("wh_")
    [listed] = wh.list_webhooks(sf(cp), "u1")
    assert "secret" not in listed and listed["events"] == ["deployment.ready"]
    with session_scope(sf(cp)) as s:
        assert s.get(WebhookORM, made["webhook_id"]).last_event_id >= 1  # history isn't replayed


@pytest.mark.parametrize("url, error", [
    ("http://hooks.example.com/x", "https://"),
    ("https://user:pw@hooks.example.com/x", "user name or password"),
    ("https://hooks.example.com/a b", "spaces"),
    ("https://hooks.example.com:99999/x", "invalid port"),
    ("", "required"),
])
def test_bad_urls_are_refused_with_the_reason(cp, url, error):
    with pytest.raises(wh.WebhookError, match=error):
        wh.create_webhook(sf(cp), "u1", url, resolve=PUBLIC)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "172.16.106.10", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1",
])
def test_a_url_that_resolves_inside_any_private_network_is_refused(cp, monkeypatch, address):
    monkeypatch.setattr(webhook_http.socket, "getaddrinfo",
                        lambda host, port, **kw: [(socket.AF_INET6 if ":" in address else socket.AF_INET, 1, 6, "",
                                                   (address, port))])
    with pytest.raises(wh.WebhookError, match="doesn't resolve to a public address"):
        wh.create_webhook(sf(cp), "u1", "https://sneaky.example.com/x")


def test_one_private_address_among_public_ones_is_enough_to_refuse(monkeypatch):
    monkeypatch.setattr(webhook_http.socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, 1, 6, "", ("93.184.216.34", port)), (socket.AF_INET, 1, 6, "", ("10.0.0.1", port))])
    with pytest.raises(webhook_http.WebhookURLError):
        webhook_http.resolve_public("mixed.example.com", 443)


def test_limits_types_and_other_accounts(cp):
    for i in range(wh.MAX_WEBHOOKS):
        wh.create_webhook(sf(cp), "u1", f"https://h{i}.example.com/x", resolve=PUBLIC)
    with pytest.raises(wh.WebhookError, match="at most 5"):
        wh.create_webhook(sf(cp), "u1", "https://h9.example.com/x", resolve=PUBLIC)
    with pytest.raises(wh.WebhookError, match="unknown event type"):
        wh.create_webhook(sf(cp), "u2", "https://h.example.com/x", ["deployment.boom"], resolve=PUBLIC)
    some = wh.list_webhooks(sf(cp), "u1")[0]["webhook_id"]
    with pytest.raises(wh.WebhookNotFound):
        wh.delete_webhook(sf(cp), "u2", some)
    with pytest.raises(wh.WebhookNotFound):
        wh.update_webhook(sf(cp), "u2", some, active=False)
    wh.delete_webhook(sf(cp), "u1", some)
    assert len(wh.list_webhooks(sf(cp), "u1")) == wh.MAX_WEBHOOKS - 1


# --- webhooks: delivery ------------------------------------------------------------------


class Endpoint:
    """A fake endpoint: records what it got, fails while `down`."""

    def __init__(self):
        self.got = []
        self.down = False

    def __call__(self, url, body, headers):
        if self.down:
            return None, "ConnectionRefusedError: [Errno 111] Connection refused"
        self.got.append((url, body, headers))
        return 204, None


def _hook(cp, events=None):
    return wh.create_webhook(sf(cp), "u1", "https://hooks.example.com/gc", events, resolve=PUBLIC)


def test_events_arrive_in_order_signed_with_the_secret(cp):
    made = _hook(cp)
    deps = [_dep(cp, name=f"rig-0{i}") for i in range(3)]
    for d in deps:
        _report(cp, d, DeploymentState.READY)
    endpoint = Endpoint()
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER) == 3
    names = [json.loads(body)["data"]["name"] for _, body, _ in endpoint.got]
    assert names == ["rig-00", "rig-01", "rig-02"]
    _, body, headers = endpoint.got[0]
    t, v1 = (p.split("=", 1)[1] for p in headers["Green-Compute-Signature"].split(","))
    assert v1 == hmac.new(made["secret"].encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["Green-Compute-Event"] == "deployment.ready"
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER) == 0  # nothing twice


def test_a_failing_endpoint_backs_off_then_gets_the_same_event(cp):
    made = _hook(cp)
    _report(cp, _dep(cp), DeploymentState.READY)
    endpoint = Endpoint()
    endpoint.down = True
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER) == 0
    [row] = wh.list_webhooks(sf(cp), "u1")
    assert row["failures"] == 1 and "Connection refused" in row["last_error"]
    assert wh.due_webhooks(sf(cp), LATER) == []  # waiting out the backoff
    assert wh.due_webhooks(sf(cp), LATER + timedelta(seconds=11)) == [made["webhook_id"]]
    endpoint.down = False
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER + timedelta(seconds=11)) == 1
    [row] = wh.list_webhooks(sf(cp), "u1")
    assert row["failures"] == 0 and row["last_error"] is None and row["last_success_at"]


def test_only_the_chosen_types_are_sent_and_the_rest_are_passed_over(cp):
    made = _hook(cp, ["deployment.failed"])
    _report(cp, _dep(cp), DeploymentState.READY)
    _report(cp, _dep(cp), DeploymentState.FAILED, error="boom")
    endpoint = Endpoint()
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER) == 1
    assert json.loads(endpoint.got[0][1])["type"] == "deployment.failed"
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER) == 0


def test_after_three_days_of_failures_it_is_turned_off_and_can_be_turned_on_again(cp):
    made = _hook(cp)
    _report(cp, _dep(cp), DeploymentState.READY)
    endpoint = Endpoint()
    endpoint.down = True
    wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER)
    wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER + timedelta(days=3, minutes=1))
    [row] = wh.list_webhooks(sf(cp), "u1")
    assert row["active"] is False and "turned off after failing since" in row["disabled_reason"]
    assert wh.due_webhooks(sf(cp), LATER + timedelta(days=4)) == []
    wh.update_webhook(sf(cp), "u1", made["webhook_id"], active=True)
    endpoint.down = False
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER + timedelta(days=4)) == 1


def test_old_events_are_pruned(cp):
    _report(cp, _dep(cp), DeploymentState.READY)
    assert wh.prune_events(sf(cp), now=LATER) == 0
    assert wh.prune_events(sf(cp), now=LATER + timedelta(days=31)) == 1


def test_a_test_delivery_reports_how_it_went(cp):
    made = _hook(cp)
    endpoint = Endpoint()
    out = wh.send_test(sf(cp), "u1", made["webhook_id"], post=endpoint)
    assert out == {"delivered": True, "status_code": 204, "error": None}
    assert json.loads(endpoint.got[0][1])["type"] == "webhook.test"
    endpoint.down = True
    assert wh.send_test(sf(cp), "u1", made["webhook_id"], post=endpoint)["delivered"] is False


# --- the HTTP sender against a real TLS server ------------------------------------------


@pytest.fixture
def tls_server(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    state = {"reply": b"HTTP/1.1 204 No Content\r\n\r\n", "requests": []}

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += tls.recv(4096)
                    state["requests"].append(data)
                    if state["reply"] is None:
                        time.sleep(3)  # say nothing
                    else:
                        tls.sendall(state["reply"])
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    client_ctx = ssl.create_default_context(cafile=str(cert))
    yield listener.getsockname()[1], client_ctx, state
    listener.close()


def _send(port, ctx, **kw):
    return webhook_http.post(f"https://localhost:{port}/hook?x=1", b'{"a":1}', {"Green-Compute-Signature": "t=1,v1=ab"},
                             resolve=lambda h, p: "127.0.0.1", context=ctx, **kw)


def test_sender_posts_json_with_its_headers_and_reads_the_status(tls_server):
    port, ctx, state = tls_server
    assert _send(port, ctx) == (204, None)
    request = state["requests"][0].decode()
    assert request.startswith("POST /hook?x=1 HTTP/1.1\r\n")
    assert f"Host: localhost:{port}" in request and "Green-Compute-Signature: t=1,v1=ab" in request
    assert "Content-Type: application/json" in request


def test_sender_counts_errors_and_redirects_as_failures(tls_server):
    port, ctx, state = tls_server
    state["reply"] = b"HTTP/1.1 500 Internal Server Error\r\n\r\n"
    assert _send(port, ctx) == (500, "HTTP 500")
    state["reply"] = b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:28001/\r\n\r\n"
    assert _send(port, ctx) == (302, "HTTP 302")  # never followed


def test_sender_gives_up_at_its_deadline(tls_server):
    port, ctx, state = tls_server
    state["reply"] = None
    started = time.monotonic()
    status, error = _send(port, ctx, timeout=1.0)
    assert (status, error) == (None, webhook_http.UNREACHABLE) and time.monotonic() - started < 2.5


def test_sender_checks_the_certificate(tls_server):
    port, _, _ = tls_server
    status, error = _send(port, ssl.create_default_context())  # doesn't trust the test cert
    # Refused, reset, bad certificate, timeout: all the same answer, so the
    # test endpoint can't be used to map which ports are open where.
    assert (status, error) == (None, webhook_http.UNREACHABLE)


def test_sender_refuses_private_addresses_at_send_time_too():
    status, error = webhook_http.post("https://localhost/x", b"{}", {})
    assert status is None and "doesn't resolve to a public address" in error


# --- API surface ---------------------------------------------------------------------------


def test_routes_are_in_the_spec_and_scoped():
    app = FastAPI()
    app.include_router(routes.router)
    paths = app.openapi()["paths"]
    assert {"/platform/events", "/platform/webhooks", "/platform/webhooks/{webhook_id}",
            "/platform/webhooks/{webhook_id}/test"} <= set(paths)
    assert required_scope("GET", "/platform/events") == "read"
    assert required_scope("GET", "/platform/webhooks") == "read"
    # Pointing your events somewhere needs a full-access key.
    assert required_scope("POST", "/platform/webhooks") is None
    assert required_scope("DELETE", "/platform/webhooks/wh_1") is None


def test_our_own_servers_are_refused_even_though_their_addresses_are_public(monkeypatch):
    monkeypatch.setattr(webhook_http.socket, "getaddrinfo",
                        lambda host, port, **kw: [(socket.AF_INET, 1, 6, "", ("217.138.104.88", port))])
    with pytest.raises(webhook_http.WebhookURLError, match="doesn't resolve to a public address"):
        webhook_http.resolve_public("our-server.example.com", 28001)


def test_the_next_address_is_tried_only_if_nothing_was_sent(tls_server):
    port, ctx, state = tls_server
    # The first address refuses the connection; the second is the server.
    assert webhook_http.post(f"https://localhost:{port}/hook", b"{}", {}, context=ctx,
                             resolve=lambda h, p: ["127.0.0.2", "127.0.0.1"]) == (204, None)
    assert len(state["requests"]) == 1  # sent once, to the second address
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()
    status, error = webhook_http.post(f"https://localhost:{dead_port}/hook", b"{}", {}, context=ctx,
                                      resolve=lambda h, p: ["127.0.0.1", "127.0.0.1"])
    assert (status, error) == (None, webhook_http.UNREACHABLE)


def test_a_new_url_starts_the_retry_count_over(cp):
    made = _hook(cp)
    _report(cp, _dep(cp), DeploymentState.READY)
    endpoint = Endpoint()
    endpoint.down = True
    wh.deliver_webhook(sf(cp), made["webhook_id"], post=endpoint, clock=lambda: LATER)
    assert wh.due_webhooks(sf(cp), LATER) == []
    wh.update_webhook(sf(cp), "u1", made["webhook_id"], url="https://fixed.example.com/gc", resolve=PUBLIC)
    [row] = wh.list_webhooks(sf(cp), "u1")
    assert row["failures"] == 0 and row["url"] == "https://fixed.example.com/gc"
    assert wh.due_webhooks(sf(cp), LATER) == [made["webhook_id"]]


def test_a_turn_ends_when_its_time_is_up_and_the_rest_waits_for_the_next(cp):
    made = _hook(cp)
    for i in range(3):
        _report(cp, _dep(cp, name=f"rig-0{i}"), DeploymentState.READY)

    def slow(url, body, headers):
        time.sleep(0.3)
        return 204, None
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=slow, turn_seconds=0.5) == 2
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=slow, turn_seconds=5) == 1


def test_a_slow_endpoint_never_holds_up_another_customers_webhook(cp):
    slow_hook = wh.create_webhook(sf(cp), "slow", "https://slow.example.com/x", resolve=PUBLIC)
    fast_hook = wh.create_webhook(sf(cp), "fast", "https://fast.example.com/x", resolve=PUBLIC)
    _report(cp, _dep(cp, owner="slow"), DeploymentState.READY)
    _report(cp, _dep(cp, owner="fast"), DeploymentState.READY)
    got, release = [], threading.Event()

    def post(url, body, headers):
        if "slow" in url:
            release.wait(10)
        got.append(url)
        return 204, None
    deliverer = wh.Deliverer(sf(cp), post=post, workers=4)
    try:
        assert deliverer.submit_due() == 2  # returns at once
        for _ in range(50):
            if "https://fast.example.com/x" in got:
                break
            time.sleep(0.05)
        assert got == ["https://fast.example.com/x"]  # the fast one went while the slow one hangs
        assert deliverer.submit_due() == 0  # the slow one isn't started twice; the fast one has nothing new
        release.set()
        for _ in range(50):
            if deliverer.idle():
                break
            time.sleep(0.05)
        assert len(got) == 2 and slow_hook["webhook_id"] and fast_hook["webhook_id"]
    finally:
        release.set()
        deliverer.pool.shutdown(wait=True)


def test_a_delete_during_a_turn_stops_it_before_the_next_event(cp):
    made = _hook(cp)
    for i in range(3):
        _report(cp, _dep(cp, name=f"rig-0{i}"), DeploymentState.READY)
    sent = []

    def post(url, body, headers):
        sent.append(body)
        if len(sent) == 1:
            wh.delete_webhook(sf(cp), "u1", made["webhook_id"])
        return 204, None
    assert wh.deliver_webhook(sf(cp), made["webhook_id"], post=post) == 1
    assert len(sent) == 1


def test_only_webhooks_with_something_new_are_picked_up(cp):
    made = _hook(cp)
    assert wh.due_webhooks(sf(cp), LATER) == []  # nothing to send: no empty turns
    _report(cp, _dep(cp), DeploymentState.READY)
    assert wh.due_webhooks(sf(cp), LATER) == [made["webhook_id"]]
    wh.deliver_webhook(sf(cp), made["webhook_id"], post=Endpoint())
    assert wh.due_webhooks(sf(cp), LATER) == []


def test_extra_denied_ranges_add_to_ours(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_WEBHOOK_DENY_CIDRS", "203.0.113.0/24")
    nets = [str(n) for n in webhook_http._denied_networks()]
    assert "217.138.104.0/24" in nets and "203.0.113.0/24" in nets


def test_the_leader_is_always_this_process_without_postgres(cp):
    leader = wh._Leader(cp.repository.engine, 1)
    assert leader.acquire() and leader.still_held()
    leader.release()

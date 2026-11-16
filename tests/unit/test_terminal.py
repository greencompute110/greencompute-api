"""The website's terminal into a rental (request #34): a one-time ticket from
POST /platform/deployments/{id}/terminal, then a WebSocket relayed to the node
agent running the pod.

Through the real ASGI app (middleware, scopes, error handlers) against one
SQLite database; only the node's end is a stand-in."""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from greencompute_gateway.application import sanctions, terminal
from greencompute_gateway.domain.scopes import required_scope
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import errors, routes, security
from greencompute_persistence import CredentialStore, FixedWindowRateLimiter, session_scope
from greencompute_persistence.orm import APIKeyORM, KeyValueStateORM
from greencompute_protocol import DeploymentState
from sqlalchemy import select

SITE = "https://www.green-compute.com"
NODE_SECRET = "node-secret-for-tests"


class FakeNode:
    """The node agent's terminal: says ready, echoes keystrokes as output,
    ends with an exit message on b"exit\\n". `script` changes that."""

    def __init__(self, script: dict) -> None:
        self.script, self.sent, self.closed = script, [], False
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queue.put_nowait(json.dumps(script.get("first", {"type": "ready"})))

    def inject(self, frame) -> None:
        """From the test's thread: the next thing the node sends (or raises)."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, frame)

    async def send(self, data):
        self.sent.append(data)
        if data == b"exit\n":
            self.queue.put_nowait(json.dumps({"type": "exit", "code": 0}))
        elif isinstance(data, bytes):
            self.queue.put_nowait(b"out:" + data)

    async def recv(self):
        frame = await self.queue.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def close(self):
        self.closed = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'gc.sqlite'}"
    repo = GatewayRepository(database_url=url, bootstrap=True)
    monkeypatch.setattr(routes.service, "repository", repo)
    monkeypatch.setattr(security, "credential_store", CredentialStore(session_factory=repo.session_factory))
    monkeypatch.setattr(security, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setenv("GREENCOMPUTE_TERMINAL_ALLOWED_ORIGINS", f"{SITE},https://green-compute.com")
    monkeypatch.setenv("GREENCOMPUTE_INFERENCE_AUTH_SECRET", NODE_SECRET)
    monkeypatch.delenv("GREENCOMPUTE_TERMINAL_ENABLED", raising=False)
    monkeypatch.delenv("GREENCOMPUTE_PUBLIC_API_URL", raising=False)
    with session_scope(repo.session_factory) as s:
        for key_id, user, scopes, admin in [("k1", "u1", [], False), ("k1b", "u1", [], False),
                                            ("k1read", "u1", ["read"], False), ("k1rent", "u1", ["rentals"], False),
                                            ("k2", "u2", [], False), ("kadmin", "admin-1", [], True)]:
            s.add(APIKeyORM(key_id=key_id, user_id=user, name=key_id, admin=admin, scopes=scopes,
                            secret=f"secret-{key_id}", created_at=datetime.now(UTC)))
    deployments = {
        "d1": SimpleNamespace(deployment_id="d1", hotkey="hk", node_id="n1", state=DeploymentState.READY,
                              owner_user_id="u1", endpoint="ssh://root@203.0.113.7:30001"),
        "d2": SimpleNamespace(deployment_id="d2", hotkey="hk", node_id="n1", state=DeploymentState.READY,
                              owner_user_id="u1", endpoint="ssh://root@203.0.113.7:30002"),
        "model": SimpleNamespace(deployment_id="model", hotkey="hk", node_id="n1", state=DeploymentState.READY,
                                 owner_user_id="u1", endpoint="http://10.0.0.5:30500"),
    }

    def get_deployment(deployment_id, user_id=None, *, admin=False):
        d = deployments.get(deployment_id)
        return d if d is not None and (admin or d.owner_user_id == user_id) else None
    monkeypatch.setattr(routes.service, "get_deployment", get_deployment)
    monkeypatch.setattr(routes, "_node_base_url", lambda deployment: "http://10.0.0.5:8007")
    blocked = set()
    monkeypatch.setattr(sanctions, "account_blocked", lambda sf, user_id: "OFAC" if user_id in blocked else None)
    script: dict = {}
    nodes, opened = [], []

    async def open_node_link(url, secret):
        opened.append((url, secret))
        if script.get("unavailable"):
            raise terminal.NodeUnavailableError("couldn't reach the machine (OSError)")
        node = FakeNode(script)
        nodes.append(node)
        return node
    monkeypatch.setattr(terminal, "open_node_link", open_node_link)
    yield SimpleNamespace(sf=repo.session_factory, deployments=deployments, blocked=blocked, script=script,
                          nodes=nodes, opened=opened, client=TestClient(app))
    assert terminal._open == {} and terminal._total == 0, "a terminal's slot was never given back"


def ticket(env, deployment_id="d1", key="k1", body=None, headers=None):
    res = env.client.post(f"/platform/deployments/{deployment_id}/terminal", json=body,
                          headers={"Authorization": f"Bearer secret-{key}", **(headers or {})})
    return res


def ws_path(deployment_id, t, extra=""):
    return f"/platform/deployments/{deployment_id}/terminal?ticket={t}{extra}"


def until_closed(ws):
    """Every frame until the server closes: (messages, close code)."""
    frames = []
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return frames, message.get("code")
        frames.append(json.loads(message["text"]) if message.get("text") is not None else message["bytes"])


def refused(env, path, headers=None):
    """The first message and close code of a terminal that won't open."""
    with env.client.websocket_connect(path, headers={"origin": SITE, **(headers or {})}) as ws:
        return until_closed(ws)


# --- the ticket ---------------------------------------------------------------------


def test_the_owner_gets_a_one_time_ticket_for_a_websocket(env):
    res = ticket(env)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["expires_in_seconds"] == 60 and body["idle_timeout_seconds"] == 900
    assert body["max_session_seconds"] == 4 * 3600
    assert body["websocket_url"] == f"ws://testserver/platform/deployments/d1/terminal?ticket={body['ticket']}"
    assert len(body["ticket"]) >= 40
    # Only its hash is stored: someone reading the database can't use it.
    with session_scope(env.sf) as s:
        stored = [(k, v) for k, v in s.execute(select(KeyValueStateORM.key, KeyValueStateORM.value))
                  if k.startswith("term:")]
    assert len(stored) == 1 and body["ticket"] not in stored[0][0] + stored[0][1]


def test_behind_nginx_the_url_is_wss_on_the_public_host(env, monkeypatch):
    from greencompute_gateway import main

    monkeypatch.setattr(main, "_TRUST_PROXY", True)
    body = ticket(env, headers={"X-Forwarded-Proto": "https", "Host": "api.green-compute.com"}).json()
    assert body["websocket_url"].startswith("wss://api.green-compute.com/platform/deployments/d1/terminal?ticket=")


@pytest.mark.parametrize("deployment_id, key, status, code", [
    ("d1", "k2", 404, "not_found"),  # someone else's rental
    ("d1", "kadmin", 404, "not_found"),  # an admin key gets no more than over SSH
    ("nope", "k1", 404, "not_found"),
    ("model", "k1", 409, "conflict"),  # a model, not a pod
    ("d1", "k1read", 403, "scope_required"),  # root in a pod is not a read
])
def test_only_the_owner_of_a_running_pod_gets_a_ticket(env, deployment_id, key, status, code):
    res = ticket(env, deployment_id, key)
    assert (res.status_code, res.json()["error"]["code"]) == (status, code)
    assert ticket(env, "d1", "k1rent").status_code == 200  # the rentals scope is enough


def test_a_stopped_rental_gets_no_ticket(env):
    env.deployments["d1"].state = DeploymentState.SUSPENDED
    res = ticket(env)
    assert res.status_code == 409 and "suspended" in res.json()["error"]["message"]


def test_a_blocked_account_gets_no_ticket(env):
    env.blocked.add("u1")
    res = ticket(env)
    assert (res.status_code, res.json()["error"]["code"]) == (403, "wallet_blocked")


@pytest.mark.parametrize("shell", ["bash; touch /tmp/pwned", "zsh", "$(id)", ""])
def test_only_known_shells(env, shell):
    res = ticket(env, body={"shell": shell})
    assert (res.status_code, res.json()["error"]["code"]) == (422, "validation_error")


def test_a_chosen_shell_reaches_the_node_as_one_of_three_words(env):
    t = ticket(env, body={"shell": "sh"}).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t, "&cols=abc&rows=;rm"), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
    assert env.opened == [("ws://10.0.0.5:8007/pods/d1/terminal?cols=80&rows=24&shell=sh", NODE_SECRET)]


def test_the_id_in_the_node_url_is_one_path_segment():
    url = terminal.node_terminal_url("https://n:8007/", "a/../agent?x=1#y", cols=80, rows=24, shell="auto")
    assert url == "wss://n:8007/pods/a%2F..%2Fagent%3Fx%3D1%23y/terminal?cols=80&rows=24&shell=auto"


def test_turned_off_it_says_so(env, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_TERMINAL_ENABLED", "false")
    res = ticket(env)
    assert (res.status_code, res.json()["error"]["code"]) == (501, "not_implemented")


# --- the WebSocket ------------------------------------------------------------------


def test_a_terminal_session_end_to_end(env, caplog):
    t = ticket(env).json()["ticket"]
    with caplog.at_level(logging.INFO), \
            env.client.websocket_connect(ws_path("d1", t, "&cols=120&rows=40"), headers={"origin": SITE}) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["idle_timeout_seconds"] == 900
        assert ready["max_session_seconds"] == 14400 and ready["session_ends_at"]
        assert env.opened == [("ws://10.0.0.5:8007/pods/d1/terminal?cols=120&rows=40&shell=auto", NODE_SECRET)]
        ws.send_bytes(b"export TOKEN=s3cret-value\n")
        assert ws.receive_bytes() == b"out:export TOKEN=s3cret-value\n"
        ws.send_text(json.dumps({"type": "input", "data": "ls\n"}))
        assert ws.receive_bytes() == b"out:ls\n"
        ws.send_text(json.dumps({"type": "resize", "cols": 10**6, "rows": 0}))
        ws.send_text(json.dumps({"type": "ping"}))
        assert ws.receive_json() == {"type": "pong"}
        ws.send_text("not json")
        ws.send_text(json.dumps({"type": "something-new"}))
        ws.send_bytes(b"exit\n")
        frames, code = until_closed(ws)
    assert frames == [{"type": "exit", "code": 0}] and code == 1000
    node = env.nodes[0]
    assert {"type": "resize", "cols": 500, "rows": 2} in [json.loads(x) for x in node.sent if isinstance(x, str)]
    assert not any("ping" in x for x in node.sent if isinstance(x, str))  # answered here
    assert node.closed
    logs = [r.getMessage() for r in caplog.records if "deployment_terminal" in r.getMessage()]
    start = next(m for m in logs if "deployment_terminal_start" in m)
    end = next(m for m in logs if "deployment_terminal_end" in m)
    assert "user_id=u1" in start and "key_id=k1" in start and "deployment_id=d1" in start
    assert "reason=exit" in end and "bytes_in=34" in end and "bytes_out=37" in end
    assert not any("s3cret" in m for m in logs)  # who, where, when and how much: never what


def test_a_ticket_works_once(env):
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
    frames, code = refused(env, ws_path("d1", t))
    assert frames[0]["code"] == "terminal_ticket_invalid" and code == 4401
    assert "request a new one" in frames[0]["message"]


def test_a_ticket_is_for_one_rental_and_is_spent_by_trying_another(env):
    t = ticket(env, "d1").json()["ticket"]
    frames, code = refused(env, ws_path("d2", t))  # the same customer's other pod
    assert (frames[0]["code"], code) == ("terminal_ticket_invalid", 4401)
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("terminal_ticket_invalid", 4401)
    assert env.opened == []


def test_a_ticket_is_for_one_user_and_one_key(env):
    t = ticket(env, "d1", "k1").json()["ticket"]
    frames, code = refused(env, ws_path("d1", t), headers={"authorization": "Bearer secret-k2"})
    assert (frames[0]["code"], code) == ("terminal_ticket_invalid", 4401)  # another customer's key
    t = ticket(env, "d1", "k1").json()["ticket"]
    frames, code = refused(env, ws_path("d1", t), headers={"x-api-key": "secret-k1b"})
    assert (frames[0]["code"], code) == ("terminal_ticket_invalid", 4401)  # the same customer, another key
    t = ticket(env, "d1", "k1").json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"authorization": "Bearer secret-k1"}) as ws:
        assert ws.receive_json()["type"] == "ready"  # an agent sending its own key, and no Origin
    assert len(env.opened) == 1


def test_a_rental_that_changed_hands_is_refused(env):
    t = ticket(env).json()["ticket"]
    env.deployments["d1"].owner_user_id = "u2"
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("not_found", 4404)


def test_an_expired_ticket_is_refused(env):
    t, expires = terminal.issue_ticket(env.sf, user_id="u1", key_id="k1", deployment_id="d1", shell="auto",
                                       now=datetime.now(UTC) - timedelta(seconds=61))
    assert expires < datetime.now(UTC)
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("terminal_ticket_invalid", 4401)
    for bad in ("", "x" * 300, "not-a-ticket"):
        frames, code = refused(env, ws_path("d1", bad))
        assert code == 4401


def test_unused_tickets_are_cleared_out(env):
    old = datetime.now(UTC) - timedelta(minutes=5)
    for _ in range(3):
        terminal.issue_ticket(env.sf, user_id="u1", key_id="k1", deployment_id="d1", shell="auto", now=old)
    terminal.issue_ticket(env.sf, user_id="u1", key_id="k1", deployment_id="d1", shell="auto")
    with session_scope(env.sf) as s:
        keys = [k for (k,) in s.execute(select(KeyValueStateORM.key)) if k.startswith("term:")]
    assert len(keys) == 1


def test_the_key_must_still_exist_and_the_rental_still_run(env):
    t = ticket(env).json()["ticket"]
    with session_scope(env.sf) as s:
        s.delete(s.get(APIKeyORM, "k1"))
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("forbidden", 4403)
    t = ticket(env, key="k1b").json()["ticket"]
    env.deployments["d1"].state = DeploymentState.SUSPENDED
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("conflict", 4409)
    env.deployments["d1"].state = DeploymentState.READY
    t = ticket(env, key="k1b").json()["ticket"]
    env.deployments["d1"].endpoint = "http://10.0.0.5:30500"  # no longer a pod
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("conflict", 4409)
    assert env.opened == []


def test_a_blocked_account_is_refused_at_connect(env):
    t = ticket(env).json()["ticket"]
    env.blocked.add("u1")
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("wallet_blocked", 4403)


def handshake(path: str, origin: str) -> list[dict]:
    """What the app sends for a WebSocket handshake, straight through the
    ASGI app (as uvicorn would see it)."""
    path, _, query = path.partition("?")
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "scheme": "ws", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "headers": [(b"host", b"api.test"), (b"origin", origin.encode())],
             "client": ("203.0.113.9", 1234), "server": ("api.test", 80), "subprotocols": [], "state": {}}
    inbox, sent = [{"type": "websocket.connect"}], []

    async def receive():
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(3600)  # a refused handshake never reads further

    async def send(message):
        sent.append(message)
    asyncio.run(asyncio.wait_for(app(scope, receive, send), 10))
    return sent


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "https://www.green-compute.com.evil.example",
                                    "http://www.green-compute.com"])
def test_the_websocket_refuses_other_origins(env, origin):
    t = ticket(env).json()["ticket"]
    sent = handshake(ws_path("d1", t), origin)
    # Closed before being accepted: uvicorn answers the handshake with 403.
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert env.opened == []
    # The page on the other site didn't even spend the ticket.
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"


def test_at_most_three_terminals_per_account(env):
    tickets = [ticket(env).json()["ticket"] for _ in range(4)]
    for _ in range(terminal.PER_USER):
        assert terminal.take_slot("u1") is None
    try:
        res = ticket(env)
        assert (res.status_code, res.json()["error"]["code"]) == (429, "terminal_limit")
        assert res.headers["retry-after"] == "5" and res.json()["error"]["retryable"] is True
        frames, code = refused(env, ws_path("d1", tickets[0]))
        assert (frames[0]["code"], code) == ("terminal_limit", 4429)
        assert terminal.take_slot("u2") is None  # someone else isn't held up
        terminal.release_slot("u2")
    finally:
        for _ in range(terminal.PER_USER):
            terminal.release_slot("u1")
    with env.client.websocket_connect(ws_path("d1", tickets[1]), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"


def test_an_idle_terminal_warns_then_closes(env, monkeypatch):
    monkeypatch.setattr(terminal, "IDLE_SECONDS", 1.0)
    monkeypatch.setattr(terminal, "IDLE_WARNING_SECONDS", 0.6)
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
        frames, code = until_closed(ws)
    assert frames[0]["type"] == "notice" and frames[0]["code"] == "idle_warning"
    assert frames[-1]["code"] == "terminal_idle" and code == 4408
    assert env.nodes[0].closed


def test_pings_alone_dont_keep_a_terminal_open(env, monkeypatch):
    monkeypatch.setattr(terminal, "IDLE_SECONDS", 1.0)
    monkeypatch.setattr(terminal, "IDLE_WARNING_SECONDS", 0.1)
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_text(json.dumps({"type": "ping"}))
        assert ws.receive_json() == {"type": "pong"}
        frames, code = until_closed(ws)
    assert code == 4408


def test_a_terminal_ends_at_the_time_limit(env, monkeypatch):
    monkeypatch.setattr(terminal, "MAX_SESSION_SECONDS", 1.0)
    monkeypatch.setattr(terminal, "SESSION_WARNING_SECONDS", 0.6)
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
        frames, code = until_closed(ws)
    assert frames[0]["code"] == "session_ending" and frames[-1]["code"] == "terminal_session_limit"
    assert code == 4410


def test_a_deleted_key_ends_an_open_terminal(env, monkeypatch):
    monkeypatch.setattr(terminal, "RECHECK_SECONDS", 0.2)
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
        with session_scope(env.sf) as s:
            s.delete(s.get(APIKeyORM, "k1"))
        frames, code = until_closed(ws)
    assert (frames[-1]["code"], code) == ("forbidden", 4403)


def test_an_oversized_message_closes_it(env, monkeypatch):
    monkeypatch.setattr(terminal, "MAX_MESSAGE_BYTES", 1024)
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"x" * 1025)
        frames, code = until_closed(ws)
    assert code == 1009 and env.nodes[0].sent == []


def test_an_unreachable_machine_is_unavailable(env, monkeypatch):
    env.script["unavailable"] = True
    t = ticket(env).json()["ticket"]
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("unavailable", 4503)
    monkeypatch.setattr(routes, "_node_base_url", lambda deployment: None)
    t = ticket(env).json()["ticket"]
    frames, code = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], code) == ("unavailable", 4503)


@pytest.mark.parametrize("node_code, close_code, code", [
    ("terminal_limit", 4429, "terminal_limit"),
    ("conflict", 4409, "conflict"),
    ("not_found", 4409, "conflict"),
    ("something odd", 4503, "unavailable"),
])
def test_the_machine_refusing_is_passed_on(env, node_code, close_code, code):
    env.script["first"] = {"type": "error", "code": node_code, "message": "internal detail"}
    t = ticket(env).json()["ticket"]
    frames, got = refused(env, ws_path("d1", t))
    assert (frames[0]["code"], got) == (code, close_code)
    assert "internal detail" not in frames[0]["message"]


def test_losing_the_machine_mid_session_says_so(env):
    t = ticket(env).json()["ticket"]
    with env.client.websocket_connect(ws_path("d1", t), headers={"origin": SITE}) as ws:
        assert ws.receive_json()["type"] == "ready"
        env.nodes[0].inject(terminal.NodeClosedError(1006))
        frames, code = until_closed(ws)
    assert (frames[-1]["code"], code) == ("unavailable", 4503)
    assert "lost" in frames[-1]["message"]


# --- documented and scoped ----------------------------------------------------------


def test_it_needs_the_rentals_scope_and_is_in_the_spec():
    assert required_scope("POST", "/platform/deployments/d1/terminal") == "rentals"
    from fastapi import FastAPI

    spec_app = FastAPI()
    spec_app.include_router(routes.router)
    spec = spec_app.openapi()
    post = spec["paths"]["/platform/deployments/{deployment_id}/terminal"]["post"]
    assert post["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TerminalTicket"}
    assert {"terminal_ticket_invalid", "terminal_limit", "terminal_idle", "terminal_session_limit"} <= set(errors.CODES)
    assert "terminal_limit" in errors.RETRYABLE


def test_the_relay_never_blocks_the_event_loop():
    """The WebSocket handler is a coroutine and hands every database and
    network lookup to the thread pool."""
    import ast
    import inspect

    assert inspect.iscoroutinefunction(routes.deployment_terminal)
    tree = ast.parse(inspect.getsource(routes.deployment_terminal))
    direct = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in {"get_deployment", "consume_ticket", "account_blocked", "key_still_allows"}]
    assert direct == []
    offloaded = [n.args[0].id for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "run_in_threadpool" and isinstance(n.args[0], ast.Name)]
    assert {"_terminal_admission", "_node_base_url", "_terminal_still_allowed"} <= set(offloaded)


# --- the gateway's WebSocket client against a real server ---------------------------


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # uvicorn's own websockets server, standing in for the node
def test_the_node_link_speaks_to_a_real_websocket_server():
    """open_node_link with the websockets library against a uvicorn server
    standing in for the node: the secret is sent, a refused handshake is
    unavailable, and the node's close code comes through."""
    pytest.importorskip("websockets")
    import socket
    import threading
    import time

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    async def pod(websocket):
        if websocket.headers.get("x-agent-auth") != NODE_SECRET:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_text('{"type": "ready"}')
        data = await websocket.receive_bytes()
        await websocket.send_bytes(b"echo:" + data)
        await websocket.close(code=4429)

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    port, nobody = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[WebSocketRoute("/pods/{id}/terminal", pod)]),
                                           host="127.0.0.1", port=port, log_level="warning", ws="websockets"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    async def go():
        url = terminal.node_terminal_url(f"http://127.0.0.1:{port}", "d1", cols=80, rows=24, shell="auto")
        node = await terminal.open_node_link(url, NODE_SECRET)
        assert await terminal.wait_ready(node) is None
        await node.send(b"ls\n")
        assert await node.recv() == b"echo:ls\n"
        with pytest.raises(terminal.NodeClosedError) as closed:
            await node.recv()
        assert closed.value.code == 4429
        await node.close()
        with pytest.raises(terminal.NodeUnavailableError):
            await terminal.open_node_link(url, "wrong-secret")
        with pytest.raises(terminal.NodeUnavailableError):
            await terminal.open_node_link(f"ws://127.0.0.1:{nobody}/pods/d1/terminal", None)
    try:
        asyncio.run(asyncio.wait_for(go(), 30))
    finally:
        server.should_exit = True
        thread.join(timeout=10)

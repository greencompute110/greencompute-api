"""The production app (main.app: its real middleware stack) under a real
uvicorn, in front of a stand-in node: whatever way a /v1 request ends, its
per-account in-flight slot is given back at once, what was served is billed
and the node's connection is closed (review round 2, H1). Under the old
BaseHTTPMiddleware a caller hanging up mid-stream left all of that waiting
for the garbage collector: eight hang-ups locked an account out for hours."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

uvicorn = pytest.importorskip("uvicorn")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from greencompute_gateway.transport import routes, v1_capacity  # noqa: E402
from test_inference_hardening import catalog_model, charges, cp, gw  # noqa: E402,F401


class SlowNode(BaseHTTPRequestHandler):
    """Streams a token every 50 ms (400 of them, or 3 in "short" mode)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json(200, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.server.mode == "reject":
            self._json(400, {"object": "error", "message": "bad", "type": "BadRequestError", "code": 400})
            return
        if not body.get("stream"):
            self._json(200, {"model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        n = 3 if self.server.mode == "short" else 400
        try:
            for i in range(n):
                chunk = {"choices": [{"index": 0, "delta": {"content": f"t{i}"}}], "id": "c", "created": 1, "model": "m"}
                payload = f"data: {json.dumps(chunk)}\n\n".encode()
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            usage = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": n}, "id": "c", "created": 1,
                     "model": "m"}
            for payload in (f"data: {json.dumps(usage)}\n\n".encode(), b"data: [DONE]\n\n"):
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.server.aborted += 1


@pytest.fixture
def stack(cp, gw, monkeypatch):  # noqa: F811
    from greencompute_gateway import main

    v1_capacity._slots.clear()
    node = ThreadingHTTPServer(("127.0.0.1", 0), SlowNode)
    node.daemon_threads = True
    node.mode, node.aborted = "long", 0
    threading.Thread(target=node.serve_forever, daemon=True).start()
    monkeypatch.setattr(routes, "service", gw)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(
        user_id="u1", key_id="k1", admin=False))
    for name in ("enforce_rate_limit", "_enforce_key_limits", "_enforce_account_spend_limit",
                 "_refuse_blocked_account"):
        monkeypatch.setattr(routes, name, lambda *a, **k: None)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    # The production app and middleware stack; no lifespan (its background
    # loops reach the network).
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    catalog_model(cp, SimpleNamespace(server_address=node.server_address))
    yield node, port
    server.should_exit = True
    node.shutdown()


MSG = [{"role": "user", "content": "hi"}]


def post(port, body):
    s = socket.create_connection(("127.0.0.1", port))
    data = json.dumps(body).encode()
    s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\n"
              f"content-length: {len(data)}\r\n\r\n".encode() + data)
    return s


def soon(predicate, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def read_until(s, count):
    got = b""
    while got.count(b"data:") < count:
        got += s.recv(4096)
    return got


def test_a_hang_up_mid_stream_frees_bills_and_aborts_at_once(stack, cp):  # noqa: F811
    node, port = stack
    s = post(port, {"model": "qwen2.5-7b-instruct", "messages": MSG, "stream": True})
    read_until(s, 3)
    assert v1_capacity.in_flight("u1") == 1
    s.close()
    started = time.monotonic()
    assert soon(lambda: v1_capacity.in_flight("u1") == 0), "the slot wasn't given back"
    assert soon(lambda: len(charges(cp)) == 1), "what was served wasn't billed"
    assert soon(lambda: node.aborted == 1), "the node's connection wasn't closed"
    assert time.monotonic() - started < 2.0


def test_eight_hang_ups_leave_the_account_free(stack, cp):  # noqa: F811
    node, port = stack
    held = [post(port, {"model": "qwen2.5-7b-instruct", "messages": MSG, "stream": True}) for _ in range(8)]
    for h in held:
        read_until(h, 1)
    ninth = post(port, {"model": "qwen2.5-7b-instruct", "messages": MSG})
    ninth.settimeout(5)
    answer = b""
    while b"shared models" not in answer:
        chunk = ninth.recv(8192)
        if not chunk:
            break
        answer += chunk
    for h in held:
        h.close()
    assert answer.startswith(b"HTTP/1.1 429") and b"shared models" in answer
    assert soon(lambda: v1_capacity.in_flight("u1") == 0, 3.0)
    again = post(port, {"model": "qwen2.5-7b-instruct", "messages": MSG})
    again.settimeout(5)
    assert again.recv(8192).startswith(b"HTTP/1.1 200")


def test_billing_raising_at_the_end_still_frees_the_slot(stack, cp, monkeypatch):  # noqa: F811
    node, port = stack
    node.mode = "short"

    def ledger_down(*a, **k):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(routes.service, "_charge_inference_tokens", ledger_down)
    s = post(port, {"model": "qwen2.5-7b-instruct", "messages": MSG, "stream": True})
    s.settimeout(5)
    try:
        while s.recv(4096):
            pass
    except OSError:
        pass
    s.close()
    assert soon(lambda: v1_capacity.in_flight("u1") == 0)


@pytest.mark.parametrize("stream", [True, False])
def test_a_refused_request_frees_the_slot(stack, stream):
    node, port = stack
    node.mode = "reject"
    s = post(port, {"model": "qwen2.5-7b-instruct", "messages": MSG, "stream": stream})
    s.settimeout(5)
    assert s.recv(4096).startswith(b"HTTP/1.1 400")
    s.close()
    assert soon(lambda: v1_capacity.in_flight("u1") == 0)

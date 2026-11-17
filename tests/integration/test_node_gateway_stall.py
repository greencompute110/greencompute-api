"""The review's probe (H1), made a test: the gateway (real service and
HttpInferenceClient) in front of the REAL node agent app under uvicorn, in
front of a stand-in vLLM whose /health stalls for 1.7 s (its event loop busy
with a big prompt) while three requests arrive. That used to take the shared
model to FAILED; now the node answers "busy" and nothing fails.

Needs the node agent's source: GREENCOMPUTE_NODE_AGENT_SRC, or a
greencompute-node checkout next to this repo (skipped otherwise)."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

_NODE_SRC = os.getenv("GREENCOMPUTE_NODE_AGENT_SRC") or str(
    Path(__file__).resolve().parents[3] / "greencompute-node/services/node-agent/src")
if Path(_NODE_SRC).is_dir():
    sys.path.insert(0, _NODE_SRC)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

uvicorn = pytest.importorskip("uvicorn")
node_routes = pytest.importorskip("greencompute_node_agent.transport.routes")
if not hasattr(node_routes, "_model_server_state"):
    pytest.skip("this node agent predates the busy health check", allow_module_level=True)

import fastapi  # noqa: E402
from greencompute_node_agent.domain.inference import DockerInferenceBackend  # noqa: E402
from greencompute_protocol import ChatCompletionRequest, DeploymentState  # noqa: E402
from test_inference_hardening import catalog_model, cp, gw, state  # noqa: E402,F401

SECRET = "s"


class VLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, data, content_type="application/json"):
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.server.stall:
            time.sleep(1.7)
        self._send(b"")

    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        self._send(json.dumps({"model": "m", "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}).encode())


@pytest.fixture
def stack():
    vllm = ThreadingHTTPServer(("127.0.0.1", 0), VLLM)
    vllm.daemon_threads = True
    vllm.stall = False
    threading.Thread(target=vllm.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{vllm.server_address[1]}"
    backend = DockerInferenceBackend.__new__(DockerInferenceBackend)
    backend.backend_name = "docker"
    repo = SimpleNamespace(get_runtime=lambda dep: SimpleNamespace(
        deployment_id=dep, status="ready", runtime_url=url, metadata={}, container_id=None))
    node_routes.set_service(SimpleNamespace(repository=repo, inference_backend=backend), SimpleNamespace(
        agent_auth_secret=SECRET, inference_auth_secret=None, compute_auth_secret=None))
    app = fastapi.FastAPI()
    app.include_router(node_routes.router)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    yield vllm, SimpleNamespace(server_address=("127.0.0.1", port))
    server.should_exit = True
    vllm.shutdown()
    node_routes.set_service(None, None)


def test_one_health_stall_with_three_arrivals_fails_nothing(cp, gw, stack):  # noqa: F811
    vllm, node = stack
    gw.inference_client.miner_auth_secret = SECRET
    gw.inference_client.health_timeout_seconds = 2.0  # production default
    gw.inference_client.upstream_timeout_seconds = 10.0
    _, dep = catalog_model(cp, node)
    req = ChatCompletionRequest(model="qwen2.5-7b-instruct", messages=[{"role": "user", "content": "hi"}])
    gw.invoke_chat_completion(req)  # served fine
    vllm.stall = True
    errors = []

    def call():
        try:
            gw.invoke_chat_completion(req)
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    vllm.stall = False
    assert errors == []  # the stall made the probes slow, not failed
    assert state(cp, dep) == DeploymentState.READY
    assert cp.repository.get_deployment(dep.deployment_id).health_check_failures == 0

"""/v1 inference against a stand-in node agent on a local port, through the
real HttpInferenceClient, gateway service, control plane and routes.

2026-11-17: a reviewer took both shared models down with three malformed
requests. vLLM answered each with a 400, the node turned it into a 502, the
gateway counted it as a health failure, and the third marked the shared
deployment FAILED: 409 model_not_ready for everyone until Flux replaced it.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.inference_client import HttpInferenceClient
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes
from greencompute_gateway.transport.errors import install_error_handlers
from greencompute_persistence import session_scope
from greencompute_persistence.orm import LedgerEntryORM, UserORM
from greencompute_protocol import (
    CapacityUpdate,
    DeploymentRecord,
    DeploymentState,
    Heartbeat,
    NodeCapability,
    WorkloadKind,
    WorkloadSpec,
)
from sqlalchemy import select

VLLM_400 = {"object": "error", "message": "This model's maximum context length is 32768 tokens.",
            "type": "BadRequestError", "param": None, "code": 400}


class FakeNode(BaseHTTPRequestHandler):
    """The node agent's /inference/{id}/... routes. `server.chat` is
    (status, body, headers) or "hang"; `server.health` an HTTP status."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body, headers=None, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.health_probes += 1
        self._send(self.server.health, {"status": "ok" if self.server.health == 200 else "down"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.requests.append(body)
        chat = self.server.chat
        if chat == "hang":
            self.server.release.wait(5)
            return
        if chat == "stream":
            lines = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}], "id": "c1", "created": 1,
                 "model": "m"},
                {"choices": [{"index": 0, "delta": {"content": "Hel"}}], "id": "c1", "created": 1, "model": "m"},
                {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}], "id": "c1",
                 "created": 1, "model": "m"},
                {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}, "id": "c1",
                 "created": 1, "model": "m"},
            ]
            data = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
            self._send(200, data.encode(), content_type="text/event-stream")
            return
        status, payload, headers = chat
        self._send(status, payload, headers)


@pytest.fixture
def node():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeNode)
    server.daemon_threads = True
    server.health, server.health_probes, server.requests = 200, 0, []
    server.release = threading.Event()
    server.chat = (200, {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}, None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.release.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def cp(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'infer.db'}"
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)
    repo = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", email="u1@x.io", balance_credits=1000))
        s.add(UserORM(user_id="u2", username="u2", email="u2@x.io", balance_credits=1000))
    cp = ControlPlaneService(repository=repo)
    seen = datetime.now(UTC)
    repo.upsert_heartbeat(Heartbeat(hotkey="hk", healthy=True, observed_at=seen))
    repo.upsert_capacity(CapacityUpdate(hotkey="hk", observed_at=seen, nodes=[NodeCapability(
        hotkey="hk", node_id="n1", gpu_model="rtx4090", gpu_count=8, available_gpus=6, vram_gb_per_gpu=24,
        cpu_cores=64, memory_gb=256, observed_at=seen)]))
    return cp


def catalog_model(cp, node, name="qwen2.5-7b-instruct", **runtime):
    workload = cp.repository.upsert_workload(WorkloadSpec(
        workload_id=f"catalog-{name}", name=name, image="vllm/vllm-openai:v0.8.5", kind=WorkloadKind.INFERENCE,
        public=True, requirements={"gpu_count": 1}, workload_alias=name,
        runtime={"model_identifier": f"Qwen/{name}", **runtime},
        metadata={"managed_by": "flux", "catalog_model_id": name},
    ))
    deployment = None
    if node is not None:
        deployment = ready_deployment(cp, workload, node)
    return workload, deployment


def ready_deployment(cp, workload, node, owner=None):
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id=owner, state=DeploymentState.READY, ready_instances=1,
        hotkey="hk", node_id="n1", hourly_rate_cents=40,
    ))
    endpoint = f"http://127.0.0.1:{node.server_address[1]}/inference/{dep.deployment_id}"
    return cp.repository.update_deployment(dep.model_copy(update={"endpoint": endpoint}))


@pytest.fixture
def gw(cp):
    return GatewayService(
        repository=GatewayRepository(database_url="sqlite:///:memory:", bootstrap=True), control_plane=cp,
        inference_client=HttpInferenceClient(upstream_timeout_seconds=1.0, health_timeout_seconds=1.0),
    )


@pytest.fixture
def api(gw, monkeypatch):
    """The real /v1 routes on the service above, called as user u1."""
    caller = SimpleNamespace(user_id="u1", key_id="k1", admin=False)
    monkeypatch.setattr(routes, "service", gw)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: caller)
    for name in ("enforce_rate_limit", "_enforce_key_limits", "_enforce_account_spend_limit", "_refuse_blocked_account"):
        monkeypatch.setattr(routes, name, lambda *a, **k: None)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.router)
    client = TestClient(app, raise_server_exceptions=False)
    client.caller = caller
    return client


def chat(api, model="qwen2.5-7b-instruct", **body):
    return api.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}],
                                                  **body})


def failures(cp, deployment):
    return cp.repository.get_deployment(deployment.deployment_id).health_check_failures


def state(cp, deployment):
    return cp.repository.get_deployment(deployment.deployment_id).state


def charges(cp, user="u1"):
    with session_scope(cp.repository.session_factory) as s:
        return s.scalars(select(LedgerEntryORM).where(LedgerEntryORM.user_id == user)).all()


# --- item 2: a model's 4xx is the caller's, not the model's ----------------------------


def test_a_model_400_comes_back_to_the_caller_and_never_counts(api, cp, node):
    _, dep = catalog_model(cp, node)
    node.chat = (400, VLLM_400, {"X-Upstream-Status": "400"})
    for _ in range(5):  # three used to mark the shared model FAILED
        res = chat(api)
        assert res.status_code == 400
        err = res.json()["error"]
        assert err["code"] == "invalid_request" and err["retryable"] is False
        assert "maximum context length is 32768" in err["message"]
    assert failures(cp, dep) == 0 and state(cp, dep) == DeploymentState.READY
    assert charges(cp) == []  # not billed
    node.chat = (200, {"model": "m", "choices": [{"message": {"role": "assistant", "content": "ok"}}]}, None)
    assert chat(api).status_code == 200  # everyone else is still served


@pytest.mark.parametrize("status", [413, 422])
def test_a_meaningful_4xx_keeps_its_status(api, cp, node, status):
    catalog_model(cp, node)
    node.chat = (status, {"object": "error", "message": "too big"}, None)
    res = chat(api)
    assert res.status_code == status and res.json()["error"]["code"] == "invalid_request"


def test_an_old_node_that_wraps_the_400_in_a_502_is_still_read_as_a_400(api, cp, node):
    """Until the node agents are updated they answer {"detail": "upstream
    error: HTTP Error 400: Bad Request"} with a 502."""
    _, dep = catalog_model(cp, node)
    node.chat = (502, {"detail": "upstream error: HTTP Error 400: Bad Request"}, None)
    for _ in range(4):
        res = chat(api)
        assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_request"
    assert failures(cp, dep) == 0 and state(cp, dep) == DeploymentState.READY


def test_a_refused_stream_gets_its_status_not_a_broken_200(api, cp, node):
    _, dep = catalog_model(cp, node)
    node.chat = (400, VLLM_400, None)
    res = chat(api, stream=True)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_request"
    assert failures(cp, dep) == 0


def test_a_stream_still_streams(api, cp, node):
    catalog_model(cp, node)
    node.chat = "stream"
    with api.stream("POST", "/v1/chat/completions", json={
        "model": "qwen2.5-7b-instruct", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }) as res:
        assert res.status_code == 200
        text = "".join(res.iter_text())
    assert '"Hel"' in text and text.rstrip().endswith("data: [DONE]")
    assert len(charges(cp)) == 1  # billed from the usage chunk


def test_a_refusal_isnt_sent_on_to_another_replica(api, cp, node):
    workload, _ = catalog_model(cp, node)
    ready_deployment(cp, workload, node)
    node.chat = (400, VLLM_400, None)
    assert chat(api).status_code == 400
    assert len(node.requests) == 1  # every replica would refuse it

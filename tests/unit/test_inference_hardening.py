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
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from greencompute_control_plane.application import services as cp_services
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
        if self.server.health == "slow":  # answers after the gateway's probe gave up
            time.sleep(1.5)
            self._send(200, {"status": "ok"})
            return
        self._send(self.server.health, {"status": "ok" if self.server.health == 200 else "down"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.requests.append(body)
        chat = self.server.chat
        messages = body.get("messages") or [{}]
        if messages[-1].get("content") == "hold":  # generates until the test lets it finish
            self.server.release.wait(10)
            chat = (200, {"model": "m", "choices": [{"message": {"role": "assistant", "content": "held"}}]}, None)
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
        if chat[0] == "raw-stream":
            self._send(200, chat[1], content_type="text/event-stream")
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
def clock(monkeypatch):
    """The control plane's clock for counting health failures (they count
    only spread over 30 s); advance(seconds) moves it on."""
    now = [5000.0]
    monkeypatch.setattr(cp_services, "_monotonic", lambda: now[0])

    def advance(seconds):
        now[0] += seconds
    return advance


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
    """The real /v1 routes on the service above, called as user u1 (or as
    the user named in an X-API-Key header). One event loop for the client's
    life, as under uvicorn."""
    caller = SimpleNamespace(user_id="u1", key_id="k1", admin=False)

    def key(authorization=None, x_api_key=None, *a, **k):
        if isinstance(x_api_key, str):
            return SimpleNamespace(user_id=x_api_key, key_id=f"k-{x_api_key}", admin=False)
        return caller
    monkeypatch.setattr(routes, "service", gw)
    monkeypatch.setattr(routes, "require_api_key", key)
    for name in ("enforce_rate_limit", "_enforce_key_limits", "_enforce_account_spend_limit", "_refuse_blocked_account"):
        monkeypatch.setattr(routes, name, lambda *a, **k: None)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.caller = caller
        yield client


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


# --- item 3: requests that can't be valid never reach a model ----------------------------


def _refused(res, status, field):
    assert res.status_code == status, res.text
    err = res.json()["error"]
    assert err["retryable"] is False
    return err


def test_empty_messages_are_refused_before_any_backend(api, cp, node):
    _, dep = catalog_model(cp, node)
    res = api.post("/v1/chat/completions", json={"model": "qwen2.5-7b-instruct", "messages": []})
    err = _refused(res, 422, "messages")
    assert err["code"] == "validation_error" and "messages" in err["message"]
    assert res.json()["detail"][0]["loc"] == ["body", "messages"]
    assert node.requests == [] and node.health_probes == 0
    assert failures(cp, dep) == 0


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_an_absurd_token_count_names_the_limit(api, cp, node, field):
    catalog_model(cp, node)
    err = _refused(chat(api, **{field: 10**9}), 422, field)
    assert f"{field}: {field} must be a whole number from 1 to 131072" in err["message"]
    assert _refused(chat(api, **{field: 0}), 422, field)
    assert node.requests == []


def test_the_limit_is_the_models_context_when_it_is_known(api, cp, node):
    catalog_model(cp, node, max_model_len=32768)
    err = _refused(chat(api, max_tokens=40000), 422, "max_tokens")
    assert "from 1 to 32768 (the model's context length)" in err["message"]
    assert chat(api, max_tokens=32768).status_code == 200  # vLLM still checks prompt + completion


@pytest.mark.parametrize("field, value", [
    ("n", 0), ("n", 9), ("n", "2"), ("temperature", 3), ("top_p", 0), ("top_p", 1.5),
    ("presence_penalty", 5), ("frequency_penalty", -3), ("tools", "not-a-list"),
])
def test_other_impossible_values_are_422(api, cp, node, field, value):
    catalog_model(cp, node)
    res = chat(api, **{field: value})
    err = _refused(res, 422, field)
    assert err["code"] == "validation_error" and field in err["message"]
    assert node.requests == []


TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]


def test_auto_tool_choice_on_a_model_not_started_for_it_says_so_up_front(api, cp, node):
    catalog_model(cp, node)
    for body in ({"tools": TOOLS}, {"tools": TOOLS, "tool_choice": "auto"}):
        err = _refused(chat(api, **body), 400, "tool_choice")
        assert err["code"] == "invalid_request" and "tool_choice" in err["message"]
    assert node.requests == []
    # A named function, or "required", needs no tool parser: passed through.
    assert chat(api, tools=TOOLS, tool_choice="required").status_code == 200
    assert chat(api, tools=TOOLS, tool_choice={"type": "function", "function": {"name": "get_weather"}}).status_code == 200
    assert len(node.requests) == 2 and node.requests[0]["tools"] == TOOLS


def test_tools_pass_through_to_a_model_started_with_a_tool_parser(api, cp, node):
    catalog_model(cp, node, extra_engine_args=["--enable-auto-tool-choice", "--tool-call-parser", "hermes"])
    assert chat(api, tools=TOOLS).status_code == 200
    assert node.requests[0]["tools"] == TOOLS


def test_several_completions_are_billed_as_several(gw):
    from greencompute_protocol import ChatCompletionMessage, ChatCompletionRequest

    request = ChatCompletionRequest(model="m", messages=[ChatCompletionMessage(role="user", content="hi")],
                                    max_tokens=100, n=3)
    assert gw._completion_token_bound(request) == 300
    assert gw._clamp_reported_usage(request, 10, 300)[1] == 300


# --- items 4 and 5: a failure counts against a model only if its probe fails too --------


def test_a_timeout_on_a_healthy_model_is_a_504_and_never_counts(api, cp, node):
    """One long completion (a huge max_tokens, not streamed) ran past the
    gateway's timeout. The model is fine: the probe right after says so."""
    _, dep = catalog_model(cp, node)
    node.chat = "hang"
    for _ in range(4):
        res = chat(api)
        assert res.status_code == 504
        err = res.json()["error"]
        assert (err["code"], err["retryable"]) == ("upstream_timeout", False)
        assert "stream it" in err["message"]
    assert failures(cp, dep) == 0 and state(cp, dep) == DeploymentState.READY
    assert charges(cp) == []


def test_a_5xx_on_a_healthy_model_from_one_account_never_counts(api, cp, node):
    """One caller's requests that make the model fail (crafted or not)
    can't take a shared model down by themselves."""
    _, dep = catalog_model(cp, node)
    node.chat = (502, {"object": "error", "message": "CUDA error on this input"}, {"X-Upstream-Status": "500"})
    for _ in range(12):
        res = chat(api)
        assert res.status_code == 502
        err = res.json()["error"]
        assert (err["code"], err["retryable"]) == ("upstream_error", False)
        assert "CUDA error on this input" in err["message"]
    assert failures(cp, dep) == 0 and state(cp, dep) == DeploymentState.READY


def test_a_model_that_fails_its_probe_too_is_503_and_counts_to_failed(api, cp, node, clock):
    _, dep = catalog_model(cp, node)
    chat(api)  # healthy, served
    node.chat = (502, {"object": "error", "message": "engine dead"}, None)
    node.health = 503  # ...and the probe right after fails
    res = chat(api)
    # The pre-request probe fails first now: no replica is answering.
    assert res.status_code == 503
    err = res.json()["error"]
    assert (err["code"], err["retryable"], err["retry_after_seconds"]) == ("model_unavailable", True, 30)
    assert res.headers["retry-after"] == "30"
    for _ in range(2):
        clock(16)  # failures count only spread over 30 s
        chat(api)
    assert state(cp, dep) == DeploymentState.FAILED  # 3 failed probes over 32 s: replaced


def test_a_failure_whose_probe_fails_after_a_healthy_preflight_is_503(api, cp, node, monkeypatch):
    _, dep = catalog_model(cp, node)
    node.chat = (502, {"object": "error", "message": "engine dead"}, None)
    probes = iter([True, False])  # healthy before the request, down right after it
    monkeypatch.setattr(routes.service.inference_client, "check_deployment_health", lambda d: next(probes))
    res = chat(api)
    assert res.status_code == 503 and res.json()["error"]["code"] == "model_unavailable"
    assert failures(cp, dep) == 1


def test_a_node_that_cant_reach_its_model_counts_without_a_probe(api, cp, node):
    _, dep = catalog_model(cp, node)
    node.chat = (502, {"detail": "couldn't reach the model server", "reason": "model_unreachable"},
                 {"X-GC-Node-Error": "model_unreachable"})
    res = chat(api)
    assert res.status_code == 503 and res.json()["error"]["code"] == "model_unavailable"
    assert failures(cp, dep) == 1


def test_an_old_node_that_cant_reach_its_model_counts_too(api, cp, node):
    """Before the node update: 502 {"detail": "upstream error: <urlopen error
    [Errno 111] Connection refused>"} while its healthz still says ok."""
    _, dep = catalog_model(cp, node)
    node.chat = (502, {"detail": "upstream error: <urlopen error [Errno 111] Connection refused>"}, None)
    res = chat(api)
    assert res.status_code == 503 and failures(cp, dep) == 1


def test_a_success_still_resets_the_count(api, cp, node):
    _, dep = catalog_model(cp, node)
    cp.repository.update_deployment(dep.model_copy(update={"health_check_failures": 2}))
    assert chat(api).status_code == 200
    assert failures(cp, dep) == 0


def test_a_healthy_timeout_isnt_sent_on_to_another_replica(api, cp, node):
    workload, _ = catalog_model(cp, node)
    ready_deployment(cp, workload, node)
    node.chat = "hang"
    assert chat(api).status_code == 504
    assert len(node.requests) == 1  # not a second full wait on the other replica


def test_a_down_replica_is_skipped_for_the_next(api, cp, node, monkeypatch):
    workload, first = catalog_model(cp, node)
    second = ready_deployment(cp, workload, node)
    calls = []

    def invoke(deployment, request, *, request_id):
        calls.append(deployment.deployment_id)
        if len(calls) == 1:
            from greencompute_gateway.infrastructure.inference_client import InferenceConnectionError
            raise InferenceConnectionError("down", model_unreachable=True)
        return original(deployment, request, request_id=request_id)

    original = routes.service.inference_client.invoke_chat_completion
    monkeypatch.setattr(routes.service.inference_client, "invoke_chat_completion", invoke)
    assert chat(api).status_code == 200
    assert len(calls) == 2 and {calls[0], calls[1]} == {first.deployment_id, second.deployment_id}


# --- item 7: what an unknown, loading, paused or missing model answers -------------------


def test_an_unknown_model_is_404_model_not_found(api, cp, node):
    res = chat(api, model="no-such-model")
    assert res.status_code == 404
    err = res.json()["error"]
    assert (err["code"], err["retryable"]) == ("model_not_found", False)
    assert "/v1/models" in err["message"]


def test_someone_elses_private_model_is_404_like_an_unknown_one(api, cp, node):
    workload = cp.repository.upsert_workload(WorkloadSpec(
        name="their-llama", image="vllm", kind=WorkloadKind.INFERENCE, owner_user_id="u2", requirements={"gpu_count": 1}))
    ready_deployment(cp, workload, node, owner="u2")
    assert chat(api, model=workload.workload_id).json()["error"]["code"] == "model_not_found"
    assert chat(api, model="their-llama").json()["error"]["code"] == "model_not_found"


def test_a_model_that_is_starting_is_409_model_not_ready(api, cp, node):
    workload, _ = catalog_model(cp, None)
    cp.repository.create_deployment(DeploymentRecord(workload_id=workload.workload_id, state=DeploymentState.STARTING))
    res = chat(api)
    assert res.status_code == 409 and res.headers["retry-after"] == "30"
    err = res.json()["error"]
    assert (err["code"], err["retryable"], err["retry_after_seconds"]) == ("model_not_ready", True, 30)


def test_a_shared_model_with_no_replica_up_is_503(api, cp, node):
    workload, _ = catalog_model(cp, None)
    cp.repository.create_deployment(DeploymentRecord(workload_id=workload.workload_id, state=DeploymentState.FAILED))
    res = chat(api)
    assert res.status_code == 503 and res.json()["error"]["code"] == "model_unavailable"


def test_a_ready_model_on_a_silent_machine_is_503(api, cp, node):
    _, dep = catalog_model(cp, node)
    cp.repository.upsert_heartbeat(Heartbeat(hotkey="hk", healthy=False))
    res = chat(api)
    assert res.status_code == 503 and res.json()["error"]["code"] == "model_unavailable"


def _private(cp, owner="u1", name="meta-llama-Llama-3.1-8B-Instruct"):
    return cp.repository.upsert_workload(WorkloadSpec(
        name=name, image="vllm", kind=WorkloadKind.INFERENCE, owner_user_id=owner, requirements={"gpu_count": 1},
        runtime={"model_identifier": "meta-llama/Llama-3.1-8B-Instruct"}))


def test_your_paused_model_says_how_to_resume_it(api, cp, node):
    workload = _private(cp)
    paused = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.SUSPENDED,
        last_error="idle — no invocations in 30 min"))
    res = chat(api, model=workload.workload_id)
    assert res.status_code == 409
    body = res.json()
    assert (body["error"]["code"], body["error"]["retryable"]) == ("model_not_running", False)
    assert body["detail"]["deployment_id"] == paused.deployment_id and body["detail"]["paused"] is True
    assert f"/platform/deployments/{paused.deployment_id}/resume" in body["error"]["message"]


def test_your_model_whose_deployment_ended_says_to_deploy_it_again(api, cp, node):
    workload = _private(cp)
    cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.FAILED,
        last_error="container exited prematurely"))
    res = chat(api, model=workload.workload_id)
    assert res.status_code == 409 and res.json()["error"]["code"] == "model_not_running"
    assert "deploy it again" in res.json()["error"]["message"]


def _listed(api):
    res = api.get("/v1/models")
    assert res.status_code == 200
    return [m["id"] for m in res.json()["data"]]


def test_models_are_listed_only_while_a_backend_can_serve_them(api, cp, node):
    catalog_model(cp, node, name="qwen2.5-7b-instruct")
    loading, _ = catalog_model(cp, None, name="qwen2-vl-7b-instruct")
    cp.repository.create_deployment(DeploymentRecord(workload_id=loading.workload_id, state=DeploymentState.STARTING))
    assert _listed(api) == ["qwen2.5-7b-instruct"]
    cp.repository.upsert_heartbeat(Heartbeat(hotkey="hk", healthy=False))  # its machine stops answering
    assert _listed(api) == []


def test_your_own_private_model_is_listed_for_you_and_reachable_by_that_id(api, cp, node):
    mine = _private(cp)
    ready_deployment(cp, mine, node, owner="u1")
    theirs = _private(cp, owner="u2")
    ready_deployment(cp, theirs, node, owner="u2")
    catalog_model(cp, node)
    listed = _listed(api)
    assert listed == sorted(["qwen2.5-7b-instruct", "meta-llama-Llama-3.1-8B-Instruct"])
    assert chat(api, model="meta-llama-Llama-3.1-8B-Instruct").status_code == 200  # reaches u1's own
    api.caller.user_id = "u3"  # nobody's: sees the shared model only
    assert _listed(api) == ["qwen2.5-7b-instruct"]


def test_a_private_model_named_like_a_shared_one_is_listed_by_its_id(api, cp, node):
    catalog_model(cp, node, name="qwen2.5-7b-instruct")
    mine = _private(cp, name="qwen2.5-7b-instruct")
    ready_deployment(cp, mine, node, owner="u1")
    listed = _listed(api)
    assert sorted(listed) == sorted(["qwen2.5-7b-instruct", mine.workload_id])
    # Each id reaches what it says.
    assert chat(api, model=mine.workload_id).status_code == 200


# --- item 6: a private model that crashes is brought back --------------------------------


def test_a_crashed_private_model_is_replaced_and_says_so(api, cp, node, clock):
    mine = _private(cp)
    dep = ready_deployment(cp, mine, node, owner="u1")
    cp.repository.update_deployment(cp.repository.get_deployment(dep.deployment_id).model_copy(
        update={"ready_at": datetime.now(UTC)}))
    node.health = 503  # vLLM died: every probe fails
    for _ in range(3):
        clock(16)
        res = chat(api, model=mine.workload_id)
        assert res.status_code == 503 and res.json()["error"]["code"] == "model_unavailable"
    assert state(cp, dep) == DeploymentState.FAILED
    replacement = [d for d in cp.repository.list_deployments_for_workload(mine.workload_id)
                   if d.deployment_id != dep.deployment_id]
    assert [d.state for d in replacement] == [DeploymentState.PENDING]
    # While it starts, callers are told to retry shortly.
    res = chat(api, model=mine.workload_id)
    assert res.status_code == 409 and res.json()["error"]["code"] == "model_not_ready"


# --- item 8: embeddings are never made up ------------------------------------------------


def test_embeddings_never_answer_with_made_up_vectors(api, cp, node):
    catalog_model(cp, node)
    for body in ({"model": "qwen2.5-7b-instruct", "input": "hello"},
                 {"model": "qwen2.5-7b-instruct", "input": ["a", "b"]}):
        res = api.post("/v1/embeddings", json=body)
        assert res.status_code == 400 and "data" not in res.json()
        err = res.json()["error"]
        assert err["code"] == "invalid_request" and "no embedding model is available" in err["message"]
    res = api.post("/v1/embeddings", json={"model": "text-embedding-3-small", "input": "hello"})
    assert res.status_code == 404 and res.json()["error"]["code"] == "model_not_found"
    res = api.post("/v1/embeddings", json={"input": "hello"})
    assert res.status_code == 400 and "no embedding model is available" in res.json()["error"]["message"].lower()
    assert node.requests == []


# --- item 9: /v1/completions answers in the text_completion shape -------------------------


def test_legacy_completions_answer_with_text(api, cp, node):
    catalog_model(cp, node)
    res = api.post("/v1/completions", json={"model": "qwen2.5-7b-instruct", "prompt": "Say hi", "max_tokens": 5,
                                            "top_p": 0.9, "stop": ["\n"]})
    assert res.status_code == 200
    body = res.json()
    assert body["object"] == "text_completion"
    assert body["choices"] == [{"text": "Hello", "index": 0, "logprobs": None, "finish_reason": "stop"}]
    assert body["usage"]["completion_tokens"] == 2
    sent = node.requests[-1]
    assert [(m["role"], m["content"]) for m in sent["messages"]] == [("user", "Say hi")]
    assert (sent["max_tokens"], sent["top_p"], sent["stop"]) == (5, 0.9, ["\n"])  # passed on


def test_legacy_completions_stream_text_chunks(api, cp, node):
    catalog_model(cp, node)
    node.chat = "stream"
    with api.stream("POST", "/v1/completions", json={"model": "qwen2.5-7b-instruct", "prompt": "Say hi",
                                                     "stream": True}) as res:
        assert res.status_code == 200
        text = "".join(res.iter_text())
    chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    assert all(c["object"] == "text_completion" for c in chunks)
    assert "".join(ch["text"] for c in chunks for ch in c["choices"]) == "Hello"
    assert chunks[-1]["usage"]["total_tokens"] == 7 and chunks[-1]["choices"] == []
    assert all("delta" not in ch for c in chunks for ch in c["choices"])
    assert text.rstrip().endswith("data: [DONE]")


@pytest.mark.parametrize("body, field", [
    ({"prompt": ""}, "prompt"), ({"prompt": "   "}, "prompt"), ({}, "prompt"), ({"prompt": ["a", "b"]}, "prompt"),
    ({"prompt": "hi", "echo": True}, "echo"), ({"prompt": "hi", "logprobs": 2}, "logprobs"),
    ({"prompt": "hi", "max_tokens": 10**9}, "max_tokens"),
])
def test_legacy_completions_refuse_what_cant_work(api, cp, node, body, field):
    catalog_model(cp, node)
    res = api.post("/v1/completions", json={"model": "qwen2.5-7b-instruct", **body})
    assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error"
    assert field in res.json()["error"]["message"]
    assert node.requests == []


def test_a_one_item_prompt_list_is_a_prompt(api, cp, node):
    catalog_model(cp, node)
    assert api.post("/v1/completions", json={"model": "qwen2.5-7b-instruct", "prompt": ["Say hi"]}).status_code == 200


# --- review follow-ups -------------------------------------------------------------------


def test_a_cut_off_stream_line_doesnt_crash_billing(api, cp, node):
    catalog_model(cp, node)
    good = json.dumps({"choices": [{"index": 0, "delta": {"content": "Hi"}}], "id": "c", "created": 1, "model": "m"})
    node.chat = ("raw-stream", f"data: {good}\n\ndata: {{\"choices\": [{{\"delta\": {{\"cont\n".encode())
    with api.stream("POST", "/v1/chat/completions", json={
        "model": "qwen2.5-7b-instruct", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }) as res:
        assert res.status_code == 200
        text = "".join(res.iter_text())
    assert '"Hi"' in text
    assert len(charges(cp)) == 1  # what was served is billed


def test_someone_its_shared_with_isnt_told_the_owners_details(api, cp, node):
    workload = cp.repository.upsert_workload(WorkloadSpec(
        name="u1-public-llama", image="vllm", kind=WorkloadKind.INFERENCE, owner_user_id="u1", public=True,
        requirements={"gpu_count": 1}))
    cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.SUSPENDED,
        last_error="insufficient balance: $0.00 left"))
    api.caller.user_id = "u2"
    body = chat(api, model=workload.workload_id).json()
    assert body["error"]["code"] == "model_not_running"
    assert body["detail"]["deployment_id"] is None
    assert "balance" not in body["error"]["message"] and "/resume" not in body["error"]["message"]
    api.caller.user_id = "u1"  # its owner is told which, and why
    body = chat(api, model=workload.workload_id).json()
    assert body["detail"]["deployment_id"] and "balance" in body["error"]["message"]


# --- review H1: a moment's trouble never fails a model -----------------------------------


def test_three_requests_during_a_health_stall_dont_fail_the_shared_model(api, cp, node, clock):
    """The reviewer's case: a 1.7 s stall of the model's health check while
    three requests arrived took the shared model to FAILED. Each request's
    probe times out, but failures at one moment count once."""
    _, dep = catalog_model(cp, node)
    assert chat(api).status_code == 200
    node.health = "slow"  # the probe (1 s timeout here) gives up on it
    results = []
    threads = [threading.Thread(target=lambda: results.append(chat(api).status_code)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [503, 503, 503]
    assert state(cp, dep) == DeploymentState.READY and failures(cp, dep) == 1
    node.health = 200
    assert chat(api).status_code == 200 and failures(cp, dep) == 0


def test_a_15_second_node_restart_under_traffic_fails_nothing(api, cp, node, clock):
    _, dep = catalog_model(cp, node)
    node.health = 503  # the node agent is restarting: nothing answers
    node.chat = (502, {"detail": "x", "reason": "model_unreachable"}, {"X-GC-Node-Error": "model_unreachable"})
    for _ in range(15):  # a request a second
        assert chat(api).status_code == 503
        clock(1)
    assert state(cp, dep) == DeploymentState.READY and failures(cp, dep) <= 2
    node.health = 200
    node.chat = (200, {"model": "m", "choices": [{"message": {"role": "assistant", "content": "back"}}]}, None)
    assert chat(api).status_code == 200 and failures(cp, dep) == 0


# --- review H2: a replica that passes /health but fails every request --------------------


def test_a_replica_failing_everyones_requests_is_escalated_then_failed(api, cp, node, clock):
    """A hung engine or a misconfigured server: /health passes, every request
    fails. Five in a row from two accounts count one health failure; the
    window rule then fails it after 30 s of that."""
    _, dep = catalog_model(cp, node)
    node.chat = (502, {"object": "error", "message": "engine hung"}, {"X-Upstream-Status": "500"})
    for i in range(5):
        api.caller.user_id = "u1" if i % 2 else "u2"
        assert chat(api).status_code == 502
    assert failures(cp, dep) == 1 and state(cp, dep) == DeploymentState.READY
    for _ in range(2):
        clock(16)
        for i in range(5):
            api.caller.user_id = "u1" if i % 2 else "u2"
            chat(api)
    assert state(cp, dep) == DeploymentState.FAILED


def test_a_success_between_restarts_the_count(api, cp, node):
    _, dep = catalog_model(cp, node)
    bad = (502, {"object": "error", "message": "sometimes"}, None)
    good = (200, {"model": "m", "choices": [{"message": {"role": "assistant", "content": "ok"}}]}, None)
    for i in range(12):
        api.caller.user_id = "u1" if i % 2 else "u2"
        node.chat = good if i % 4 == 3 else bad
        chat(api)
    assert failures(cp, dep) == 0


def test_a_5xx_from_a_healthy_replica_fails_over_to_another(api, cp, node, monkeypatch):
    workload, first = catalog_model(cp, node)
    second = ready_deployment(cp, workload, node)
    calls = []
    original = routes.service.inference_client.invoke_chat_completion

    def invoke(deployment, request, *, request_id):
        calls.append(deployment.deployment_id)
        if len(calls) == 1:
            from greencompute_gateway.infrastructure.inference_client import InferenceUpstreamError
            raise InferenceUpstreamError("boom", status_code=502, upstream_message="engine error")
        return original(deployment, request, request_id=request_id)

    monkeypatch.setattr(routes.service.inference_client, "invoke_chat_completion", invoke)
    assert chat(api).status_code == 200
    assert len(calls) == 2 and set(calls) == {first.deployment_id, second.deployment_id}


# --- review M1: a 4xx is the request's fault only in the model server's own words ---------


@pytest.mark.parametrize("body", [
    {"object": "error", "message": "bad", "type": "BadRequestError", "code": 400},  # vLLM 0.8
    {"error": {"message": "bad", "type": "BadRequestError", "param": None, "code": 400}},  # newer vLLM, OpenAI
])
def test_a_model_shaped_4xx_is_the_callers(api, cp, node, body):
    _, dep = catalog_model(cp, node)
    node.chat = (400, body, None)
    res = chat(api)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [b"<html><body>404 Not Found</body></html>", {"detail": "Not Found"}, {"ok": False}])
def test_any_other_4xx_is_the_servers(api, cp, node, body):
    """Something else answering on the model's port (a proxy's HTML page, a
    stray service) is a server problem, not the caller's."""
    _, dep = catalog_model(cp, node)
    node.chat = (400, body, None)
    res = chat(api)
    assert res.status_code == 502 and res.json()["error"]["code"] == "upstream_error"


def test_a_passed_on_reason_is_capped(api, cp, node):
    catalog_model(cp, node)
    node.chat = (400, {"object": "error", "message": "x" * 5000}, None)
    message = chat(api).json()["error"]["message"]
    assert len(message) < 400 and message.rstrip(".").endswith("…")


# --- review H3: one account can't pin the gateway's threads ------------------------------


def _hold(api, results, user, **body):
    def run():
        res = api.post("/v1/chat/completions", headers={"X-API-Key": user}, json={
            "model": "qwen2.5-7b-instruct", "messages": [{"role": "user", "content": "hold"}], **body})
        results.append(res.status_code)
    t = threading.Thread(target=run)
    t.start()
    return t


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


def test_an_account_has_a_few_requests_in_progress_at_most(api, cp, node, gw, monkeypatch):
    from greencompute_gateway.transport import v1_capacity

    monkeypatch.setattr(v1_capacity, "V1_MAX_IN_FLIGHT_PER_ACCOUNT", 2)
    gw.inference_client.upstream_timeout_seconds = 15
    catalog_model(cp, node)
    results = []
    threads = [_hold(api, results, "u1") for _ in range(2)]
    _wait_for(lambda: len(node.requests) == 2)
    res = chat(api)  # u1's third
    assert res.status_code == 429 and res.headers["retry-after"] == "2"
    err = res.json()["error"]
    assert (err["code"], err["retryable"]) == ("rate_limited", True)
    assert res.json()["detail"]["limit"] == 2 and "in progress" in err["message"]
    api.caller.user_id = "u2"  # another account isn't held up
    assert chat(api).status_code == 200
    node.release.set()
    for t in threads:
        t.join()
    assert results == [200, 200]
    api.caller.user_id = "u1"
    assert chat(api).status_code == 200  # its slots are free again
    assert v1_capacity.in_flight("u1") == 0


def test_a_finished_or_abandoned_stream_frees_its_slot(api, cp, node):
    from greencompute_gateway.transport import v1_capacity

    catalog_model(cp, node)
    node.chat = "stream"
    with api.stream("POST", "/v1/chat/completions", json={
        "model": "qwen2.5-7b-instruct", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }) as res:
        "".join(res.iter_text())
    assert v1_capacity.in_flight("u1") == 0
    with api.stream("POST", "/v1/chat/completions", json={
        "model": "qwen2.5-7b-instruct", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }) as res:
        next(res.iter_bytes())  # read a little, then hang up
    import gc
    gc.collect()
    _wait_for(lambda: v1_capacity.in_flight("u1") == 0)


def test_v1_runs_on_its_own_threads_so_platform_stays_responsive(api, cp, node, gw, monkeypatch):
    """Every /v1 thread busy with long completions: /platform still answers
    at once (it used to queue behind them on the one shared pool)."""
    from greencompute_gateway.transport import v1_capacity

    monkeypatch.setattr(v1_capacity, "V1_THREADS", 2)
    monkeypatch.setattr(v1_capacity, "_limiters", __import__("weakref").WeakKeyDictionary())
    import anyio.to_thread

    # The shared pool as small as the /v1 one: if /v1 still ran on it, two
    # held completions would take every thread /platform has.
    api.portal.call(lambda: setattr(anyio.to_thread.current_default_thread_limiter(), "total_tokens", 2))
    gw.inference_client.upstream_timeout_seconds = 15
    catalog_model(cp, node)
    results = []
    threads = [_hold(api, results, "u1") for _ in range(3)]  # two run, one waits for a /v1 thread
    _wait_for(lambda: len(node.requests) == 2)
    started = time.monotonic()
    assert api.get("/platform/nodes/supported").status_code == 200
    assert time.monotonic() - started < 2
    node.release.set()
    for t in threads:
        t.join()
    assert results == [200, 200, 200]


def test_a_full_replica_is_busy_not_failing(api, cp, node):
    """The node's per-model limit of requests in progress: 503 model_busy."""
    _, dep = catalog_model(cp, node)
    node.chat = (503, {"detail": "busy", "reason": "model_busy"}, {"X-GC-Node-Error": "model_busy"})
    for i in range(8):
        api.caller.user_id = "u1" if i % 2 else "u2"
        res = chat(api)
        assert res.status_code == 503 and res.headers["retry-after"] == "2"
        assert res.json()["error"]["code"] == "model_unavailable"
    assert failures(cp, dep) == 0  # neither counted nor escalated

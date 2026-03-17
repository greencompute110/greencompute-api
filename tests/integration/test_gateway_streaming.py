from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from greenference_builder.application.services import BuilderService
from greenference_builder.infrastructure.repository import BuilderRepository
from greenference_control_plane.application.services import ControlPlaneService
from greenference_control_plane.infrastructure.repository import ControlPlaneRepository
from greenference_gateway.application.services import GatewayService
from greenference_gateway.infrastructure import inference_client as inference_client_module
from greenference_gateway.infrastructure.repository import GatewayRepository
from greenference_gateway.transport import routes as routes_module
from greenference_miner_agent.application.services import MinerAgentService
from greenference_miner_agent.infrastructure.repository import MinerAgentRepository
from greenference_protocol import (
    BuildRequest,
    CapacityUpdate,
    ChatCompletionRequest,
    Heartbeat,
    MinerRegistration,
    NodeCapability,
    WorkloadCreateRequest,
)


class _FakeResponse:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.status = 200
        self._lines = None if not isinstance(payload, str) else [f"{line}\n".encode() for line in payload.splitlines()]
        self._line_index = 0

    def read(self) -> bytes:
        if isinstance(self.payload, str):
            return self.payload.encode()
        return json.dumps(self.payload).encode()

    def readline(self) -> bytes:
        if self._lines is None:
            return b""
        if self._line_index >= len(self._lines):
            return b""
        line = self._lines[self._line_index]
        self._line_index += 1
        return line

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _patch_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module, "require_api_key", lambda *args, **kwargs: SimpleNamespace(key_id="key-1"))
    monkeypatch.setattr(routes_module, "enforce_rate_limit", lambda *args, **kwargs: None)


def _patch_upstream(
    monkeypatch: pytest.MonkeyPatch,
    miner: MinerAgentService,
    *,
    timeout_on_invoke: bool = False,
) -> None:
    def fake_urlopen(target, timeout=None):  # type: ignore[no-untyped-def]
        path = urlparse(target.full_url).path
        if path.endswith("/healthz"):
            deployment_id = path.split("/")[2]
            return _FakeResponse({"status": "ok", "deployment_id": deployment_id})
        if path.endswith("/v1/chat/completions"):
            if timeout_on_invoke:
                raise socket.timeout("timed out")
            deployment_id = path.split("/")[2]
            payload = ChatCompletionRequest(**json.loads(target.data.decode()))
            if payload.stream:
                stream = "".join(miner.stream_chat_completion(deployment_id, payload))
                return _FakeResponse(stream)
            response = miner.serve_chat_completion(deployment_id, payload)
            return _FakeResponse(response.model_dump(mode="json"))
        raise HTTPError(target.full_url, 404, "not found", hdrs=None, fp=None)

    monkeypatch.setattr(inference_client_module.request, "urlopen", fake_urlopen)


def _ready_gateway(shared_db: str) -> tuple[GatewayService, MinerAgentService, ControlPlaneService, str]:
    control_plane = ControlPlaneService(ControlPlaneRepository(database_url=shared_db, bootstrap=True))
    builder = BuilderService(BuilderRepository(database_url=shared_db, bootstrap=True))
    gateway = GatewayService(
        GatewayRepository(database_url=shared_db, bootstrap=True),
        control_plane=control_plane,
        builder=builder,
    )
    miner = MinerAgentService(MinerAgentRepository(), control_plane=control_plane)

    miner.onboard(
        MinerRegistration(
            hotkey="miner-a",
            payout_address="5Fminer",
            api_base_url="http://miner-a.local",
            validator_url="http://validator.local",
        )
    )
    miner.publish_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    miner.publish_capacity(
        CapacityUpdate(
            hotkey="miner-a",
            nodes=[
                NodeCapability(
                    hotkey="miner-a",
                    node_id="node-a",
                    gpu_model="a100",
                    gpu_count=1,
                    available_gpus=1,
                    vram_gb_per_gpu=80,
                    cpu_cores=32,
                    memory_gb=128,
                    performance_score=1.3,
                )
            ],
        )
    )
    build = gateway.start_build(BuildRequest(image="greenference/echo:latest", context_uri="s3://echo.zip"))
    builder.process_pending_events()
    workload = gateway.create_workload(
        WorkloadCreateRequest(
            name="stream-model",
            image=build.image,
            requirements={"gpu_count": 1, "min_vram_gb_per_gpu": 40},
        )
    )
    deployment = gateway.create_deployment({"workload_id": workload.workload_id})
    control_plane.process_pending_events()
    miner.reconcile_once("miner-a")
    return gateway, miner, control_plane, workload.workload_id


def test_gateway_streaming_route_returns_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway, miner, control_plane, workload_id = _ready_gateway(shared_db)
    _patch_auth(monkeypatch)
    _patch_upstream(monkeypatch, miner)
    monkeypatch.setattr(routes_module, "service", gateway)

    response = routes_module.chat_completions(
        ChatCompletionRequest(
            model=workload_id,
            messages=[{"role": "user", "content": "stream this response"}],
            stream=True,
        ),
        x_api_key="gk_demo",
    )

    assert isinstance(response, StreamingResponse)
    body = "".join(list(gateway.stream_chat_completion(
        ChatCompletionRequest(
            model=workload_id,
            messages=[{"role": "user", "content": "stream this response"}],
            stream=True,
        )
    )))
    assert "text/event-stream" in response.media_type
    assert "data: [DONE]" in body
    assert "chat.completion.chunk" in body
    assert "stream" in body
    assert "this" in body
    assert "response" in body
    persisted_usage = control_plane.process_pending_events()
    summary = control_plane.usage_summary()
    assert len(persisted_usage["usage_records"]) == 1
    deployment_id = next(iter(summary))
    assert summary[deployment_id]["requests"] == 0.0
    assert summary[deployment_id]["streamed_requests"] == 1.0
    assert summary[deployment_id]["stream_chunks"] > 0.0


def test_gateway_returns_504_on_upstream_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway, miner, _control_plane, workload_id = _ready_gateway(shared_db)
    _patch_auth(monkeypatch)
    _patch_upstream(monkeypatch, miner, timeout_on_invoke=True)
    monkeypatch.setattr(routes_module, "service", gateway)

    with pytest.raises(HTTPException) as exc:
        routes_module.chat_completions(
            ChatCompletionRequest(
                model=workload_id,
                messages=[{"role": "user", "content": "timeout please"}],
            ),
            x_api_key="gk_demo",
        )

    assert exc.value.status_code == 504

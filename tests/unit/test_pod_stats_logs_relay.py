"""GET /platform/deployments/{id}/stats and /logs (request #17): relayed to
the node agent running the rental, with the node secret, size-capped."""
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from greencompute_gateway.transport import routes
from greencompute_protocol import DeploymentState


class Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def relay(monkeypatch):
    dep = SimpleNamespace(deployment_id="d1", hotkey="hk", state=DeploymentState.READY)
    seen = {}

    def urlopen(req, timeout):
        seen["url"], seen["auth"], seen["timeout"] = req.full_url, req.get_header("X-agent-auth"), timeout
        if seen.get("fail"):
            raise routes.URLError("no route to host")
        return Reply(seen.get("body", json.dumps({"cpu_pct": 12.5, "gpus": [{"index": 0, "power_w": 441.2}]}).encode()))
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", admin=False))
    monkeypatch.setattr(routes.service, "get_deployment", lambda *a, **k: dep)
    monkeypatch.setattr(routes, "_lookup_miner_base_url", lambda hotkey: "http://172.16.10.12:8007")
    monkeypatch.setattr(routes.urllib_request, "urlopen", urlopen)
    monkeypatch.setenv("GREENCOMPUTE_INFERENCE_AUTH_SECRET", "node-secret")
    return dep, seen


def test_stats_come_from_the_node_with_its_secret(relay):
    dep, seen = relay
    out = routes.get_deployment_stats("d1", authorization="Bearer k", x_api_key=None)
    assert out["gpus"][0]["power_w"] == 441.2
    assert seen["url"] == "http://172.16.10.12:8007/pods/d1/stats" and seen["auth"] == "node-secret"


def test_stats_are_empty_when_the_node_doesnt_answer(relay):
    _, seen = relay
    seen["fail"] = True
    assert routes.get_deployment_stats("d1", authorization="Bearer k", x_api_key=None) == {}


def test_logs_relay_the_lines_asked_for_within_limits(relay):
    _, seen = relay
    seen["body"] = json.dumps({"lines": 50, "command_log": "hashrate 61 MH/s", "container_log": None}).encode()
    out = routes.get_deployment_logs("d1", lines=50, authorization="Bearer k", x_api_key=None)
    assert out["command_log"] == "hashrate 61 MH/s"
    assert seen["url"].endswith("/pods/d1/logs?lines=50")
    routes.get_deployment_logs("d1", lines=10**9, authorization="Bearer k", x_api_key=None)
    assert seen["url"].endswith("lines=2000")


def test_logs_say_when_the_machine_doesnt_answer(relay):
    _, seen = relay
    seen["fail"] = True
    with pytest.raises(HTTPException) as exc:
        routes.get_deployment_logs("d1", authorization="Bearer k", x_api_key=None)
    assert exc.value.status_code == 503 and exc.value.error_code == "unavailable"
    assert exc.value.headers["Retry-After"] == "10"


def test_logs_only_while_it_runs(relay):
    dep, _ = relay
    dep.state = DeploymentState.SUSPENDED
    with pytest.raises(HTTPException) as exc:
        routes.get_deployment_logs("d1", authorization="Bearer k", x_api_key=None)
    assert exc.value.status_code == 409


def test_an_oversized_reply_is_dropped_not_buffered(relay):
    _, seen = relay
    seen["body"] = b"{" + b" " * (300 * 1024) + b"}"
    assert routes.get_deployment_stats("d1", authorization="Bearer k", x_api_key=None) == {}
    assert seen["timeout"] == 6.0  # under the dashboard's poll, plus a little


def test_spec_documents_both():
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    assert "/platform/deployments/{deployment_id}/logs" in spec["paths"]
    props = spec["components"]["schemas"]["GpuReading"]["properties"]
    assert {"power_w", "temperature_c", "sm_clock_mhz", "util_pct"} <= set(props)

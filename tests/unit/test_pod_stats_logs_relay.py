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
    dep = SimpleNamespace(deployment_id="d1", hotkey="hk", state=DeploymentState.READY, owner_user_id="u1",
                          endpoint="ssh://root@1.2.3.4:30001")
    seen = {}

    def urlopen(req, timeout):
        seen["url"], seen["auth"], seen["timeout"] = req.full_url, req.get_header("X-agent-auth"), timeout
        if seen.get("fail"):
            raise routes.URLError("no route to host")
        return Reply(seen.get("body", json.dumps({"cpu_pct": 12.5, "gpus": [{"index": 0, "power_w": 441.2}]}).encode()))
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", admin=False, key_id="k1"))
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


def test_a_reply_cut_short_is_no_reading_not_a_crash(relay, monkeypatch):
    import http.client

    def cut(req, timeout):
        raise http.client.IncompleteRead(b"{\"cpu")
    monkeypatch.setattr(routes.urllib_request, "urlopen", cut)
    assert routes.get_deployment_stats("d1", authorization="Bearer k", x_api_key=None) == {}


def test_a_command_is_posted_to_the_node_and_audited_without_its_text(relay, monkeypatch, caplog):
    import logging

    from greencompute_gateway.transport import schemas

    dep, seen = relay
    dep.endpoint = "ssh://root@1.2.3.4:30001"
    seen["body"] = json.dumps({"exit_code": 0, "output": "ok", "output_truncated": False, "timed_out": False}).encode()
    posted = {}
    real = routes.urllib_request.urlopen

    def urlopen(req, timeout):
        posted["method"], posted["data"], posted["timeout"] = req.get_method(), json.loads(req.data), timeout
        return real(req, timeout)
    monkeypatch.setattr(routes.urllib_request, "urlopen", urlopen)
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    with caplog.at_level(logging.INFO):
        out = routes.run_in_deployment("d1", schemas.ExecRequest(command="export TOKEN=s3cret; ./update.sh",
                                                                 timeout_seconds=30),
                                       authorization="Bearer k", x_api_key=None)
    assert out["exit_code"] == 0 and seen["url"].endswith("/pods/d1/exec")
    assert posted == {"method": "POST", "data": {"command": "export TOKEN=s3cret; ./update.sh", "timeout_seconds": 30},
                      "timeout": 45.0}
    audit = [r.getMessage() for r in caplog.records if "deployment_exec" in r.getMessage()]
    assert audit and "s3cret" not in audit[0] and "command_sha256=" in audit[0]


def test_commands_only_in_running_pods(relay, monkeypatch):
    from greencompute_gateway.transport import schemas

    dep, _ = relay
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    dep.endpoint = "http://10.0.0.1:8000"  # a model, not a pod
    with pytest.raises(HTTPException) as exc:
        routes.run_in_deployment("d1", schemas.ExecRequest(command="ls"), authorization="Bearer k", x_api_key=None)
    assert exc.value.status_code == 409
    with pytest.raises(ValueError):
        schemas.ExecRequest(command="ls\x00rm")
    with pytest.raises(ValueError):
        schemas.ExecRequest(command="ls", timeout_seconds=600)


def _exec(**kw):
    from greencompute_gateway.transport import schemas

    return routes.run_in_deployment("d1", schemas.ExecRequest(command="ls", **kw), authorization="Bearer k", x_api_key=None)


def test_an_admin_key_cannot_run_commands_in_someone_elses_pod(relay, monkeypatch):
    dep, _ = relay
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="admin-1", admin=True, key_id="ka"))
    with pytest.raises(HTTPException) as exc:
        _exec()
    assert exc.value.status_code == 404


def test_each_customer_runs_at_most_four_at_once(relay, monkeypatch):
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    with routes._exec_slot("u1"), routes._exec_slot("u1"), routes._exec_slot("u1"), routes._exec_slot("u1"):
        with pytest.raises(HTTPException) as exc:
            _exec()
        assert exc.value.status_code == 429 and exc.value.headers["Retry-After"] == "5"
        with routes._exec_slot("u2"):  # someone else isn't held up
            pass
    assert routes._exec_running == {}


@pytest.mark.parametrize("outcome, status, code, retry", [
    (("refused", 409, "the pod isn't running"), 409, "conflict", None),
    (("refused", 429, "this machine is running other commands"), 429, "rate_limited", "5"),
    (("unknown", None, None), 504, "exec_unknown", None),
    (("unreachable", None, None), 503, "unavailable", "10"),
])
def test_what_went_wrong_decides_whether_to_retry(relay, monkeypatch, outcome, status, code, retry):
    monkeypatch.setattr(routes, "enforce_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_node_request", lambda *a, **k: outcome)
    with pytest.raises(HTTPException) as exc:
        _exec()
    assert (exc.value.status_code, exc.value.error_code) == (status, code)
    assert (exc.value.headers or {}).get("Retry-After") == retry


def test_the_relay_finds_the_box_by_node_not_by_the_shared_hotkey(relay, monkeypatch):
    dep, seen = relay
    dep.node_id = "node-8x4090-001"
    monkeypatch.setattr(routes.service.control_plane.repository, "node_api_base_url",
                        lambda node_id: "http://172.16.10.24:8007" if node_id == "node-8x4090-001" else None,
                        raising=False)
    routes.get_deployment_stats("d1", authorization="Bearer k", x_api_key=None)
    assert seen["url"] == "http://172.16.10.24:8007/pods/d1/stats"
    dep.node_id = "node-unknown"  # a node that doesn't report its address yet: the hotkey's
    routes.get_deployment_stats("d1", authorization="Bearer k", x_api_key=None)
    assert seen["url"] == "http://172.16.10.12:8007/pods/d1/stats"

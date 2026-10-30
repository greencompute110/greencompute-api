"""POST /mcp: Green Compute as an MCP server (request #26). Each tool runs as
its REST request (so a scoped key's scopes apply), errors come back as tool
results with the API's error object, and no SSH private key is ever returned."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from greencompute_gateway.transport import mcp as mcp_module
from greencompute_gateway.transport import routes
from greencompute_gateway.transport.errors import coded
from greencompute_gateway.transport.security import current_request_route

AUTH = {"Authorization": "Bearer gk_test"}


@pytest.fixture
def client(monkeypatch):
    seen = {}

    def fake_key(authorization, x_api_key, **kw):
        seen["scope_route_at_auth"] = current_request_route.get()
        if authorization != "Bearer gk_test":
            raise HTTPException(status_code=401, detail="invalid api key", headers={"WWW-Authenticate": "Bearer"})
        return SimpleNamespace(key_id="k1", user_id="u1", admin=False, scopes=[])
    monkeypatch.setattr(mcp_module, "require_api_key", fake_key)
    app = FastAPI()
    app.include_router(mcp_module.router)
    return TestClient(app), seen, monkeypatch


def rpc(client, method, params=None, rpc_id=1, headers=AUTH):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers)


def call(client, name, arguments=None):
    r = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_initialize_negotiates_and_says_what_it_is(client):
    c, _, _ = client
    r = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "green-compute" and "rent_gpu" in result["instructions"]
    # An older client gets the newest we speak.
    assert rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_are_accepted_without_a_reply(client):
    c, _, _ = client
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH)
    assert r.status_code == 202 and r.content == b""


def test_tools_are_listed_with_schemas_and_hints(client):
    c, _, _ = client
    tools = {t["name"]: t for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert {"list_gpus", "rent_gpu", "get_rental", "stop_rental", "rental_logs", "rental_stats", "recent_events",
            "get_balance", "list_rentals", "alert_when_free", "run_command"} == set(tools)
    assert tools["rent_gpu"]["inputSchema"]["required"] == ["ssh_public_keys"]
    assert tools["stop_rental"]["annotations"]["destructiveHint"] is True
    assert tools["list_gpus"]["annotations"]["readOnlyHint"] is True


def test_a_bad_key_is_refused_before_anything_runs(client):
    c, _, _ = client
    r = rpc(c, "tools/list", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == -32001


def test_any_valid_key_may_connect_and_each_tool_runs_as_its_rest_call(client):
    from greencompute_gateway.domain.scopes import required_scope
    from greencompute_gateway.transport.security import _check_scopes

    assert required_scope("POST", "/mcp") == "*"
    token = current_request_route.set(("POST", "/mcp"))
    try:
        _check_scopes(SimpleNamespace(admin=False, scopes=["read"]))  # a scoped key gets in: no raise
    finally:
        current_request_route.reset(token)
    c, seen, mp = client
    calls = []

    def fake_list(state, authorization, x_api_key):
        calls.append((current_request_route.get(), state, authorization))
        return [{"deployment_id": "d1", "state": "ready", "name": "rig", "last_error": "x" * 400}]
    mp.setattr(routes, "list_deployments", fake_list)
    result = call(c, "list_rentals")
    assert calls == [(("GET", "/platform/deployments"), mcp_module.ACTIVE_STATES, "Bearer gk_test")]
    rental = result["structuredContent"]["rentals"][0]
    assert rental["deployment_id"] == "d1" and rental["last_error"].endswith(" …") and len(rental["last_error"]) < 310
    assert result["isError"] is False and result["content"][0]["type"] == "text"


def test_api_errors_become_tool_errors_with_the_code(client):
    c, _, mp = client

    def full(*a, **k):
        raise coded(503, "no_capacity", {"message": "Every rtx4090 is rented"}, headers={"Retry-After": "60"})
    mp.setattr(routes, "create_workload", lambda *a, **k: {"workload_id": "w1"})
    mp.setattr(routes, "create_deployment", full)
    deleted = []
    mp.setattr(routes, "delete_workload", lambda workload_id, **k: deleted.append(workload_id))
    result = call(c, "rent_gpu", {"ssh_public_keys": ["ssh-ed25519 AAAA me@laptop"]})
    assert result["isError"] is True
    err = result["structuredContent"]["error"]
    assert (err["code"], err["retryable"], err["retry_after_seconds"]) == ("no_capacity", True, 60)
    assert deleted == ["w1"]  # nothing rented: the workload made for it is cleaned up


def test_rent_builds_the_quickstart_request_and_scopes_each_step(client):
    c, _, mp = client
    steps = []

    def wl(payload, response, authorization, x_api_key, idempotency_key):
        steps.append(("workload", current_request_route.get(), payload, idempotency_key))
        return {"workload_id": "w9"}

    def dep(payload, response, authorization, x_api_key, idempotency_key):
        steps.append(("deployment", current_request_route.get(), payload.model_dump(exclude_none=True), idempotency_key))
        return {"deployment_id": "d9", "state": "pending", "name": "rig-1"}
    mp.setattr(routes, "create_workload", wl)
    mp.setattr(routes, "create_deployment", dep)
    result = call(c, "rent_gpu", {"ssh_public_keys": ["ssh-ed25519 AAAA me"], "gpu_count": 2, "name": "rig-1",
                                  "command": "./mine.sh", "restart_policy": "always", "idempotency_key": "abc"})
    (_, wl_route, workload, wl_idem), (_, dep_route, body, dep_idem) = steps
    assert wl_route == ("POST", "/platform/workloads") and dep_route == ("POST", "/platform/deployments")
    req = workload["requirements"]
    assert (req["gpu_count"], req["supported_gpu_models"], req["min_vram_gb_per_gpu"], req["cpu_cores"]) == (2, ["rtx4090"], 24, 16)
    assert workload["metadata"]["command"] == "./mine.sh" and workload["metadata"]["restart_policy"] == "always"
    assert body["workload_id"] == "w9" and body["name"] == "rig-1"
    assert (wl_idem, dep_idem) == ("abc:workload", "abc")  # a retry never rents twice
    assert result["structuredContent"]["rental"]["deployment_id"] == "d9"


def test_get_rental_gives_ssh_from_the_rental_never_a_private_key(client):
    c, _, mp = client
    ed = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFDpfaWJh7dYs3/G3VaELhN36Tj0pY7oUj+Afdy7rk93"
    mp.setattr(routes, "get_deployment", lambda dep_id, **k: {
        "deployment_id": dep_id, "workload_id": "w1", "state": "ready", "endpoint": "ssh://root@1.2.3.4:30001",
        "ssh_host_keys": [ed]})

    def audited(*a, **k):
        raise AssertionError("the private-key route must not be called: it audit-logs a key hand-out")
    mp.setattr(routes, "get_deployment_ssh", audited)
    mp.setattr(routes, "_workload_has_own_ssh_keys", lambda workload_id: False)
    result = call(c, "get_rental", {"deployment_id": "d1"})
    ssh = result["structuredContent"]["ssh"]
    assert ssh["ssh_command"] == "ssh root@1.2.3.4 -p 30001"
    assert ssh["known_hosts"] == [f"[1.2.3.4]:30001 {ed}"]
    assert "platform made" in ssh["note"] and "PRIVATE KEY" not in result["content"][0]["text"]


def test_renting_needs_a_key_to_log_in_with_and_checks_everything_before_creating(client):
    c, _, mp = client
    created = []
    mp.setattr(routes, "create_workload", lambda *a, **k: created.append(1) or {"workload_id": "w1"})
    for args in ({}, {"ssh_public_keys": []}, {"ssh_public_keys": ["ssh-ed25519 AAAA"], "max_runtime_minutes": 0},
                 {"ssh_public_keys": ["ssh-ed25519 AAAA"], "name": "bad name!"}):
        result = call(c, "rent_gpu", args)
        assert result["isError"] is True and result["structuredContent"]["status"] == 400
    assert created == []  # nothing made for a request that was going to fail


def test_errors_carry_the_rest_detail_too(client):
    c, _, mp = client

    def refused(*a, **k):
        raise HTTPException(status_code=403, detail={"message": "this API key's scopes don't allow this request",
                                                     "required_scope": "rentals", "key_scopes": ["read"]})
    mp.setattr(routes, "terminate_deployment", refused)
    result = call(c, "stop_rental", {"deployment_id": "d1"})
    payload = result["structuredContent"]
    assert payload["error"]["code"] == "scope_required" and payload["detail"]["required_scope"] == "rentals"


def test_a_crash_is_ours_not_the_callers(client):
    c, _, mp = client
    mp.setattr(routes, "get_deployment_stats", lambda *a, **k: int("not a number"))
    payload = call(c, "rental_stats", {"deployment_id": "d1"})["structuredContent"]
    assert payload["status"] == 500 and "not a number" not in str(payload)


def test_notifications_need_a_key_too_and_bodies_are_capped(client):
    c, _, _ = client
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 401
    big = b'{"jsonrpc": "2.0", "method": "x", "params": {"p": "' + b"a" * (mcp_module.MAX_BODY_BYTES + 10) + b'"}}'
    r = c.post("/mcp", content=big, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_the_rate_limit_says_when_to_retry(client, monkeypatch):
    c, _, _ = client

    def limited(*a, **k):
        raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": "17"})
    monkeypatch.setattr(mcp_module, "enforce_rate_limit", limited)
    r = rpc(c, "ping")
    assert r.status_code == 429 and r.headers["Retry-After"] == "17"


def test_bad_arguments_are_a_tool_error_not_a_crash(client):
    c, _, _ = client
    result = call(c, "get_rental", {})
    assert result["isError"] is True and "deployment_id" in result["structuredContent"]["error"]["message"]
    result = call(c, "rental_logs", {"deployment_id": "d1", "lines": 99999})
    assert result["isError"] is True


@pytest.mark.parametrize("body, status, code", [
    (b"not json", 400, -32700),
    (b'[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]', 400, -32600),
    (b'{"jsonrpc": "1.0", "id": 1, "method": "ping"}', 400, -32600),
])
def test_malformed_requests(client, body, status, code):
    c, _, _ = client
    r = c.post("/mcp", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_unknown_methods_and_tools(client):
    c, _, _ = client
    assert rpc(c, "resources/list").json()["error"]["code"] == -32601
    assert rpc(c, "tools/call", {"name": "rm_rf"}).json()["error"]["code"] == -32602
    assert rpc(c, "ping").json()["result"] == {}
    assert c.get("/mcp").status_code == 405


def test_run_command_runs_as_the_exec_request(client):
    c, _, mp = client
    seen = []
    mp.setattr(routes, "run_in_deployment", lambda dep_id, payload, **k: seen.append(
        (current_request_route.get(), dep_id, payload.command, payload.timeout_seconds)) or {"exit_code": 0, "output": "ok"})
    result = call(c, "run_command", {"deployment_id": "d1", "command": "nvidia-smi", "timeout_seconds": 10})
    assert seen == [(("POST", "/platform/deployments/d1/exec"), "d1", "nvidia-smi", 10)]
    assert result["structuredContent"]["exit_code"] == 0
    assert call(c, "run_command", {"deployment_id": "d1"})["isError"] is True


def test_list_gpus_reads_the_real_price_list(client):
    """Against the real /platform/pricing builder, not a stand-in: the GPUs
    sit under gpu_rental.gpus (a stand-in hid that the tool read `gpus`)."""
    c, _, mp = client
    mp.setattr(routes, "_capacity_by_model", lambda: {"rtx4090": {"available_gpus": 5, "max_gpus_per_deployment": 3}})
    mp.setattr(routes, "_rentable_now", lambda: ["rtx4090"])
    mp.setattr(routes, "_public_inference_model_names", lambda: [])
    result = call(c, "list_gpus")
    assert result["isError"] is False, result
    gpus = result["structuredContent"]["gpus"]
    by_model = {g["gpu_model"]: g for g in gpus}
    assert by_model["rtx4090"]["available_gpus"] == 5 and by_model["rtx4090"]["max_gpus_per_deployment"] == 3
    assert by_model["rtx4090"]["usd_per_gpu_hour"] > 0

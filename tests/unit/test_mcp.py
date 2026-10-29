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
            "get_balance", "list_rentals", "alert_when_free"} == set(tools)
    assert tools["rent_gpu"]["inputSchema"]["required"] == ["ssh_public_keys"]
    assert tools["stop_rental"]["annotations"]["destructiveHint"] is True
    assert tools["list_gpus"]["annotations"]["readOnlyHint"] is True


def test_a_bad_key_is_refused_before_anything_runs(client):
    c, _, _ = client
    r = rpc(c, "tools/list", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == -32001


def test_the_key_check_at_the_door_ignores_scopes_and_each_tool_runs_as_its_rest_call(client):
    c, seen, mp = client
    calls = []

    def fake_list(state, authorization, x_api_key):
        calls.append((current_request_route.get(), state, authorization))
        return [{"deployment_id": "d1", "state": "ready", "name": "rig", "last_error": "x" * 400}]
    mp.setattr(routes, "list_deployments", fake_list)
    result = call(c, "list_rentals")
    assert seen["scope_route_at_auth"] is None
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


def test_get_rental_gives_ssh_but_never_a_private_key(client):
    c, _, mp = client
    mp.setattr(routes, "get_deployment", lambda dep_id, **k: {"deployment_id": dep_id, "state": "ready",
                                                               "endpoint": "ssh://root@1.2.3.4:30001"})
    seen = []

    def ssh(dep_id, **k):
        seen.append(current_request_route.get())
        return {"ssh_command": "ssh root@1.2.3.4 -p 30001", "ssh_host": "1.2.3.4", "ssh_port": 30001,
                "known_hosts": ["[1.2.3.4]:30001 ssh-ed25519 AAAA"], "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----"}
    mp.setattr(routes, "get_deployment_ssh", ssh)
    result = call(c, "get_rental", {"deployment_id": "d1"})
    assert "PRIVATE KEY" not in result["content"][0]["text"]
    assert result["structuredContent"]["ssh"]["ssh_command"] == "ssh root@1.2.3.4 -p 30001"
    assert seen == [("GET", "/platform/deployments/d1/ssh")]  # needs the rentals scope, like the REST call


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

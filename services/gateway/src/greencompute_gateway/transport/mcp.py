"""Green Compute as an MCP server (external API user, request #26: "every
agent user has to write their own integration").

POST /mcp speaks the Model Context Protocol's Streamable HTTP transport,
statelessly: each JSON-RPC request gets one JSON reply, notifications get 202,
there is no server-sent stream (GET is 405). Authenticate with the API key, as
`Authorization: Bearer <key>`.

Every tool is one or two of the REST calls an agent would make, called the
same way, so it gets exactly their checks, limits and error codes: a scoped
key (Settings → API keys) can do over MCP just what it can over REST, because
each call runs as that REST request. Errors come back as tool results with
`isError: true` and the API's `error` object, so the assistant can read
`code`, `message` and `retryable`.

A platform-made SSH private key is never put in a tool result: it would land
in the assistant's context. Pass your own public key to rent_gpu instead.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from greencompute_gateway.transport.errors import describe_error
from greencompute_gateway.transport.security import current_request_route, enforce_rate_limit, require_api_key

log = logging.getLogger(__name__)
router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "green-compute", "title": "Green Compute", "version": "1.0.0"}
INSTRUCTIONS = (
    "Rent NVIDIA GPUs by the minute (billed only while `ready`). Typical flow: list_gpus to see what is free "
    "and its price; rent_gpu with your SSH public key; poll get_rental until state is `ready` (usually 1-3 "
    "minutes), then SSH in with its ssh_command; stop_rental when done, which stops billing and deletes the "
    "disk. A 503 `no_capacity` error means nothing is free: retry later or call alert_when_free. "
    "recent_events lists every change since a cursor instead of polling each rental. Docs: "
    "https://www.green-compute.com/docs"
)
ACTIVE_STATES = "pending,scheduled,pulling,starting,ready,suspended"

# --- tools -----------------------------------------------------------------------


def _routes():
    from greencompute_gateway.transport import routes

    return routes


def _as(method: str, path: str, fn: Callable[[], Any]) -> Any:
    """Run `fn` as the REST request `method path`, so the key's scopes are
    checked exactly as they would be for that request."""
    token = current_request_route.set((method, path))
    try:
        return fn()
    finally:
        current_request_route.reset(token)


class ToolInputError(ValueError):
    """The tool's arguments are wrong; the message says how."""


def _str(args: dict, key: str, *, required: bool = False, default: str | None = None) -> str | None:
    value = args.get(key, default)
    if value is None:
        if required:
            raise ToolInputError(f"`{key}` is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"`{key}` must be a non-empty string")
    return value.strip()


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"`{key}` must be a whole number from {low} to {high}")
    return value


def _brief(dep: dict) -> dict:
    error = dep.get("last_error")
    return {
        "deployment_id": dep.get("deployment_id"),
        "name": dep.get("name"),
        "state": dep.get("state"),
        "workload_id": dep.get("workload_id"),
        "hourly_rate_cents_per_gpu": dep.get("hourly_rate_cents"),
        "created_at": dep.get("created_at"),
        "ready_at": dep.get("ready_at"),
        "endpoint": dep.get("endpoint"),
        "last_error": (error[:300] + " …") if isinstance(error, str) and len(error) > 300 else error,
    }


def t_list_gpus(auth: tuple, args: dict) -> dict:
    r = _routes()
    pricing = _as("GET", "/platform/pricing", r.public_pricing)
    return {"gpus": pricing.get("gpus", []),
            "note": "Prices are per GPU per hour, charged by the minute while a rental is ready."}


def t_get_balance(auth: tuple, args: dict) -> dict:
    r = _routes()
    return _as("GET", "/platform/billing/forecast", lambda: r.billing_forecast(authorization=auth[0], x_api_key=auth[1]))


def t_list_rentals(auth: tuple, args: dict) -> dict:
    r = _routes()
    state = _str(args, "state") or ACTIVE_STATES
    deps = _as("GET", "/platform/deployments",
               lambda: r.list_deployments(state=state, authorization=auth[0], x_api_key=auth[1]))
    return {"rentals": [_brief(d) for d in deps], "states": state}


def t_get_rental(auth: tuple, args: dict) -> dict:
    """SSH details come from the rental itself (its address and host keys,
    which the `read` scope may see), not from GET .../ssh: that route hands
    out the platform's private key, and every call to it is audit-logged as
    such, which an agent polling here would flood."""
    from greencompute_protocol.ssh_host_keys import known_hosts_line

    r = _routes()
    dep_id = _str(args, "deployment_id", required=True)
    dep = _as("GET", f"/platform/deployments/{dep_id}",
              lambda: r.get_deployment(dep_id, authorization=auth[0], x_api_key=auth[1]))
    out: dict[str, Any] = {"rental": _brief(dep)}
    endpoint = str(dep.get("endpoint") or "")
    if dep.get("state") == "ready" and endpoint.startswith("ssh://"):
        user_host = endpoint[len("ssh://"):]
        user, _, host_port = user_host.rpartition("@")
        host, _, port = host_port.rpartition(":")
        port_n = int(port) if port.isdigit() else 22
        user = user or "root"
        out["ssh"] = {
            "ssh_command": f"ssh {user}@{host} -p {port_n}",
            "ssh_host": host,
            "ssh_port": port_n,
            "known_hosts": [known_hosts_line(host, port_n, k) for k in dep.get("ssh_host_keys") or []],
        }
        if not r._workload_has_own_ssh_keys(dep.get("workload_id") or ""):
            out["ssh"]["note"] = (
                "This rental uses a key the platform made; it isn't shown here. Fetch it with "
                f"GET /platform/deployments/{dep_id}/ssh, or rent with your own ssh_public_keys."
            )
    elif dep.get("state") in ("pending", "scheduled", "pulling", "starting"):
        out["next"] = "Still starting: call get_rental again in 15-30 seconds."
    return out


def t_rent_gpu(auth: tuple, args: dict) -> dict:
    from pydantic import ValidationError

    from greencompute_gateway.application.services import GatewayDeploymentCreateRequest
    from greencompute_protocol import GPU_VRAM_GB

    r = _routes()
    # Every argument is checked before anything is created.
    gpu_model = "".join(ch for ch in (_str(args, "gpu_model", default="rtx4090") or "").lower() if ch.isalnum())
    gpu_count = _int(args, "gpu_count", 1, 1, 8)
    image = _str(args, "image", default="pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime")
    name = _str(args, "name")
    keys = args.get("ssh_public_keys")
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k.strip() for k in keys):
        raise ToolInputError(
            "`ssh_public_keys` needs at least one SSH public key, e.g. [\"ssh-ed25519 AAAA… me@laptop\"] "
            "(the contents of ~/.ssh/id_ed25519.pub); without one you couldn't log in")
    metadata: dict[str, Any] = {"ssh_public_keys": keys, "volume_size_gb": _int(args, "disk_gb", 50, 10, 500)}
    for key in ("command", "restart_policy", "env"):
        if args.get(key) is not None:
            metadata[key] = args[key]
    body: dict[str, Any] = {"workload_id": "pending"}
    if name:
        body["name"] = name
    if args.get("max_runtime_minutes") is not None:
        body["max_runtime_minutes"] = _int(args, "max_runtime_minutes", 60, 1, 60 * 24 * 30)
    try:
        GatewayDeploymentCreateRequest(**body)
    except ValidationError as exc:
        raise ToolInputError(f"invalid rental settings: {exc.errors()[0].get('msg')}") from exc
    workload = {
        "name": name or "mcp-rental",
        "kind": "pod",
        "image": image,
        "requirements": {
            "gpu_count": gpu_count,
            "supported_gpu_models": [gpu_model],
            "min_vram_gb_per_gpu": GPU_VRAM_GB.get(gpu_model, 24),
            "cpu_cores": 8 * gpu_count,
            "memory_gb": 32 * gpu_count,
        },
        "metadata": metadata,
    }
    idem = _str(args, "idempotency_key")
    wl = _as("POST", "/platform/workloads", lambda: r.create_workload(
        workload, Response(), authorization=auth[0], x_api_key=auth[1],
        idempotency_key=f"{idem}:workload" if idem else None))
    body["workload_id"] = wl["workload_id"]
    try:
        dep = _as("POST", "/platform/deployments", lambda: r.create_deployment(
            GatewayDeploymentCreateRequest(**body), Response(), authorization=auth[0], x_api_key=auth[1],
            idempotency_key=idem))
    except Exception:
        # Nothing was rented: don't leave the workload behind. (With an
        # idempotency key a retry must find the same workload, so it stays.)
        if not idem:
            try:
                _as("DELETE", f"/platform/workloads/{wl['workload_id']}",
                    lambda: r.delete_workload(wl["workload_id"], authorization=auth[0], x_api_key=auth[1]))
            except Exception:  # noqa: BLE001
                log.warning("mcp rent_gpu: could not remove workload %s after a failed rent", wl["workload_id"])
        raise
    return {"rental": _brief(dep), "next": "Starting. Call get_rental in 30 seconds; it is usually ready in 1-3 minutes."}


def t_stop_rental(auth: tuple, args: dict) -> dict:
    r = _routes()
    dep_id = _str(args, "deployment_id", required=True)
    dep = _as("DELETE", f"/platform/deployments/{dep_id}",
              lambda: r.terminate_deployment(dep_id, authorization=auth[0], x_api_key=auth[1]))
    return {"rental": _brief(dep), "note": "Stopped: billing has stopped and its disk is deleted."}


def t_rental_logs(auth: tuple, args: dict) -> dict:
    r = _routes()
    dep_id = _str(args, "deployment_id", required=True)
    lines = _int(args, "lines", 100, 1, 2000)
    return _as("GET", f"/platform/deployments/{dep_id}/logs",
               lambda: r.get_deployment_logs(dep_id, lines=lines, authorization=auth[0], x_api_key=auth[1]))


def t_rental_stats(auth: tuple, args: dict) -> dict:
    r = _routes()
    dep_id = _str(args, "deployment_id", required=True)
    return _as("GET", f"/platform/deployments/{dep_id}/stats",
               lambda: r.get_deployment_stats(dep_id, authorization=auth[0], x_api_key=auth[1]))


def t_recent_events(auth: tuple, args: dict) -> dict:
    r = _routes()
    after = _int(args, "after", 0, 0, 2**31 - 1)
    limit = _int(args, "limit", 50, 1, 500)
    types = _str(args, "types")
    return _as("GET", "/platform/events", lambda: r.list_account_events(
        after=after, limit=limit, types=types, authorization=auth[0], x_api_key=auth[1]))


def t_run_command(auth: tuple, args: dict) -> dict:
    from pydantic import ValidationError

    from greencompute_gateway.transport import schemas

    r = _routes()
    dep_id = _str(args, "deployment_id", required=True)
    try:
        payload = schemas.ExecRequest(command=_str(args, "command", required=True),
                                      timeout_seconds=_int(args, "timeout_seconds", 60, 1, 120))
    except ValidationError as exc:
        raise ToolInputError(f"invalid command: {exc.errors()[0].get('msg')}") from exc
    return _as("POST", f"/platform/deployments/{dep_id}/exec",
               lambda: r.run_in_deployment(dep_id, payload, authorization=auth[0], x_api_key=auth[1]))


def t_alert_when_free(auth: tuple, args: dict) -> dict:
    from greencompute_gateway.transport import schemas

    r = _routes()
    payload = schemas.CapacityAlertCreate(
        gpu_model=_str(args, "gpu_model", default="rtx4090") or "rtx4090",
        gpu_count=_int(args, "gpu_count", 1, 1, 8), count=_int(args, "count", 1, 1, 64))
    return _as("POST", "/platform/capacity-alerts",
               lambda: r.create_capacity_alert(payload, authorization=auth[0], x_api_key=auth[1]))


_ID = {"deployment_id": {"type": "string", "description": "The rental's id (deployment_id)."}}
TOOLS: list[dict[str, Any]] = [
    {"name": "list_gpus", "title": "List GPUs", "handler": t_list_gpus, "readOnly": True,
     "description": "GPU models for rent: how many are free now, the most free together on one machine, VRAM and the price per GPU-hour.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_balance", "title": "Balance and forecast", "handler": t_get_balance, "readOnly": True,
     "description": "Your balance, what everything running costs per hour, and how many hours the balance lasts.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_rentals", "title": "List rentals", "handler": t_list_rentals, "readOnly": True,
     "description": "Your rentals and private models. By default the ones running, starting or paused.",
     "inputSchema": {"type": "object", "properties": {"state": {
         "type": "string", "description": "Comma-separated states, e.g. `ready` or `terminated,failed`."}}}},
    {"name": "get_rental", "title": "Get a rental", "handler": t_get_rental, "readOnly": True,
     "description": "One rental's state and, once it is ready, how to SSH in (command and host keys).",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["deployment_id"]}},
    {"name": "rent_gpu", "title": "Rent a GPU", "handler": t_rent_gpu, "readOnly": False,
     "description": ("Start a GPU rental (a pod with SSH, billed by the minute once ready). Returns its deployment_id; "
                     "poll get_rental until `ready`. Fails with no_capacity (503) when nothing is free."),
     "inputSchema": {"type": "object", "properties": {
         "gpu_model": {"type": "string", "description": "e.g. `rtx4090` (default). See list_gpus."},
         "gpu_count": {"type": "integer", "minimum": 1, "maximum": 8, "description": "GPUs on one machine (default 1)."},
         "ssh_public_keys": {"type": "array", "items": {"type": "string"},
                             "description": "Your SSH public key(s), e.g. the contents of ~/.ssh/id_ed25519.pub."},
         "image": {"type": "string", "description": "Docker image (default a PyTorch CUDA 12.8 image)."},
         "name": {"type": "string", "description": "A name for it (letters, digits, . _ -); the pod gets it as GC_DEPLOYMENT_NAME."},
         "command": {"type": "string", "description": "Optional command to run when it starts (output in /workspace/greencompute-command.log)."},
         "restart_policy": {"type": "string", "enum": ["never", "on-failure", "always"], "description": "Run the command again when it exits."},
         "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Environment variables."},
         "max_runtime_minutes": {"type": "integer", "minimum": 1, "description": "Stop it automatically after this long."},
         "disk_gb": {"type": "integer", "minimum": 10, "maximum": 500, "description": "Disk at /workspace (default 50)."},
         "idempotency_key": {"type": "string", "description": "Send the same key when retrying, so a retry never rents twice."},
     }, "required": ["ssh_public_keys"]}},
    {"name": "stop_rental", "title": "Stop a rental", "handler": t_stop_rental, "readOnly": False, "destructive": True,
     "description": "Stop a rental for good: billing stops and its disk (/workspace) is deleted.",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["deployment_id"]}},
    {"name": "rental_logs", "title": "Rental output", "handler": t_rental_logs, "readOnly": True,
     "description": "The last lines of a running rental's start command log and container output.",
     "inputSchema": {"type": "object", "properties": {**_ID, "lines": {"type": "integer", "minimum": 1, "maximum": 2000}},
                     "required": ["deployment_id"]}},
    {"name": "rental_stats", "title": "Rental stats", "handler": t_rental_stats, "readOnly": True,
     "description": "Live readings of a running rental: CPU, memory, disk, and each GPU's use, temperature, power and clocks.",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["deployment_id"]}},
    {"name": "recent_events", "title": "Recent events", "handler": t_recent_events, "readOnly": True,
     "description": ("What happened since event `after`: rentals becoming ready, failing, pausing or ending, balance "
                     "low, GPUs free. Pass the returned next_after next time."),
     "inputSchema": {"type": "object", "properties": {
         "after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500},
         "types": {"type": "string", "description": "Comma-separated event types to keep."}}}},
    {"name": "run_command", "title": "Run a command", "handler": t_run_command, "readOnly": False, "destructive": True,
     "description": ("Run a shell command in a running rental, as root, from /workspace (no SSH needed). Returns its "
                     "exit code and the end of its output. Waits at most timeout_seconds (120); longer jobs: run them "
                     "in the background with their own log."),
     "inputSchema": {"type": "object", "properties": {
         **_ID, "command": {"type": "string", "description": "e.g. `nvidia-smi` or `./update.sh && pkill -f miner`."},
         "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120}},
         "required": ["deployment_id", "command"]}},
    {"name": "alert_when_free", "title": "Alert when free", "handler": t_alert_when_free, "readOnly": False,
     "description": "Be emailed once (and get a capacity.available event) when GPUs of a model are free. Lasts 7 days.",
     "inputSchema": {"type": "object", "properties": {
         "gpu_model": {"type": "string"}, "gpu_count": {"type": "integer", "minimum": 1, "maximum": 8},
         "count": {"type": "integer", "minimum": 1, "maximum": 64}}}},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def tool_list() -> list[dict[str, Any]]:
    out = []
    for t in TOOLS:
        out.append({
            "name": t["name"], "title": t["title"], "description": t["description"], "inputSchema": t["inputSchema"],
            "annotations": {"title": t["title"], "readOnlyHint": t["readOnly"],
                            "destructiveHint": bool(t.get("destructive")), "openWorldHint": False},
        })
    return out


def _result(payload: Any, *, error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, default=str, separators=(",", ":"))
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error}
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


def call_tool(auth: tuple, name: str, args: dict) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    tool = _BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    try:
        return _result(tool["handler"](auth, args))
    except HTTPException as exc:
        # As the REST call answers: its `error` object and its `detail`
        # (e.g. which scope a key lacks, or what is free).
        return _result({"status": exc.status_code, "detail": jsonable_encoder(exc.detail), "error": describe_error(
            exc.status_code, exc.detail, exc.headers, getattr(exc, "error_code", None))}, error=True)
    except ToolInputError as exc:
        return _result({"status": 400, "error": {"code": "invalid_request", "message": str(exc), "retryable": False}},
                       error=True)
    except Exception:  # noqa: BLE001 - our fault, not the caller's: logged, and said so
        log.exception("mcp tool %s failed", name)
        return _result({"status": 500, "error": describe_error(500, "internal server error")}, error=True)


# --- the endpoint ------------------------------------------------------------------


def _rpc_error(rpc_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}, status_code=status)


MAX_BODY_BYTES = 1024 * 1024


async def _body(request: Request) -> bytes | None:
    """The request body, or None past MAX_BODY_BYTES (read no further)."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_BODY_BYTES:
            return None
    return bytes(data)


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request) -> Response:
    # Who is calling, before anything else is read or answered. Any valid key
    # may connect; each tool then runs as its REST request, with its scope.
    authorization = request.headers.get("authorization")
    x_api_key = request.headers.get("x-api-key")
    try:
        api_key = await run_in_threadpool(require_api_key, authorization, x_api_key)
    except HTTPException as exc:
        body = {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": describe_error(
            exc.status_code, exc.detail, exc.headers, getattr(exc, "error_code", None))["message"]}}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
    try:
        enforce_rate_limit("mcp", api_key.key_id, limit=120, window_seconds=60)
    except HTTPException as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32002, "message": "rate limit: at most 120 MCP requests a minute; wait and try again"}},
            status_code=429, headers=exc.headers)

    raw = await _body(request)
    if raw is None:
        return _rpc_error(None, -32600, f"request too large (over {MAX_BODY_BYTES // 1024} KiB)", 413)
    try:
        message = json.loads(raw or b"null")
    except ValueError:
        return _rpc_error(None, -32700, "parse error: the body must be JSON", 400)
    if isinstance(message, list):
        return _rpc_error(None, -32600, "batches are not supported; send one JSON-RPC message per request", 400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid JSON-RPC request", 400)
    method, rpc_id = message["method"], message.get("id")
    if "id" not in message:  # a notification (e.g. notifications/initialized): nothing to answer
        return Response(status_code=202)
    creds = (authorization, x_api_key)  # the tools act with exactly what was checked

    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(rpc_id, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}})
    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tool_list()}})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or name not in _BY_NAME:
            return _rpc_error(rpc_id, -32602, f"unknown tool {name!r}; see tools/list")
        if not isinstance(args, dict):
            return _rpc_error(rpc_id, -32602, "arguments must be an object")
        result = await run_in_threadpool(call_tool, creds, name, args)
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    return _rpc_error(rpc_id, -32601, f"method not found: {method}")


@router.get("/mcp", include_in_schema=False)
def mcp_get() -> Response:
    """No server-to-client stream: everything is answered in the POST reply."""
    return Response(status_code=405, headers={"Allow": "POST"})

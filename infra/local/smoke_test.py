from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


GATEWAY_URL = os.getenv("GREENFERENCE_GATEWAY_URL", "http://127.0.0.1:8000")
CONTROL_PLANE_URL = os.getenv("GREENFERENCE_CONTROL_PLANE_URL", "http://127.0.0.1:8001")
VALIDATOR_URL = os.getenv("GREENFERENCE_VALIDATOR_URL", "http://127.0.0.1:8002")
BUILDER_URL = os.getenv("GREENFERENCE_BUILDER_URL", "http://127.0.0.1:8003")
MINER_URL = os.getenv("GREENFERENCE_MINER_URL", "http://127.0.0.1:8004")
NATS_MONITOR_URL = os.getenv("GREENFERENCE_NATS_MONITOR_URL", "http://127.0.0.1:8222/healthz")
TIMEOUT_SECONDS = float(os.getenv("GREENFERENCE_STACK_TIMEOUT_SECONDS", "60"))
MINER_HOTKEY = os.getenv("GREENFERENCE_MINER_HOTKEY", "miner-local")
MINER_NODE_ID = os.getenv("GREENFERENCE_MINER_NODE_ID", "node-local")
COMPOSE_FILE = os.getenv("GREENFERENCE_DOCKER_COMPOSE_FILE", "greenference-api/infra/local/docker-compose.yml")
RESTART_SERVICES = tuple(
    part.strip()
    for part in os.getenv("GREENFERENCE_STACK_RESTART_SERVICES", "control-plane,builder,miner-agent").split(",")
    if part.strip()
)


def _request_json(method: str, url: str, payload: dict | None = None, headers: dict[str, str] | None = None) -> dict:
    encoded = None if payload is None else json.dumps(payload).encode()
    req = request.Request(url=url, data=encoded, method=method)
    req.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with request.urlopen(req) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _request_text(method: str, url: str, payload: dict | None = None, headers: dict[str, str] | None = None) -> str:
    encoded = None if payload is None else json.dumps(payload).encode()
    req = request.Request(url=url, data=encoded, method=method)
    req.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with request.urlopen(req) as response:  # noqa: S310
        return response.read().decode()


def _wait_json(url: str, predicate, timeout: float = TIMEOUT_SECONDS, headers: dict[str, str] | None = None):
    deadline = time.time() + timeout
    last_error: str | None = None
    while time.time() < deadline:
        try:
            payload = _request_json("GET", url, headers=headers)
            if predicate(payload):
                return payload
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(1.0)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def _service_ready_payload(base_url: str, payload: dict[str, Any]) -> bool:
    if payload.get("status") != "ok":
        return False
    if base_url in {CONTROL_PLANE_URL, VALIDATOR_URL, BUILDER_URL}:
        if payload.get("bus_transport") != "nats":
            return False
        if not payload.get("worker_running"):
            return False
        if payload.get("worker_last_iteration") in {None, ""}:
            return False
    if base_url == MINER_URL:
        if not payload.get("worker_running"):
            return False
        if payload.get("worker_last_iteration") in {None, ""}:
            return False
        if not payload.get("bootstrapped"):
            return False
    return True


def wait_for_stack_readiness() -> None:
    print("waiting for service readiness")
    for base_url in [GATEWAY_URL, CONTROL_PLANE_URL, VALIDATOR_URL, BUILDER_URL, MINER_URL]:
        payload = _wait_json(f"{base_url}/readyz", lambda body: _service_ready_payload(base_url, body))
        print(f"ready: {base_url} -> {payload}")

    nats_health = _wait_json(NATS_MONITOR_URL, lambda body: body.get("status") == "ok")
    print(f"ready: nats -> {nats_health}")


def _register_admin() -> tuple[dict[str, str], dict[str, Any]]:
    user = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/register",
        {"username": "stack-admin", "email": "stack-admin@greenference.local"},
    )
    admin_key = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/api-keys",
        {"name": "stack-admin", "user_id": user["user_id"], "admin": True, "scopes": ["*"]},
    )
    return {"X-API-Key": admin_key["secret"]}, user


def run_happy_path() -> dict[str, Any]:
    headers, _user = _register_admin()

    _request_json(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/capabilities",
        {
            "hotkey": MINER_HOTKEY,
            "node_id": MINER_NODE_ID,
            "gpu_model": "a100",
            "gpu_count": 1,
            "available_gpus": 1,
            "vram_gb_per_gpu": 80,
            "cpu_cores": 32,
            "memory_gb": 128,
            "performance_score": 1.25,
        },
        headers={"X-Miner-Hotkey": MINER_HOTKEY},
    )

    build = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/images",
        {
            "image": "greenference/echo:stack",
            "context_uri": "s3://greenference/builds/stack.zip",
            "dockerfile_path": "Dockerfile",
            "public": False,
        },
        headers=headers,
    )
    print(f"build accepted: {build['build_id']}")

    builds = _wait_json(
        f"{GATEWAY_URL}/platform/images",
        lambda body: any(item["build_id"] == build["build_id"] and item["status"] == "published" for item in body),
    )
    published = next(item for item in builds if item["build_id"] == build["build_id"])
    print(f"build published: {published['artifact_uri']}")

    workload = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/workloads",
        {
            "name": "stack-echo-model",
            "image": published["image"],
            "requirements": {"gpu_count": 1, "min_vram_gb_per_gpu": 40},
        },
        headers=headers,
    )
    deployment = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/deployments",
        {"workload_id": workload["workload_id"], "requested_instances": 1},
        headers=headers,
    )
    print(f"deployment requested: {deployment['deployment_id']}")

    deployments = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments",
        lambda body: any(
            item["deployment_id"] == deployment["deployment_id"] and item["state"] == "ready" for item in body
        ),
        timeout=TIMEOUT_SECONDS,
        headers=headers,
    )
    ready = next(item for item in deployments if item["deployment_id"] == deployment["deployment_id"])
    print(f"deployment ready: {ready['endpoint']}")

    response = _request_json(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        {"model": workload["workload_id"], "messages": [{"role": "user", "content": "hello stack"}]},
        headers=headers,
    )
    print(f"inference response: {response['content']}")

    usage = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/usage",
        lambda body: deployment["deployment_id"] in body and body[deployment["deployment_id"]]["requests"] >= 1.0,
        timeout=TIMEOUT_SECONDS,
        headers=headers,
    )
    print(f"usage summary: {usage[deployment['deployment_id']]}")

    challenge = _request_json(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/probes/{MINER_HOTKEY}/{MINER_NODE_ID}",
        headers=headers,
    )
    scorecard = _request_json(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/probes/results",
        {
            "challenge_id": challenge["challenge_id"],
            "hotkey": MINER_HOTKEY,
            "node_id": MINER_NODE_ID,
            "latency_ms": 95.0,
            "throughput": 185.0,
            "success": True,
            "benchmark_signature": "stack-smoke",
            "proxy_suspected": False,
            "readiness_failures": 0,
        },
        headers={"X-Miner-Hotkey": MINER_HOTKEY},
    )
    snapshot = _request_json("POST", f"{VALIDATOR_URL}/validator/v1/weights", headers=headers)
    print(f"scorecard: {scorecard['final_score']}")
    print(f"weight snapshot: {snapshot['snapshot_id']}")

    return {
        "headers": headers,
        "workload": workload,
        "deployment": deployment,
        "response": response,
        "snapshot": snapshot,
        "usage": usage,
    }


def _restart_services(services: tuple[str, ...]) -> None:
    if not services:
        return
    subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", COMPOSE_FILE, "restart", *services],
        check=True,
    )


def _assert_metrics(headers: dict[str, str], deployment_id: str) -> None:
    gateway_metrics = _request_json("GET", f"{GATEWAY_URL}/platform/v1/metrics", headers=headers)
    control_plane_metrics = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/metrics", headers=headers)
    validator_metrics = _request_json("GET", f"{VALIDATOR_URL}/validator/v1/metrics", headers=headers)
    if gateway_metrics.get("invoke.success", 0) < 1:
        raise RuntimeError("gateway invoke.success metric did not increment")
    if control_plane_metrics.get("deployment.state.ready", 0) < 1:
        raise RuntimeError("control-plane ready metric did not increment")
    if validator_metrics.get("weights.published", 0) < 1:
        raise RuntimeError("validator weights.published metric did not increment")
    deployment_events = _request_json(
        "GET",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployment-events/{deployment_id}",
        headers=headers,
    )
    if not any(item["state"] == "ready" for item in deployment_events):
        raise RuntimeError("deployment ready event was not recorded")


def verify_recovery(context: dict[str, Any], restart_services: tuple[str, ...] = RESTART_SERVICES) -> None:
    _restart_services(restart_services)
    wait_for_stack_readiness()

    headers = context["headers"]
    workload = context["workload"]
    deployment = context["deployment"]

    deployments = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments",
        lambda body: any(
            item["deployment_id"] == deployment["deployment_id"] and item["state"] == "ready" for item in body
        ),
        timeout=TIMEOUT_SECONDS,
        headers=headers,
    )
    ready = next(item for item in deployments if item["deployment_id"] == deployment["deployment_id"])
    print(f"deployment recovered: {ready['endpoint']}")

    response = _request_json(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        {"model": workload["workload_id"], "messages": [{"role": "user", "content": "hello after restart"}]},
        headers=headers,
    )
    print(f"post-restart inference response: {response['content']}")

    usage = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/usage",
        lambda body: deployment["deployment_id"] in body and body[deployment["deployment_id"]]["requests"] >= 2.0,
        timeout=TIMEOUT_SECONDS,
        headers=headers,
    )
    print(f"post-restart usage summary: {usage[deployment['deployment_id']]}")
    _assert_metrics(headers, deployment["deployment_id"])


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    check_recovery = "--check-recovery" in args

    wait_for_stack_readiness()
    context = run_happy_path()
    _assert_metrics(context["headers"], context["deployment"]["deployment_id"])
    if check_recovery:
        verify_recovery(context)
    print("local stack smoke test passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"local stack smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

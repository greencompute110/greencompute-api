from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent / "greenference" / "protocol" / "src"))

from greenference_protocol import NodeCapability, ProbeResult  # noqa: E402

GATEWAY_URL = os.getenv("GREENFERENCE_GATEWAY_URL", "http://127.0.0.1:28000")
CONTROL_PLANE_URL = os.getenv("GREENFERENCE_CONTROL_PLANE_URL", "http://127.0.0.1:28001")
VALIDATOR_URL = os.getenv("GREENFERENCE_VALIDATOR_URL", "http://127.0.0.1:28002")
BUILDER_URL = os.getenv("GREENFERENCE_BUILDER_URL", "http://127.0.0.1:28003")
MINER_URL = os.getenv("GREENFERENCE_MINER_URL", "http://127.0.0.1:28004")
FAILOVER_MINER_URL = os.getenv("GREENFERENCE_FAILOVER_MINER_URL", "http://127.0.0.1:28005")
NATS_MONITOR_URL = os.getenv("GREENFERENCE_NATS_MONITOR_URL", "http://127.0.0.1:18222/healthz")
TIMEOUT_SECONDS = float(os.getenv("GREENFERENCE_STACK_TIMEOUT_SECONDS", "60"))
MINER_HOTKEY = os.getenv("GREENFERENCE_MINER_HOTKEY", "miner-local")
MINER_NODE_ID = os.getenv("GREENFERENCE_MINER_NODE_ID", "node-local")
MINER_AUTH_SECRET = os.getenv("GREENFERENCE_MINER_AUTH_SECRET", "greenference-miner-local-secret")
FAILOVER_MINER_HOTKEY = os.getenv("GREENFERENCE_FAILOVER_MINER_HOTKEY", "miner-failover")
FAILOVER_MINER_NODE_ID = os.getenv("GREENFERENCE_FAILOVER_MINER_NODE_ID", "node-failover")
FAILOVER_MINER_AUTH_SECRET = os.getenv(
    "GREENFERENCE_FAILOVER_MINER_AUTH_SECRET",
    "greenference-miner-failover-secret",
)
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


def _request_json_body(
    method: str,
    url: str,
    body_json: str,
    headers: dict[str, str] | None = None,
) -> dict:
    req = request.Request(url=url, data=body_json.encode(), method=method)
    req.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with request.urlopen(req) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _signed_miner_headers(hotkey: str, secret: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = f"{time.time_ns():x}"
    digest = hashlib.sha256(body).hexdigest()
    signature = hmac.new(
        secret.encode(),
        f"{hotkey}:{nonce}:{timestamp}:{digest}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Miner-Hotkey": hotkey,
        "X-Miner-Signature": signature,
        "X-Miner-Nonce": nonce,
        "X-Miner-Timestamp": timestamp,
    }


def _miner_headers(body: bytes) -> dict[str, str]:
    return _signed_miner_headers(MINER_HOTKEY, MINER_AUTH_SECRET, body)


def _failover_miner_headers(body: bytes) -> dict[str, str]:
    return _signed_miner_headers(FAILOVER_MINER_HOTKEY, FAILOVER_MINER_AUTH_SECRET, body)


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
    if base_url == BUILDER_URL and payload.get("build_execution_mode") != "live":
        return False
    if base_url == MINER_URL:
        if not payload.get("worker_running"):
            return False
        if payload.get("worker_last_iteration") in {None, ""}:
            return False
        if not payload.get("bootstrapped"):
            return False
        if payload.get("runtime_count") is None:
            return False
    return True


def wait_for_stack_readiness() -> None:
    print("waiting for service readiness")
    for base_url in [GATEWAY_URL, CONTROL_PLANE_URL, VALIDATOR_URL, BUILDER_URL, MINER_URL, FAILOVER_MINER_URL]:
        payload = _wait_json(f"{base_url}/readyz", lambda body: _service_ready_payload(base_url, body))
        print(f"ready: {base_url} -> {payload}")

    nats_health = _wait_json(NATS_MONITOR_URL, lambda body: body.get("status") == "ok")
    print(f"ready: nats -> {nats_health}")


def _register_admin() -> tuple[dict[str, str], dict[str, Any]]:
    suffix = f"{time.time_ns():x}"
    username = f"stack-admin-{suffix}"
    user = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/register",
        {"username": username, "email": f"{username}@greenference.local"},
    )
    admin_key = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/api-keys",
        {"name": username, "user_id": user["user_id"], "admin": True, "scopes": ["*"]},
    )
    return {"X-API-Key": admin_key["secret"]}, user


def _cleanup_active_deployments(headers: dict[str, str]) -> None:
    deployments = _request_json(
        "GET",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments",
        headers=headers,
    )
    for deployment in deployments:
        if deployment["state"] not in {"scheduled", "pulling", "starting", "ready"}:
            continue
        miner_url = None
        if deployment.get("hotkey") == MINER_HOTKEY:
            miner_url = MINER_URL
        elif deployment.get("hotkey") == FAILOVER_MINER_HOTKEY:
            miner_url = FAILOVER_MINER_URL
        if miner_url is not None:
            try:
                _request_json(
                    "POST",
                    f"{miner_url}/agent/v1/deployments/{deployment['deployment_id']}/terminate",
                )
            except HTTPError:
                pass
        _request_json(
            "POST",
            f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments/{deployment['deployment_id']}/cleanup",
            headers=headers,
        )


def run_happy_path() -> dict[str, Any]:
    headers, _user = _register_admin()
    _cleanup_active_deployments(headers)
    run_suffix = f"{time.time_ns():x}"
    workload_name = f"stack-echo-model-{run_suffix}"
    workload_alias = f"stack-echo-alias-{run_suffix}"
    ingress_host = f"{workload_alias}.greenference.local"

    capability_model = NodeCapability(
        hotkey=MINER_HOTKEY,
        node_id=MINER_NODE_ID,
        gpu_model="a100",
        gpu_count=1,
        available_gpus=1,
        vram_gb_per_gpu=80,
        cpu_cores=32,
        memory_gb=128,
        performance_score=1.25,
    )
    capability_json = capability_model.model_dump_json()
    _request_json_body(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/capabilities",
        capability_json,
        headers=_miner_headers(capability_json.encode()),
    )
    failover_capability_model = NodeCapability(
        hotkey=FAILOVER_MINER_HOTKEY,
        node_id=FAILOVER_MINER_NODE_ID,
        gpu_model="a100",
        gpu_count=1,
        available_gpus=1,
        vram_gb_per_gpu=80,
        cpu_cores=32,
        memory_gb=128,
        performance_score=1.1,
    )
    failover_capability_json = failover_capability_model.model_dump_json()
    _request_json_body(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/capabilities",
        failover_capability_json,
        headers=_failover_miner_headers(failover_capability_json.encode()),
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
        headers=headers,
    )
    published = next(item for item in builds if item["build_id"] == build["build_id"])
    print(f"build published: {published['artifact_uri']}")
    build_record = _request_json("GET", f"{GATEWAY_URL}/platform/builds/{build['build_id']}", headers=headers)
    image_history = _request_json(
        "GET",
        f"{GATEWAY_URL}/platform/images/{quote(published['image'], safe='')}/history",
        headers=headers,
    )
    if build_record["status"] != "published":
        raise RuntimeError("published build was not queryable through build detail endpoint")
    if not any(item["build_id"] == build["build_id"] for item in image_history):
        raise RuntimeError("published build missing from image history endpoint")

    workload = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/workloads",
        {
            "name": workload_name,
            "workload_alias": workload_alias,
            "ingress_host": ingress_host,
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

    runtime_records = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes")
    runtime = next((item for item in runtime_records if item["deployment_id"] == deployment["deployment_id"]), None)
    if runtime is None:
        raise RuntimeError("miner runtime record missing after reconcile")
    if runtime["status"] != "ready":
        raise RuntimeError("miner runtime did not reach ready status")
    if not runtime.get("staged_artifact_path"):
        raise RuntimeError("miner runtime missing staged artifact path")
    if runtime.get("backend_name") == "process-local-runtime":
        if not runtime.get("runtime_url") or not runtime.get("process_id"):
            raise RuntimeError("process-backed miner runtime missing runtime URL or process id")
    runtime_detail = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/{deployment['deployment_id']}")
    runtime_summary = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/summary")
    runtime_health = _request_json("GET", f"{MINER_URL}/deployments/{deployment['deployment_id']}/healthz")
    if runtime_detail["deployment_id"] != deployment["deployment_id"]:
        raise RuntimeError("miner runtime detail endpoint returned wrong deployment")
    if runtime_summary["total"] < 1 or runtime_summary["by_status"].get("ready", 0) < 1:
        raise RuntimeError("miner runtime summary did not capture ready runtime")
    if runtime_health["status"] != "ok":
        raise RuntimeError("miner deployment health did not reflect backend health")

    response = _request_json(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        {"model": "ignored-by-host-routing", "messages": [{"role": "user", "content": "hello stack"}]},
        headers={**headers, "Host": ingress_host},
    )
    if not str(response.get("content", "")).strip():
        raise RuntimeError("non-streaming inference returned empty content")
    print(f"inference response: {response['content']}")

    streamed = _request_text(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        {
            "model": workload["workload_id"],
            "messages": [{"role": "user", "content": "stream through runtime"}],
            "stream": True,
        },
        headers=headers,
    )
    if "data: [DONE]" not in streamed:
        raise RuntimeError("streamed inference did not finish cleanly")
    if not streamed.strip():
        raise RuntimeError("streamed inference returned empty output")

    invocations = _wait_json(
        f"{GATEWAY_URL}/platform/v1/invocations",
        lambda body: any(item["request_id"] == response["id"] for item in body),
        timeout=TIMEOUT_SECONDS,
        headers=headers,
    )
    invocation = next(item for item in invocations if item["request_id"] == response["id"])
    invocation_export = _request_json(
        "GET",
        f"{GATEWAY_URL}/platform/v1/invocations/exports/recent",
        headers=headers,
    )
    if invocation["status"] != "succeeded":
        raise RuntimeError("successful invocation missing from invocation records")
    if invocation_export["summary"]["count"] < 1:
        raise RuntimeError("invocation export did not include the routed request")

    routing = _request_json(
        "GET",
        f"{GATEWAY_URL}/platform/v1/debug/route/{workload_alias}?host={ingress_host}",
        headers=headers,
    )
    decisions = _request_json(
        "GET",
        f"{GATEWAY_URL}/platform/v1/debug/routing-decisions",
        headers=headers,
    )
    if routing.get("routing", {}).get("matched_by") != "ingress_host":
        raise RuntimeError("debug route did not resolve through ingress host")
    if not any(item.get("decision") == "selected" and item.get("matched_by") == "ingress_host" for item in decisions):
        raise RuntimeError("routing decision history did not capture ingress-host selection")

    usage = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/usage",
        lambda body: deployment["deployment_id"] in body and body[deployment["deployment_id"]]["requests"] >= 1.0,
        timeout=TIMEOUT_SECONDS,
        headers=headers,
    )
    print(f"usage summary: {usage[deployment['deployment_id']]}")

    servers = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/servers", headers=headers)
    nodes = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/nodes", headers=headers)
    placements = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/placements", headers=headers)
    if not servers or not nodes:
        raise RuntimeError("server or node inventory debug endpoints returned no data")
    if not any(item["deployment_id"] == deployment["deployment_id"] for item in placements):
        raise RuntimeError("placement history missing deployment assignment")

    challenge = _request_json(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/probes/{MINER_HOTKEY}/{MINER_NODE_ID}",
        headers=headers,
    )
    probe_result_model = ProbeResult(
        challenge_id=challenge["challenge_id"],
        hotkey=MINER_HOTKEY,
        node_id=MINER_NODE_ID,
        latency_ms=95.0,
        throughput=185.0,
        success=True,
        benchmark_signature="stack-smoke",
        proxy_suspected=False,
        readiness_failures=0,
    )
    probe_result_json = probe_result_model.model_dump_json()
    scorecard = _request_json_body(
        "POST",
        f"{VALIDATOR_URL}/validator/v1/probes/results",
        probe_result_json,
        headers=_miner_headers(probe_result_json.encode()),
    )
    snapshot = _request_json("POST", f"{VALIDATOR_URL}/validator/v1/weights", headers=headers)
    print(f"scorecard: {scorecard['final_score']}")
    print(f"weight snapshot: {snapshot['snapshot_id']}")

    return {
        "headers": headers,
        "workload": workload,
        "workload_alias": workload_alias,
        "ingress_host": ingress_host,
        "deployment": deployment,
        "build": build,
        "response": response,
        "runtime": runtime,
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
    gateway_prometheus = _request_text("GET", f"{GATEWAY_URL}/_metrics")
    control_plane_prometheus = _request_text("GET", f"{CONTROL_PLANE_URL}/_metrics")
    validator_prometheus = _request_text("GET", f"{VALIDATOR_URL}/_metrics")
    if gateway_metrics.get("invoke.success", 0) < 1 and "greenference_invoke_success" not in gateway_prometheus:
        raise RuntimeError("gateway invoke.success metric did not increment")
    if (
        control_plane_metrics.get("deployment.scheduled", 0) < 1
        and "greenference_deployment_scheduled" not in control_plane_prometheus
    ):
        raise RuntimeError("control-plane deployment.scheduled metric did not increment")
    if validator_metrics.get("weights.published", 0) < 1 and "greenference_weights_published" not in validator_prometheus:
        raise RuntimeError("validator weights.published metric did not increment")
    deployment_events = _request_json(
        "GET",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployment-events?deployment_id={deployment_id}",
        headers=headers,
    )
    if not any(item["state"] == "ready" for item in deployment_events):
        raise RuntimeError("deployment ready event was not recorded")


def _assert_operational_surfaces(headers: dict[str, str]) -> None:
    for base_url, expected_metric in [
        (GATEWAY_URL, "greenference_invoke_success"),
        (CONTROL_PLANE_URL, "greenference_deployment_scheduled"),
        (BUILDER_URL, "greenference_build_published"),
        (VALIDATOR_URL, "greenference_weights_published"),
    ]:
        body = _request_text("GET", f"{base_url}/_metrics")
        if expected_metric not in body:
            raise RuntimeError(f"missing {expected_metric} in metrics output for {base_url}")

    workers = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/workers", headers=headers)
    if not workers:
        raise RuntimeError("worker debug endpoint returned no workers")
    if not all("status_breakdown" in worker for worker in workers):
        raise RuntimeError("worker debug endpoint missing status breakdown")

    deliveries = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/event-deliveries", headers=headers)
    if not deliveries:
        raise RuntimeError("event deliveries debug endpoint returned no deliveries")

    status = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/status", headers=headers)
    if "workers" not in status or "unhealthy_miners" not in status:
        raise RuntimeError("operator status endpoint missing expected sections")


def verify_failures(headers: dict[str, str]) -> None:
    failing_build = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/images",
        {
            "image": "greenference/failing:stack",
            "context_uri": "s3://greenference/fail-once-object-store/builds/failing.zip",
            "dockerfile_path": "Dockerfile",
            "public": False,
        },
        headers=headers,
    )
    failed_build = _wait_json(
        f"{GATEWAY_URL}/platform/builds/{failing_build['build_id']}",
        lambda body: body["status"] == "failed",
        headers=headers,
    )
    if failed_build.get("failure_class") != "object_store_failure":
        raise RuntimeError("transient build failure did not surface object_store_failure")
    _request_json("POST", f"{GATEWAY_URL}/platform/builds/{failing_build['build_id']}/cleanup", headers=headers)
    retried = _request_json("POST", f"{GATEWAY_URL}/platform/builds/{failing_build['build_id']}/retry", headers=headers)
    if retried["retry_count"] < 1:
        raise RuntimeError("build retry count did not increment")
    published = _wait_json(
        f"{GATEWAY_URL}/platform/builds/{failing_build['build_id']}",
        lambda body: body["status"] == "published",
        headers=headers,
    )
    attempts = _request_json("GET", f"{GATEWAY_URL}/platform/builds/{failing_build['build_id']}/attempts", headers=headers)
    if len(attempts) < 2:
        raise RuntimeError("build attempts endpoint did not record multiple attempts")
    print(f"failed build recovered: {published['artifact_uri']}")

    exhausting_workload = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/workloads",
        {
            "name": "stack-unplaceable-model",
            "image": published["image"],
            "requirements": {"gpu_count": 2, "min_vram_gb_per_gpu": 80},
        },
        headers=headers,
    )
    deployment = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/deployments",
        {"workload_id": exhausting_workload["workload_id"], "requested_instances": 1},
        headers=headers,
    )
    retries = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployment-retries",
        lambda body: any(item["deployment_id"] == deployment["deployment_id"] and item["retry_exhausted"] for item in body),
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    lease_history = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/lease-history", headers=headers)
    drift = _request_json("GET", f"{CONTROL_PLANE_URL}/platform/v1/debug/miner-drift", headers=headers)
    build_failures = _request_json("GET", f"{GATEWAY_URL}/platform/v1/debug/build-failures", headers=headers)
    if not any(item["deployment_id"] == deployment["deployment_id"] for item in retries):
        raise RuntimeError("deployment retry exhaustion not visible")
    if drift.get("miners") is None or drift.get("nodes") is None:
        raise RuntimeError("miner drift endpoint missing expected sections")
    if not any(item["build_id"] == failing_build["build_id"] for item in build_failures):
        raise RuntimeError("build failures endpoint missing failed build")
    if not isinstance(lease_history, list):
        raise RuntimeError("lease history endpoint did not return a list")
    print(f"deployment retry exhaustion observed: {deployment['deployment_id']}")


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

    miner_ready = _request_json("GET", f"{MINER_URL}/readyz")
    runtime_detail = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/{deployment['deployment_id']}")
    if miner_ready.get("resumed_runtimes", 0) < 1:
        raise RuntimeError("miner recovery counters did not record resumed runtimes")
    if runtime_detail.get("recovery_count", 0) < 1:
        raise RuntimeError("runtime recovery count did not increase after restart")

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


def verify_failover(context: dict[str, Any]) -> None:
    headers = context["headers"]
    workload = context["workload"]
    deployment = context["deployment"]
    unhealthy_payload = {
        "hotkey": MINER_HOTKEY,
        "healthy": False,
        "active_deployments": 0,
        "active_leases": 0,
    }
    _request_json(
        "POST",
        f"{MINER_URL}/agent/v1/heartbeat",
        unhealthy_payload,
    )
    print("primary miner marked unhealthy")

    reassignments = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/reassignments",
        lambda body: any(item["payload"]["deployment_id"] == deployment["deployment_id"] for item in body),
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    print(f"reassignment observed: {reassignments[-1]['payload']}")

    miners = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/miners",
        lambda body: any(item["hotkey"] == MINER_HOTKEY and item["status"] == "unhealthy" for item in body),
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    print(f"miner health: {miners}")

    deployments = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments",
        lambda body: any(
            item["deployment_id"] == deployment["deployment_id"]
            and item["state"] == "ready"
            and item["hotkey"] == FAILOVER_MINER_HOTKEY
            for item in body
        ),
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    ready = next(item for item in deployments if item["deployment_id"] == deployment["deployment_id"])
    print(f"deployment failed over: {ready['hotkey']} -> {ready['endpoint']}")

    failover_runtime = _request_json("GET", f"{FAILOVER_MINER_URL}/agent/v1/runtimes/{deployment['deployment_id']}")
    primary_failed = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/failed")
    if failover_runtime.get("status") != "ready":
        raise RuntimeError("failover miner runtime did not reach ready state")
    if not any(item["deployment_id"] == deployment["deployment_id"] for item in primary_failed):
        raise RuntimeError("primary miner did not retain failed runtime visibility after failover")

    response = _request_json(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        {"model": workload["workload_id"], "messages": [{"role": "user", "content": "hello failover"}]},
        headers=headers,
    )
    if response["routed_hotkey"] != FAILOVER_MINER_HOTKEY:
        raise RuntimeError(f"expected failover hotkey {FAILOVER_MINER_HOTKEY}, got {response['routed_hotkey']}")
    print(f"post-failover inference response: {response['content']}")


def verify_miner_runtime(context: dict[str, Any]) -> None:
    deployment = context["deployment"]
    runtime_detail = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/{deployment['deployment_id']}")
    runtime_summary = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/summary")
    failed_runtimes = _request_json("GET", f"{MINER_URL}/agent/v1/runtimes/failed")
    if runtime_detail.get("status") != "ready":
        raise RuntimeError("runtime detail endpoint did not show ready runtime")
    if not runtime_detail.get("backend_name"):
        raise RuntimeError("runtime detail missing backend name")
    if not runtime_detail.get("artifact_uri"):
        raise RuntimeError("runtime detail missing artifact URI")
    if not runtime_detail.get("model_identifier"):
        raise RuntimeError("runtime detail missing model identifier")
    if runtime_detail.get("runtime_mode") not in {"process", "fallback"}:
        raise RuntimeError("runtime detail missing runtime mode")
    runtime_manifest = runtime_detail.get("metadata", {}).get("runtime_manifest", {})
    if runtime_manifest.get("runtime_kind") != "hf-causal-lm":
        raise RuntimeError("runtime detail did not use the default GPU-serving runtime kind")
    if runtime_summary.get("by_mode", {}).get(runtime_detail["runtime_mode"], 0) < 1:
        raise RuntimeError("runtime summary missing runtime mode breakdown")
    if runtime_summary.get("by_backend", {}).get(runtime_detail["backend_name"], 0) < 1:
        raise RuntimeError("runtime summary missing backend breakdown")
    if runtime_detail.get("backend_name") == "process-local-runtime":
        if not runtime_detail.get("runtime_url") or not runtime_detail.get("process_id"):
            raise RuntimeError("runtime detail missing process-backed runtime metadata")
    if runtime_summary.get("by_status", {}).get("ready", 0) < 1:
        raise RuntimeError("runtime summary missing ready runtime count")
    if not isinstance(failed_runtimes, list):
        raise RuntimeError("failed runtimes endpoint did not return a list")


def verify_operator_actions(context: dict[str, Any]) -> None:
    headers = context["headers"]
    deployment = context["deployment"]

    cancel_build = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/images",
        {
            "image": "greenference/operator-cancel:stack",
            "context_uri": "s3://greenference/builds/operator-cancel.zip",
            "dockerfile_path": "Dockerfile",
            "public": False,
        },
        headers=headers,
    )
    cancelled = _request_json(
        "POST",
        f"{GATEWAY_URL}/platform/builds/{cancel_build['build_id']}/cancel",
        headers=headers,
    )
    logs = _request_json(
        "GET",
        f"{GATEWAY_URL}/platform/builds/{cancel_build['build_id']}/logs",
        headers=headers,
    )
    if cancelled["status"] != "cancelled" or not any(item["stage"] == "cancelled" for item in logs):
        raise RuntimeError("build cancellation was not persisted through operator surfaces")

    drained = _request_json(
        "POST",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/miners/{MINER_HOTKEY}/drain",
        headers=headers,
    )
    exclusions = _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/placement-exclusions",
        lambda body: any(item["reason"] == "miner_drained" and item["hotkey"] == MINER_HOTKEY for item in body),
        headers=headers,
    )
    if drained["drained"] is not True:
        raise RuntimeError("miner drain did not persist")

    requeued = _request_json(
        "POST",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments/{deployment['deployment_id']}/requeue",
        headers=headers,
    )
    if requeued["state"] != "pending":
        raise RuntimeError("deployment requeue did not move deployment back to pending")

    _wait_json(
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments",
        lambda body: any(item["deployment_id"] == deployment["deployment_id"] and item["state"] == "ready" for item in body),
        headers=headers,
    )

    failed = _request_json(
        "POST",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployments/{deployment['deployment_id']}/fail",
        headers=headers,
    )
    failure_report = _request_json(
        "GET",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/deployment-failures",
        headers=headers,
    )
    if failed["state"] != "failed" or not any(item["deployment_id"] == deployment["deployment_id"] for item in failure_report):
        raise RuntimeError("deployment force-fail was not reflected in failure surfaces")

    undrained = _request_json(
        "POST",
        f"{CONTROL_PLANE_URL}/platform/v1/debug/miners/{MINER_HOTKEY}/undrain",
        headers=headers,
    )
    if undrained["drained"] is not False or not exclusions:
        raise RuntimeError("miner undrain did not complete")


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    check_recovery = "--check-recovery" in args
    check_failover = "--check-failover" in args
    check_ops = "--check-ops" in args
    check_failures = "--check-failures" in args
    check_operator_actions = "--check-operator-actions" in args
    check_miner_runtime = "--check-miner-runtime" in args

    wait_for_stack_readiness()
    context = run_happy_path()
    _assert_metrics(context["headers"], context["deployment"]["deployment_id"])
    if check_miner_runtime:
        verify_miner_runtime(context)
    if check_ops:
        _assert_operational_surfaces(context["headers"])
    if check_failures:
        verify_failures(context["headers"])
    if check_operator_actions:
        verify_operator_actions(context)
    if check_failover:
        verify_failover(context)
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

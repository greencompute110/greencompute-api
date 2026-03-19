from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "infra" / "local" / "smoke_test.py"
SPEC = importlib.util.spec_from_file_location("greenference_local_smoke_test", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
SMOKE_TEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE_TEST)


def test_service_ready_payload_accepts_supported_bus_transports_for_api_workers() -> None:
    payload = {
        "status": "ok",
        "bus_transport": "durable",
        "build_execution_mode": "live",
        "worker_running": True,
        "worker_last_iteration": 1.0,
    }

    assert SMOKE_TEST._service_ready_payload(SMOKE_TEST.CONTROL_PLANE_URL, payload) is True
    assert SMOKE_TEST._service_ready_payload(SMOKE_TEST.BUILDER_URL, payload) is True
    assert SMOKE_TEST._service_ready_payload(SMOKE_TEST.VALIDATOR_URL, payload) is True


def test_service_ready_payload_rejects_missing_worker_state() -> None:
    assert (
        SMOKE_TEST._service_ready_payload(
            SMOKE_TEST.CONTROL_PLANE_URL,
            {
                "status": "ok",
                "bus_transport": "durable",
                "build_execution_mode": "live",
                "worker_running": False,
                "worker_last_iteration": None,
            },
        )
        is False
    )
    assert (
        SMOKE_TEST._service_ready_payload(
            SMOKE_TEST.MINER_URL,
            {"status": "ok", "worker_running": True, "worker_last_iteration": "now", "bootstrapped": False},
        )
        is False
    )


def test_service_ready_payload_rejects_builder_without_live_execution() -> None:
    assert (
        SMOKE_TEST._service_ready_payload(
            SMOKE_TEST.BUILDER_URL,
            {
                "status": "ok",
                "bus_transport": "durable",
                "build_execution_mode": "simulated",
                "worker_running": True,
                "worker_last_iteration": 1.0,
            },
        )
        is False
    )


def test_service_ready_payload_accepts_builder_with_execution_details() -> None:
    assert (
        SMOKE_TEST._service_ready_payload(
            SMOKE_TEST.BUILDER_URL,
            {
                "status": "ok",
                "bus_transport": "durable",
                "build_execution_mode": "live",
                "builder_runner": "AdapterBackedBuildRunner",
                "builder_object_store_adapter": "S3CompatibleObjectStoreAdapter",
                "builder_registry_adapter": "OCIRegistryAdapter",
                "pending_delivery_count": 0.0,
                "failed_delivery_count": 0.0,
                "worker_running": True,
                "worker_last_iteration": 1.0,
            },
        )
        is True
    )


def test_main_runs_recovery_when_flag_present(monkeypatch) -> None:
    calls: list[str] = []

    def fake_wait() -> None:
        calls.append("wait")

    def fake_run_happy_path() -> dict:
        calls.append("happy")
        return {"headers": {}, "deployment": {"deployment_id": "dep-1"}}

    def fake_assert_metrics(headers: dict, deployment_id: str) -> None:
        calls.append(f"metrics:{deployment_id}")

    def fake_verify(context: dict) -> None:
        calls.append(f"recovery:{context['deployment']['deployment_id']}")

    monkeypatch.setattr(SMOKE_TEST, "wait_for_stack_readiness", fake_wait)
    monkeypatch.setattr(SMOKE_TEST, "run_happy_path", fake_run_happy_path)
    monkeypatch.setattr(SMOKE_TEST, "_assert_metrics", fake_assert_metrics)
    monkeypatch.setattr(SMOKE_TEST, "verify_recovery", fake_verify)

    result = SMOKE_TEST.main(["--check-recovery"])

    assert result == 0
    assert calls == ["wait", "happy", "metrics:dep-1", "recovery:dep-1"]


def test_main_runs_failover_when_flag_present(monkeypatch) -> None:
    calls: list[str] = []

    def fake_wait() -> None:
        calls.append("wait")

    def fake_run_happy_path() -> dict:
        calls.append("happy")
        return {"headers": {}, "deployment": {"deployment_id": "dep-1"}}

    def fake_assert_metrics(headers: dict, deployment_id: str) -> None:
        calls.append(f"metrics:{deployment_id}")

    def fake_failover(context: dict) -> None:
        calls.append(f"failover:{context['deployment']['deployment_id']}")

    monkeypatch.setattr(SMOKE_TEST, "wait_for_stack_readiness", fake_wait)
    monkeypatch.setattr(SMOKE_TEST, "run_happy_path", fake_run_happy_path)
    monkeypatch.setattr(SMOKE_TEST, "_assert_metrics", fake_assert_metrics)
    monkeypatch.setattr(SMOKE_TEST, "verify_failover", fake_failover)

    result = SMOKE_TEST.main(["--check-failover"])

    assert result == 0
    assert calls == ["wait", "happy", "metrics:dep-1", "failover:dep-1"]


def test_wait_json_retries_transient_oserror(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_request_json(method: str, url: str, payload=None, headers=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return {"status": "ok"}

    monkeypatch.setattr(SMOKE_TEST, "_request_json", fake_request_json)

    payload = SMOKE_TEST._wait_json("http://example.test/readyz", lambda body: body.get("status") == "ok", timeout=2)

    assert payload == {"status": "ok"}
    assert calls["count"] == 2


def test_main_runs_operational_checks_when_flag_present(monkeypatch) -> None:
    calls: list[str] = []

    def fake_wait() -> None:
        calls.append("wait")

    def fake_run_happy_path() -> dict:
        calls.append("happy")
        return {"headers": {}, "deployment": {"deployment_id": "dep-1"}}

    def fake_assert_metrics(headers: dict, deployment_id: str) -> None:
        calls.append(f"metrics:{deployment_id}")

    def fake_assert_ops(headers: dict) -> None:
        calls.append("ops")

    monkeypatch.setattr(SMOKE_TEST, "wait_for_stack_readiness", fake_wait)
    monkeypatch.setattr(SMOKE_TEST, "run_happy_path", fake_run_happy_path)
    monkeypatch.setattr(SMOKE_TEST, "_assert_metrics", fake_assert_metrics)
    monkeypatch.setattr(SMOKE_TEST, "_assert_operational_surfaces", fake_assert_ops)

    result = SMOKE_TEST.main(["--check-ops"])

    assert result == 0
    assert calls == ["wait", "happy", "metrics:dep-1", "ops"]


def test_main_runs_failure_checks_when_flag_present(monkeypatch) -> None:
    calls: list[str] = []

    def fake_wait() -> None:
        calls.append("wait")

    def fake_run_happy_path() -> dict:
        calls.append("happy")
        return {"headers": {}, "deployment": {"deployment_id": "dep-1"}}

    def fake_assert_metrics(headers: dict, deployment_id: str) -> None:
        calls.append(f"metrics:{deployment_id}")

    def fake_failures(headers: dict) -> None:
        calls.append("failures")

    monkeypatch.setattr(SMOKE_TEST, "wait_for_stack_readiness", fake_wait)
    monkeypatch.setattr(SMOKE_TEST, "run_happy_path", fake_run_happy_path)
    monkeypatch.setattr(SMOKE_TEST, "_assert_metrics", fake_assert_metrics)
    monkeypatch.setattr(SMOKE_TEST, "verify_failures", fake_failures)

    result = SMOKE_TEST.main(["--check-failures"])

    assert result == 0
    assert calls == ["wait", "happy", "metrics:dep-1", "failures"]


def test_main_runs_operator_action_checks_when_flag_present(monkeypatch) -> None:
    calls: list[str] = []

    def fake_wait() -> None:
        calls.append("wait")

    def fake_run_happy_path() -> dict:
        calls.append("happy")
        return {"headers": {}, "deployment": {"deployment_id": "dep-1"}}

    def fake_assert_metrics(headers: dict, deployment_id: str) -> None:
        calls.append(f"metrics:{deployment_id}")

    def fake_operator_actions(context: dict) -> None:
        calls.append(f"operator:{context['deployment']['deployment_id']}")

    monkeypatch.setattr(SMOKE_TEST, "wait_for_stack_readiness", fake_wait)
    monkeypatch.setattr(SMOKE_TEST, "run_happy_path", fake_run_happy_path)
    monkeypatch.setattr(SMOKE_TEST, "_assert_metrics", fake_assert_metrics)
    monkeypatch.setattr(SMOKE_TEST, "verify_operator_actions", fake_operator_actions)

    result = SMOKE_TEST.main(["--check-operator-actions"])

    assert result == 0
    assert calls == ["wait", "happy", "metrics:dep-1", "operator:dep-1"]


def test_main_runs_miner_runtime_checks_when_flag_present(monkeypatch) -> None:
    calls: list[str] = []

    def fake_wait() -> None:
        calls.append("wait")

    def fake_run_happy_path() -> dict:
        calls.append("happy")
        return {"headers": {}, "deployment": {"deployment_id": "dep-1"}}

    def fake_assert_metrics(headers: dict, deployment_id: str) -> None:
        calls.append(f"metrics:{deployment_id}")

    def fake_runtime(context: dict) -> None:
        calls.append(f"runtime:{context['deployment']['deployment_id']}")

    monkeypatch.setattr(SMOKE_TEST, "wait_for_stack_readiness", fake_wait)
    monkeypatch.setattr(SMOKE_TEST, "run_happy_path", fake_run_happy_path)
    monkeypatch.setattr(SMOKE_TEST, "_assert_metrics", fake_assert_metrics)
    monkeypatch.setattr(SMOKE_TEST, "verify_miner_runtime", fake_runtime)

    result = SMOKE_TEST.main(["--check-miner-runtime"])

    assert result == 0
    assert calls == ["wait", "happy", "metrics:dep-1", "runtime:dep-1"]


def test_cleanup_active_deployments_terminates_miner_before_control_plane(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_request_json(method: str, url: str, payload=None, headers=None):  # noqa: ANN001
        calls.append((method, url))
        if url.endswith("/platform/v1/debug/deployments"):
            return [
                {
                    "deployment_id": "dep-1",
                    "state": "ready",
                    "hotkey": SMOKE_TEST.MINER_HOTKEY,
                },
                {
                    "deployment_id": "dep-2",
                    "state": "failed",
                    "hotkey": SMOKE_TEST.FAILOVER_MINER_HOTKEY,
                },
            ]
        return {"status": "ok"}

    monkeypatch.setattr(SMOKE_TEST, "_request_json", fake_request_json)

    SMOKE_TEST._cleanup_active_deployments({"X-API-Key": "secret"})

    assert calls == [
        ("GET", f"{SMOKE_TEST.CONTROL_PLANE_URL}/platform/v1/debug/deployments"),
        ("POST", f"{SMOKE_TEST.MINER_URL}/agent/v1/deployments/dep-1/terminate"),
        ("POST", f"{SMOKE_TEST.CONTROL_PLANE_URL}/platform/v1/debug/deployments/dep-1/cleanup"),
    ]

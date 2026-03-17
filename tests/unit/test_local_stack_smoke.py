from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "infra" / "local" / "smoke_test.py"
SPEC = importlib.util.spec_from_file_location("greenference_local_smoke_test", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
SMOKE_TEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE_TEST)


def test_service_ready_payload_requires_nats_for_api_workers() -> None:
    payload = {
        "status": "ok",
        "bus_transport": "nats",
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
                "bus_transport": "nats",
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
                "bus_transport": "nats",
                "build_execution_mode": "simulated",
                "worker_running": True,
                "worker_last_iteration": 1.0,
            },
        )
        is False
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

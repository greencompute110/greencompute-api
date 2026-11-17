"""A private model that was serving and then FAILED its health checks is
redeployed at once (a new deployment of the same workload), instead of
staying failed until its owner deletes and redeploys it. Shared catalog
models are Flux's to replace."""
import threading
from datetime import UTC, datetime, timedelta

import pytest
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.config import settings
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import UserORM
from greencompute_protocol import DeploymentRecord, DeploymentState, WorkloadKind, WorkloadSpec


@pytest.fixture
def cp(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'replace.db'}"
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)  # BillingRepository() reads balances from here
    repo = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", email="u1@x.io", balance_credits=1000))
        s.add(UserORM(user_id="broke", username="broke", email="b@x.io", balance_credits=10))
    return ControlPlaneService(repository=repo)


def private_model(cp, owner="u1", gpus=1):
    return cp.repository.upsert_workload(WorkloadSpec(
        name="meta-llama-Llama-3.1-8B-Instruct", image="vllm", kind=WorkloadKind.INFERENCE, owner_user_id=owner,
        requirements={"gpu_count": gpus}, runtime={"model_identifier": "meta-llama/Llama-3.1-8B-Instruct"},
    ))


def serving(cp, workload, owner="u1", **fields):
    values = {"ready_at": datetime.now(UTC) - timedelta(minutes=20), "hourly_rate_cents": 40, "hotkey": "hk",
              "node_id": "n1", "name": "my-llama", "created_by_key_id": "k1", **fields}
    return cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id=owner, state=DeploymentState.READY, ready_instances=1,
        **values,
    ))


def fail(cp, deployment):
    for _ in range(settings.deployment_health_failure_threshold):
        out = cp.record_deployment_health_failure(deployment.deployment_id, "upstream timed out for deployment=x")
    return out


def others(cp, workload, deployment):
    return [d for d in cp.repository.list_deployments_for_workload(workload.workload_id)
            if d.deployment_id != deployment.deployment_id]


def test_a_private_model_that_stops_answering_is_redeployed(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    failed = fail(cp, dep)
    assert failed.state == DeploymentState.FAILED and failed.failure_class == "health_check_failure"
    [new] = others(cp, workload, dep)
    assert new.state == DeploymentState.PENDING  # placement takes it from here
    assert (new.owner_user_id, new.name, new.created_by_key_id) == ("u1", "my-llama", "k1")
    assert f"Restarted automatically as deployment {new.deployment_id}" in failed.last_error
    assert cp.repository.get_deployment(dep.deployment_id).last_error == failed.last_error
    assert new.hourly_rate_cents <= 40  # never dearer than what its owner paid


def test_two_requests_failing_at_once_replace_it_once(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    for _ in range(settings.deployment_health_failure_threshold - 1):
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    start = threading.Barrier(4)

    def hit():
        start.wait()
        cp.record_deployment_health_failure(dep.deployment_id, "x")
    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(others(cp, workload, dep)) == 1


def test_one_that_never_served_isnt_replaced(cp):
    """A start problem (wrong model, no room for its context) would only fail again."""
    workload = private_model(cp)
    dep = serving(cp, workload, ready_at=None)
    fail(cp, dep)
    assert others(cp, workload, dep) == []


def test_shared_catalog_models_are_left_to_flux(cp):
    workload = cp.repository.upsert_workload(WorkloadSpec(
        workload_id="catalog-qwen", name="qwen", image="vllm", kind=WorkloadKind.INFERENCE, public=True,
        requirements={"gpu_count": 1}, metadata={"managed_by": "flux", "catalog_model_id": "qwen"}))
    dep = serving(cp, workload, owner=None)
    fail(cp, dep)
    assert others(cp, workload, dep) == []


def test_not_while_another_of_its_deployments_is_live(cp):
    workload = private_model(cp)
    dep = serving(cp, workload)
    starting = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.STARTING))
    fail(cp, dep)
    assert [d.deployment_id for d in others(cp, workload, dep)] == [starting.deployment_id]


def test_at_most_a_few_times_an_hour_then_its_owner_is_told(cp):
    workload = private_model(cp)
    replaced = 0
    dep = serving(cp, workload)
    for _ in range(settings.private_model_auto_replace_per_hour + 1):
        failed = fail(cp, dep)
        live = [d for d in others(cp, workload, dep) if d.state == DeploymentState.PENDING]
        if not live:
            break
        replaced += 1
        # The replacement comes up and serves, then crashes too.
        cp.repository.update_deployment(live[0].model_copy(update={
            "state": DeploymentState.READY, "ready_at": datetime.now(UTC), "hourly_rate_cents": 40}))
        dep = cp.repository.get_deployment(live[0].deployment_id)
    assert replaced == settings.private_model_auto_replace_per_hour
    assert "Not restarted automatically: it failed 4 times in the last hour" in failed.last_error


def test_not_without_an_hour_of_credit(cp):
    workload = private_model(cp, owner="broke")
    dep = serving(cp, workload, owner="broke")
    failed = fail(cp, dep)
    assert others(cp, workload, dep) == []
    assert "Not restarted automatically: the balance doesn't cover an hour of it" in failed.last_error


def test_the_replacement_gets_what_is_left_of_its_runtime_cap(cp):
    workload = private_model(cp)
    dep = serving(cp, workload, max_runtime_minutes=60, ready_at=datetime.now(UTC) - timedelta(minutes=45))
    fail(cp, dep)
    [new] = others(cp, workload, dep)
    assert new.max_runtime_minutes in (14, 15)


def test_none_once_its_runtime_cap_is_used_up(cp):
    workload = private_model(cp)
    dep = serving(cp, workload, max_runtime_minutes=30, ready_at=datetime.now(UTC) - timedelta(minutes=45))
    fail(cp, dep)
    assert others(cp, workload, dep) == []


def test_it_can_be_turned_off(cp, monkeypatch):
    monkeypatch.setattr(settings, "private_model_auto_replace_per_hour", 0)
    workload = private_model(cp)
    dep = serving(cp, workload)
    fail(cp, dep)
    assert others(cp, workload, dep) == []

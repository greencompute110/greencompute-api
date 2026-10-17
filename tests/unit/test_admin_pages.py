"""Admin page fixes (2026-10-17): Network Health and All Deployments took
minutes because the routing report checked all 7k deployments with several
queries each and the deployment list carried 14 MB of container logs;
capacity history had grown to 42M rows. Plus per-GPU-class usage for the
analytics page and the account behind each catalog submission."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.infrastructure.analytics_repository import AnalyticsRepository
from greencompute_gateway.transport import routes
from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    CapacityHistoryORM,
    DeploymentORM,
    NodeInventoryORM,
    UserORM,
    WorkloadORM,
)
from greencompute_protocol import (
    CapacityUpdate,
    CatalogSubmission,
    DeploymentRecord,
    DeploymentState,
    NodeCapability,
    WorkloadSpec,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def cp():
    return ControlPlaneService(repository=ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True))


def _deployment(cp, state, error=None, updated=NOW):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="w", image="img", kind="pod"))
    return cp.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, state=state, last_error=error, updated_at=updated,
    ))


def test_routing_report_leaves_finished_deployments_out(cp):
    live = _deployment(cp, DeploymentState.READY)
    for _ in range(30):
        _deployment(cp, DeploymentState.TERMINATED)
    _deployment(cp, DeploymentState.FAILED, error="x" * 50_000)
    report = cp.routing_eligibility_report(now=NOW)
    assert [r["deployment_id"] for r in report] == [live.deployment_id]


def test_failure_report_is_newest_first_with_errors_capped(cp):
    old = _deployment(cp, DeploymentState.FAILED, error="old", updated=NOW - timedelta(days=2))
    new = _deployment(cp, DeploymentState.FAILED, error="y" * 50_000, updated=NOW)
    report = cp.deployment_failure_report()
    assert [r["deployment_id"] for r in report] == [new.deployment_id, old.deployment_id]
    assert len(report[0]["last_error"]) < 2100


def _post(cp, available, when):
    cp.repository.upsert_capacity(CapacityUpdate(hotkey="hk", observed_at=when, nodes=[NodeCapability(
        hotkey="hk", node_id="n1", gpu_model="rtx4090", gpu_count=8, available_gpus=available,
        vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256, observed_at=when)]))


def _history_rows(cp):
    with session_scope(cp.repository.session_factory) as s:
        return s.query(CapacityHistoryORM).count()


def test_capacity_history_records_changes_not_every_post(cp):
    for i in range(20):
        _post(cp, 8, NOW + timedelta(seconds=5 * i))  # same numbers, every 5 s
    assert _history_rows(cp) == 1
    _post(cp, 6, NOW + timedelta(minutes=2))  # a change is recorded
    assert _history_rows(cp) == 2
    _post(cp, 6, NOW + timedelta(minutes=13))  # and an unchanged node every 10 min
    assert _history_rows(cp) == 3


def test_old_capacity_history_is_pruned_in_batches(cp):
    with session_scope(cp.repository.session_factory) as s:
        for i in range(25):
            s.add(CapacityHistoryORM(history_id=str(uuid4()), hotkey="hk", node_id="n1", available_gpus=8,
                                     total_gpus=8, observed_at=NOW - timedelta(days=10, minutes=i)))
        s.add(CapacityHistoryORM(history_id=str(uuid4()), hotkey="hk", node_id="n1", available_gpus=8,
                                 total_gpus=8, observed_at=NOW - timedelta(hours=1)))
    assert cp.repository.prune_capacity_history(NOW - timedelta(days=7), batch=10) == 10
    assert cp.repository.prune_capacity_history(NOW - timedelta(days=7), batch=10) == 10
    assert cp.repository.prune_capacity_history(NOW - timedelta(days=7), batch=10) == 5
    assert _history_rows(cp) == 1


def test_deployment_list_caps_errors_but_detail_keeps_them(monkeypatch):
    dep = DeploymentRecord(workload_id="w", last_error="z" * 20_000, endpoint="ssh://root@1.2.3.4:22")
    listed = routes._deployment_out(dep, admin=True, brief=True)
    assert len(listed["last_error"]) <= routes.LIST_ERROR_CHARS + 2
    assert len(routes._deployment_out(dep, admin=True)["last_error"]) == 20_000


# --- GPU usage by class -------------------------------------------------------------


@pytest.fixture
def analytics(tmp_path, monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_NODE_INVENTORY_TIMEOUT_SECONDS", raising=False)
    repo = AnalyticsRepository(database_url=f"sqlite:///{tmp_path / 'a.sqlite'}", bootstrap=True)
    return repo


def _node(s, node_id, model, gpus, seen):
    s.add(NodeInventoryORM(node_id=node_id, hotkey="hk", payload={"gpu_model": model, "gpu_count": gpus,
                                                                  "available_gpus": 0}, observed_at=seen))


def _running(s, node_id, kind, gpus, state="ready", owner="u1", error=None, wanted=None, updated=NOW):
    wid = str(uuid4())
    req = {"gpu_count": gpus}
    if wanted is not None:
        req["supported_gpu_models"] = wanted
    s.add(WorkloadORM(workload_id=wid, owner_user_id=owner, name="w", image="i", kind=kind, security_tier="standard",
                      pricing_class="standard", requirements=req, runtime={}))
    s.add(DeploymentORM(deployment_id=str(uuid4()), workload_id=wid, owner_user_id=owner, node_id=node_id, state=state,
                        requested_instances=1, ready_instances=1, last_error=error, updated_at=updated))


def test_gpu_usage_by_class(analytics):
    with session_scope(analytics.session_factory) as s:
        _node(s, "a", "rtx4090", 8, NOW - timedelta(seconds=30))
        _node(s, "b", "RTX 4090", 8, NOW - timedelta(seconds=30))
        _node(s, "c", "rtx5090", 8, NOW - timedelta(days=3))  # registered but silent
        _running(s, "a", "pod", 6)
        _running(s, "b", "pod", 6)
        _running(s, "b", "inference", 1, owner=None)
        _running(s, "b", "inference", 1, owner=None)
        _running(s, "a", "pod", 2, state="terminated")
        for i in range(3):  # one customer turned away three times
            _running(s, None, "pod", 1, state="failed", owner="corbin", wanted=["rtx4090"],
                     error="no compatible miner capacity available after 10 attempts")
        _running(s, None, "pod", 1, state="failed", owner="ana", wanted=["rtx4090"],
                 error="GPU allocation failed: requested 1 GPUs but only 0 usable free")
        _running(s, None, "pod", 1, state="failed", owner="old", wanted=["rtx4090"],
                 error="no compatible miner capacity", updated=NOW - timedelta(days=30))
        _running(s, None, "pod", 1, state="failed", owner="bob", wanted=["rtx4090"], error="image pull failed")
    out = analytics.gpu_usage(days=7, now=NOW)
    by = {c["gpu_model"]: c for c in out["classes"]}
    r4090, r5090 = by["rtx4090"], by["rtx5090"]
    assert [c["gpu_model"] for c in out["classes"]][:2] == ["rtx4090", "rtx5090"]
    assert (r4090["online_gpus"], r4090["nodes_online"], r4090["rented_gpus"], r4090["model_gpus"]) == (16, 2, 12, 2)
    assert (r4090["free_gpus"], r4090["utilization_pct"]) == (2, 87.5)
    assert (r4090["turned_away_requests"], r4090["turned_away_customers"]) == (4, 2)
    assert (r5090["online_gpus"], r5090["offline_gpus"], r5090["nodes_offline"], r5090["utilization_pct"]) == (0, 8, 1, None)
    assert r4090["label"] == "RTX 4090"


# --- catalog submitter --------------------------------------------------------------


def test_catalog_submission_records_the_signed_in_account(monkeypatch):
    from greencompute_validator.infrastructure.repository import ValidatorRepository
    from greencompute_validator.transport import security as vsecurity

    repo = ValidatorRepository(database_url="sqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u-ana", username="ana", email="ana@example.com", balance_credits=0))
    monkeypatch.setattr(vsecurity.credential_store, "get_api_key_by_secret",
                        lambda secret: SimpleNamespace(user_id="u-ana", admin=False) if secret == "gk_ana" else None)
    # What the submit route resolves from the headers the website sends.
    assert vsecurity.caller_user_id("Bearer gk_ana", None) == "u-ana"
    assert vsecurity.caller_user_id(None, "gk_unknown") is None
    assert vsecurity.caller_user_id(None, None) is None

    signed_in = repo.create_catalog_submission(CatalogSubmission(model_id="Qwen/Qwen2.5-7B-Instruct"),
                                               submitted_by_user_id="u-ana")
    anonymous = repo.create_catalog_submission(CatalogSubmission(model_id="meta-llama/Llama-3.2-1B", hotkey="5Fabc"))
    who = repo.catalog_submitters([signed_in.submission_id, anonymous.submission_id])
    assert who == {signed_in.submission_id: {"user_id": "u-ana", "username": "ana", "email": "ana@example.com"}}

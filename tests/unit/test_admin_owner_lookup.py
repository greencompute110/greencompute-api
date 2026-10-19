"""Admin deployment list enriches rows with the owner's email/username so
sales/admin can identify the customer behind each deployment (the UUID
owner_user_id alone isn't searchable by a human)."""
from greencompute_gateway.transport.routes import _attach_owner_info
from greencompute_protocol import UserRecord


def test_attach_owner_info_adds_email_and_username():
    alice = UserRecord(user_id="u-alice", username="alice", email="alice@x.io")
    rows = [{"deployment_id": "d1", "owner_user_id": "u-alice"}]

    _attach_owner_info(rows, {alice.user_id: alice})

    assert rows[0]["owner_email"] == "alice@x.io"
    assert rows[0]["owner_username"] == "alice"


def test_attach_owner_info_skips_unknown_owner():
    rows = [{"deployment_id": "d1", "owner_user_id": "ghost"}, {"deployment_id": "d2"}]
    _attach_owner_info(rows, {})
    assert "owner_email" not in rows[0]
    assert "owner_email" not in rows[1]


def test_attach_workload_info_adds_kind_and_name():
    from greencompute_gateway.transport.routes import _attach_workload_info
    from greencompute_protocol import WorkloadSpec, WorkloadKind

    pod = WorkloadSpec(name="rental-x", image="img", kind=WorkloadKind.POD, display_name="Rental X")
    model = WorkloadSpec(name="qwen", image="img", kind=WorkloadKind.INFERENCE)
    rows = [
        {"deployment_id": "d1", "workload_id": pod.workload_id},
        {"deployment_id": "d2", "workload_id": model.workload_id},
        {"deployment_id": "d3", "workload_id": "ghost"},
    ]
    _attach_workload_info(rows, {pod.workload_id: pod, model.workload_id: model})
    assert rows[0]["workload_kind"] == "pod"
    assert rows[0]["workload_name"] == "Rental X"
    assert rows[1]["workload_kind"] == "inference"
    assert rows[1]["workload_name"] == "qwen"  # falls back to name when no display_name
    assert "workload_kind" not in rows[2]
    assert rows[0]["gpu_count"] == 1 and rows[0]["gpu_model"] is None


def test_users_own_deployment_list_names_what_each_runs(monkeypatch):
    """A user's list used to carry only workload ids, so the dashboard showed
    UUIDs instead of the model or GPU. It now names them, from the workloads
    behind the caller's own deployments only."""
    from types import SimpleNamespace

    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_gateway.transport import routes
    from greencompute_protocol import DeploymentRecord, DeploymentState, WorkloadKind, WorkloadSpec

    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    pod = repo.upsert_workload(WorkloadSpec(
        name="rental-1", image="pytorch", kind=WorkloadKind.POD,
        requirements={"gpu_count": 2, "supported_gpu_models": ["rtx-4090"]},
    ))
    model = repo.upsert_workload(WorkloadSpec(
        name="meta-llama/Llama-3.1-8B-Instruct", image="vllm",
        kind=WorkloadKind.INFERENCE, display_name="Llama 3.1 8B",
    ))
    failed, ready = DeploymentState.FAILED, DeploymentState.READY
    rows = [
        DeploymentRecord(workload_id=pod.workload_id, state=failed, owner_user_id="u1"),
        DeploymentRecord(workload_id=model.workload_id, state=ready, owner_user_id="u1"),
        DeploymentRecord(workload_id="deleted", state=failed, owner_user_id="u1"),
    ]
    caller = SimpleNamespace(user_id="u1", key_id="k1", admin=False)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: caller)
    monkeypatch.setattr(routes.service, "list_deployments", lambda **kw: rows)
    monkeypatch.setattr(routes.service.control_plane, "repository", repo)

    out = routes.list_deployments(state=None, authorization="x", x_api_key=None)

    assert (out[0]["workload_kind"], out[0]["gpu_model"], out[0]["gpu_count"]) == (
        "pod", "rtx-4090", 2,
    )
    assert (out[1]["workload_kind"], out[1]["workload_name"], out[1]["gpu_model"]) == (
        "inference", "Llama 3.1 8B", None,
    )
    assert "workload_name" not in out[2]
    assert "owner_email" not in out[0]  # owner identity stays admin-only


def test_get_workloads_fetches_several_by_id():
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_protocol import WorkloadSpec

    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    a = repo.upsert_workload(WorkloadSpec(name="a", image="img"))
    repo.upsert_workload(WorkloadSpec(name="b", image="img"))
    assert set(repo.get_workloads({a.workload_id, "missing", ""})) == {a.workload_id}
    assert repo.get_workloads(set()) == {}


def test_deployment_schema_documents_the_list_fields():
    from greencompute_gateway.transport import schemas

    fields = schemas.Deployment.model_fields
    assert {"workload_kind", "workload_name", "gpu_model", "gpu_count"} <= set(fields)
    assert "deployment_id" in fields and "ssh_private_key" not in fields

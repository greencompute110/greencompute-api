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

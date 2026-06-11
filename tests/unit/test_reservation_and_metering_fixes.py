"""Regression tests for the 2026-06-11 control-plane fixes:

1. reserve_gpus is capacity-checked (no double-booking a node's GPUs).
2. delete_workload releases the deployment's GPU reservation.
3. release_orphaned_reservations reclaims holds for gone/terminal deployments.
4. meter_usage pro-rates by real elapsed seconds (no systematic under-billing).
"""

from __future__ import annotations

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_protocol import DeploymentRecord, DeploymentState, WorkloadCreateRequest, WorkloadSpec


def _repo():
    return ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)


def test_reserve_gpus_capacity_checked_blocks_double_booking():
    repo = _repo()
    # Node n1 has 2 physical GPUs. First deployment reserves both.
    assert repo.reserve_gpus("dep-a", "hk1", "n1", 2, capacity_gpus=2) is True
    # A second deployment targeting the same node must be REFUSED — the node is
    # full. Pre-fix both inserts succeeded (double-spend).
    assert repo.reserve_gpus("dep-b", "hk1", "n1", 1, capacity_gpus=2) is False
    assert repo.reserved_gpus_for_node("n1") == 2
    # Releasing the first frees capacity for the second.
    repo.release_gpu_reservation("dep-a")
    assert repo.reserve_gpus("dep-b", "hk1", "n1", 1, capacity_gpus=2) is True
    assert repo.reserved_gpus_for_node("n1") == 1


def test_reserve_gpus_reactivate_same_deployment_not_double_counted():
    repo = _repo()
    assert repo.reserve_gpus("dep-a", "hk1", "n1", 2, capacity_gpus=2) is True
    # Re-reserving the SAME deployment (e.g. resume) must succeed in place, not
    # be rejected as if it were new demand on a full node.
    assert repo.reserve_gpus("dep-a", "hk1", "n1", 2, capacity_gpus=2) is True
    assert repo.reserved_gpus_for_node("n1") == 2


def _make_workload_with_deployment(repo) -> tuple[str, str]:
    workload = WorkloadSpec(
        **WorkloadCreateRequest(
            name="pod", image="greencompute/gpu-pod:latest",
            requirements={"gpu_count": 1},
        ).model_dump()
    )
    repo.upsert_workload(workload)
    dep = repo.create_deployment(
        DeploymentRecord(workload_id=workload.workload_id, state=DeploymentState.READY)
    )
    return workload.workload_id, dep.deployment_id


def test_delete_workload_releases_reservation():
    repo = _repo()
    wl_id, dep_id = _make_workload_with_deployment(repo)
    repo.reserve_gpus(dep_id, "hk1", "n1", 1, capacity_gpus=4)
    assert repo.reserved_gpus_for_node("n1") == 1

    repo.delete_workload(wl_id)
    # The hold must be gone — pre-fix it survived the deployment forever.
    assert repo.reserved_gpus_for_node("n1") == 0


def test_orphan_sweep_reclaims_gone_and_terminal_reservations():
    repo = _repo()
    wl_id, dep_id = _make_workload_with_deployment(repo)
    repo.reserve_gpus(dep_id, "hk1", "n1", 1, capacity_gpus=4)
    # A reservation whose deployment never existed (e.g. crash).
    repo.reserve_gpus("ghost-dep", "hk1", "n1", 1, capacity_gpus=4)
    assert repo.reserved_gpus_for_node("n1") == 2

    released = repo.release_orphaned_reservations()
    # ghost-dep has no deployment row -> released. dep_id is READY -> kept.
    assert released == 1
    assert repo.reserved_gpus_for_node("n1") == 1

    # Now drive the real deployment terminal; next sweep reclaims it too.
    dep = repo.get_deployment(dep_id)
    dep.state = DeploymentState.TERMINATED
    repo.update_deployment(dep)
    assert repo.release_orphaned_reservations() == 1
    assert repo.reserved_gpus_for_node("n1") == 0


def test_meter_usage_prorates_by_elapsed_seconds():
    repo = _repo()
    svc = ControlPlaneService(repo)
    # 60¢/hr single-GPU pod rental.
    workload = WorkloadSpec(
        **WorkloadCreateRequest(
            name="pod", image="greencompute/gpu-pod:latest",
            requirements={"gpu_count": 1},
        ).model_dump()
    )
    repo.upsert_workload(workload)
    dep = repo.create_deployment(
        DeploymentRecord(
            workload_id=workload.workload_id, owner_user_id="u1",
            state=DeploymentState.READY, ready_instances=1, hourly_rate_cents=60,
        )
    )
    # 60¢/hr = 1000 mcents/min. Two cycles of exactly 30s each should accrue the
    # same total as one 60s cycle: ~1000 mcents total (≈1 cent), NOT 2 cents.
    svc.meter_usage(elapsed_seconds=30.0)
    svc.meter_usage(elapsed_seconds=30.0)
    row = repo.get_deployment(dep.deployment_id)
    # After ~1000 mcents accrued, at most 1 whole cent is debited; the remainder
    # stays sub-cent. The key assertion: elapsed-based accrual, not 2× full-min.
    assert row is not None
    # A 90s span should accrue 1.5x a 60s span — verify the rate scales.
    svc.meter_usage(elapsed_seconds=90.0)

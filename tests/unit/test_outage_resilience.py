"""Running rentals survive network blips. Twice (2026-10-14: the control plane
lost its network; 2026-10-16: the GPU boxes did) a short silence made the
control plane requeue every running pod, fail them all ("no compatible miner
capacity"), and the node agent then removed the containers. Now a running pod
is never moved, billing pauses while its box is silent, model replicas wait
out a grace period, and nothing is touched when every box is silent at once."""
from datetime import UTC, datetime, timedelta

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_protocol import (
    CapacityUpdate,
    DeploymentRecord,
    DeploymentState,
    Heartbeat,
    LeaseAssignment,
    NodeCapability,
    WorkloadSpec,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def cp():
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    return ControlPlaneService(repository=repo)


def _inventory(hotkey, node_id, seen):
    return CapacityUpdate(hotkey=hotkey, observed_at=seen, nodes=[NodeCapability(
        hotkey=hotkey, node_id=node_id, gpu_model="rtx4090", gpu_count=8, available_gpus=7,
        vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256, observed_at=seen,
    )])


def box(cp, hotkey, node_id, seen, healthy=True):
    """A machine that last reported at `seen`."""
    cp.repository.upsert_heartbeat(Heartbeat(hotkey=hotkey, healthy=healthy, observed_at=seen))
    cp.repository.upsert_capacity(_inventory(hotkey, node_id, seen))


def running(cp, kind, hotkey, node_id, state=DeploymentState.READY, lease="active"):
    wl = cp.repository.upsert_workload(WorkloadSpec(name=f"{kind}-w", image="img", kind=kind,
                                                    requirements={"gpu_count": 1}))
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, state=state, hotkey=hotkey, node_id=node_id, owner_user_id="u1",
        hourly_rate_cents=40, ready_instances=1,
    ))
    cp.repository.save_assignment(LeaseAssignment(
        deployment_id=dep.deployment_id, workload_id=wl.workload_id, hotkey=hotkey, node_id=node_id, status=lease,
    ))
    return dep.deployment_id


def state(cp, dep_id):
    return cp.repository.get_deployment(dep_id).state


def lease_kept(cp, dep_id):
    return any(a.deployment_id == dep_id for a in cp.repository.list_assignments(statuses=["active"]))


def test_every_box_silent_leaves_everything_in_place(cp):
    """The 2026-10-14 case: nothing reports, so the control plane is the one cut off."""
    box(cp, "hk1", "n1", NOW - timedelta(hours=1))
    pod = running(cp, "pod", "hk1", "n1")
    model = running(cp, "inference", "hk1", "n1")
    assert cp.process_unhealthy_miners(now=NOW) == []
    assert (state(cp, pod), state(cp, model)) == (DeploymentState.READY, DeploymentState.READY)
    assert lease_kept(cp, pod) and lease_kept(cp, model)


def test_a_running_pod_stays_when_its_box_goes_quiet(cp):
    box(cp, "hk1", "n1", NOW - timedelta(minutes=44))  # the 2026-10-16 .12 link outage
    box(cp, "hk2", "n2", NOW)
    pod = running(cp, "pod", "hk1", "n1")
    assert cp.process_unhealthy_miners(now=NOW) == []
    assert state(cp, pod) == DeploymentState.READY and lease_kept(cp, pod)


def test_a_pod_is_ended_after_a_day_offline(cp):
    box(cp, "hk1", "n1", NOW - timedelta(hours=25))
    box(cp, "hk2", "n2", NOW)
    pod = running(cp, "pod", "hk1", "n1")
    cp.process_unhealthy_miners(now=NOW)
    ended = cp.repository.get_deployment(pod)
    assert ended.state == DeploymentState.TERMINATED
    assert "stopped reporting 25 hours ago" in ended.last_error


def test_a_model_replica_moves_only_after_the_grace(cp):
    box(cp, "hk2", "n2", NOW)
    box(cp, "hk1", "n1", NOW - timedelta(minutes=5))
    model = running(cp, "inference", "hk1", "n1")
    assert cp.process_unhealthy_miners(now=NOW) == []
    assert state(cp, model) == DeploymentState.READY

    box(cp, "hk2", "n2", NOW + timedelta(minutes=12))
    moved = cp.process_unhealthy_miners(now=NOW + timedelta(minutes=12))  # silent 17 min
    assert [d.deployment_id for d in moved] == [model]
    assert state(cp, model) == DeploymentState.PENDING


def test_work_not_yet_running_still_moves_quickly(cp):
    box(cp, "hk2", "n2", NOW)
    box(cp, "hk1", "n1", NOW - timedelta(minutes=3))
    starting = running(cp, "pod", "hk1", "n1", state=DeploymentState.SCHEDULED, lease="assigned")
    moved = cp.process_unhealthy_miners(now=NOW)
    assert [d.deployment_id for d in moved] == [starting]


def test_a_box_reporting_unhealthy_still_has_its_models_moved(cp):
    box(cp, "hk2", "n2", NOW)
    box(cp, "hk1", "n1", NOW, healthy=False)
    model = running(cp, "inference", "hk1", "n1")
    pod = running(cp, "pod", "hk1", "n1")
    moved = cp.process_unhealthy_miners(now=NOW)
    assert [d.deployment_id for d in moved] == [model]
    assert state(cp, pod) == DeploymentState.READY


def test_billing_pauses_while_a_box_is_silent(tmp_path, monkeypatch):
    from greencompute_persistence import session_scope
    from greencompute_persistence.orm import UserORM

    db_url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)  # meter_usage's BillingRepository()
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=db_url, bootstrap=True))
    now = datetime.now(UTC)
    box(cp, "hk1", "n1", now - timedelta(minutes=30))
    box(cp, "hk2", "n2", now)
    silent = running(cp, "pod", "hk1", "n1")
    fine = running(cp, "pod", "hk2", "n2")
    with session_scope(cp.repository.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u", email="u@x.io", balance_credits=1000))
    charged = cp.meter_usage(elapsed_seconds=600)  # 10 minutes at 40¢/hr ≈ 6¢
    assert charged.get(fine, 0) > 0
    assert charged.get(silent, 0) == 0

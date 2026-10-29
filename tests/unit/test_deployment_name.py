"""A renter names a deployment (#27 from an external API user: cards showed up
at the pool as deployment-id prefixes); the name rides on the lease to the
node, which passes it to the pod as GC_DEPLOYMENT_NAME."""
import pytest
from pydantic import ValidationError

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayDeploymentCreateRequest
from greencompute_protocol import (
    DeploymentCreateRequest,
    DeploymentUpdateRequest,
    LeaseAssignment,
    WorkloadSpec,
)


@pytest.fixture
def cp():
    return ControlPlaneService(repository=ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:",
                                                                 bootstrap=True))


def test_the_name_is_stored_carried_on_the_lease_and_renamable(cp):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="miner", image="img", kind="pod"))
    dep = cp.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id, name="rig-07"))
    assert cp.repository.get_deployment(dep.deployment_id).name == "rig-07"
    cp.repository.save_assignment(LeaseAssignment(deployment_id=dep.deployment_id, workload_id=wl.workload_id,
                                                  hotkey="m", node_id="n1"))
    [lease] = cp.repository.list_assignments(hotkey="m")
    assert lease.deployment_name == "rig-07"
    cp.update_deployment(dep.deployment_id, DeploymentUpdateRequest(name="rig-08"))
    assert cp.repository.get_deployment(dep.deployment_id).name == "rig-08"
    cp.update_deployment(dep.deployment_id, DeploymentUpdateRequest(requested_instances=1))  # other edits keep it
    assert cp.repository.get_deployment(dep.deployment_id).name == "rig-08"


def test_no_name_is_fine(cp):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="miner", image="img", kind="pod"))
    dep = cp.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id))
    assert cp.repository.get_deployment(dep.deployment_id).name is None


@pytest.mark.parametrize("name", ["rig-07", "worker_1.eu", "A", "x" * 48])
def test_pool_friendly_names_are_accepted(name):
    assert GatewayDeploymentCreateRequest(workload_id="w", name=name).name == name


@pytest.mark.parametrize("name", ["-rig", "rig 07", "x" * 49, "rig/07", "émile", ""])
def test_other_names_are_refused(name):
    with pytest.raises(ValidationError):
        GatewayDeploymentCreateRequest(workload_id="w", name=name)


def test_the_platform_env_names_are_reserved():
    from greencompute_gateway.application.services import _RESERVED_ENV

    assert {"GC_DEPLOYMENT_ID", "GC_DEPLOYMENT_NAME"} <= _RESERVED_ENV


def test_an_empty_name_clears_it(cp):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="miner", image="img", kind="pod"))
    dep = cp.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id, name="rig-07"))
    cp.update_deployment(dep.deployment_id, DeploymentUpdateRequest(name=""))
    assert cp.repository.get_deployment(dep.deployment_id).name is None


def test_billing_shows_the_rentals_own_name(cp):
    from greencompute_gateway.application.credit_watch import running_by_user
    from greencompute_gateway.application.statements import _Labels
    from greencompute_protocol import DeploymentRecord, DeploymentState

    wl = cp.repository.upsert_workload(WorkloadSpec(name="miner", image="img", kind="pod",
                                                    requirements={"gpu_count": 1, "supported_gpu_models": ["rtx4090"]}))
    a = cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id="ann",
                                                         name="rig-01", state=DeploymentState.READY))
    b = cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id="ann",
                                                         state=DeploymentState.READY))
    labels = _Labels(cp)
    assert labels.get(a.deployment_id, "")["name"] == "rig-01"
    assert labels.get(b.deployment_id, "")["name"] == "miner"
    names = sorted(r["name"] for r in running_by_user(cp)["ann"])
    assert names == ["miner", "rig-01"]


def test_the_deleted_snapshot_keeps_the_name(cp):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="miner", image="img", kind="pod"))
    dep = cp.create_deployment(DeploymentCreateRequest(workload_id=wl.workload_id, name="rig-07"))
    cp.repository.delete_workload(wl.workload_id)
    assert cp.repository.list_deployment_events(dep.deployment_id)[-1]["name"] == "rig-07"

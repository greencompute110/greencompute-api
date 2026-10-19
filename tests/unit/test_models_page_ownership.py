"""The Models page listed the two shared catalog models under "Private", with
Delete buttons, because /platform/workloads returns every public workload. A
customer's own list is now `?mine=true`. The catalog rows have no owner, so a
key not bound to a user used to count as their owner (None == None) and could
list, stop and delete them; ownership now needs a real user. Customers also
can't deploy a catalog workload themselves: Flux runs its replicas."""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes
from greencompute_protocol import DeploymentRecord, DeploymentState, WorkloadSpec


@pytest.fixture
def svc(tmp_path):
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    repo = ControlPlaneRepository(database_url=url, bootstrap=True)
    return GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True),
                          control_plane=ControlPlaneService(repository=repo))


def _catalog(svc):
    """A shared catalog model as the validator writes it, with one replica."""
    wl = svc.control_plane.repository.upsert_workload(WorkloadSpec(
        workload_id="catalog-qwen2.5-7b-instruct", name="qwen2.5-7b-instruct", image="vllm",
        owner_user_id=None, public=True, tags=["catalog"], metadata={"managed_by": "flux"}))
    dep = svc.control_plane.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id=None, state=DeploymentState.READY, endpoint="http://x"))
    return wl, dep


def _own(svc, owner, *, public=False):
    return svc.control_plane.repository.upsert_workload(
        WorkloadSpec(name=f"{owner}-llama", image="vllm", owner_user_id=owner, public=public))


def test_mine_lists_only_the_callers_own_models(svc):
    catalog, _ = _catalog(svc)
    mine = _own(svc, "ana")
    theirs = _own(svc, "bob", public=True)
    everything = {w.workload_id for w in svc.list_workloads(user_id="ana")}
    assert {catalog.workload_id, mine.workload_id, theirs.workload_id} <= everything  # public stays browsable
    assert [w.workload_id for w in svc.list_workloads(user_id="ana", mine=True)] == [mine.workload_id]
    # An admin's own list is theirs too, not the whole platform.
    assert svc.list_workloads(user_id="admin-user", admin=True, mine=True) == []
    assert svc.list_workloads(user_id=None, mine=True) == []


def test_mine_reaches_the_route(monkeypatch, svc):
    _catalog(svc)
    mine = _own(svc, "ana")
    monkeypatch.setattr(routes, "service", svc)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="ana", key_id="k", admin=False))
    rows = routes.list_workloads(mine=True, authorization="x", x_api_key=None)
    assert [r["workload_id"] for r in rows] == [mine.workload_id]


def test_a_customer_cannot_delete_or_change_a_catalog_model(svc):
    catalog, dep = _catalog(svc)
    with pytest.raises(PermissionError):
        svc.delete_workload(catalog.workload_id, actor_user_id="ana")
    with pytest.raises(PermissionError):
        svc.terminate_deployment(dep.deployment_id, actor_user_id="ana")
    assert svc.list_deployments(user_id="ana") == []


def test_a_key_without_a_user_does_not_own_the_catalog(svc):
    catalog, dep = _catalog(svc)
    assert svc.list_deployments(user_id=None) == []
    assert svc.get_deployment(dep.deployment_id, user_id=None) is None
    with pytest.raises(PermissionError):
        svc.terminate_deployment(dep.deployment_id, actor_user_id=None)
    with pytest.raises(PermissionError):
        svc.delete_workload(catalog.workload_id, actor_user_id=None)
    assert svc.control_plane.repository.get_workload(catalog.workload_id) is not None
    # Admins still manage the catalog.
    assert svc.get_deployment(dep.deployment_id, admin=True) is not None


def test_a_customer_cannot_deploy_the_catalog_workload(svc):
    catalog, _ = _catalog(svc)
    with pytest.raises(PermissionError, match="shared model"):
        svc.create_deployment({"workload_id": catalog.workload_id}, user_id="ana")


def test_owners_keep_full_control_of_their_own(svc):
    mine = _own(svc, "ana")
    dep = svc.control_plane.repository.create_deployment(DeploymentRecord(
        workload_id=mine.workload_id, owner_user_id="ana", state=DeploymentState.READY, endpoint="http://x"))
    assert [d.deployment_id for d in svc.list_deployments(user_id="ana")] == [dep.deployment_id]
    assert svc.list_deployments(user_id="bob") == []
    with pytest.raises(PermissionError):
        svc.delete_workload(mine.workload_id, actor_user_id="bob")
    assert svc.delete_workload(mine.workload_id, actor_user_id="ana").workload_id == mine.workload_id

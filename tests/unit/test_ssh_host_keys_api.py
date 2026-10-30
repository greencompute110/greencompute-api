"""A pod's SSH host keys (request #9): the machine sends them with the ready
report, the control plane keeps them for as long as that container runs, and
the API returns them with fingerprints and known_hosts lines. Host ports are
reused between rentals, so without them clients see "host key changed" and
turn checking off."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.transport import routes
from greencompute_protocol import (
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    WorkloadKind,
    WorkloadSpec,
)

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFDpfaWJh7dYs3/G3VaELhN36Tj0pY7oUj+Afdy7rk93"
ED_FP = "SHA256:A2j3vFHUT8XfIsjGuvojYy+ktwi5ULcpzurhUOD3jJg"
EC = ("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEbBtMZtLYqFI5v+Dgiy8Hpi5QQd"
      "8pxTsAiqLATbh2bTDlNRKAcOaPO6FkAzthlAm4jQrj+KCUW2Ou65ZDcBAs8=")


@pytest.fixture
def svc(tmp_path):
    repo = ControlPlaneRepository(database_url=f"sqlite+pysqlite:///{tmp_path / 'hk.db'}", bootstrap=True)
    return ControlPlaneService(repository=repo)


def _starting_pod(svc):
    wl = svc.repository.upsert_workload(WorkloadSpec(name="job", image="img", kind=WorkloadKind.POD))
    return svc.repository.create_deployment(DeploymentRecord(
        workload_id=wl.workload_id, owner_user_id="u1", hotkey="hk", node_id="n1", state=DeploymentState.STARTING,
    ))


def _report(svc, dep, state, **kw):
    svc.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=state, **kw))
    return svc.repository.get_deployment(dep.deployment_id)


def test_ready_report_stores_clean_keys_and_later_reports_keep_them(svc):
    dep = _starting_pod(svc)
    got = _report(svc, dep, DeploymentState.READY, endpoint="ssh://root@203.0.113.7:40123",
                  ssh_host_keys=[EC + " root@3f2a", ED + " root@3f2a", "ssh-rsa garbage"])
    assert got.ssh_host_keys == [EC, ED]
    # A report without keys (a re-report, a suspend) keeps them: same container.
    assert _report(svc, dep, DeploymentState.READY).ssh_host_keys == [EC, ED]
    assert _report(svc, dep, DeploymentState.SUSPENDED).ssh_host_keys == [EC, ED]
    # A machine that sends an empty list (it couldn't read them) clears them.
    assert _report(svc, dep, DeploymentState.READY, ssh_host_keys=[]).ssh_host_keys == []


def test_pods_from_before_have_no_keys(svc):
    dep = _starting_pod(svc)
    assert _report(svc, dep, DeploymentState.READY, endpoint="ssh://root@h:1").ssh_host_keys == []


def test_ssh_endpoint_returns_keys_fingerprints_and_known_hosts(monkeypatch):
    from tests.unit.test_api_surface import _ssh

    out = _ssh(monkeypatch, ["ssh-ed25519 AAAA agent@box"], host_keys=[ED, EC])
    assert out["host_keys"][0] == {"type": "ssh-ed25519", "key": ED, "fingerprint": ED_FP}
    assert out["host_keys"][1]["type"] == "ecdsa-sha2-nistp256"
    assert out["known_hosts"] == [f"[203.0.113.7]:40123 {ED}", f"[203.0.113.7]:40123 {EC}"]
    empty = _ssh(monkeypatch, [], host_keys=[])
    assert empty["host_keys"] == [] and empty["known_hosts"] == []


def test_keys_are_on_one_deployment_but_not_in_lists():
    dep = DeploymentRecord(workload_id="w", state=DeploymentState.READY, endpoint="ssh://root@h:2200",
                           ssh_host_keys=[ED])
    assert routes._deployment_out(dep, admin=False)["ssh_host_keys"] == [ED]
    assert "ssh_host_keys" not in routes._deployment_out(dep, admin=False, brief=True)


def test_spec_documents_the_new_fields():
    app = FastAPI()
    app.include_router(routes.router)
    schemas = app.openapi()["components"]["schemas"]
    assert {"host_keys", "known_hosts"} <= set(schemas["SSHAccess"]["properties"])
    assert set(schemas["SSHHostKey"]["properties"]) == {"type", "key", "fingerprint"}
    assert "ssh_host_keys" in schemas["Deployment"]["properties"]


def test_a_requeued_pod_drops_the_old_containers_keys(svc):
    dep = _starting_pod(svc)
    _report(svc, dep, DeploymentState.READY, endpoint="ssh://root@h:1", ssh_host_keys=[ED])
    requeued = svc.requeue_deployment(dep.deployment_id)
    assert requeued.ssh_host_keys == []
    assert svc.repository.get_deployment(dep.deployment_id).ssh_host_keys == []


def test_stored_keys_are_checked_too_so_the_ssh_endpoint_never_crashes(monkeypatch):
    # Records are also built from rows and by hand; a broken key is dropped.
    dep = DeploymentRecord(workload_id="w", ssh_host_keys=["ssh-ed25519", "ssh-ed25519 !!!", ED])
    assert dep.ssh_host_keys == [ED]
    from tests.unit.test_api_surface import _ssh

    out = _ssh(monkeypatch, [], host_keys=dep.ssh_host_keys)
    assert [k["fingerprint"] for k in out["host_keys"]] == [ED_FP]


def test_pods_cannot_turn_off_their_own_host_keys():
    from greencompute_gateway.application.services import _RESERVED_ENV

    assert "GC_NEW_HOST_KEYS" in _RESERVED_ENV


def test_a_nodes_own_api_address_is_kept_from_its_capacity(svc):
    from greencompute_protocol import CapacityUpdate, MinerRegistration, NodeCapability

    svc.repository.upsert_miner(MinerRegistration(hotkey="hk", payout_address="p", api_base_url="http://172.16.10.12:8007",
                                                  validator_url="http://v", auth_secret="s3cret-test"))
    node = NodeCapability(hotkey="hk", node_id="node-a", gpu_model="rtx4090", gpu_count=8, available_gpus=8,
                          vram_gb_per_gpu=24, cpu_cores=64, memory_gb=256, api_base_url="http://172.16.10.24:8007/")
    svc.repository.upsert_capacity(CapacityUpdate(hotkey="hk", nodes=[node]))
    assert svc.repository.node_api_base_url("node-a") == "http://172.16.10.24:8007"
    assert svc.repository.node_api_base_url("node-b") is None

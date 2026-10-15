"""Every model gets its own address, <name>-<id>.inference.green-compute.com,
so it can be called without exposing the box that serves it; owners see that
address as the deployment's endpoint instead of the box's own URL."""
import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import (
    GatewayService,
    default_ingress_label,
    validate_ingress_host,
)
from greencompute_gateway.transport import routes
from greencompute_protocol import DeploymentRecord, WorkloadCreateRequest

ZONE = "inference.green-compute.com"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_MODEL_INGRESS_DOMAIN", raising=False)
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    return GatewayService(control_plane=ControlPlaneService(repository=repo))


def _create(svc, name="meta-llama/Llama-3.1-8B-Instruct", **fields):
    return svc.create_workload(WorkloadCreateRequest(name=name, image="vllm", **fields), "alice")


def test_a_new_model_gets_an_address_that_routes_to_it(svc):
    wl = _create(svc)
    label = f"llama-3-1-8b-instruct-{wl.workload_id.replace('-', '')[:6]}"
    assert wl.ingress_host == f"{label}.{ZONE}"
    assert validate_ingress_host(wl.ingress_host) == wl.ingress_host
    resolved, routing = svc.resolve_workload_reference("ignored", routed_host=wl.ingress_host)
    assert resolved.workload_id == wl.workload_id and routing["matched_by"] == "ingress_host"


def test_pods_get_no_address(svc):
    assert _create(svc, name="box", kind="pod").ingress_host is None


def test_an_address_the_owner_chose_is_kept(svc):
    assert _create(svc, ingress_host=f"mine.{ZONE}").ingress_host == f"mine.{ZONE}"


def test_long_or_odd_names_still_make_a_valid_label():
    for name in ("x" * 200, "!!!", "Org/Some_Model.v2 (Q4)", ""):
        label = default_ingress_label(name, "0123456789abcdef", 9)
        assert validate_ingress_host(f"{label}.{ZONE}")


def test_a_clash_falls_back_to_a_longer_suffix(svc, monkeypatch):
    real = svc.control_plane.find_workload_by_ingress_host
    taken = SimpleNamespace(workload_id="someone-else")
    monkeypatch.setattr(svc.control_plane, "find_workload_by_ingress_host",
                        lambda host: taken if host.split(".")[0].count("-") and len(host.split(".")[0].rsplit("-", 1)[1]) == 6 else real(host))
    wl = _create(svc)
    assert len(wl.ingress_host.split(".")[0].rsplit("-", 1)[1]) == 9


def test_the_migration_labels_models_the_same_way():
    path = next(pathlib.Path("alembic/versions").glob("*_0065_model_addresses.py"))
    spec = importlib.util.spec_from_file_location("m0065", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for name in ("meta-llama/Llama-3.1-8B-Instruct", "qwen2-vl", "!!!"):
        assert m._label(name, "abcdef12-3456", 6) == default_ingress_label(name, "abcdef12-3456", 6)


# --- what deployment responses show -----------------------------------------------


@pytest.fixture
def hosts(monkeypatch):
    workloads = {"w-model": SimpleNamespace(ingress_host=f"llama-abc123.{ZONE}")}
    repo = SimpleNamespace(get_workload=lambda wid: workloads.get(wid))
    monkeypatch.setattr(routes.service, "control_plane", SimpleNamespace(repository=repo))


def test_owner_sees_the_model_address_not_the_box(hosts):
    dep = DeploymentRecord(workload_id="w-model", endpoint="http://217.138.104.12:8007/inference/d1")
    assert routes._deployment_out(dep, admin=False)["endpoint"] == f"https://llama-abc123.{ZONE}/v1"
    assert routes._deployment_out(dep, admin=True)["endpoint"] == "http://217.138.104.12:8007/inference/d1"


def test_rentals_keep_their_ssh_endpoint(hosts):
    dep = DeploymentRecord(workload_id="w-pod", endpoint="ssh://root@203.0.113.7:40123")
    assert routes._deployment_out(dep, admin=False)["endpoint"] == "ssh://root@203.0.113.7:40123"


def test_a_model_without_an_address_shows_no_endpoint(hosts):
    dep = DeploymentRecord(workload_id="w-gone", endpoint="http://10.0.0.5:8007/inference/d2")
    assert routes._deployment_out(dep, admin=False)["endpoint"] is None


def test_a_call_to_the_address_reaches_the_model_whatever_model_says(svc):
    """The model is picked by the address; the upstream model id then comes
    from that deployment's workload, not from re-reading `model`."""
    from greencompute_protocol import ChatCompletionMessage, ChatCompletionRequest

    wl = _create(svc, name="qwen", runtime={"model_identifier": "Qwen/Qwen2.5-7B-Instruct"})
    request = ChatCompletionRequest(model="anything-at-all", messages=[ChatCompletionMessage(role="user", content="hi")])
    resolved, _ = svc.resolve_workload_reference(request.model, routed_host=wl.ingress_host)
    deployment = DeploymentRecord(workload_id=resolved.workload_id, endpoint="http://10.0.0.5:8007/inference/d1")
    assert svc._rewrite_model_for_upstream(request, deployment).model == "Qwen/Qwen2.5-7B-Instruct"

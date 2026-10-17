"""Every model gets its own address, <short name>-<id>.inference.green-compute.com,
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
    label = f"llama-3-1-8b-{wl.workload_id.replace('-', '')[:6]}"
    assert wl.ingress_host == f"{label}.{ZONE}"
    assert validate_ingress_host(wl.ingress_host) == wl.ingress_host
    resolved, routing = svc.resolve_workload_reference("ignored", routed_host=wl.ingress_host)
    assert resolved.workload_id == wl.workload_id and routing["matched_by"] == "ingress_host"


def test_pods_get_no_address(svc):
    assert _create(svc, name="box", kind="pod").ingress_host is None


def test_an_address_the_owner_chose_is_kept(svc):
    assert _create(svc, ingress_host=f"mine.{ZONE}").ingress_host == f"mine.{ZONE}"


def test_long_or_odd_names_still_make_a_valid_label():
    for name in ("x" * 200, "!!!", "Org/Some_Model.v2 (Q4)", "", "Meta"):
        label = default_ingress_label(name, "0123456789abcdef", 9)
        assert validate_ingress_host(f"{label}.{ZONE}")


@pytest.mark.parametrize("name, model_id, label", [
    # The model's own name, without the vendor tag or filler words.
    ("meta-llama-Meta-Llama-3.1-8B-Instruct", "meta-llama/Meta-Llama-3.1-8B-Instruct", "llama-3-1-8b"),
    ("Qwen-Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-Coder-7B-Instruct", "qwen2-5-coder-7b"),
    ("mistralai-Mistral-7B-Instruct-v0.3", "mistralai/Mistral-7B-Instruct-v0.3", "mistral-7b-v0-3"),
    ("gemma", "google/gemma-2-9b-it", "gemma-2-9b"),
    # Long names stop at a whole word.
    ("x", "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", "deepseek-r1-distill-qwen"),
    # No real model id: the workload's name.
    ("free-tier-test", "sshleifer/tiny-gpt2", "free-tier-test"),
    ("probe", "http://127.0.0.1:8765/", "probe"),
    ("my-model", None, "my-model"),
])
def test_the_label_is_a_short_form_of_the_model_name(name, model_id, label):
    wid = "7fb157eb-aa8c-4fbb-8aae-7bd3587114a5"
    assert default_ingress_label(name, wid, 6, model_identifier=model_id) == f"{label}-7fb157"


def test_catalog_models_get_a_hashed_id_not_part_of_the_word_catalog():
    label = default_ingress_label("qwen2.5-7b-instruct", "catalog-qwen2.5-7b-instruct", 6,
                                  model_identifier="Qwen/Qwen2.5-7B-Instruct")
    slug, short = label.rsplit("-", 1)
    assert slug == "qwen2-5-7b" and short != "catalo" and len(short) == 6


def test_a_clash_falls_back_to_a_longer_suffix(svc, monkeypatch):
    real = svc.control_plane.find_workload_by_ingress_host
    taken = SimpleNamespace(workload_id="someone-else")
    monkeypatch.setattr(svc.control_plane, "find_workload_by_ingress_host",
                        lambda host: taken if host.split(".")[0].count("-") and len(host.split(".")[0].rsplit("-", 1)[1]) == 6 else real(host))
    wl = _create(svc)
    assert len(wl.ingress_host.split(".")[0].rsplit("-", 1)[1]) == 9


def _migration(number):
    path = next(pathlib.Path("alembic/versions").glob(f"*_{number}_*.py"))
    spec = importlib.util.spec_from_file_location(f"m{number}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_migration_labels_models_the_same_way():
    m = _migration("0068")
    for name, model_id in (("meta-llama/Llama-3.1-8B-Instruct", None), ("qwen2-vl", "Qwen/Qwen2-VL-7B-Instruct"),
                           ("!!!", None), ("probe", "http://x/"), ("x" * 200, "sshleifer/tiny-gpt2")):
        for wid in ("abcdef12-3456", "catalog-qwen2-vl"):
            for n in (6, 9):
                assert m._label(name, wid, n, model_id) == default_ingress_label(name, wid, n, model_identifier=model_id)
    # and knows the old addresses by the rule 0065 gave them with
    for name in ("meta-llama/Llama-3.1-8B-Instruct", "qwen2-vl", "!!!"):
        assert m._old_label(name, "abcdef12-3456", 6) == _migration("0065")._label(name, "abcdef12-3456", 6)


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

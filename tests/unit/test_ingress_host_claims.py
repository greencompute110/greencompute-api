"""A workload's ingress_host routes requests by their Host header, ahead of
the requested model. Any user could set it to anything, so one API call
claiming "api.green-compute.com" would have captured every customer's
inference request. Hosts are now confined to the model ingress zone."""
import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService, validate_ingress_host
from greencompute_protocol import WorkloadCreateRequest, WorkloadSpec, WorkloadUpdateRequest


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_MODEL_INGRESS_DOMAIN", raising=False)
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    return GatewayService(control_plane=ControlPlaneService(repository=repo))


def _create(svc, owner, **fields):
    return svc.create_workload(WorkloadCreateRequest(name=fields.pop("name", "m"), image="vllm", **fields), owner)


@pytest.mark.parametrize("host", [
    "api.green-compute.com",
    "API.GREEN-COMPUTE.COM",
    "control.green-compute.com",
    "127.0.0.1",
    "evil.example.com",
    "inference.green-compute.com",
    "a.b.inference.green-compute.com",
    "x.inference.green-compute.com.evil.com",
    "-bad-.inference.green-compute.com",
])
def test_hosts_outside_the_model_zone_are_refused(svc, host):
    with pytest.raises(ValueError):
        _create(svc, "attacker", ingress_host=host, public=True)


def test_a_name_in_the_model_zone_is_accepted_and_routes(svc):
    wl = _create(svc, "alice", name="llama", ingress_host="Alice-Llama.inference.green-compute.com")
    assert wl.ingress_host == "alice-llama.inference.green-compute.com"
    resolved, routing = svc.resolve_workload_reference("anything", routed_host="alice-llama.inference.green-compute.com:443")
    assert resolved.workload_id == wl.workload_id and routing["matched_by"] == "ingress_host"


def test_a_taken_name_cannot_be_claimed_again(svc):
    _create(svc, "alice", name="a", ingress_host="shared.inference.green-compute.com")
    with pytest.raises(ValueError, match="already taken"):
        _create(svc, "bob", name="b", ingress_host="shared.inference.green-compute.com")


def test_update_is_validated_too(svc):
    wl = _create(svc, "attacker", name="x")
    with pytest.raises(ValueError):
        svc.update_workload(wl.workload_id, WorkloadUpdateRequest(ingress_host="api.green-compute.com"),
                            actor_user_id="attacker")


def test_the_api_host_never_routes_by_hostname_even_with_a_bad_stored_value(svc):
    """Defence in depth: a value written before this check (or straight to the
    database) must not capture requests to the API."""
    target = _create(svc, "alice", name="the-model-asked-for")
    svc.control_plane.repository.upsert_workload(
        WorkloadSpec(name="hijack", image="vllm", owner_user_id="attacker", public=True,
                     ingress_host="api.green-compute.com")
    )
    resolved, routing = svc.resolve_workload_reference("the-model-asked-for", routed_host="api.green-compute.com")
    assert resolved.workload_id == target.workload_id and routing["matched_by"] != "ingress_host"


def test_zone_is_configurable(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_MODEL_INGRESS_DOMAIN", "inference.example.org")
    assert validate_ingress_host("my-model.inference.example.org") == "my-model.inference.example.org"
    with pytest.raises(ValueError):
        validate_ingress_host("my-model.inference.green-compute.com")

"""GET /platform/status: the website's status page. Each shared model is
available, starting or unavailable, by what inference routing would do, and
nothing else is published: no counts, deployment ids or hosts."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from greencompute_gateway.transport import routes
from greencompute_protocol import DeploymentState


def _workload(workload_id, name, *, public=True, owner=None, kind="inference"):
    return SimpleNamespace(
        workload_id=workload_id, name=name, public=public, owner_user_id=owner, kind=kind, metadata={},
    )


def _deployment(state, endpoint="http://10.0.0.5:8000"):
    return SimpleNamespace(deployment_id=f"dep-{state}", state=state, endpoint=endpoint, hotkey="5Hot", node_id="n1")


@pytest.fixture
def status(monkeypatch):
    """Call the route with these workloads and, per workload id, the
    deployments routing would use (ready) and all of them."""
    routes._status_cache.clear()

    def run(workloads, ready=None, all_deployments=None):
        routes._status_cache.clear()
        cp = SimpleNamespace(
            list_workloads=lambda: workloads,
            list_ready_deployments=lambda wid: (ready or {}).get(wid, []),
            repository=SimpleNamespace(list_deployments_for_workload=lambda wid: (all_deployments or {}).get(wid, [])),
        )
        monkeypatch.setattr(routes.service, "control_plane", cp)
        return {m["model"]: m["state"] for m in routes.public_status()["models"]}

    yield run
    routes._status_cache.clear()


def test_a_model_with_a_routable_deployment_is_available(status):
    ready = _deployment(DeploymentState.READY)
    assert status([_workload("w1", "Qwen/Qwen2.5-7B-Instruct")], ready={"w1": [ready]}) == {
        "Qwen/Qwen2.5-7B-Instruct": "available",
    }


@pytest.mark.parametrize("state", [DeploymentState.PENDING, DeploymentState.SCHEDULED,
                                   DeploymentState.PULLING, DeploymentState.STARTING])
def test_a_model_coming_up_is_starting(status, state):
    states = status([_workload("w1", "m")], all_deployments={"w1": [_deployment(state)]})
    assert states == {"m": "starting"}


@pytest.mark.parametrize("state", [DeploymentState.FAILED, DeploymentState.TERMINATED,
                                   DeploymentState.SUSPENDED, DeploymentState.READY])
def test_a_model_routing_would_refuse_is_unavailable(status, state):
    # READY here is a ready row routing skips (its node went silent): it
    # can't take requests, so it isn't "available".
    states = status([_workload("w1", "m")], all_deployments={"w1": [_deployment(state)]})
    assert states == {"m": "unavailable"}


def test_a_ready_deployment_without_an_endpoint_is_not_available(status):
    ready = _deployment(DeploymentState.READY, endpoint=None)
    assert status([_workload("w1", "m")], ready={"w1": [ready]}) == {"m": "unavailable"}


def test_any_workload_of_the_name_counts(status):
    workloads = [_workload("w1", "m"), _workload("w2", "m")]
    assert status(workloads, ready={"w2": [_deployment(DeploymentState.READY)]}) == {"m": "available"}


def test_only_shared_models_are_listed(status):
    workloads = [
        _workload("w1", "shared"),
        _workload("w2", "private", public=False),
        _workload("w3", "customers-own", owner="u1"),
        _workload("w4", "a-pod", kind="pod"),
        _workload("w5", "  "),
    ]
    assert status(workloads) == {"shared": "unavailable"}


def test_the_same_models_as_the_price_list(status):
    workloads = [_workload("w1", "b"), _workload("w2", "a"), _workload("w3", "hidden", public=False)]
    status(workloads)
    assert sorted(routes._public_inference_model_names()) == ["a", "b"]


def test_nothing_but_names_and_states(status):
    status([_workload("w1", "m")], ready={"w1": [_deployment(DeploymentState.READY)]})
    routes._status_cache.clear()
    body = routes.public_status()
    assert set(body) == {"models", "checked_at"}
    for row in body["models"]:
        assert set(row) == {"model", "state"}
    assert "10.0.0.5" not in str(body) and "dep-" not in str(body) and "5Hot" not in str(body)


def test_answers_are_reused_briefly(status, monkeypatch):
    status([_workload("w1", "m")])
    calls = []
    cp = SimpleNamespace(
        list_workloads=lambda: calls.append(1) or [],
        list_ready_deployments=lambda wid: [],
        repository=SimpleNamespace(list_deployments_for_workload=lambda wid: []),
    )
    monkeypatch.setattr(routes.service, "control_plane", cp)
    routes._status_cache.clear()
    first = routes.public_status()
    assert routes.public_status() is first
    assert len(calls) == 1


def test_it_is_public_and_documented():
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    op = spec["paths"]["/platform/status"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PlatformStatus"
    }
    state = spec["components"]["schemas"]["ModelStatus"]["properties"]["state"]
    assert set(state["enum"]) == {"available", "starting", "unavailable"}

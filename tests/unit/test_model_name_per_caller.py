"""Model names repeat (everyone who deploys Llama 3.1 8B from the dashboard
gets the same name). Resolving a name took the first match: on 2026-10-17 a
customer's chat with their own running model went to another customer's
private one from April and failed (409). A name now means the caller's own
workload first, then one shared with them or public, preferring running ones."""
from datetime import timedelta

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService, NoReadyDeploymentError
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_protocol import DeploymentRecord, DeploymentState, WorkloadSpec

NAME = "meta-llama-Meta-Llama-3.1-8B-Instruct"


@pytest.fixture
def svc(tmp_path):
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    repo = ControlPlaneRepository(database_url=url, bootstrap=True)
    return GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True),
                          control_plane=ControlPlaneService(repository=repo))


def _model(svc, owner, *, public=False, ready=False, age_days=0):
    wl = WorkloadSpec(name=NAME, image="vllm", owner_user_id=owner, public=public)
    wl.created_at = wl.created_at - timedelta(days=age_days)
    wl = svc.control_plane.repository.upsert_workload(wl)
    if ready:
        svc.control_plane.repository.create_deployment(DeploymentRecord(
            workload_id=wl.workload_id, owner_user_id=owner, state=DeploymentState.READY, endpoint="http://x"))
    return wl


def test_a_name_means_the_callers_own_running_model(svc):
    _model(svc, "miguel", age_days=180)
    _model(svc, "zhou", ready=True, age_days=12)
    mine = _model(svc, "greenference", ready=True)
    resolved, routing = svc.resolve_workload_reference(NAME, user_id="greenference")
    assert resolved.workload_id == mine.workload_id and routing["matched_by"] == "name"


def test_own_running_model_beats_own_stopped_one(svc):
    _model(svc, "ana", age_days=1)
    running = _model(svc, "ana", ready=True, age_days=5)
    assert svc.resolve_workload_reference(NAME, user_id="ana")[0].workload_id == running.workload_id


def test_without_one_of_your_own_a_public_one_is_used(svc):
    _model(svc, "miguel", age_days=180)
    public = _model(svc, "zhou", public=True, ready=True)
    assert svc.resolve_workload_reference(NAME, user_id="bob")[0].workload_id == public.workload_id


def test_other_peoples_private_models_still_read_as_unknown(svc):
    _model(svc, "miguel", ready=True)
    resolved, _ = svc.resolve_workload_reference(NAME, user_id="bob")
    assert not svc._user_can_access_workload(resolved, "bob")  # the caller's access check then refuses
    with pytest.raises(NoReadyDeploymentError, match="unknown model"):
        svc.resolve_workload_reference("no-such-model", user_id="bob")

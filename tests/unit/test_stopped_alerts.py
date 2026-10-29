"""An email when a running rental stops by itself (request #36): one per
customer per pass, never for their own stops or for starts that failed."""
from datetime import UTC, datetime, timedelta

import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application import stopped_alerts as sa
from greencompute_protocol import DeploymentRecord, DeploymentState, DeploymentStatusUpdate, WorkloadKind, WorkloadSpec

LATER = datetime.now(UTC) + timedelta(minutes=5)


@pytest.fixture
def cp(tmp_path):
    return ControlPlaneService(repository=ControlPlaneRepository(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'st.db'}", bootstrap=True))


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, sf, user_id, kind, fields, *, event_id):
        self.sent.append((user_id, kind, fields, event_id))


def _dep(cp, owner, name, state=DeploymentState.STARTING):
    wl = cp.repository.upsert_workload(WorkloadSpec(name="j", image="i", kind=WorkloadKind.POD, owner_user_id=owner))
    return cp.repository.create_deployment(DeploymentRecord(workload_id=wl.workload_id, owner_user_id=owner,
                                                            hotkey="hk", node_id="n1", state=state, name=name))


def _to(cp, dep, state, **kw):
    cp.update_deployment_status(DeploymentStatusUpdate(deployment_id=dep.deployment_id, state=state, **kw))


def test_rentals_that_die_while_running_are_one_email_per_customer(cp):
    sf = cp.repository.session_factory
    mail = Mail()
    assert sa.stopped_once(sf, mail, now=LATER) == 0  # first pass: starts from now
    a1, a2, b1 = _dep(cp, "ann", "rig-01"), _dep(cp, "ann", "rig-02"), _dep(cp, "bob", "box")
    for d in (a1, a2, b1):
        _to(cp, d, DeploymentState.READY)
    _to(cp, a1, DeploymentState.FAILED, error="container exited (OOM)")
    _to(cp, a2, DeploymentState.FAILED, error="node lost")
    _to(cp, b1, DeploymentState.TERMINATED, error="deleted by user")  # their own stop: no email
    never_ran = _dep(cp, "bob", "never")
    _to(cp, never_ran, DeploymentState.FAILED, error="SSH setup failed")  # a failed start: no email
    assert sa.stopped_once(sf, mail, now=LATER) == 1
    [(uid, kind, fields, event_id)] = mail.sent
    assert (uid, kind, fields["count"]) == ("ann", "deployments_stopped", 2)
    assert [r["name"] for r in fields["rentals"]] == ["rig-01", "rig-02"]
    assert fields["rentals"][0]["error"] == "container exited (OOM)"
    assert sa.stopped_once(sf, mail, now=LATER) == 0 and len(mail.sent) == 1  # never twice


def test_fresh_events_wait_until_settled(cp):
    sf = cp.repository.session_factory
    mail = Mail()
    sa.stopped_once(sf, mail, now=LATER)
    d = _dep(cp, "ann", "rig")
    _to(cp, d, DeploymentState.READY)
    _to(cp, d, DeploymentState.FAILED, error="boom")
    assert sa.stopped_once(sf, mail, now=datetime.now(UTC)) == 0  # just now: not yet
    assert sa.stopped_once(sf, mail, now=LATER) == 1

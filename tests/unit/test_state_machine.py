import pytest

from greenference_protocol import DeploymentState
from greenference_control_plane.domain.state import InvalidDeploymentTransition, transition_state


def test_state_machine_allows_ready_path():
    state = DeploymentState.PENDING
    for next_state in (
        DeploymentState.SCHEDULED,
        DeploymentState.PULLING,
        DeploymentState.STARTING,
        DeploymentState.READY,
        DeploymentState.DRAINING,
        DeploymentState.TERMINATED,
    ):
        state = transition_state(state, next_state)
    assert state == DeploymentState.TERMINATED


def test_state_machine_rejects_invalid_jump():
    with pytest.raises(InvalidDeploymentTransition):
        transition_state(DeploymentState.PENDING, DeploymentState.READY)


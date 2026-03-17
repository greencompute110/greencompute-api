from greenference_protocol import DeploymentState


class InvalidDeploymentTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[DeploymentState, set[DeploymentState]] = {
    DeploymentState.PENDING: {DeploymentState.SCHEDULED, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.SCHEDULED: {DeploymentState.PULLING, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.PULLING: {DeploymentState.STARTING, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.STARTING: {DeploymentState.READY, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.READY: {DeploymentState.DRAINING, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.DRAINING: {DeploymentState.TERMINATED, DeploymentState.FAILED},
    DeploymentState.FAILED: {DeploymentState.PENDING, DeploymentState.TERMINATED},
    DeploymentState.TERMINATED: set(),
}


def transition_state(current: DeploymentState, desired: DeploymentState) -> DeploymentState:
    if desired == current:
        return current
    if desired not in ALLOWED_TRANSITIONS[current]:
        raise InvalidDeploymentTransition(f"cannot transition from {current} to {desired}")
    return desired


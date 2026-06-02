from greencompute_protocol import DeploymentState


class InvalidDeploymentTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[DeploymentState, set[DeploymentState]] = {
    DeploymentState.PENDING: {DeploymentState.SCHEDULED, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.SCHEDULED: {DeploymentState.PULLING, DeploymentState.STARTING, DeploymentState.READY, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.PULLING: {DeploymentState.STARTING, DeploymentState.READY, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.STARTING: {DeploymentState.READY, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.READY: {DeploymentState.DRAINING, DeploymentState.SUSPENDED, DeploymentState.FAILED, DeploymentState.TERMINATED},
    DeploymentState.DRAINING: {DeploymentState.TERMINATED, DeploymentState.FAILED},
    # ORCH-H3: a pod-safe resume drives the node-agent to `docker start` the
    # preserved container and re-report STARTING/PULLING then READY. Allow those
    # forward states out of SUSPENDED so the resume reports don't 409; the lease
    # flip back to 'active' is what actually triggers the node-agent resume.
    DeploymentState.SUSPENDED: {
        DeploymentState.PULLING,
        DeploymentState.STARTING,
        DeploymentState.READY,
        DeploymentState.TERMINATED,
    },
    DeploymentState.FAILED: {DeploymentState.PENDING, DeploymentState.TERMINATED},
    DeploymentState.TERMINATED: set(),
}


def transition_state(current: DeploymentState, desired: DeploymentState) -> DeploymentState:
    if desired == current:
        return current
    if desired not in ALLOWED_TRANSITIONS[current]:
        raise InvalidDeploymentTransition(f"cannot transition from {current} to {desired}")
    return desired


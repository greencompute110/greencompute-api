"""A pod can be given a start command and environment variables, so a job
runs unattended instead of needing an SSH session after boot."""
import pytest

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import GatewayService, normalize_pod_start
from greencompute_gateway.transport.routes import _redact_workload
from greencompute_protocol import WorkloadCreateRequest


@pytest.fixture
def svc():
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    return GatewayService(control_plane=ControlPlaneService(repository=repo))


def _pod(svc, **metadata):
    return svc.create_workload(
        WorkloadCreateRequest(name="job", image="pytorch", kind="pod", metadata=metadata), "u1"
    )


def test_command_and_env_are_kept_on_a_pod(svc):
    wl = _pod(svc, command="python train.py --epochs 3", env={"HF_HOME": "/workspace/hf", "SEED": 7})
    assert wl.metadata["command"] == "python train.py --epochs 3"
    assert wl.metadata["env"] == {"HF_HOME": "/workspace/hf", "SEED": "7"}


def test_a_list_command_is_quoted_and_joined():
    meta = normalize_pod_start({"command": ["python", "-c", "print('hi there')"]})
    assert meta["command"] == "python -c 'print('\"'\"'hi there'\"'\"')'"


@pytest.mark.parametrize("metadata, error", [
    ({"command": ""}, "non-empty"),
    ({"command": ["python", 3]}, "non-empty"),
    ({"command": "x" * 9000}, "at most"),
    ({"env": ["A=1"]}, "object"),
    ({"env": {"1BAD": "x"}}, "not a valid variable name"),
    ({"env": {"AUTHORIZED_KEYS": "ssh-rsa evil"}}, "set by the platform"),
    ({"env": {"nvidia_visible_devices": "all"}}, "set by the platform"),
    ({"env": {"A": {"nested": 1}}}, "must be a string"),
    ({"env": {f"V{i}": "x" for i in range(65)}}, "at most 64"),
])
def test_bad_command_or_env_is_refused_with_the_reason(svc, metadata, error):
    with pytest.raises(ValueError, match=error):
        _pod(svc, **metadata)


def test_model_workloads_are_left_alone(svc):
    wl = svc.create_workload(
        WorkloadCreateRequest(name="m", image="vllm", kind="inference", metadata={"env": ["not", "checked"]}), "u1"
    )
    assert wl.metadata["env"] == ["not", "checked"]


def test_env_values_never_leave_the_api():
    out = _redact_workload({"metadata": {"command": "python x.py", "env": {"HF_TOKEN": "hf_secret", "SEED": "7"}}})
    assert out["metadata"]["env"] == {"HF_TOKEN": "***redacted***", "SEED": "***redacted***"}
    assert out["metadata"]["command"] == "python x.py"

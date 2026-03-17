from greenference_control_plane.infrastructure.repository import ControlPlaneRepository
from greenference_protocol import Heartbeat, MinerRegistration, WorkloadCreateRequest, WorkloadSpec


def test_control_plane_repository_round_trips_core_records():
    repository = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:")
    registration = MinerRegistration(
        hotkey="miner-a",
        payout_address="5Fminer",
        api_base_url="http://miner-a.local",
        validator_url="http://validator.local",
    )
    workload = WorkloadSpec(
        **WorkloadCreateRequest(
            name="demo-workload",
            image="greenference/demo:latest",
        ).model_dump()
    )

    repository.upsert_miner(registration)
    repository.upsert_heartbeat(Heartbeat(hotkey="miner-a", healthy=True))
    repository.upsert_workload(workload)

    assert repository.get_miner("miner-a") is not None
    assert repository.get_heartbeat("miner-a") is not None
    assert repository.get_workload(workload.workload_id) is not None

from greenference_gateway.application.services import GatewayService
from greenference_gateway.infrastructure.repository import GatewayRepository
from greenference_control_plane.application.services import ControlPlaneService
from greenference_control_plane.infrastructure.repository import ControlPlaneRepository
from greenference_builder.application.services import BuilderService
from greenference_builder.infrastructure.repository import BuilderRepository
from greenference_protocol import (
    APIKeyCreateRequest,
    BuildRequest,
    DeploymentUpdateRequest,
    UserProfileUpdateRequest,
    UserRegistrationRequest,
    UserSecretCreateRequest,
    WorkloadCreateRequest,
    WorkloadShareCreateRequest,
    WorkloadUpdateRequest,
)


def test_gateway_registers_user_and_persists_api_key():
    gateway = GatewayService(
        repository=GatewayRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    )
    user = gateway.register_user(UserRegistrationRequest(username="alice", email="alice@example.com"))
    api_key = gateway.create_api_key(APIKeyCreateRequest(name="default", user_id=user.user_id))

    assert user.user_id
    assert api_key.user_id == user.user_id
    assert api_key.secret.startswith("gk_")


def test_gateway_persists_product_foundation_entities():
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway = GatewayService(repository=GatewayRepository(database_url=shared_db, bootstrap=True))
    gateway.control_plane = ControlPlaneService(ControlPlaneRepository(database_url=shared_db, bootstrap=True))

    owner = gateway.register_user(UserRegistrationRequest(username="owner", email="owner@example.com"))
    peer = gateway.register_user(UserRegistrationRequest(username="peer", email="peer@example.com"))

    updated = gateway.update_user_profile(
        owner.user_id,
        UserProfileUpdateRequest(display_name="Owner Name", bio="gpu builder", metadata={"team": "alpha"}),
    )
    assert updated.display_name == "Owner Name"
    assert updated.metadata["team"] == "alpha"

    secret = gateway.create_secret(owner.user_id, UserSecretCreateRequest(name="HF_TOKEN", value="token-123"))
    assert [item.name for item in gateway.list_secrets(owner.user_id)] == ["HF_TOKEN"]
    deleted = gateway.delete_secret(secret.secret_id, user_id=owner.user_id)
    assert deleted.secret_id == secret.secret_id

    workload = gateway.create_workload(
        WorkloadCreateRequest(
            name="private-model",
            image="greenference/private:latest",
            requirements={"gpu_count": 1},
            public=False,
        ),
        owner.user_id,
    )
    assert gateway.list_workloads(user_id=peer.user_id) == []

    share = gateway.share_workload(
        workload.workload_id,
        WorkloadShareCreateRequest(shared_with_user_id=peer.user_id),
        actor_user_id=owner.user_id,
    )
    assert share.shared_with_user_id == peer.user_id

    peer_visible = gateway.list_workloads(user_id=peer.user_id)
    assert [item.workload_id for item in peer_visible] == [workload.workload_id]

    deployment = gateway.create_deployment(
        {"workload_id": workload.workload_id, "requested_instances": 1},
        user_id=peer.user_id,
    )
    assert deployment.owner_user_id == peer.user_id
    assert [item.deployment_id for item in gateway.list_deployments(user_id=peer.user_id)] == [deployment.deployment_id]


def test_gateway_build_visibility_and_workload_metadata():
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway = GatewayService(repository=GatewayRepository(database_url=shared_db, bootstrap=True))
    gateway.control_plane = ControlPlaneService(ControlPlaneRepository(database_url=shared_db, bootstrap=True))
    gateway.builder = BuilderService(BuilderRepository(database_url=shared_db, bootstrap=True))

    owner = gateway.register_user(UserRegistrationRequest(username="builder", email="builder@example.com"))
    peer = gateway.register_user(UserRegistrationRequest(username="viewer", email="viewer@example.com"))

    private_build = gateway.start_build(
        BuildRequest(
            image="greenference/private:latest",
            context_uri="s3://greenference/private.zip",
            display_name="Private Image",
            readme="# private",
            tags=["llm", "gpu"],
            public=False,
        ),
        owner_user_id=owner.user_id,
    )
    public_build = gateway.start_build(
        BuildRequest(
            image="greenference/public:latest",
            context_uri="s3://greenference/public.zip",
            display_name="Public Image",
            public=True,
        ),
        owner_user_id=owner.user_id,
    )

    owner_images = gateway.list_builds(user_id=owner.user_id)
    peer_images = gateway.list_builds(user_id=peer.user_id)
    assert {item.build_id for item in owner_images} == {private_build.build_id, public_build.build_id}
    assert {item.build_id for item in peer_images} == {public_build.build_id}
    assert gateway.get_build(private_build.build_id, user_id=peer.user_id) is None
    assert gateway.get_build(private_build.build_id, user_id=owner.user_id) is not None

    workload = gateway.create_workload(
        WorkloadCreateRequest(
            name="metadata-model",
            image=public_build.image,
            display_name="Metadata Model",
            readme="runtime docs",
            logo_uri="https://example.com/logo.png",
            tags=["chat", "public"],
            requirements={"gpu_count": 1},
            public=True,
        ),
        owner.user_id,
    )
    assert workload.display_name == "Metadata Model"
    assert workload.logo_uri == "https://example.com/logo.png"
    saved = gateway.control_plane.repository.get_workload(workload.workload_id)
    assert saved is not None
    assert saved.tags == ["chat", "public"]


def test_gateway_workload_and_deployment_product_policy():
    shared_db = "sqlite+pysqlite:///:memory:"
    gateway = GatewayService(repository=GatewayRepository(database_url=shared_db, bootstrap=True))
    gateway.control_plane = ControlPlaneService(ControlPlaneRepository(database_url=shared_db, bootstrap=True))

    owner = gateway.register_user(UserRegistrationRequest(username="owner2", email="owner2@example.com"))
    peer = gateway.register_user(UserRegistrationRequest(username="peer2", email="peer2@example.com"))

    workload = gateway.create_workload(
        WorkloadCreateRequest(
            name="policy-model",
            image="greenference/policy:latest",
            requirements={"gpu_count": 2},
            public=False,
            lifecycle={
                "warmup_enabled": True,
                "warmup_path": "/ready",
                "shutdown_after_seconds": 900,
                "scaling_threshold": 0.85,
            },
        ),
        owner.user_id,
    )
    assert workload.lifecycle.warmup_enabled is True
    assert workload.lifecycle.shutdown_after_seconds == 900

    updated = gateway.update_workload(
        workload.workload_id,
        WorkloadUpdateRequest(
            display_name="Policy Model",
            public=True,
            lifecycle={
                "warmup_enabled": True,
                "warmup_path": "/healthz",
                "shutdown_after_seconds": 1200,
                "scaling_threshold": 0.9,
            },
        ),
        actor_user_id=owner.user_id,
    )
    assert updated.display_name == "Policy Model"
    assert updated.public is True
    assert updated.lifecycle.warmup_path == "/healthz"
    assert updated.lifecycle.scaling_threshold == 0.9
    assert gateway.get_workload(workload.workload_id, user_id=peer.user_id) is not None

    try:
        gateway.update_workload(
            workload.workload_id,
            WorkloadUpdateRequest(display_name="nope"),
            actor_user_id=peer.user_id,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("expected workload update permission error")

    deployment = gateway.create_deployment(
        {
            "workload_id": workload.workload_id,
            "requested_instances": 2,
            "accept_fee": False,
        },
        user_id=owner.user_id,
    )
    assert deployment.deployment_fee_usd == 1.2
    assert deployment.fee_acknowledged is False
    assert deployment.warmup_state == "pending"

    saved_deployment = gateway.get_deployment(deployment.deployment_id, user_id=owner.user_id)
    assert saved_deployment is not None
    assert saved_deployment.deployment_fee_usd == deployment.deployment_fee_usd

    updated_deployment = gateway.update_deployment(
        deployment.deployment_id,
        DeploymentUpdateRequest(requested_instances=3, fee_acknowledged=True),
        actor_user_id=owner.user_id,
    )
    assert updated_deployment.requested_instances == 3
    assert updated_deployment.deployment_fee_usd == 1.8
    assert updated_deployment.fee_acknowledged is True

    try:
        gateway.update_deployment(
            deployment.deployment_id,
            DeploymentUpdateRequest(requested_instances=4),
            actor_user_id=peer.user_id,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("expected deployment update permission error")

"""Regression tests for three 2026-06-10 review HIGH fixes.

1. PATCH /platform/users/{id} wiped the user's entire metadata on any
   partial update that omitted the field.
2. stream_chat_completion was a generator, so deployment selection ran
   lazily during SSE iteration — after the 200 had started — and routing
   errors (no ready deployment) surfaced as a dead stream instead of 409.
3. add_result never persisted prompt_sha256/response_sha256, leaving the
   anti-proxy audit trail null in every stored probe row.
"""
import pytest
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.domain.routing import NoReadyDeploymentError
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_protocol import (
    ChatCompletionMessage,
    ChatCompletionRequest,
    ProbeResult,
    UserProfileUpdateRequest,
    UserRecord,
)
from greencompute_validator.infrastructure.repository import ValidatorRepository


def test_partial_profile_update_preserves_metadata():
    service = GatewayService.__new__(GatewayService)
    service.repository = GatewayRepository(database_url="sqlite:///:memory:", bootstrap=True)
    user = service.repository.save_user(
        UserRecord(username="u1", email="u@x.io", metadata={"plan": "pro", "flags": ["beta"]})
    )

    updated = service.update_user_profile(
        user.user_id, UserProfileUpdateRequest(display_name="New Name")
    )

    assert updated.display_name == "New Name"
    assert updated.metadata == {"plan": "pro", "flags": ["beta"]}
    # Explicit {} still clears it (the documented escape hatch).
    cleared = service.update_user_profile(user.user_id, UserProfileUpdateRequest(metadata={}))
    assert cleared.metadata == {}


def test_stream_routing_errors_raise_at_call_time_not_first_iteration():
    service = GatewayService.__new__(GatewayService)

    def _no_ready(request, **kwargs):
        raise NoReadyDeploymentError("no ready deployment for model")

    service._select_healthy_deployments = _no_ready
    request = ChatCompletionRequest(
        model="m", messages=[ChatCompletionMessage(role="user", content="hi")], stream=True
    )

    # Pre-fix this returned a generator without raising (the error only
    # escaped on first next(), after StreamingResponse had begun the 200).
    with pytest.raises(NoReadyDeploymentError):
        service.stream_chat_completion(request)


def test_probe_result_digests_round_trip():
    repo = ValidatorRepository(database_url="sqlite:///:memory:", bootstrap=True)
    repo.add_result(
        ProbeResult(
            challenge_id="c1",
            hotkey="hk",
            node_id="n1",
            latency_ms=10.0,
            throughput=1.0,
            prompt_sha256="a" * 64,
            response_sha256="b" * 64,
        )
    )

    stored = repo.get_result("c1", "hk")
    assert stored.prompt_sha256 == "a" * 64
    assert stored.response_sha256 == "b" * 64
    listed = repo.list_results("hk")
    assert listed[0].prompt_sha256 == "a" * 64

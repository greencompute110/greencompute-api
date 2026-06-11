"""In-flight inference authorization holds — the concurrent-spend gate.

Closes the hole where N concurrent requests from a near-zero balance all passed
the old `balance > 0` check and extracted near-free bulk inference.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from greencompute_gateway.application.services import (
    GatewayService,
    InsufficientBalanceForInferenceError,
)
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import InferenceBalanceHoldORM, UserORM
from greencompute_protocol import (
    ChatCompletionMessage,
    ChatCompletionRequest,
    DeploymentRecord,
)


def _repo_with_balance(cents: int) -> BillingRepository:
    repo = BillingRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", balance_credits=cents))
    return repo


# ----------------------------- repository core -----------------------------

def test_concurrent_reserves_cannot_exceed_balance():
    repo = _repo_with_balance(100)
    # First in-flight request reserves 60 → 40 available.
    assert repo.reserve_inference_hold("u1", 60, "req-a") is True
    # A second, concurrent request needing 60 must be REFUSED (only 40 left) —
    # pre-fix both would have passed the balance>0 gate and run for ~free.
    assert repo.reserve_inference_hold("u1", 60, "req-b") is False
    assert repo.active_hold_cents("u1") == 60
    # One that fits the remainder is allowed.
    assert repo.reserve_inference_hold("u1", 40, "req-b") is True
    assert repo.active_hold_cents("u1") == 100


def test_release_frees_the_reservation():
    repo = _repo_with_balance(100)
    repo.reserve_inference_hold("u1", 80, "req-a")
    assert repo.active_hold_cents("u1") == 80
    repo.release_inference_hold("req-a")
    assert repo.active_hold_cents("u1") == 0
    # Releasing an unknown ref is a harmless no-op.
    repo.release_inference_hold("does-not-exist")


def test_same_reference_id_reserve_is_idempotent():
    repo = _repo_with_balance(100)
    assert repo.reserve_inference_hold("u1", 70, "req-a") is True
    # Re-reserving the SAME request id updates in place, never double-counts.
    assert repo.reserve_inference_hold("u1", 30, "req-a") is True
    assert repo.active_hold_cents("u1") == 30


def test_expired_holds_do_not_reserve_capacity():
    repo = _repo_with_balance(100)
    repo.reserve_inference_hold("u1", 90, "stale")
    # Force the hold to look expired (simulates an abandoned/crashed request).
    with session_scope(repo.session_factory) as s:
        row = s.get(InferenceBalanceHoldORM, "stale")
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert repo.active_hold_cents("u1") == 0
    # Its capacity is reclaimable by a new request despite the stale row.
    assert repo.reserve_inference_hold("u1", 100, "fresh") is True


def test_unknown_user_reserve_returns_false():
    repo = _repo_with_balance(100)
    assert repo.reserve_inference_hold("ghost", 1, "req") is False


# ----------------------------- service wiring -----------------------------

class _Metrics:
    def observe(self, *a, **k):
        pass

    def increment(self, *a, **k):
        pass


def _svc():
    svc = GatewayService.__new__(GatewayService)
    svc.metrics = _Metrics()
    svc._select_healthy_deployments = lambda *a, **k: (
        [DeploymentRecord(workload_id="w1", hotkey="hk1", deployment_id="dep1")],
        {"host": "h"},
    )
    return svc


def _request():
    return ChatCompletionRequest(
        model="m", messages=[ChatCompletionMessage(role="user", content="hi")]
    )


def _raise_insufficient(reserved):
    def _stub(uid, ref, req):
        reserved.append(ref)
        raise InsufficientBalanceForInferenceError(current_cents=0)
    return _stub


def test_invoke_blocks_when_reservation_refused():
    svc = _svc()
    reserved, dispatched = [], []
    svc._reserve_inference_budget = _raise_insufficient(reserved)
    svc._release_inference_budget = lambda ref: None
    svc._invoke_from_candidates = lambda *a, **k: dispatched.append(1)
    with pytest.raises(InsufficientBalanceForInferenceError):
        svc.invoke_chat_completion(_request(), user_id="u1", admin=False)
    assert reserved and not dispatched  # gated before any upstream dispatch


def test_invoke_releases_hold_after_success():
    svc = _svc()
    released = []
    svc._reserve_inference_budget = lambda *a, **k: None
    svc._release_inference_budget = lambda ref: released.append(ref)
    svc._invoke_from_candidates = lambda *a, **k: "RESP"
    assert svc.invoke_chat_completion(_request(), user_id="u1", admin=False) == "RESP"
    assert len(released) == 1


def test_admin_bypasses_reservation():
    svc = _svc()
    reserved = []
    svc._reserve_inference_budget = _raise_insufficient(reserved)
    svc._release_inference_budget = lambda ref: None
    svc._invoke_from_candidates = lambda *a, **k: "RESP"
    # Admin is not gated — reserve is never called, so the stub never raises.
    assert svc.invoke_chat_completion(_request(), user_id="u1", admin=True) == "RESP"
    assert reserved == []


def test_stream_blocks_when_reservation_refused():
    svc = _svc()
    svc._reserve_inference_budget = _raise_insufficient([])
    with pytest.raises(InsufficientBalanceForInferenceError):
        svc.stream_chat_completion(_request(), user_id="u1", admin=False)


def test_stream_releases_hold_on_normal_completion_and_disconnect():
    svc = _svc()
    svc._reserve_inference_budget = lambda *a, **k: None

    def fake_inner(*a, **k):
        yield "data: a\n"
        yield "data: b\n"

    svc._stream_from_candidates = fake_inner

    # Normal completion releases.
    released_a = []
    svc._release_inference_budget = lambda ref: released_a.append(ref)
    list(svc.stream_chat_completion(_request(), user_id="u1", admin=False))
    assert len(released_a) == 1

    # Mid-stream disconnect (generator closed) also releases.
    released_b = []
    svc._release_inference_budget = lambda ref: released_b.append(ref)
    gen = svc.stream_chat_completion(_request(), user_id="u1", admin=False)
    next(gen)
    gen.close()
    assert len(released_b) == 1

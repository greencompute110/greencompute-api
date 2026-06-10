"""Regression tests for the three money bugs from the 2026-06-10 review.

1. Miner-reported token counts were trusted raw — an inflated usage block
   drained the caller's balance and inflated the miner's own accrual, while
   a zero report gave free inference (the gateway now clamps to the
   request's plausible bounds before any money moves).
2. confirm_and_credit_invoice had no terminal-state guard — a REJECTED or
   EXPIRED crypto invoice could still be credited via the admin confirm.
3. meter_usage billed gpu_count × requested_instances while the scheduler
   only ever places ONE instance — every requested_instances>1 deployment
   was overcharged N×.
"""
from datetime import UTC, datetime, timedelta

from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import LedgerEntryORM, UserORM
from greencompute_protocol import (
    ChatCompletionMessage,
    ChatCompletionRequest,
    CryptoInvoice,
    DeploymentRecord,
    DeploymentState,
    WorkloadKind,
    WorkloadRequirements,
    WorkloadSpec,
)

# ---------------------------------------------------------------------------
# 1. Miner-reported usage clamp
# ---------------------------------------------------------------------------


class _Metrics:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def increment(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def observe(self, name: str, value: float) -> None:  # pragma: no cover
        pass


def _bare_gateway() -> GatewayService:
    service = GatewayService.__new__(GatewayService)
    service.metrics = _Metrics()
    return service


def _request(
    prompt: str = "hello world", max_tokens: int | None = 100, **extra
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="m",
        messages=[ChatCompletionMessage(role="user", content=prompt)],
        max_tokens=max_tokens,
        **extra,
    )


def test_inflated_completion_clamped_to_requested_max_tokens():
    gw = _bare_gateway()
    prompt_billed, completion_billed = gw._clamp_reported_usage(
        _request(max_tokens=100), prompt_tokens=5, completion_tokens=1_000_000_000
    )
    assert completion_billed == 100
    assert prompt_billed == 5
    assert gw.metrics.counts.get("inference.usage.clamped") == 1


def test_inflated_prompt_clamped_to_request_derived_bound():
    gw = _bare_gateway()
    request = _request(prompt="x" * 40)
    prompt_billed, _ = gw._clamp_reported_usage(
        request, prompt_tokens=1_000_000_000, completion_tokens=1
    )
    # Bound = chars + per-message overhead + base overhead — tiny vs reported.
    assert prompt_billed <= 40 + 32 + 256
    assert gw.metrics.counts.get("inference.usage.clamped") == 1


def test_honest_usage_passes_through_unclamped():
    gw = _bare_gateway()
    request = _request(prompt="tell me a story " * 20, max_tokens=512)
    prompt_billed, completion_billed = gw._clamp_reported_usage(
        request, prompt_tokens=80, completion_tokens=400
    )
    assert (prompt_billed, completion_billed) == (80, 400)
    assert "inference.usage.clamped" not in gw.metrics.counts


def test_negative_reported_usage_floors_at_zero():
    gw = _bare_gateway()
    prompt_billed, completion_billed = gw._clamp_reported_usage(
        _request(), prompt_tokens=-50, completion_tokens=-7
    )
    assert (prompt_billed, completion_billed) == (0, 0)


def test_max_completion_tokens_extra_field_raises_the_cap():
    gw = _bare_gateway()
    request = _request(max_tokens=100, max_completion_tokens=5000)
    _, completion_billed = gw._clamp_reported_usage(
        request, prompt_tokens=1, completion_tokens=4096
    )
    assert completion_billed == 4096


def test_completion_ceiling_applies_when_caller_set_no_max():
    gw = _bare_gateway()
    request = _request(max_tokens=None)
    _, completion_billed = gw._clamp_reported_usage(
        request, prompt_tokens=1, completion_tokens=10_000_000
    )
    assert completion_billed == GatewayService._BILLED_COMPLETION_TOKENS_CEILING


# ---------------------------------------------------------------------------
# 2. Invoice terminal-state guard
# ---------------------------------------------------------------------------


def _billing_repo() -> BillingRepository:
    return BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)


def _seed_user(repo: BillingRepository, user_id: str = "u1") -> str:
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id=user_id, username="u", email="u@x.io", balance_credits=0))
    return user_id


def _invoice(repo: BillingRepository, user_id: str, expires_in_minutes: int = 30) -> CryptoInvoice:
    inv = CryptoInvoice(
        user_id=user_id, currency="tao", amount_crypto=0.0577, amount_usd=20.0,
        bonus_pct=0.10, total_credits=2200, deposit_address="5Addr",
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
    )
    repo.create_crypto_invoice(inv)
    return inv


def _assert_never_credited(repo: BillingRepository, user_id: str) -> None:
    with session_scope(repo.session_factory) as s:
        assert s.get(UserORM, user_id).balance_credits == 0
        assert s.query(LedgerEntryORM).filter(LedgerEntryORM.kind == "topup").count() == 0


def test_rejected_invoice_is_never_credited():
    repo = _billing_repo()
    user = _seed_user(repo)
    inv = _invoice(repo, user)
    assert repo.reject_crypto_invoice(inv.invoice_id) is not None

    result = repo.confirm_and_credit_invoice(
        invoice_id=inv.invoice_id, tx_hash="0xabc", description="d", deposit_ref="tao:1:0"
    )
    assert result == {"refused": True, "invoice_id": inv.invoice_id, "status": "rejected"}
    _assert_never_credited(repo, user)
    assert repo.get_crypto_invoice(inv.invoice_id).status == "rejected"


def test_expired_invoice_is_never_credited():
    repo = _billing_repo()
    user = _seed_user(repo)
    inv = _invoice(repo, user, expires_in_minutes=-120)
    assert repo.expire_stale_pending_invoices(grace_seconds=0) == 1

    result = repo.confirm_and_credit_invoice(
        invoice_id=inv.invoice_id, tx_hash="0xabc", description="d", deposit_ref="tao:2:0"
    )
    assert result == {"refused": True, "invoice_id": inv.invoice_id, "status": "expired"}
    _assert_never_credited(repo, user)


def test_pending_invoice_still_credits_normally():
    repo = _billing_repo()
    user = _seed_user(repo)
    inv = _invoice(repo, user)

    result = repo.confirm_and_credit_invoice(
        invoice_id=inv.invoice_id, tx_hash="0xabc", description="d", deposit_ref="tao:3:0"
    )
    assert result and result.get("credited")
    with session_scope(repo.session_factory) as s:
        assert s.get(UserORM, user).balance_credits == 2200


# ---------------------------------------------------------------------------
# 3. meter_usage bills the placed instance, not requested_instances
# ---------------------------------------------------------------------------


def test_meter_usage_ignores_requested_instances(tmp_path, monkeypatch):
    from greencompute_control_plane.application.services import ControlPlaneService
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository

    db_url = f"sqlite+pysqlite:///{tmp_path / 'meter.db'}"
    # meter_usage constructs BillingRepository() with default args — point it
    # at the same database the control-plane repository uses.
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)

    repository = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    service = ControlPlaneService(repository)

    workload = WorkloadSpec(
        name="pod-rental",
        image="img",
        kind=WorkloadKind.POD,
        requirements=WorkloadRequirements(gpu_count=2),
    )
    repository.upsert_workload(workload)
    deployment = DeploymentRecord(
        workload_id=workload.workload_id,
        owner_user_id="u1",
        state=DeploymentState.READY,
        requested_instances=4,
        ready_instances=1,
        hourly_rate_cents=60,
    )
    repository.create_deployment(deployment)
    with session_scope(repository.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u", email="u@x.io", balance_credits=1000))

    result = service.meter_usage()

    # 60¢/hr × 2 GPUs × ONE placed instance = 2000 mcents/min → 2¢ debited.
    # The old requested_instances multiplier would have taken 8¢.
    assert result == {deployment.deployment_id: 2}
    with session_scope(repository.session_factory) as s:
        assert s.get(UserORM, "u1").balance_credits == 998

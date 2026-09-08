"""Lifecycle orchestration for custodied provider wallets.

The expensive failures this guards against, in order of cost:
  * burning a provider's TAO twice on registration,
  * paying a provider's alpha out twice,
  * whitelisting a hotkey that has no uid, so it silently earns nothing.
"""
import pytest

from greencompute_validator.application.managed_wallets import ManagedWalletService
from greencompute_validator.domain.managed_wallet import (
    GeneratedKeypair,
    ManagedWallet,
    WalletState,
    can_transition,
)

COLDKEY = "5FmpATtoNvMUisuqPNeanXxXDkhaDpYJShCNi4Xm4cXkwWKH"
HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
PAYOUT = HOTKEY


class FakeRepo:
    """In-memory stand-in that enforces the same transition table as the real one."""

    def __init__(self):
        self.wallets: dict[str, ManagedWallet] = {}
        self.payouts: list[dict] = []
        self.whitelist: list[str] = []
        self.credited: list[tuple[str, float]] = []

    def get_managed_wallet_by_application(self, application_id):
        return next(
            (w for w in self.wallets.values() if w.application_id == application_id), None
        )

    def create_managed_wallet(self, wallet):
        self.wallets[wallet.wallet_id] = wallet
        return wallet

    def list_managed_wallets_in_state(self, state):
        return [w for w in self.wallets.values() if w.state == WalletState(state)]

    def advance_managed_wallet(self, wallet_id, target, **kw):
        w = self.wallets.get(wallet_id)
        if w is None or not can_transition(w.state, target):
            return None
        updates = {"state": target, "failure_reason": kw.get("failure_reason")}
        for field in ("funded_tao", "uid", "required_funding_tao"):
            if kw.get(field) is not None:
                updates[field] = kw[field]
        self.wallets[wallet_id] = w.model_copy(update=updates)
        return self.wallets[wallet_id]

    def record_managed_payout(self, **kw):
        for existing in self.payouts:
            if existing["payout_id"] == kw["payout_id"]:
                existing.update(kw)
                return
        self.payouts.append(dict(kw))

    def credit_managed_payout(self, wallet_id, alpha_amount, **kw):
        self.credited.append((wallet_id, alpha_amount))

    def has_inflight_payout(self, wallet_id):
        return any(
            p["wallet_id"] == wallet_id and p["status"] == "submitting" for p in self.payouts
        )

    def add_whitelist_entry(self, entry):
        self.whitelist.append(entry.hotkey)
        return entry


class FakeFactory:
    def __init__(self):
        self.n = 0

    def create_keypair(self):
        self.n += 1
        return GeneratedKeypair(
            ss58_address=COLDKEY if self.n % 2 else HOTKEY, mnemonic=f"m{self.n}"
        )


class FakeBox:
    def encrypt(self, plaintext, *, context):
        return f"enc:{context}"

    def decrypt(self, ciphertext, *, context):
        return f"dec:{context}"


class FakeChain:
    def __init__(self, burn=0.4, balance=1.0, alpha=0.0, uid=None):
        self.burn, self.balance, self.alpha, self.uid = burn, balance, alpha, uid
        self.registers, self.transfers = [], []
        self.register_ok, self.transfer_ok = True, True
        self.register_raises = self.transfer_raises = False

    def registration_burn_tao(self, netuid):
        return self.burn

    def coldkey_balance_tao(self, coldkey):
        return self.balance

    def staked_alpha(self, coldkey, hotkey, netuid):
        return self.alpha

    def is_registered(self, hotkey, netuid):
        return self.uid

    def register(self, **kw):
        if self.register_raises:
            raise RuntimeError("rpc exploded")
        self.registers.append(kw)
        if self.register_ok:
            self.uid = 77
        return _Outcome(self.register_ok, "0xreg", "" if self.register_ok else "burn failed")

    def transfer_alpha(self, **kw):
        if self.transfer_raises:
            raise RuntimeError("rpc exploded")
        self.transfers.append(kw)
        return _Outcome(self.transfer_ok, "0xpay", "" if self.transfer_ok else "no funds")


class _Outcome:
    def __init__(self, success, h, message):
        self.success, self.extrinsic_hash, self.message = success, h, message


def build(**chain_kw):
    repo, chain = FakeRepo(), FakeChain(**chain_kw)
    return repo, chain, ManagedWalletService(repo, FakeFactory(), FakeBox(), chain, netuid=110)


def seed(repo, state, **over):
    base = dict(
        wallet_id="w1", application_id="app1", coldkey_ss58=COLDKEY, hotkey_ss58=HOTKEY,
        coldkey_mnemonic_enc="enc:w1:coldkey", hotkey_mnemonic_enc="enc:w1:hotkey",
        payout_address=PAYOUT, state=state, netuid=110, required_funding_tao=0.65,
    )
    base.update(over)
    repo.wallets["w1"] = ManagedWallet(**base)
    return repo.wallets["w1"]


# --- Provisioning ------------------------------------------------------------


def test_provisioning_stores_a_wallet_awaiting_funding():
    repo, _, svc = build()
    w = svc.provision_for_application("app1", PAYOUT)
    assert w.state is WalletState.AWAITING_FUNDING
    assert repo.wallets[w.wallet_id].payout_address == PAYOUT


def test_provisioning_is_idempotent_per_application():
    # A second keypair would orphan the first coldkey — and any TAO already
    # sent to it.
    repo, _, svc = build()
    first = svc.provision_for_application("app1", PAYOUT)
    second = svc.provision_for_application("app1", PAYOUT)
    assert first.wallet_id == second.wallet_id
    assert len(repo.wallets) == 1


def test_provisioning_rejects_a_bad_payout_address():
    _, _, svc = build()
    with pytest.raises(ValueError, match="SS58"):
        svc.provision_for_application("app1", COLDKEY[:-1] + "G")


# --- Funding -----------------------------------------------------------------


def test_funding_tick_advances_a_paid_wallet():
    repo, _, svc = build(balance=0.65)
    seed(repo, WalletState.AWAITING_FUNDING)
    assert svc.tick_funding() == 1
    assert repo.wallets["w1"].state is WalletState.FUNDED


def test_funding_tick_leaves_a_partial_payment_waiting():
    repo, _, svc = build(balance=0.3)
    seed(repo, WalletState.AWAITING_FUNDING)
    assert svc.tick_funding() == 0
    assert repo.wallets["w1"].state is WalletState.AWAITING_FUNDING


def test_rpc_failure_does_not_expire_a_providers_funding_window():
    repo, chain, svc = build()
    seed(repo, WalletState.AWAITING_FUNDING)

    def boom(_):
        raise RuntimeError("rpc down")

    chain.coldkey_balance_tao = boom
    assert svc.tick_funding() == 0
    assert repo.wallets["w1"].state is WalletState.AWAITING_FUNDING


# --- Registration ------------------------------------------------------------


def test_funded_wallet_registers_and_activates_in_one_tick():
    repo, chain, svc = build(balance=0.65, uid=None)
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    w = repo.wallets["w1"]
    assert w.state is WalletState.ACTIVE
    assert w.uid == 77
    assert repo.whitelist == [HOTKEY]


def test_a_wallet_stranded_at_registered_is_activated_on_a_later_tick():
    # The activation pass is separate precisely so a crash after registering
    # but before whitelisting recovers instead of holding a paid-for neuron
    # that earns nothing forever.
    repo, chain, svc = build(balance=0.65, uid=42)
    seed(repo, WalletState.REGISTERED, uid=42)
    svc.tick_registration()
    assert repo.wallets["w1"].state is WalletState.ACTIVE
    assert repo.whitelist == [HOTKEY]


def test_hotkey_is_never_whitelisted_before_it_has_a_uid():
    # A whitelisted hotkey with no uid is silently skipped by
    # _commit_weights_to_chain: the provider looks onboarded and earns nothing.
    repo, chain, svc = build(balance=0.65)
    chain.register_ok = False
    chain.uid = None
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    assert repo.whitelist == []
    assert repo.wallets["w1"].state is WalletState.REGISTRATION_FAILED


def test_already_registered_hotkey_does_not_burn_a_second_fee():
    repo, chain, svc = build(balance=0.65, uid=42)
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    assert chain.registers == []  # no extrinsic submitted
    assert repo.wallets["w1"].uid == 42
    assert repo.wallets["w1"].state is WalletState.ACTIVE


def test_burn_spike_sends_the_wallet_back_for_more_funding():
    repo, _, svc = build(burn=1.2, balance=0.65)
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    w = repo.wallets["w1"]
    assert w.state is WalletState.AWAITING_FUNDING
    assert w.required_funding_tao > 0.65


def test_registration_marks_registering_before_submitting():
    # The pre-flight write is what stops the FUNDED sweep re-burning after a
    # crash mid-extrinsic.
    repo, chain, svc = build(balance=0.65)
    seen = []
    original = chain.register

    def spy(**kw):
        seen.append(repo.wallets["w1"].state)
        return original(**kw)

    chain.register = spy
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    assert seen == [WalletState.REGISTERING]


def test_registration_exception_is_recorded_not_swallowed():
    repo, chain, svc = build(balance=0.65)
    chain.register_raises = True
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    w = repo.wallets["w1"]
    assert w.state is WalletState.REGISTRATION_FAILED
    assert "rpc exploded" in w.failure_reason


def test_chain_truth_beats_a_lying_failure_response():
    # An extrinsic can report failure after landing. Marking a live neuron
    # failed would burn a second registration on retry.
    repo, chain, svc = build(balance=0.65)
    chain.register_ok = False

    def register(**kw):
        chain.registers.append(kw)
        chain.uid = 99  # it actually landed
        return _Outcome(False, None, "timeout waiting for finalization")

    chain.register = register
    seed(repo, WalletState.FUNDED, funded_tao=0.65)
    svc.tick_registration()
    assert repo.wallets["w1"].state is not WalletState.REGISTRATION_FAILED
    assert repo.wallets["w1"].uid == 99


# --- Payout ------------------------------------------------------------------


def test_active_wallet_pays_accrued_alpha_to_the_provider():
    repo, chain, svc = build(alpha=5.0)
    seed(repo, WalletState.ACTIVE)
    assert svc.tick_payouts() == 1
    assert chain.transfers[0]["destination_coldkey_ss58"] == PAYOUT
    assert chain.transfers[0]["alpha_amount"] == 5.0
    assert repo.credited == [("w1", 5.0)]


def test_payout_is_recorded_before_submission():
    # A crash mid-flight must leave evidence, not silence.
    repo, chain, svc = build(alpha=5.0)
    states = []
    original = chain.transfer_alpha

    def spy(**kw):
        states.append([p["status"] for p in repo.payouts])
        return original(**kw)

    chain.transfer_alpha = spy
    seed(repo, WalletState.ACTIVE)
    svc.tick_payouts()
    assert states == [["submitting"]]
    assert [p["status"] for p in repo.payouts] == ["confirmed"]


def test_unresolved_payout_blocks_a_second_transfer():
    # THE double-pay guard: after a crash between submit and confirm we do not
    # know whether alpha moved, so re-reading the balance and transferring
    # again would pay twice with two distinct extrinsics.
    repo, chain, svc = build(alpha=5.0)
    seed(repo, WalletState.ACTIVE)
    repo.payouts.append({
        "payout_id": "stuck", "wallet_id": "w1", "status": "submitting",
        "destination": PAYOUT, "alpha_amount": 5.0, "netuid": 110,
    })
    assert svc.tick_payouts() == 0
    assert chain.transfers == []


def test_failed_transfer_is_not_credited():
    repo, chain, svc = build(alpha=5.0)
    chain.transfer_ok = False
    seed(repo, WalletState.ACTIVE)
    assert svc.tick_payouts() == 0
    assert repo.credited == []
    assert repo.payouts[-1]["status"] == "failed"


def test_transfer_exception_records_a_failure_row_for_reconciliation():
    repo, chain, svc = build(alpha=5.0)
    chain.transfer_raises = True
    seed(repo, WalletState.ACTIVE)
    svc.tick_payouts()
    assert repo.payouts[-1]["status"] == "failed"
    assert "rpc exploded" in repo.payouts[-1]["failure_reason"]
    assert repo.credited == []


def test_suspended_wallet_still_receives_what_it_earned():
    repo, chain, svc = build(alpha=5.0)
    seed(repo, WalletState.SUSPENDED)
    assert svc.tick_payouts() == 1


def test_dust_is_not_paid():
    repo, chain, svc = build(alpha=0.001)
    seed(repo, WalletState.ACTIVE)
    assert svc.tick_payouts() == 0
    assert chain.transfers == []


def test_unregistered_wallet_is_never_paid():
    repo, chain, svc = build(alpha=99.0)
    seed(repo, WalletState.AWAITING_FUNDING)
    assert svc.tick_payouts() == 0


# --- Read model --------------------------------------------------------------


def test_status_reports_the_outstanding_amount():
    repo, _, svc = build()
    seed(repo, WalletState.AWAITING_FUNDING, funded_tao=0.4)
    s = svc.wallet_status("app1")
    assert s["funding_address"] == COLDKEY
    assert s["outstanding_tao"] == pytest.approx(0.25)


def test_status_never_exposes_a_mnemonic():
    repo, _, svc = build()
    seed(repo, WalletState.ACTIVE)
    assert not [k for k in svc.wallet_status("app1") if "mnemonic" in k]


def test_status_is_none_for_an_unknown_application():
    _, _, svc = build()
    assert svc.wallet_status("nope") is None

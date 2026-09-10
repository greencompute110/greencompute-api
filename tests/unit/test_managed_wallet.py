"""Platform-custodied miner wallets: provisioning, funding, registration, payout.

Every assertion here is about someone else's money. The module decides when to
burn a provider's TAO and where to send their alpha, so the cases that matter
most are the refusals — the states where it must NOT act.
"""
import re
from datetime import UTC, datetime, timedelta

import pytest

from greencompute_validator.domain.managed_wallet import (
    FUNDING_WINDOW,
    MIN_PAYOUT_ALPHA,
    PAYOUT_FEE_RESERVE_TAO,
    REGISTRATION_BUFFER,
    FundingQuote,
    GeneratedKeypair,
    ManagedWallet,
    WalletState,
    can_transition,
    check_funding,
    is_valid_ss58,
    plan_payout,
    plan_registration,
    provision_wallet,
    quote_funding,
)

# A real Bittensor hotkey (the team fleet key) and a well-known Substrate
# address — both must validate, or the checksum implementation is wrong.
REAL_HOTKEY = "5FmpATtoNvMUisuqPNeanXxXDkhaDpYJShCNi4Xm4cXkwWKH"
ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
PROVIDER_ADDR = ALICE


class FakeFactory:
    """Deterministic keypair source. Yields distinct addresses per call."""

    def __init__(self, addresses=None):
        self._addresses = list(addresses or [REAL_HOTKEY, ALICE])
        self.calls = 0

    def create_keypair(self):
        addr = self._addresses[self.calls % len(self._addresses)]
        self.calls += 1
        return GeneratedKeypair(ss58_address=addr, mnemonic=f"word{self.calls} " * 12)


class FakeBox:
    """Records what it was asked to seal so tests can assert on the AAD binding."""

    def __init__(self):
        self.sealed: list[tuple[str, str]] = []

    def encrypt(self, plaintext, *, context):
        self.sealed.append((plaintext, context))
        return f"enc({context})"

    def decrypt(self, ciphertext, *, context):
        return "plaintext"


class FakeChain:
    def __init__(self, burn=0.4, balance=1.0, alpha=0.0, uid=None):
        self._burn, self._balance, self._alpha, self._uid = burn, balance, alpha, uid

    def registration_burn_tao(self, netuid):
        return self._burn

    def coldkey_balance_tao(self, coldkey_ss58):
        return self._balance

    def staked_alpha(self, coldkey_ss58, hotkey_ss58, netuid):
        return self._alpha

    def is_registered(self, hotkey_ss58, netuid):
        return self._uid


def _quote(required=0.65):
    now = datetime.now(UTC)
    return FundingQuote(
        required_tao=required,
        burn_tao=0.4,
        buffer_tao=0.2,
        fee_reserve_tao=PAYOUT_FEE_RESERVE_TAO,
        quoted_at=now,
        expires_at=now + FUNDING_WINDOW,
    )


def _wallet(**over):
    base = dict(
        wallet_id="w1",
        application_id="app1",
        coldkey_ss58=REAL_HOTKEY,
        hotkey_ss58=ALICE,
        coldkey_mnemonic_enc="enc(c)",
        hotkey_mnemonic_enc="enc(h)",
        payout_address=PROVIDER_ADDR,
        state=WalletState.AWAITING_FUNDING,
        required_funding_tao=0.65,
    )
    base.update(over)
    return ManagedWallet(**base)


# --- SS58 validation ---------------------------------------------------------


def test_real_bittensor_addresses_validate():
    assert is_valid_ss58(REAL_HOTKEY)
    assert is_valid_ss58(ALICE)


@pytest.mark.parametrize(
    "bad,label",
    [
        (REAL_HOTKEY[:-1] + "G", "single-character typo"),
        (REAL_HOTKEY[:-3] + "KWH", "transposed characters"),
        (REAL_HOTKEY[:-1], "truncated"),
        (REAL_HOTKEY[:-1] + "0", "non-base58 character"),
        ("0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb1", "ethereum address"),
        ("", "empty"),
    ],
)
def test_bad_payout_addresses_are_rejected(bad, label):
    # base58 has no redundancy, so a typo still *looks* like an address. Only
    # the blake2b checksum catches it, and a miss sends alpha somewhere
    # unrecoverable.
    assert is_valid_ss58(bad) is False, label


def test_wrong_network_prefix_is_rejected():
    # A structurally valid address for another chain is as lost as a typo.
    assert is_valid_ss58(ALICE, prefix=0) is False


# --- Quoting -----------------------------------------------------------------


def test_quote_reads_burn_live_and_adds_buffer_plus_reserve():
    q = quote_funding(FakeChain(burn=0.40))
    assert q.burn_tao == 0.40
    assert q.buffer_tao == pytest.approx(0.40 * (REGISTRATION_BUFFER - 1.0))
    assert q.fee_reserve_tao == PAYOUT_FEE_RESERVE_TAO
    assert q.required_tao == pytest.approx(0.40 + q.buffer_tao + PAYOUT_FEE_RESERVE_TAO)


def test_quote_tracks_a_rising_burn_rather_than_a_constant():
    # The whole point of reading live: a hardcoded 0.5 TAO stops working the
    # moment the subnet gets busy.
    cheap = quote_funding(FakeChain(burn=0.2))
    dear = quote_funding(FakeChain(burn=2.0))
    assert dear.required_tao > cheap.required_tao * 5


def test_quote_breakdown_is_itemised_for_the_provider():
    q = quote_funding(FakeChain(burn=0.40))
    assert "registration burn" in q.breakdown
    assert "buffer" in q.breakdown
    assert "payout fees" in q.breakdown


def test_absurd_burn_is_refused_not_quoted():
    with pytest.raises(ValueError, match="safety ceiling"):
        quote_funding(FakeChain(burn=500.0))


def test_nonsense_burn_is_refused():
    with pytest.raises(ValueError, match="implausible"):
        quote_funding(FakeChain(burn=0.0))


# --- Provisioning ------------------------------------------------------------


def test_provisioning_seals_both_mnemonics_bound_to_role():
    box = FakeBox()
    w = provision_wallet(
        FakeFactory(), box,
        wallet_id="w1", application_id="app1",
        payout_address=PROVIDER_ADDR, quote=_quote(),
    )
    contexts = {ctx for _, ctx in box.sealed}
    assert contexts == {"w1:coldkey", "w1:hotkey"}
    assert w.state is WalletState.AWAITING_FUNDING
    assert w.required_funding_tao == 0.65


def test_provisioning_rejects_an_invalid_payout_address():
    # Last gate before we start pointing money at this string.
    with pytest.raises(ValueError, match="SS58"):
        provision_wallet(
            FakeFactory(), FakeBox(),
            wallet_id="w1", application_id="app1",
            payout_address=REAL_HOTKEY[:-1] + "G", quote=_quote(),
        )


def test_provisioning_refuses_identical_coldkey_and_hotkey():
    # A factory returning the same key twice would make the custody split an
    # illusion — the "hotkey" would be able to move funds.
    with pytest.raises(ValueError, match="identical"):
        provision_wallet(
            FakeFactory([REAL_HOTKEY, REAL_HOTKEY]), FakeBox(),
            wallet_id="w1", application_id="app1",
            payout_address=PROVIDER_ADDR, quote=_quote(),
        )


def test_wallet_model_has_no_plaintext_mnemonic_field():
    # A stray model_dump() into a log or an API response must not be able to
    # leak a bearer credential.
    dumped = _wallet().model_dump()
    assert not [k for k in dumped if re.search(r"mnemonic", k) and not k.endswith("_enc")]


def test_generated_keypair_never_reprs_its_mnemonic():
    kp = GeneratedKeypair(ss58_address=ALICE, mnemonic="secret words here")
    assert "secret words here" not in repr(kp)
    assert "secret words here" not in str(kp)
    assert "redacted" in repr(kp)


# --- Funding -----------------------------------------------------------------


def test_full_payment_funds_the_wallet():
    c = check_funding(_wallet(), balance_tao=0.65)
    assert c.is_funded and c.state is WalletState.FUNDED


def test_partial_payment_reports_the_shortfall_and_keeps_waiting():
    # Their TAO is already on our coldkey; the only humane outcome is to say
    # exactly how much more is needed.
    c = check_funding(_wallet(), balance_tao=0.40)
    assert c.state is WalletState.AWAITING_FUNDING
    assert c.shortfall_tao == pytest.approx(0.25)
    assert "0.25" in c.reason


def test_underfunded_wallet_expires_after_the_window():
    old = _wallet(created_at=datetime.now(UTC) - FUNDING_WINDOW - timedelta(hours=1))
    c = check_funding(old, balance_tao=0.1)
    assert c.state is WalletState.FUNDING_EXPIRED


def test_fully_funded_on_the_last_day_is_funded_not_expired():
    # Paying late is still paying — expiry applies only to under-funding.
    old = _wallet(created_at=datetime.now(UTC) - FUNDING_WINDOW - timedelta(hours=1))
    assert check_funding(old, balance_tao=0.65).state is WalletState.FUNDED


def test_funding_check_ignores_wallets_in_other_states():
    c = check_funding(_wallet(state=WalletState.ACTIVE), balance_tao=99.0)
    assert c.state is WalletState.ACTIVE and not c.is_funded


# --- Registration ------------------------------------------------------------


def test_funded_wallet_registers():
    p = plan_registration(_wallet(state=WalletState.FUNDED), FakeChain(burn=0.4, balance=0.65))
    assert p.should_register


def test_unfunded_wallet_does_not_register():
    p = plan_registration(_wallet(state=WalletState.AWAITING_FUNDING), FakeChain())
    assert not p.should_register and "not funded" in p.reason


def test_already_registered_hotkey_is_not_registered_twice():
    # Idempotency: an extrinsic may have landed after we lost the response.
    # Re-registering burns a second fee of the provider's money for nothing.
    p = plan_registration(_wallet(state=WalletState.FUNDED), FakeChain(uid=42))
    assert not p.should_register and "uid 42" in p.reason


def test_burn_spiking_above_the_balance_asks_for_more_instead_of_failing():
    p = plan_registration(
        _wallet(state=WalletState.FUNDED), FakeChain(burn=1.2, balance=0.65)
    )
    assert not p.should_register
    assert p.additional_funding_tao == pytest.approx(1.2 - (0.65 - PAYOUT_FEE_RESERVE_TAO))


def test_registration_never_spends_the_payout_fee_reserve():
    # Burning the reserve would leave a registered neuron whose coldkey cannot
    # afford the extrinsic that pays its owner — earnings stranded on day one.
    balance = 0.65
    burn_that_eats_the_reserve = balance - (PAYOUT_FEE_RESERVE_TAO / 2)
    p = plan_registration(
        _wallet(state=WalletState.FUNDED),
        FakeChain(burn=burn_that_eats_the_reserve, balance=balance),
    )
    assert not p.should_register


# --- Payout ------------------------------------------------------------------


def test_active_wallet_pays_out_to_the_providers_address():
    p = plan_payout(_wallet(state=WalletState.ACTIVE), accrued_alpha=5.0)
    assert p.should_pay
    assert p.alpha_amount == 5.0
    assert p.destination == PROVIDER_ADDR


def test_suspended_wallet_still_pays_what_it_already_earned():
    # De-whitelisting stops future earning; it does not make earned alpha ours.
    p = plan_payout(_wallet(state=WalletState.SUSPENDED), accrued_alpha=5.0)
    assert p.should_pay


def test_dust_is_accrued_not_paid():
    p = plan_payout(_wallet(state=WalletState.ACTIVE), accrued_alpha=MIN_PAYOUT_ALPHA / 2)
    assert not p.should_pay and "accruing" in p.reason


def test_unregistered_wallet_never_pays():
    p = plan_payout(_wallet(state=WalletState.AWAITING_FUNDING), accrued_alpha=5.0)
    assert not p.should_pay


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_implausible_alpha_readings_are_refused_not_clamped(bad):
    # A wrong chain read must stop the payout. Clamping to zero would hide it;
    # clamping to a positive number would send the wrong amount.
    p = plan_payout(_wallet(state=WalletState.ACTIVE), accrued_alpha=bad)
    assert not p.should_pay


def test_payout_revalidates_the_destination_at_send_time():
    # The address passed validation at intake, but a hand-edited or badly
    # migrated row must not send a provider's earnings into the void.
    corrupted = _wallet(state=WalletState.ACTIVE, payout_address=REAL_HOTKEY[:-1] + "G")
    p = plan_payout(corrupted, accrued_alpha=5.0)
    assert not p.should_pay and "not a valid SS58" in p.reason


# --- State machine -----------------------------------------------------------


def test_happy_path_transitions_are_legal():
    path = [
        WalletState.AWAITING_FUNDING, WalletState.FUNDED, WalletState.REGISTERING,
        WalletState.REGISTERED, WalletState.ACTIVE,
    ]
    assert all(can_transition(a, b) for a, b in zip(path, path[1:]))


def test_requote_edge_funded_back_to_awaiting_funding_is_legal():
    assert can_transition(WalletState.FUNDED, WalletState.AWAITING_FUNDING)


def test_cannot_skip_straight_to_active():
    assert not can_transition(WalletState.AWAITING_FUNDING, WalletState.ACTIVE)
    assert not can_transition(WalletState.FUNDED, WalletState.ACTIVE)


def test_self_transition_is_idempotent_not_an_error():
    # Workers re-run over unchanged rows; that must not be an error.
    assert can_transition(WalletState.ACTIVE, WalletState.ACTIVE)


def test_suspended_can_be_reinstated():
    assert can_transition(WalletState.SUSPENDED, WalletState.ACTIVE)

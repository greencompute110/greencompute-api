"""Platform-managed miner wallets — provisioning, funding, registration, payout.

Providers who want to mine on GreenCompute are GPU operators, not Bittensor
users. Requiring them to create a coldkey, create a hotkey, acquire TAO and run
`btcli subnet register` before they can earn anything filters out exactly the
people we want. This module lets a provider apply with nothing but a **payout
address**; the platform creates and holds the keypair, registers the neuron on
their behalf, and forwards their alpha emissions to the address they gave us.

WHY THIS EXISTS AT ALL (2026-09-08): the subnet was publicly criticised —
including by Bittensor's founder — for concentrating emissions. The live cause
was not the (never-deployed) payout-accumulator branch but something with the
same on-chain signature: `miner_whitelist` held exactly ONE hotkey, so
`_commit_weights_to_chain` resolved one uid and pushed 100% of weight to it
every epoch. Giving every provider their own hotkey, their own neuron and their
own uid is what actually distributes emissions. This module is that fix, and
the friendlier onboarding is the means, not the goal.

CUSTODY IS DELIBERATE AND IT IS REAL CUSTODY. The product owner chose full
custody (2026-09-08) over browser-side key generation, accepting that the
platform can move provider funds and that this is a regulated posture for a
public business. Two consequences are load-bearing here:
  * A coldkey mnemonic is a bearer credential for that provider's money. It is
    NEVER stored, logged or returned in plaintext — it reaches the database
    only through the ``SecretBox`` protocol and comes back only for the two
    extrinsics that genuinely need it (register, payout).
  * Because we hold the keys, every transition that moves money is planned
    HERE, in deterministic, unit-tested code, and executed by a thin adapter.
    A bug in this file spends someone else's TAO.

Design follows ``application_review``: the policy is explicit Python, all I/O
sits behind Protocols, and nothing in this module imports ``bittensor`` or
``substrateinterface`` — those are pip-installed into the validator container
at start-up and are absent from the test environment, so importing them here
would make the whole module untestable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

# --- Policy constants --------------------------------------------------------

#: Subnet the managed neurons are registered on. Mainnet netuid for
#: GreenCompute; testnet is 16. Never hardcode this at a call site — pass it.
DEFAULT_NETUID = 110

#: SS58 network prefix. Bittensor uses the generic Substrate prefix (42).
BITTENSOR_SS58_PREFIX = 42

#: Registration burn is a moving target — it tracks subnet demand and can climb
#: sharply between the moment we quote a provider and the moment their transfer
#: lands. We therefore quote `burn * REGISTRATION_BUFFER` so an ordinary rise
#: doesn't strand someone mid-onboarding with a half-funded coldkey.
#: 1.5 covers the day-to-day drift observed on mainnet; a genuine spike beyond
#: that is handled by re-quoting, not by over-charging every applicant upfront.
REGISTRATION_BUFFER = 1.5

#: TAO left on the coldkey after registration to pay for future extrinsics.
#: Every payout is a `transfer_stake` and every extrinsic costs a fee; a coldkey
#: at exactly zero can never pay its owner. Small, but it must be non-zero or
#: the provider's first payout fails and looks like theft.
PAYOUT_FEE_RESERVE_TAO = 0.05

#: Don't emit a payout below this. Alpha amounts near the extrinsic fee mean the
#: provider nets ~nothing while we burn TAO from the reserve; accrue instead.
MIN_PAYOUT_ALPHA = 0.1

#: How long a provider has to fund the coldkey before we stop waiting. They are
#: buying TAO on an exchange, which can mean KYC — days, not minutes.
FUNDING_WINDOW = timedelta(days=14)

#: Guard against a fat-fingered or hostile quote. Any required funding above
#: this is refused rather than shown to a provider.
MAX_QUOTE_TAO = 10.0


class WalletState(StrEnum):
    """Lifecycle of one provider's managed wallet.

    Forward-only except for the explicit recovery edges in ``VALID_TRANSITIONS``.
    The state is what tells the funding watcher, the registration worker and the
    payout job whether this row is theirs to act on, so an unexpected value must
    never be treated as "probably fine" — see ``plan_*`` which all refuse to act
    outside the one state they own.
    """

    #: Keys generated, provider has been shown the address and the amount.
    AWAITING_FUNDING = "awaiting_funding"
    #: Deposit seen and sufficient. Ready to register; no neuron yet.
    FUNDED = "funded"
    #: Registration extrinsic submitted, uid not yet confirmed.
    REGISTERING = "registering"
    #: Neuron exists on chain with a uid. Not yet earning.
    REGISTERED = "registered"
    #: Whitelisted and eligible for weight. The only earning state.
    ACTIVE = "active"
    #: De-whitelisted (strike, provider request, offboarding). Keys retained so
    #: accrued alpha can still be paid out.
    SUSPENDED = "suspended"
    #: Provider never funded inside FUNDING_WINDOW.
    FUNDING_EXPIRED = "funding_expired"
    #: Registration failed in a way that needs a human (burn spiked past the
    #: funded amount, subnet full, repeated extrinsic failure).
    REGISTRATION_FAILED = "registration_failed"


VALID_TRANSITIONS: dict[WalletState, frozenset[WalletState]] = {
    WalletState.AWAITING_FUNDING: frozenset(
        {WalletState.FUNDED, WalletState.FUNDING_EXPIRED}
    ),
    # Back to AWAITING_FUNDING is the re-quote path when burn rose above what
    # the provider sent: we ask for the difference rather than failing them.
    WalletState.FUNDED: frozenset(
        {WalletState.REGISTERING, WalletState.AWAITING_FUNDING, WalletState.REGISTRATION_FAILED}
    ),
    WalletState.REGISTERING: frozenset(
        {WalletState.REGISTERED, WalletState.REGISTRATION_FAILED}
    ),
    WalletState.REGISTERED: frozenset({WalletState.ACTIVE, WalletState.SUSPENDED}),
    WalletState.ACTIVE: frozenset({WalletState.SUSPENDED}),
    WalletState.SUSPENDED: frozenset({WalletState.ACTIVE}),
    # Terminal unless an operator intervenes: a failed registration keeps its
    # funded coldkey, so retrying is a deliberate act, not an automatic one.
    WalletState.FUNDING_EXPIRED: frozenset({WalletState.AWAITING_FUNDING}),
    WalletState.REGISTRATION_FAILED: frozenset({WalletState.FUNDED, WalletState.SUSPENDED}),
}


def can_transition(current: WalletState, target: WalletState) -> bool:
    """Whether ``current -> target`` is a legal edge. Self-edges are legal so a
    worker that re-runs on an unchanged row is idempotent rather than an error."""
    if current == target:
        return True
    return target in VALID_TRANSITIONS.get(current, frozenset())


# --- SS58 address validation -------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_SS58_PREFIX_SALT = b"SS58PRE"


def _b58_decode(value: str) -> bytes:
    """Minimal base58 decode. Raises ValueError on any character outside the
    alphabet — we never want a silently-mangled payout address."""
    num = 0
    for char in value:
        digit = _B58_INDEX.get(char)
        if digit is None:
            raise ValueError(f"invalid base58 character: {char!r}")
        num = num * 58 + digit
    # Re-attach leading zero bytes, which base58 encodes as '1'.
    leading = len(value) - len(value.lstrip("1"))
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * leading + body


def is_valid_ss58(address: str, *, prefix: int = BITTENSOR_SS58_PREFIX) -> bool:
    """Validate an SS58 address INCLUDING its blake2b checksum.

    A payout address is the one field a provider types by hand, and a typo sends
    their earnings somewhere unrecoverable. Length/charset checks alone are not
    enough — base58 has no redundancy, so a single wrong character yields a
    string that still looks like an address. The checksum is the only thing that
    actually catches it, which is why this is a hard gate at intake rather than
    a warning.

    Rejects a valid address for the *wrong network* too: alpha sent to a
    non-Bittensor prefix is as lost as alpha sent to a typo.
    """
    if not address or not (46 <= len(address) <= 48):
        return False
    try:
        raw = _b58_decode(address)
    except ValueError:
        return False
    # Single-byte prefix (<64) + 32-byte public key + 2-byte checksum.
    if len(raw) != 35 or raw[0] != prefix:
        return False
    checksum = hashlib.blake2b(_SS58_PREFIX_SALT + raw[:-2], digest_size=64).digest()[:2]
    return checksum == raw[-2:]


# --- Injected I/O ------------------------------------------------------------


class GeneratedKeypair(BaseModel):
    """A freshly created keypair. ``mnemonic`` is a bearer credential — it must
    reach a ``SecretBox`` and nothing else. Never log this model."""

    ss58_address: str
    mnemonic: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"GeneratedKeypair(ss58_address={self.ss58_address!r}, mnemonic=<redacted>)"

    __str__ = __repr__


class WalletFactory(Protocol):
    """Creates Bittensor keypairs. Implemented over the `bittensor` library,
    which is absent from the test environment — hence the Protocol."""

    def create_keypair(self) -> GeneratedKeypair: ...


class SecretBox(Protocol):
    """Authenticated encryption for mnemonics at rest."""

    def encrypt(self, plaintext: str, *, context: str) -> str: ...
    def decrypt(self, ciphertext: str, *, context: str) -> str: ...


class ChainOps(Protocol):
    """The chain reads and writes this module plans but does not perform."""

    def registration_burn_tao(self, netuid: int) -> float: ...
    def coldkey_balance_tao(self, coldkey_ss58: str) -> float: ...
    def staked_alpha(self, coldkey_ss58: str, hotkey_ss58: str, netuid: int) -> float: ...
    def is_registered(self, hotkey_ss58: str, netuid: int) -> int | None: ...


# --- Provisioning ------------------------------------------------------------


class ManagedWallet(BaseModel):
    """One provider's managed wallet. Mirrors ``ManagedWalletORM``.

    ``coldkey_mnemonic_enc``/``hotkey_mnemonic_enc`` are ciphertext produced by
    a ``SecretBox``; this model deliberately has no plaintext mnemonic field so
    a stray ``model_dump()`` into a log or an API response cannot leak one.
    """

    wallet_id: str
    application_id: str
    coldkey_ss58: str
    hotkey_ss58: str
    coldkey_mnemonic_enc: str
    hotkey_mnemonic_enc: str
    #: The provider's OWN address — where their alpha goes. Not ours.
    payout_address: str
    state: WalletState = WalletState.AWAITING_FUNDING
    netuid: int = DEFAULT_NETUID
    required_funding_tao: float = 0.0
    funded_tao: float = 0.0
    uid: int | None = None
    total_paid_alpha: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    funded_at: datetime | None = None
    registered_at: datetime | None = None
    last_payout_at: datetime | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class FundingQuote:
    """What we ask a provider to send, and why.

    ``breakdown`` is shown to the provider verbatim. They are being asked to buy
    TAO on an exchange and send it to an address whose keys they do not hold —
    the least we owe them is an itemised number rather than a bare "send 0.5".
    """

    required_tao: float
    burn_tao: float
    buffer_tao: float
    fee_reserve_tao: float
    quoted_at: datetime
    expires_at: datetime

    @property
    def breakdown(self) -> str:
        return (
            f"{self.burn_tao:.4f} registration burn "
            f"+ {self.buffer_tao:.4f} buffer for burn-price movement "
            f"+ {self.fee_reserve_tao:.4f} reserve for future payout fees"
        )


def quote_funding(
    chain: ChainOps,
    *,
    netuid: int = DEFAULT_NETUID,
    now: datetime | None = None,
) -> FundingQuote:
    """Price the registration for a provider, reading the burn cost LIVE.

    The burn is never hardcoded. It is a market price that moves with subnet
    demand, and a stale constant produces one of two failures: quote too low and
    the provider funds an amount that can no longer register (their TAO sits on
    a coldkey they don't control, which reads as theft), or quote too high and
    we overcharge everyone for a number we invented.
    """
    now = now or datetime.now(UTC)
    burn = float(chain.registration_burn_tao(netuid))
    if burn <= 0:
        raise ValueError(f"implausible registration burn for netuid {netuid}: {burn}")
    buffer_tao = burn * (REGISTRATION_BUFFER - 1.0)
    required = burn + buffer_tao + PAYOUT_FEE_RESERVE_TAO
    if required > MAX_QUOTE_TAO:
        raise ValueError(
            f"registration would cost {required:.4f} TAO, above the "
            f"{MAX_QUOTE_TAO} TAO safety ceiling — refusing to quote"
        )
    return FundingQuote(
        required_tao=round(required, 6),
        burn_tao=round(burn, 6),
        buffer_tao=round(buffer_tao, 6),
        fee_reserve_tao=PAYOUT_FEE_RESERVE_TAO,
        quoted_at=now,
        expires_at=now + FUNDING_WINDOW,
    )


def provision_wallet(
    factory: WalletFactory,
    secrets: SecretBox,
    *,
    wallet_id: str,
    application_id: str,
    payout_address: str,
    quote: FundingQuote,
    netuid: int = DEFAULT_NETUID,
    now: datetime | None = None,
) -> ManagedWallet:
    """Create a coldkey + hotkey for an approved provider and seal the mnemonics.

    The payout address is validated HERE rather than trusted from intake,
    because this is the last point before we start directing money at it.
    """
    if not is_valid_ss58(payout_address):
        raise ValueError(
            f"payout address failed SS58 checksum validation: {payout_address!r}"
        )
    coldkey = factory.create_keypair()
    hotkey = factory.create_keypair()
    if coldkey.ss58_address == hotkey.ss58_address:
        # Would mean a broken/deterministic factory; the two keys must differ or
        # the custody split is an illusion.
        raise ValueError("wallet factory returned identical coldkey and hotkey")
    return ManagedWallet(
        wallet_id=wallet_id,
        application_id=application_id,
        coldkey_ss58=coldkey.ss58_address,
        hotkey_ss58=hotkey.ss58_address,
        # Context binds each ciphertext to its wallet and role, so a swapped or
        # replayed blob fails to authenticate instead of decrypting to the wrong
        # provider's key.
        coldkey_mnemonic_enc=secrets.encrypt(coldkey.mnemonic, context=f"{wallet_id}:coldkey"),
        hotkey_mnemonic_enc=secrets.encrypt(hotkey.mnemonic, context=f"{wallet_id}:hotkey"),
        payout_address=payout_address,
        state=WalletState.AWAITING_FUNDING,
        netuid=netuid,
        required_funding_tao=quote.required_tao,
        created_at=now or datetime.now(UTC),
    )


# --- Funding -----------------------------------------------------------------


@dataclass(frozen=True)
class FundingCheck:
    """Outcome of looking at a coldkey's balance while awaiting funding."""

    state: WalletState
    balance_tao: float
    shortfall_tao: float
    reason: str

    @property
    def is_funded(self) -> bool:
        return self.state is WalletState.FUNDED


def check_funding(
    wallet: ManagedWallet,
    balance_tao: float,
    *,
    now: datetime | None = None,
) -> FundingCheck:
    """Decide whether an awaiting-funding wallet can proceed to registration.

    Partial funding stays AWAITING_FUNDING with a shortfall rather than failing:
    the provider's TAO is already on our coldkey, so the only humane outcome is
    to tell them exactly how much more to send.
    """
    now = now or datetime.now(UTC)
    if wallet.state is not WalletState.AWAITING_FUNDING:
        return FundingCheck(
            state=wallet.state,
            balance_tao=balance_tao,
            shortfall_tao=0.0,
            reason=f"not awaiting funding (state={wallet.state})",
        )
    if balance_tao >= wallet.required_funding_tao:
        return FundingCheck(
            state=WalletState.FUNDED,
            balance_tao=balance_tao,
            shortfall_tao=0.0,
            reason=f"funded with {balance_tao:.4f} TAO",
        )
    shortfall = round(wallet.required_funding_tao - balance_tao, 6)
    # Only expire an UNDER-funded wallet. A provider who paid in full on the
    # last day is funded, not expired.
    if now - wallet.created_at > FUNDING_WINDOW:
        return FundingCheck(
            state=WalletState.FUNDING_EXPIRED,
            balance_tao=balance_tao,
            shortfall_tao=shortfall,
            reason=(
                f"funding window elapsed with {balance_tao:.4f} of "
                f"{wallet.required_funding_tao:.4f} TAO received"
            ),
        )
    return FundingCheck(
        state=WalletState.AWAITING_FUNDING,
        balance_tao=balance_tao,
        shortfall_tao=shortfall,
        reason=f"awaiting a further {shortfall:.4f} TAO",
    )


# --- Registration ------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationPlan:
    """Whether to submit a registration extrinsic for this wallet, and why not."""

    should_register: bool
    burn_tao: float
    balance_tao: float
    reason: str
    #: Set when the burn has risen above what the provider funded. The wallet
    #: goes back to AWAITING_FUNDING for this much more rather than failing.
    additional_funding_tao: float = 0.0


def plan_registration(
    wallet: ManagedWallet,
    chain: ChainOps,
) -> RegistrationPlan:
    """Decide whether to burn this provider's TAO to register their neuron.

    Re-reads the burn immediately before submitting. Between quoting and now the
    provider may have taken days to buy TAO, and the burn moves the whole time;
    submitting against a stale price is how you spend someone's balance on an
    extrinsic that then fails.
    """
    if wallet.state is not WalletState.FUNDED:
        return RegistrationPlan(
            should_register=False,
            burn_tao=0.0,
            balance_tao=0.0,
            reason=f"wallet is not funded (state={wallet.state})",
        )
    existing_uid = chain.is_registered(wallet.hotkey_ss58, wallet.netuid)
    if existing_uid is not None:
        # Idempotency: a previous run's extrinsic may have landed after we lost
        # the response. Registering again would burn a second fee for nothing.
        return RegistrationPlan(
            should_register=False,
            burn_tao=0.0,
            balance_tao=0.0,
            reason=f"hotkey already registered with uid {existing_uid}",
        )
    burn = float(chain.registration_burn_tao(wallet.netuid))
    balance = float(chain.coldkey_balance_tao(wallet.coldkey_ss58))
    # The reserve is not spendable on registration — it is what pays for the
    # provider's future payouts. Spending it here strands their earnings.
    spendable = balance - PAYOUT_FEE_RESERVE_TAO
    if burn > spendable:
        return RegistrationPlan(
            should_register=False,
            burn_tao=burn,
            balance_tao=balance,
            reason=(
                f"registration burn rose to {burn:.4f} TAO, above the "
                f"{spendable:.4f} TAO spendable balance"
            ),
            additional_funding_tao=round(burn - spendable, 6),
        )
    return RegistrationPlan(
        should_register=True,
        burn_tao=burn,
        balance_tao=balance,
        reason=f"registering at {burn:.4f} TAO burn",
    )


# --- Payout ------------------------------------------------------------------


@dataclass(frozen=True)
class PayoutPlan:
    """Whether to forward accrued alpha to the provider, and how much."""

    should_pay: bool
    alpha_amount: float
    destination: str
    reason: str


def plan_payout(
    wallet: ManagedWallet,
    accrued_alpha: float,
    *,
    min_payout: float = MIN_PAYOUT_ALPHA,
) -> PayoutPlan:
    """Plan the `transfer_stake` that moves a provider's emissions to them.

    This is the inverse of the gateway's alpha deposit watcher: the same
    `StakeTransferred` event, originating from us instead of arriving at us.

    Every guard here exists because the failure is someone else's money:
      * SUSPENDED still pays. Being de-whitelisted stops future earning; it does
        not make already-earned alpha ours to keep.
      * Dust is withheld, because a payout below the extrinsic fee costs the
        provider their reserve to deliver nearly nothing.
      * A negative or non-finite balance is refused outright rather than
        clamped — it means the chain read is wrong, and acting on a wrong read
        is worse than paying late.
    """
    if wallet.state not in (WalletState.ACTIVE, WalletState.SUSPENDED):
        return PayoutPlan(
            should_pay=False,
            alpha_amount=0.0,
            destination=wallet.payout_address,
            reason=f"wallet not earning (state={wallet.state})",
        )
    if accrued_alpha != accrued_alpha or accrued_alpha in (float("inf"), float("-inf")):
        return PayoutPlan(
            should_pay=False,
            alpha_amount=0.0,
            destination=wallet.payout_address,
            reason=f"implausible alpha balance: {accrued_alpha}",
        )
    if accrued_alpha < 0:
        return PayoutPlan(
            should_pay=False,
            alpha_amount=0.0,
            destination=wallet.payout_address,
            reason=f"negative alpha balance: {accrued_alpha}",
        )
    if accrued_alpha < min_payout:
        return PayoutPlan(
            should_pay=False,
            alpha_amount=0.0,
            destination=wallet.payout_address,
            reason=(
                f"{accrued_alpha:.6f} alpha is below the {min_payout} minimum — "
                "accruing"
            ),
        )
    # Re-validate the destination every time. The address was checked at intake,
    # but this is the moment it is used, and a row edited by hand or migrated
    # badly must not send a provider's earnings into the void.
    if not is_valid_ss58(wallet.payout_address):
        return PayoutPlan(
            should_pay=False,
            alpha_amount=0.0,
            destination=wallet.payout_address,
            reason=f"payout address is not a valid SS58 address: {wallet.payout_address!r}",
        )
    return PayoutPlan(
        should_pay=True,
        alpha_amount=accrued_alpha,
        destination=wallet.payout_address,
        reason=f"forwarding {accrued_alpha:.6f} alpha to the provider",
    )

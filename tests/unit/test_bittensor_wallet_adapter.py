"""Bittensor adapter for custodied wallets.

The chain-read tests use fakes shaped like the real ``Subtensor`` (verified
against bittensor 10.5.0). The keypair/keyfile tests need the real library and
skip where it is absent — CI runs in the gateway environment, which does not
install bittensor, while the validator container does.
"""
import os
import stat

import pytest

from greencompute_validator.infrastructure.bittensor_wallet import (
    BittensorChainOps,
    ExtrinsicOutcome,
    _outcome,
    _shred,
)

try:  # pragma: no cover - environment-dependent
    import bittensor_wallet  # noqa: F401

    _HAS_BT = True
except ImportError:  # pragma: no cover
    _HAS_BT = False

requires_bittensor = pytest.mark.skipif(not _HAS_BT, reason="bittensor not installed")


class FakeBalance:
    """Stands in for bittensor's Balance, which exposes .tao."""

    def __init__(self, tao):
        self.tao = tao


class FakeSubtensor:
    """Shaped after Subtensor 10.5.0: `recycle`, not `burn`."""

    def __init__(self, recycle_tao=0.4, balance_tao=1.0, stake_tao=0.0, uid=None):
        self._recycle, self._balance, self._stake, self._uid = (
            recycle_tao, balance_tao, stake_tao, uid,
        )

    def recycle(self, netuid, block=None):
        return FakeBalance(self._recycle)

    def get_balance(self, address, block=None):
        return FakeBalance(self._balance)

    def get_stake(self, coldkey_ss58, hotkey_ss58, netuid, block=None):
        return FakeBalance(self._stake)

    def get_uid_for_hotkey_on_subnet(self, hotkey_ss58, netuid, block=None):
        return self._uid


# --- Registration burn -------------------------------------------------------


def test_burn_is_read_from_recycle():
    # v10 renamed this. Getting it wrong quotes providers zero.
    assert BittensorChainOps(FakeSubtensor(recycle_tao=0.4)).registration_burn_tao(110) == 0.4


def test_burn_falls_back_to_legacy_burn_method():
    class Legacy:
        def burn(self, netuid, block=None):
            return FakeBalance(0.7)

    assert BittensorChainOps(Legacy()).registration_burn_tao(110) == 0.7


def test_burn_falls_back_to_raw_hyperparameter_in_rao():
    class HyperOnly:
        def get_hyperparameter(self, param_name, netuid, block=None):
            assert param_name == "Burn"
            return 900_000_000  # rao

    assert BittensorChainOps(HyperOnly()).registration_burn_tao(110) == pytest.approx(0.9)


def test_unreadable_burn_raises_rather_than_returning_zero():
    # Returning 0.0 would quote every provider nothing and leave them with an
    # unregisterable coldkey — the failure must be loud.
    class Useless:
        pass

    with pytest.raises(RuntimeError, match="cannot read registration burn"):
        BittensorChainOps(Useless()).registration_burn_tao(110)


def test_recycle_returning_none_falls_through():
    class NoneRecycle:
        def recycle(self, netuid, block=None):
            return None

        def get_hyperparameter(self, param_name, netuid, block=None):
            return 500_000_000

    assert BittensorChainOps(NoneRecycle()).registration_burn_tao(110) == pytest.approx(0.5)


# --- Other reads -------------------------------------------------------------


def test_balance_and_stake_unwrap_the_balance_object():
    ops = BittensorChainOps(FakeSubtensor(balance_tao=1.25, stake_tao=7.5))
    assert ops.coldkey_balance_tao("5X") == 1.25
    assert ops.staked_alpha("5X", "5Y", 110) == 7.5


def test_uid_lookup_passes_through_including_none():
    assert BittensorChainOps(FakeSubtensor(uid=42)).is_registered("5Y", 110) == 42
    assert BittensorChainOps(FakeSubtensor(uid=None)).is_registered("5Y", 110) is None


# --- Extrinsic outcome parsing ----------------------------------------------


class FakeReceipt:
    def __init__(self, h):
        self.extrinsic_hash = h


class FakeResponse:
    def __init__(self, success, receipt=None, message="", error=""):
        self.success, self.extrinsic_receipt = success, receipt
        self.message, self.error = message, error


def test_successful_response_yields_hash():
    out = _outcome(FakeResponse(True, FakeReceipt("0xabc")))
    assert out.success and out.extrinsic_hash == "0xabc"


def test_failed_response_carries_the_message():
    out = _outcome(FakeResponse(False, None, error="InsufficientBalance"))
    assert not out.success and "InsufficientBalance" in out.message


def test_unrecognisable_response_is_treated_as_failure():
    # Safe direction: a false failure is retried and deduped by the unique
    # extrinsic_hash, while a false success marks a provider paid for nothing.
    assert _outcome(object()).success is False


def test_missing_receipt_does_not_crash():
    out = _outcome(FakeResponse(True, None))
    assert out.success and out.extrinsic_hash is None


def test_outcome_is_immutable():
    with pytest.raises((AttributeError, TypeError)):
        _outcome(FakeResponse(True)).success = False


# --- Ephemeral keyfiles ------------------------------------------------------


def test_shred_overwrites_and_removes(tmp_path):
    d = tmp_path / "keys"
    d.mkdir()
    secret = d / "coldkey"
    secret.write_text("SUPER SECRET MNEMONIC")
    _shred(str(d))
    assert not d.exists()


def test_shred_survives_an_unreadable_file(tmp_path):
    # Must still remove the tree — leaving keyfiles behind is strictly worse
    # than failing to overwrite them.
    d = tmp_path / "keys"
    d.mkdir()
    (d / "coldkey").write_text("secret")
    (d / "coldkey").chmod(0o000)
    try:
        _shred(str(d))
        assert not d.exists()
    finally:
        if d.exists():  # pragma: no cover - cleanup on assertion failure
            (d / "coldkey").chmod(0o600)


@requires_bittensor
def test_factory_generates_distinct_real_keypairs():
    from greencompute_validator.infrastructure.bittensor_wallet import BittensorWalletFactory

    factory = BittensorWalletFactory()
    a, b = factory.create_keypair(), factory.create_keypair()
    assert a.ss58_address != b.ss58_address
    assert a.mnemonic != b.mnemonic
    assert len(a.mnemonic.split()) == 12


@requires_bittensor
def test_generated_keypairs_pass_our_own_ss58_validation():
    # Cross-check: the platform must accept the addresses it mints.
    from greencompute_validator.domain.managed_wallet import is_valid_ss58
    from greencompute_validator.infrastructure.bittensor_wallet import BittensorWalletFactory

    factory = BittensorWalletFactory()
    assert all(is_valid_ss58(factory.create_keypair().ss58_address) for _ in range(20))


@requires_bittensor
def test_ephemeral_wallet_signs_then_leaves_nothing_behind():
    from greencompute_validator.infrastructure.bittensor_wallet import (
        BittensorWalletFactory,
        ephemeral_wallet,
    )

    factory = BittensorWalletFactory()
    cold, hot = factory.create_keypair(), factory.create_keypair()
    seen_root = None
    with ephemeral_wallet(cold.mnemonic, hot.mnemonic) as wallet:
        assert wallet.coldkey.ss58_address == cold.ss58_address
        assert wallet.hotkey.ss58_address == hot.ss58_address
        signature = wallet.coldkey.sign(b"payload")
        assert wallet.coldkey.verify(b"payload", signature)
        seen_root = wallet.path
        # 0700: no other user on the host can read a provider's coldkey.
        assert stat.S_IMODE(os.stat(seen_root).st_mode) == 0o700
    assert not os.path.exists(seen_root)


@requires_bittensor
def test_ephemeral_wallet_cleans_up_even_when_the_body_raises():
    from greencompute_validator.infrastructure.bittensor_wallet import (
        BittensorWalletFactory,
        ephemeral_wallet,
    )

    factory = BittensorWalletFactory()
    cold, hot = factory.create_keypair(), factory.create_keypair()
    seen_root = None
    with pytest.raises(RuntimeError):
        with ephemeral_wallet(cold.mnemonic, hot.mnemonic) as wallet:
            seen_root = wallet.path
            raise RuntimeError("extrinsic blew up")
    assert seen_root and not os.path.exists(seen_root)

"""Bittensor adapters for custodied provider wallets.

Implements the ``WalletFactory`` and ``ChainOps`` protocols from
``domain.managed_wallet``, plus the two extrinsics that move a provider's money:
``burned_register`` (spends their TAO) and ``transfer_stake`` (delivers their
alpha). All policy lives in the domain module; this file only executes it.

Every signature below was verified against bittensor **10.5.0**, the version the
validator container actually installs under its ``bittensor>=9,<11`` pin. Three
findings are load-bearing and would each have produced a silent, expensive bug:

  1. ``Subtensor.burn`` DOES NOT EXIST in v10 — the registration cost is
     ``recycle(netuid)``. A ``getattr(subtensor, "burn")`` would have returned
     None and quoted every provider zero.
  2. ``Balance.from_tao(amount, netuid=0)`` defaults to **TAO**. Alpha needs the
     real netuid or the amount carries the wrong unit into ``transfer_stake``.
  3. ``Wallet`` is a Rust-backed object with no settable key attributes — you
     cannot hold a keypair in memory. Signing REQUIRES a keyfile on disk, which
     is why ``ephemeral_wallet`` exists.

Feature-detection over version-gating, matching ``domain/chain.py``: the SDK is
range-pinned rather than exact, and a renamed method that silently returns None
here would misprice a registration or skip a payout.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from greencompute_validator.domain.managed_wallet import GeneratedKeypair

logger = logging.getLogger(__name__)

#: Where ephemeral keyfiles are materialised. tmpfs is RAM-backed, so a
#: provider's coldkey never reaches persistent storage and cannot be recovered
#: from a disk image, a snapshot, or an unlinked-but-not-overwritten inode.
#: Overridable for containers without /dev/shm; see ``_keyfile_root``.
TMPFS_ROOT = "/dev/shm"
KEYFILE_ROOT_ENV = "GREENCOMPUTE_WALLET_KEYFILE_ROOT"


def _keyfile_root() -> str:
    """Directory for ephemeral keyfiles, preferring RAM-backed storage."""
    configured = os.getenv(KEYFILE_ROOT_ENV)
    if configured:
        return configured
    if os.path.isdir(TMPFS_ROOT) and os.access(TMPFS_ROOT, os.W_OK):
        return TMPFS_ROOT
    # Falling back to the ordinary temp dir means plaintext keys touch real
    # storage. Loud, because it changes the security properties of custody.
    logger.warning(
        "%s is unavailable — ephemeral wallet keyfiles will be written to "
        "on-disk temp storage instead of RAM. Set %s to a tmpfs path.",
        TMPFS_ROOT, KEYFILE_ROOT_ENV,
    )
    return tempfile.gettempdir()


class BittensorWalletFactory:
    """Creates real sr25519 keypairs. Implements ``WalletFactory``."""

    def create_keypair(self) -> GeneratedKeypair:
        from bittensor_wallet import Keypair

        mnemonic = Keypair.generate_mnemonic()
        keypair = Keypair.create_from_mnemonic(mnemonic)
        return GeneratedKeypair(ss58_address=keypair.ss58_address, mnemonic=mnemonic)


@contextmanager
def ephemeral_wallet(coldkey_mnemonic: str, hotkey_mnemonic: str) -> Iterator[object]:
    """Materialise a signing ``Wallet`` for the life of one extrinsic.

    bittensor's ``Wallet`` reads its keys from files; there is no in-memory
    path (verified — the Rust object rejects attribute assignment and the
    ``coldkey`` property raises KeyfileError with no file present). So a
    provider's coldkey MUST exist as a plaintext keyfile for as long as it
    takes to sign.

    This narrows that window as far as the library allows: the keyfile lives in
    a 0700 directory on tmpfs, is written 0600, exists only inside this
    ``with`` block, and is overwritten before unlinking. It is deleted even if
    the extrinsic raises.

    ``encrypt=False`` is deliberate. The alternative is a password-encrypted
    keyfile, but the password would have to live in the same process that holds
    the mnemonic, protecting nothing while adding a prompt path that can hang a
    worker. Ephemerality is the real control here, not at-rest encryption of a
    file that exists for milliseconds in RAM.
    """
    from bittensor_wallet import Keypair, Wallet

    root = tempfile.mkdtemp(prefix="gc-wallet-", dir=_keyfile_root())
    os.chmod(root, stat.S_IRWXU)  # 0700 — owner only
    try:
        wallet = Wallet(name="managed", hotkey="managed", path=root)
        coldkey = Keypair.create_from_mnemonic(coldkey_mnemonic)
        hotkey = Keypair.create_from_mnemonic(hotkey_mnemonic)
        wallet.set_coldkey(coldkey, encrypt=False, overwrite=True)
        wallet.set_coldkeypub(coldkey, overwrite=True)
        wallet.set_hotkey(hotkey, encrypt=False, overwrite=True)
        yield wallet
    finally:
        _shred(root)


def _shred(root: str) -> None:
    """Overwrite then remove every keyfile under ``root``.

    On tmpfs the pages are freed anyway, but this costs microseconds and keeps
    the guarantee if ``_keyfile_root`` ever falls back to real storage. Failure
    to shred is logged and still followed by removal — leaving the tree behind
    would be strictly worse.
    """
    try:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    with open(path, "r+b") as handle:
                        handle.write(b"\0" * size)
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError:
                    logger.warning("could not overwrite keyfile before deletion")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@dataclass(frozen=True)
class ExtrinsicOutcome:
    """Normalised result of a chain write."""

    success: bool
    extrinsic_hash: str | None
    message: str


def _outcome(response: object) -> ExtrinsicOutcome:
    """Read an ``ExtrinsicResponse`` defensively.

    Treats anything it cannot positively confirm as a FAILURE. For a payout
    that is the safe direction: a false failure is retried and caught by the
    unique ``extrinsic_hash``, while a false success would mark a provider paid
    when nothing moved.
    """
    success = bool(getattr(response, "success", False))
    receipt = getattr(response, "extrinsic_receipt", None)
    tx_hash = None
    for attr in ("extrinsic_hash", "extrinsic_id", "block_hash"):
        value = getattr(receipt, attr, None)
        if value:
            tx_hash = str(value)
            break
    message = str(getattr(response, "message", "") or getattr(response, "error", "") or "")
    return ExtrinsicOutcome(success=success, extrinsic_hash=tx_hash, message=message)


class BittensorChainOps:
    """Chain reads and writes for managed wallets. Implements ``ChainOps``.

    Takes a live ``Subtensor``; the caller owns its lifecycle (the validator
    already maintains one in ``domain/chain.py``).
    """

    def __init__(self, subtensor: object) -> None:
        self._subtensor = subtensor

    # --- reads ---

    def registration_burn_tao(self, netuid: int) -> float:
        """Current cost to register one neuron, in TAO.

        v10 calls this ``recycle``; older majors exposed ``burn``. Both are
        tried, then the raw hyperparameter. Raises rather than returning 0.0 —
        a zero here would quote every provider nothing and strand them with an
        unregisterable coldkey.
        """
        for method in ("recycle", "burn"):
            fn = getattr(self._subtensor, method, None)
            if callable(fn):
                value = fn(netuid)
                if value is not None:
                    return float(getattr(value, "tao", value))
        get_hyper = getattr(self._subtensor, "get_hyperparameter", None)
        if callable(get_hyper):
            raw = get_hyper("Burn", netuid)
            if raw is not None:
                return float(raw) / 1e9  # rao -> tao
        raise RuntimeError(
            f"cannot read registration burn for netuid {netuid} — no "
            "recycle/burn/get_hyperparameter on this bittensor version"
        )

    def coldkey_balance_tao(self, coldkey_ss58: str) -> float:
        balance = self._subtensor.get_balance(coldkey_ss58)
        return float(getattr(balance, "tao", balance))

    def staked_alpha(self, coldkey_ss58: str, hotkey_ss58: str, netuid: int) -> float:
        """Alpha currently staked to this hotkey under this coldkey."""
        balance = self._subtensor.get_stake(
            coldkey_ss58=coldkey_ss58, hotkey_ss58=hotkey_ss58, netuid=netuid
        )
        return float(getattr(balance, "tao", balance))

    def is_registered(self, hotkey_ss58: str, netuid: int) -> int | None:
        return self._subtensor.get_uid_for_hotkey_on_subnet(hotkey_ss58, netuid)

    # --- writes ---

    def register(
        self, *, coldkey_mnemonic: str, hotkey_mnemonic: str, netuid: int
    ) -> ExtrinsicOutcome:
        """Burn the provider's TAO to register their neuron.

        Waits for finalisation. A registration we believe failed but which
        actually landed would burn a second fee on retry, so the caller must
        also re-check ``is_registered`` — which ``plan_registration`` does.
        """
        with ephemeral_wallet(coldkey_mnemonic, hotkey_mnemonic) as wallet:
            response = self._subtensor.burned_register(
                wallet=wallet,
                netuid=netuid,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
        return _outcome(response)

    def transfer_alpha(
        self,
        *,
        coldkey_mnemonic: str,
        hotkey_mnemonic: str,
        destination_coldkey_ss58: str,
        hotkey_ss58: str,
        netuid: int,
        alpha_amount: float,
    ) -> ExtrinsicOutcome:
        """Forward a provider's earned alpha to the address they gave us.

        The mirror of the gateway's alpha deposit watcher: the same
        ``SubtensorModule.StakeTransferred`` event, originating from us.

        ``Balance.from_tao(amount, netuid=netuid)`` is REQUIRED — the default
        netuid=0 produces a TAO-denominated Balance, and passing that as an
        alpha amount misdenominates the transfer.
        """
        from bittensor.utils.balance import Balance

        amount = Balance.from_tao(alpha_amount, netuid=netuid)
        with ephemeral_wallet(coldkey_mnemonic, hotkey_mnemonic) as wallet:
            response = self._subtensor.transfer_stake(
                wallet=wallet,
                destination_coldkey_ss58=destination_coldkey_ss58,
                hotkey_ss58=hotkey_ss58,
                origin_netuid=netuid,
                destination_netuid=netuid,
                amount=amount,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
        return _outcome(response)

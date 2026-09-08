"""Authenticated encryption for custodied miner mnemonics.

Backs the ``SecretBox`` protocol in ``domain.managed_wallet``. Every provider
mnemonic in ``managed_wallets`` passes through here; a mnemonic is a bearer
credential for that provider's TAO, so this file is the boundary between "we
hold keys responsibly" and "we lost everyone's money at once".

WHAT THIS PROTECTS AGAINST
  * Database compromise — a stolen dump, a leaked backup, a replica snapshot, a
    misconfigured read-only user. The master key lives in the validator's
    environment, not in Postgres, so ciphertext alone is inert.
  * Row substitution — every ciphertext is bound to its ``wallet_id`` and role
    via AES-GCM's additional authenticated data. Pasting provider A's coldkey
    blob into provider B's row fails to authenticate instead of quietly
    decrypting into the wrong account.
  * Silent corruption — GCM is authenticated, so a truncated or flipped
    ciphertext raises instead of returning garbage we might submit to a chain.

WHAT IT DOES NOT PROTECT AGAINST, STATED PLAINLY
  * Anyone who can read the validator's environment or memory. The master key is
    an env var, so root on the validator host, a container escape, or an
    attacker with `docker inspect` gets every mnemonic. A KMS or HSM would keep
    the key material off the host and make decryption an auditable remote call;
    this does not. That is a deliberate, documented gap for launch, and it is
    the first thing to close if the number of custodied providers grows.
  * Insider access. There is no split-knowledge or dual-control here — one
    operator with production access can decrypt everything. Note the fleet has
    an unresolved history on exactly this axis (2026-06-10: a GPU-stealing
    binary appeared on .12 and all five candidate SSH keys belonged to the
    team). Access logging on decrypt is the minimum compensating control.

FORMAT: ``v1:<b64 nonce>:<b64 ciphertext||tag>``. The version prefix exists so a
future rotation to KMS-wrapped data keys can be introduced without a migration
that must decrypt-and-re-encrypt every row in one shot.
"""
from __future__ import annotations

import base64
import os
from typing import Final

_SCHEME_V1: Final = "v1"
_NONCE_BYTES: Final = 12  # 96-bit, the AES-GCM standard nonce size
_KEY_BYTES: Final = 32  # AES-256

#: Env var holding the base64-encoded 32-byte master key.
MASTER_KEY_ENV: Final = "GREENCOMPUTE_WALLET_MASTER_KEY"


class SecretBoxError(RuntimeError):
    """Encryption or decryption failed. Never carries plaintext or key bytes."""


def generate_master_key() -> str:
    """Mint a new base64 master key for ``GREENCOMPUTE_WALLET_MASTER_KEY``.

    Operator helper — run once, store in the secret manager, never in git.
    Losing this key makes every custodied mnemonic permanently unrecoverable,
    which means every provider's funds are gone with it. Back it up before the
    first wallet is provisioned, not after.
    """
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode()


class AesGcmSecretBox:
    """AES-256-GCM ``SecretBox`` keyed from the environment.

    The `cryptography` import is deferred to construction so that importing this
    module stays free in environments that don't have it — the validator
    installs its heavier dependencies at container start.
    """

    def __init__(self, master_key_b64: str | None = None) -> None:
        raw = master_key_b64 if master_key_b64 is not None else os.getenv(MASTER_KEY_ENV)
        if not raw:
            raise SecretBoxError(
                f"{MASTER_KEY_ENV} is unset — refusing to run with custodied "
                "wallets and no encryption key"
            )
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 - normalise to our error type
            raise SecretBoxError(f"{MASTER_KEY_ENV} is not valid base64") from exc
        if len(key) != _KEY_BYTES:
            raise SecretBoxError(
                f"{MASTER_KEY_ENV} must decode to {_KEY_BYTES} bytes, got {len(key)}"
            )
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise SecretBoxError(
                "the `cryptography` package is required to handle custodied "
                "mnemonics but is not installed"
            ) from exc
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, *, context: str) -> str:
        """Seal ``plaintext``, binding it to ``context`` (e.g. ``"<id>:coldkey"``).

        The same plaintext encrypts differently every time — the nonce is fresh
        per call — so ciphertext never reveals that two providers were issued
        the same key material.
        """
        if not context:
            # An empty AAD would silently drop the row-binding property that
            # makes ciphertext non-transplantable, so refuse rather than weaken.
            raise SecretBoxError("context is required and must be non-empty")
        nonce = os.urandom(_NONCE_BYTES)
        try:
            sealed = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        except Exception as exc:  # noqa: BLE001
            raise SecretBoxError("encryption failed") from exc
        return (
            f"{_SCHEME_V1}:"
            f"{base64.b64encode(nonce).decode()}:"
            f"{base64.b64encode(sealed).decode()}"
        )

    def decrypt(self, ciphertext: str, *, context: str) -> str:
        """Open a sealed mnemonic. Raises if ``context`` doesn't match the one
        used to seal it, which is what makes a transplanted row fail loudly."""
        if not context:
            raise SecretBoxError("context is required and must be non-empty")
        try:
            scheme, nonce_b64, payload_b64 = ciphertext.split(":", 2)
        except ValueError as exc:
            raise SecretBoxError("malformed ciphertext") from exc
        if scheme != _SCHEME_V1:
            raise SecretBoxError(f"unsupported ciphertext scheme: {scheme!r}")
        try:
            nonce = base64.b64decode(nonce_b64, validate=True)
            payload = base64.b64decode(payload_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SecretBoxError("malformed ciphertext encoding") from exc
        if len(nonce) != _NONCE_BYTES:
            raise SecretBoxError("malformed ciphertext nonce")
        try:
            opened = self._aead.decrypt(nonce, payload, context.encode())
        except Exception as exc:  # noqa: BLE001 - includes InvalidTag
            # Deliberately opaque: the caller learns it failed, not why, so this
            # can't be used as an oracle. The distinction that matters
            # operationally (wrong key vs tampered row) is not worth leaking.
            raise SecretBoxError(
                "decryption failed — wrong key, wrong context, or tampered ciphertext"
            ) from exc
        return opened.decode()

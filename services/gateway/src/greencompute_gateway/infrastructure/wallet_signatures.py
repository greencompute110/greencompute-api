"""Proof that a user controls a wallet: they sign a message we issue with the
wallet's key, and we check the signature against the wallet's address.

- bittensor (TAO, alpha): sr25519 or ed25519, as produced by `btcli wallet
  sign`, the Polkadot.js / Talisman extensions (which wrap the message in
  <Bytes>…</Bytes>; Keypair.verify accepts both) or substrate-interface.
- evm (USDT/USDC on Ethereum and Base): an EIP-191 `personal_sign` signature,
  as produced by MetaMask and every Ethereum wallet. Contract wallets (EIP-1271)
  are not supported: they have no key to sign with.
"""
from __future__ import annotations

import re

CHAINS = ("bittensor", "evm")

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Which wallet family can send each deposit currency.
CURRENCY_CHAIN = {
    "tao": "bittensor",
    "alpha": "bittensor",
    "usdt-eth": "evm",
    "usdc-eth": "evm",
    "usdt-base": "evm",
    "usdc-base": "evm",
}


class InvalidAddress(ValueError):
    pass


def normalize_address(chain: str, address: str) -> str:
    """Canonical form used for storage and comparison: SS58 with the generic
    prefix 42 for bittensor, lowercase 0x-hex for evm."""
    address = (address or "").strip()
    if chain == "evm":
        if not _EVM_ADDRESS.match(address):
            raise InvalidAddress("expected a 0x-prefixed 40-hex-digit address")
        return address.lower()
    if chain == "bittensor":
        from scalecodec.utils.ss58 import ss58_decode, ss58_encode

        # ss58_decode passes 0x-hex through unchanged, so refuse it up front.
        if address.startswith("0x"):
            raise InvalidAddress("expected an SS58 address, e.g. 5F…")
        try:
            return ss58_encode(ss58_decode(address), ss58_format=42)
        except Exception as exc:
            raise InvalidAddress("expected an SS58 address, e.g. 5F…") from exc
    raise InvalidAddress(f"chain must be one of: {', '.join(CHAINS)}")


def _signature_bytes(signature: str) -> bytes:
    sig = (signature or "").strip()
    if sig.startswith("0x"):
        sig = sig[2:]
    try:
        return bytes.fromhex(sig)
    except ValueError as exc:
        raise ValueError("signature must be hex") from exc


def verify_signature(chain: str, address: str, message: str, signature: str) -> bool:
    """True when `signature` over `message` was made by `address`'s key."""
    try:
        sig = _signature_bytes(signature)
    except ValueError:
        return False
    if chain == "bittensor":
        return _verify_bittensor(address, message, sig)
    if chain == "evm":
        return _verify_evm(address, message, sig)
    return False


def _verify_bittensor(address: str, message: str, sig: bytes) -> bool:
    from substrateinterface import Keypair, KeypairType

    if len(sig) != 64:
        return False
    for crypto_type in (KeypairType.SR25519, KeypairType.ED25519):
        try:
            keypair = Keypair(ss58_address=address, ss58_format=42, crypto_type=crypto_type)
            if keypair.verify(message.encode(), sig):
                return True
        except Exception:
            continue
    return False


def _verify_evm(address: str, message: str, sig: bytes) -> bool:
    from eth_keys import keys
    from eth_utils import keccak

    if len(sig) != 65:
        return False
    data = message.encode()
    digest = keccak(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    v = sig[64]
    if v >= 27:
        v -= 27
    if v not in (0, 1):
        return False
    try:
        signature = keys.Signature(vrs=(v, int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big")))
        recovered = signature.recover_public_key_from_msg_hash(digest).to_checksum_address()
    except Exception:
        return False
    return recovered.lower() == address.lower()

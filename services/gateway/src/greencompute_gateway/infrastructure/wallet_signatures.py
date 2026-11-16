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


# secp256k1's group order. A signature (r, s) and (r, n - s) are both valid
# for the same message; wallets produce the low one (EIP-2), so the strict
# check below refuses the other rather than accept two encodings of one proof.
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def wallet_tag(chain: str, address: str) -> str:
    """api_keys.issued_by_wallet for keys a wallet's sign-ins issued."""
    return f"{chain}:{address}"


def detect_chain(address: str) -> str:
    """"evm" for a 0x address, else "bittensor": what a sign-in request means
    when it names only the address."""
    return "evm" if (address or "").strip().lower().startswith("0x") else "bittensor"


def checksum_address(address: str) -> str:
    """EIP-55 mixed-case form of an EVM address, as Sign-In with Ethereum
    messages must show it."""
    from eth_utils import to_checksum_address

    return to_checksum_address(address)


def normalize_address_strict(chain: str, address: str) -> str:
    """normalize_address, and an EVM address typed in mixed case must carry a
    valid EIP-55 checksum: a wrong one means a typo, and a sign-in for a
    mistyped address creates an account nobody can pay into."""
    canonical = normalize_address(chain, address)
    raw = (address or "").strip()
    if chain == "evm" and raw[2:] != raw[2:].lower() and raw[2:] != raw[2:].upper():
        from eth_utils import is_checksum_address

        if not is_checksum_address(raw):
            raise InvalidAddress("the address's mixed-case checksum is wrong; copy it from your wallet again")
    return canonical


def signature_problem(chain: str, signature: str) -> str | None:
    """Why `signature` can't be one this chain's wallets make, in words that
    say what to do; None when its shape is fine (it may still not verify)."""
    try:
        sig = _signature_bytes(signature)
    except ValueError:
        return "the signature must be hex, as the wallet returned it (0x...)"
    if chain == "evm":
        if len(sig) > 65:
            return (
                "this looks like a smart-contract wallet's signature (EIP-1271/6492), which can't sign in; "
                "use a wallet with its own key, such as MetaMask or the Coinbase Wallet extension"
            )
        if len(sig) != 65:
            return "an Ethereum signature is 65 bytes (132 hex characters with 0x)"
    elif chain == "bittensor" and len(sig) != 64:
        return "a Bittensor signature is 64 bytes (130 hex characters with 0x); sign with the coldkey"
    return None


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


def verify_signature(chain: str, address: str, message: str, signature: str, *, strict: bool = False) -> bool:
    """True when `signature` over `message` was made by `address`'s key.
    `strict` (sign-in) also refuses an EVM signature's high-s twin."""
    try:
        sig = _signature_bytes(signature)
    except ValueError:
        return False
    if chain == "bittensor":
        return _verify_bittensor(address, message, sig)
    if chain == "evm":
        return _verify_evm(address, message, sig, strict=strict)
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


def _verify_evm(address: str, message: str, sig: bytes, *, strict: bool = False) -> bool:
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
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big")
    if strict and not (0 < r < _SECP256K1_N and 0 < s <= _SECP256K1_N // 2):
        return False
    try:
        signature = keys.Signature(vrs=(v, r, s))
        recovered = signature.recover_public_key_from_msg_hash(digest).to_checksum_address()
    except Exception:
        return False
    return recovered.lower() == address.lower()

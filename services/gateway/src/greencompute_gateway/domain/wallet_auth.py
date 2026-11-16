"""The text a wallet signs to sign in, and what it is bound to.

EVM wallets get a Sign-In with Ethereum message (EIP-4361), the format
MetaMask and Coinbase Wallet recognise: they show it as a sign-in request and
warn when its domain isn't the site asking. Bittensor wallets get the same
fields on one line, so it passes cleanly to `btcli wallet sign --message`.

Every message names our domain and URI, the wallet, what signing it does, a
one-time nonce and when it expires, so a signature can't be replayed: not
elsewhere (another domain's message is different text), not later (it
expires), not twice (the nonce is claimed once), and not for something else
(the statement says sign in or revoke).

- GREENCOMPUTE_WALLET_AUTH_DOMAIN (default www.green-compute.com): the host
  the website is served from. Wallets compare it with the page's origin.
- GREENCOMPUTE_WALLET_AUTH_URI (default https://<domain>)
- GREENCOMPUTE_WALLET_CHALLENGE_TTL_SECONDS (default 300)
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime

DEFAULT_DOMAIN = "www.green-compute.com"
CHALLENGE_TTL_SECONDS = 300

#: What a signature is for. The statement is part of the signed text.
ACTIONS: dict[str, str] = {
    "sign_in": "Sign in to Green Compute with this wallet and issue an API key. This moves no funds.",
    "revoke_keys": "Revoke every Green Compute API key this wallet signed in with. This moves no funds.",
}

#: EIP-155 chain ids a Sign-In with Ethereum message may name: Ethereum and
#: Base, where we take payments. It says which network the wallet is on and
#: changes nothing about the account (one address, both networks).
EVM_CHAIN_IDS = {1: "Ethereum", 8453: "Base"}


def auth_domain() -> str:
    return (os.environ.get("GREENCOMPUTE_WALLET_AUTH_DOMAIN") or "").strip() or DEFAULT_DOMAIN


def auth_uri() -> str:
    return (os.environ.get("GREENCOMPUTE_WALLET_AUTH_URI") or "").strip() or f"https://{auth_domain()}"


def challenge_ttl_seconds() -> int:
    try:
        return max(60, min(900, int(os.environ.get("GREENCOMPUTE_WALLET_CHALLENGE_TTL_SECONDS") or CHALLENGE_TTL_SECONDS)))
    except ValueError:
        return CHALLENGE_TTL_SECONDS


def new_nonce() -> str:
    """32 hex characters (128 bits): EIP-4361 asks for 8+ alphanumerics."""
    return secrets.token_hex(16)


def _rfc3339(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sign_in_message(
    *,
    chain: str,
    display_address: str,
    action: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
    chain_id: int = 1,
    domain: str | None = None,
    uri: str | None = None,
) -> str:
    """The exact text to sign. `display_address` is EIP-55 checksummed for
    EVM (the standard requires it) and SS58 for Bittensor."""
    domain = domain or auth_domain()
    uri = uri or auth_uri()
    statement = ACTIONS[action]
    issued, expires = _rfc3339(issued_at), _rfc3339(expires_at)
    if chain == "evm":
        return (
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{display_address}\n"
            "\n"
            f"{statement}\n"
            "\n"
            f"URI: {uri}\n"
            "Version: 1\n"
            f"Chain ID: {chain_id}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued}\n"
            f"Expiration Time: {expires}"
        )
    return (
        f"{domain} wants you to sign in with your Bittensor account: {display_address}. {statement} "
        f"URI: {uri}. Nonce: {nonce}. Issued At: {issued}. Expiration Time: {expires}."
    )

"""btauth/1: HTTP requests signed with a Bittensor hotkey
(https://www.bittensor.com/docs/guides/signed-requests, the normative wire
format; the SDK's bittensor.http_auth implements it in bittensor 11, which we
don't run, so it is built here on substrate-interface's sr25519).

A signed request carries
    X-Bittensor-Version: 1
    X-Bittensor-Hotkey: <sender ss58>
    X-Bittensor-Receiver: <our service hotkey ss58>
    X-Bittensor-Nonce: <unix time in nanoseconds>
    X-Bittensor-Signature: 0x<hex>
and the signature is over the UTF-8 bytes of eight lines joined by "\\n":
    btauth/1, scheme, METHOD, request target (path + ?query as sent),
    sha256(raw body) hex, nonce, sender, receiver.

Verification, in the standard's order: shape, receiver, freshness (10 s
old to 2 s ahead), signature, then replay (each (hotkey, nonce) once, looked
up only after the signature verified, so unsigned traffic can't fill it).

Only sr25519 is accepted. The standard has the server pin each sender's
scheme from its own records, never from X-Bittensor-Crypto; ours is sr25519
for every hotkey (what `btcli wallet new-hotkey` makes), so the hashed and
ed25519 schemes are refused rather than guessed.

The signature is checked with the sr25519 bindings on the exact payload
bytes (the substrate signing context), never a wrapped form.

The replay store is in this process's memory: the gateway runs as one
process (as for the per-key rate limit), and an entry is only needed for the
12 s a nonce can be fresh.
"""
from __future__ import annotations

import hashlib
import heapq
import re
import threading
import time
from dataclasses import dataclass
from typing import Mapping

VERSION = "1"
PROTOCOL = "btauth/1"
SCHEME = "sr25519"
MAX_AGE_NS = 10 * 10**9
ALLOWED_SKEW_NS = 2 * 10**9

H_VERSION = "x-bittensor-version"
H_HOTKEY = "x-bittensor-hotkey"
H_RECEIVER = "x-bittensor-receiver"
H_NONCE = "x-bittensor-nonce"
H_SIGNATURE = "x-bittensor-signature"
H_CRYPTO = "x-bittensor-crypto"
HEADERS = (H_VERSION, H_HOTKEY, H_RECEIVER, H_NONCE, H_SIGNATURE, H_CRYPTO)
#: What makes a request a signed one (the other headers must then be
#: there). Only the signature: a client that happens to send some other
#: X-Bittensor-* header with its API key keeps working as before.
TRIGGERS = (H_SIGNATURE,)

_NONCE = re.compile(r"^[0-9]{1,20}$")
_SIG = re.compile(r"^(0x)?[0-9a-fA-F]{128}$")


class BtAuthError(Exception):
    """Why a signed request was refused. `reason` is one of malformed,
    wrong_receiver, stale, bad_signature, replayed, unsupported_crypto; the
    message is safe to return to the unauthenticated caller."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class Caller:
    hotkey: str  # the sender exactly as sent (any SS58 prefix)
    public_key: bytes
    receiver: str
    nonce: int


def present(headers: Mapping[str, str]) -> bool:
    """Whether the request claims to be signed (lower-case header names)."""
    return any(h in headers for h in TRIGGERS)


def build_payload(*, scheme: str, method: str, target: str, body: bytes, nonce: int | str, sender: str,
                  receiver: str | None) -> bytes:
    """The exact bytes signed (bittensor.http_auth.build_payload's)."""
    return "\n".join([
        PROTOCOL, scheme, method.upper(), target, hashlib.sha256(body or b"").hexdigest(), str(nonce), sender,
        receiver or "-",
    ]).encode("utf-8")


def _public_key(ss58: str) -> bytes:
    from scalecodec.utils.ss58 import ss58_decode

    value = (ss58 or "").strip()
    if not value or value.startswith("0x") or value != ss58:
        raise ValueError("not an ss58 address")
    key = bytes.fromhex(ss58_decode(value))
    if len(key) != 32:
        raise ValueError("not a 32-byte account")
    return key


def sign(keypair, *, method: str, target: str, body: bytes, receiver: str, nonce: int | None = None) -> dict[str, str]:
    """Headers for a request signed by an sr25519 `keypair` (substrate-
    interface's Keypair): for our tests, examples and agents in Python."""
    nonce = time.time_ns() if nonce is None else nonce
    payload = build_payload(scheme=SCHEME, method=method, target=target, body=body, nonce=nonce,
                            sender=keypair.ss58_address, receiver=receiver)
    return {
        "X-Bittensor-Version": VERSION,
        "X-Bittensor-Hotkey": keypair.ss58_address,
        "X-Bittensor-Receiver": receiver,
        "X-Bittensor-Nonce": str(nonce),
        "X-Bittensor-Signature": "0x" + keypair.sign(payload).hex(),
    }


class NonceStore:
    """(public key, nonce) pairs accepted in the last few seconds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[tuple[bytes, int], int] = {}
        self._expiry: list[tuple[int, bytes, int]] = []

    def claim(self, public_key: bytes, nonce: int, now_ns: int) -> bool:
        """True the first time; False for a pair already accepted."""
        with self._lock:
            while self._expiry and self._expiry[0][0] < now_ns:
                _, key, n = heapq.heappop(self._expiry)
                if self._seen.get((key, n), now_ns) < now_ns:
                    del self._seen[(key, n)]
            if (public_key, nonce) in self._seen:
                return False
            expires = nonce + MAX_AGE_NS + 1
            self._seen[(public_key, nonce)] = expires
            heapq.heappush(self._expiry, (expires, public_key, nonce))
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
            self._expiry.clear()

    def __len__(self) -> int:
        return len(self._seen)


nonce_store = NonceStore()


def verify(
    headers: Mapping[str, str],
    body: bytes,
    *,
    method: str,
    target: str,
    receivers: list[str] | tuple[str, ...],
    now_ns: int | None = None,
    nonces: NonceStore | None = None,
) -> Caller:
    """The verified sender of a signed request, or BtAuthError. `headers`
    has lower-case names; `target` is the request target as sent (raw path
    plus ?query); `receivers` are our service hotkeys."""
    nonces = nonce_store if nonces is None else nonces
    now_ns = time.time_ns() if now_ns is None else now_ns

    # 1. Shape.
    missing = [h for h in (H_VERSION, H_HOTKEY, H_NONCE, H_SIGNATURE) if not (headers.get(h) or "").strip()]
    if missing:
        raise BtAuthError("malformed", "a signed request needs the headers " + ", ".join(
            "X-Bittensor-" + h.split("-", 2)[2].capitalize() for h in missing) + " (btauth/1)")
    if headers[H_VERSION].strip() != VERSION:
        raise BtAuthError("malformed", "X-Bittensor-Version must be 1 (btauth/1)")
    crypto = (headers.get(H_CRYPTO) or "").strip().lower()
    if crypto and crypto != SCHEME:
        raise BtAuthError(
            "unsupported_crypto",
            "only sr25519 hotkeys can sign requests here (send no X-Bittensor-Crypto header); "
            "make one with `btcli wallet new-hotkey`",
        )
    sender = headers[H_HOTKEY].strip()
    try:
        public_key = _public_key(sender)
    except Exception:
        raise BtAuthError("malformed", "X-Bittensor-Hotkey must be the sender hotkey's ss58 address") from None
    nonce_text = headers[H_NONCE].strip()
    if not _NONCE.match(nonce_text):
        raise BtAuthError("malformed", "X-Bittensor-Nonce must be the unix time in nanoseconds, as a decimal string")
    nonce = int(nonce_text)
    signature = headers[H_SIGNATURE].strip()
    if not _SIG.match(signature):
        raise BtAuthError("malformed", "X-Bittensor-Signature must be 0x and the 64-byte signature in hex")

    # 2. Receiver: requests addressed to us only, never unbound ones.
    receiver = (headers.get(H_RECEIVER) or "").strip()
    ours = {}
    for hk in receivers:
        try:
            ours[_public_key(hk)] = hk
        except Exception:
            continue
    try:
        receiver_key = _public_key(receiver) if receiver and receiver != "-" else None
    except Exception:
        receiver_key = None
    if receiver_key is None or receiver_key not in ours:
        raise BtAuthError(
            "wrong_receiver",
            f"X-Bittensor-Receiver must be our service hotkey {receivers[0] if receivers else ''}".strip()
            + " (GET /gamma/services.json)",
        )

    # 3. Freshness.
    if now_ns - nonce > MAX_AGE_NS or nonce - now_ns > ALLOWED_SKEW_NS:
        raise BtAuthError(
            "stale",
            "the request's nonce is more than 10 s old or 2 s ahead: sign every attempt with a fresh "
            "X-Bittensor-Nonce (unix time in nanoseconds) and check the clock",
        )

    # 4. Signature, under sr25519 only.
    payload = build_payload(scheme=SCHEME, method=method, target=target, body=body, nonce=nonce_text,
                            sender=sender, receiver=receiver)
    try:
        import sr25519

        # The bindings directly, on the exact payload: substrate-interface's
        # Keypair.verify would also accept a signature over the payload
        # wrapped in <Bytes>…</Bytes>, which btauth/1 never allows.
        ok = bool(sr25519.verify(
            bytes.fromhex(signature[2:] if signature.startswith("0x") else signature), payload, public_key))
    except Exception:
        ok = False
    if not ok:
        raise BtAuthError(
            "bad_signature",
            "the signature doesn't match the request: sign the btauth/1 payload over this method, path with "
            "?query as sent, sha256 of the exact body bytes, nonce, sender and receiver",
        )

    # 5. Replay.
    if not nonces.claim(public_key, nonce, now_ns):
        raise BtAuthError("replayed", "this nonce was already used: sign each request, retries too, with a new one")
    return Caller(hotkey=sender, public_key=public_key, receiver=receiver, nonce=nonce)

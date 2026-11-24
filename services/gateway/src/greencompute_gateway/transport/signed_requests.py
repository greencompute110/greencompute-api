"""btauth/1 at the edge: the request-context middleware (main.py) hands every
request carrying X-Bittensor-* headers here before the app sees it.

The signature covers the exact body bytes, so they are read here, hashed and
replayed to the app unchanged; the request target is rebuilt from the raw
(percent-encoded) path and query, as sent. The outcome, the verified hotkey
or why it failed, is left in `security.current_signed_request`; an
authenticated route then acts as the hotkey's account (security.py), and a
public route ignores it. When these headers are present, any Authorization
header is ignored, so OpenAI-style SDKs can send a placeholder key.
"""
from __future__ import annotations

import json
import os

from greencompute_gateway.domain import gamma
from greencompute_gateway.infrastructure import btauth
from greencompute_gateway.transport.security import SignedRequest

#: The largest body a signed request may carry (a 1M-token chat request is
#: ~5 MB); larger uploads use an API key.
MAX_SIGNED_BODY_BYTES = 64 * 1024 * 1024


def _max_body() -> int:
    try:
        return max(1024, int(os.environ.get("GREENCOMPUTE_SIGNED_REQUEST_MAX_BODY_BYTES") or MAX_SIGNED_BODY_BYTES))
    except ValueError:
        return MAX_SIGNED_BODY_BYTES


def headers_of(scope) -> dict[str, list[str]] | None:
    """The X-Bittensor-* headers, lower-case, each with every value sent;
    None when there are none that make it a signed request."""
    found: dict[str, list[str]] = {}
    for name, value in scope.get("headers") or []:
        key = name.decode("latin-1").lower()
        if key.startswith("x-bittensor-"):
            found.setdefault(key, []).append(value.decode("latin-1"))
    return found if any(h in found for h in btauth.TRIGGERS) else None


def target_of(scope) -> str:
    """The request target as sent: the raw path and the query string."""
    raw = scope.get("raw_path")
    if raw:
        path = raw.decode("latin-1")
    else:
        from urllib.parse import quote

        path = quote(scope.get("path") or "/")
    query = scope.get("query_string") or b""
    return f"{path}?{query.decode('latin-1')}" if query else path


async def read_body(receive, limit: int) -> bytes | None:
    """The whole body, or None when it is larger than `limit` or the client
    went away."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


def replay(body: bytes, receive):
    """A receive() that hands the app `body` once, then the client's own
    messages (so a disconnect still arrives)."""
    sent = False

    async def receive_again():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return receive_again


async def too_large(send, limit: int) -> None:
    body = json.dumps({
        "detail": f"a signed request's body may be at most {limit // (1024 * 1024)} MB; use an API key for larger uploads",
        "error": {"code": "invalid_request", "retryable": False,
                  "message": "The signed request's body is too large; use an API key for larger uploads."},
    }).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def check(scope, found: dict[str, list[str]], body: bytes) -> SignedRequest:
    """Verify a signed request; never raises."""
    if any(len(values) > 1 for values in found.values()):
        return SignedRequest(error=btauth.BtAuthError("malformed", "each X-Bittensor-* header may be sent once"))
    headers = {k: v[0] for k, v in found.items()}
    try:
        caller = btauth.verify(
            headers, body, method=(scope.get("method") or "GET"), target=target_of(scope),
            receivers=gamma.service_hotkeys(),
        )
    except btauth.BtAuthError as exc:
        return SignedRequest(error=exc)
    return SignedRequest(hotkey=caller.hotkey)

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from greencompute_persistence import CredentialStore, get_metrics_store
from greencompute_control_plane.application.services import service
from greencompute_control_plane.config import settings
from greencompute_protocol import MemoryReplayStore, SignedRequest, verify_payload, verify_payload_hotkey

logger = logging.getLogger(__name__)


credential_store = CredentialStore(
    engine=service.repository.engine,
    session_factory=service.repository.session_factory,
)
metrics = get_metrics_store("greencompute-control-plane")
replay_store = MemoryReplayStore()


def require_admin_api_key(authorization: str | None, x_api_key: str | None) -> None:
    if not isinstance(x_api_key, str):
        x_api_key = None
    if not isinstance(authorization, str):
        authorization = None
    secret = x_api_key
    if secret is None and authorization and authorization.lower().startswith("bearer "):
        secret = authorization[7:].strip()
    if not secret:
        metrics.increment("auth.failure.missing_admin_key")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing admin api key")
    api_key = credential_store.get_api_key_by_secret(secret)
    if api_key is None:
        metrics.increment("auth.failure.invalid_admin_key")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin api key")
    if not api_key.admin:
        metrics.increment("auth.failure.non_admin_key")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin api key required")
    metrics.increment("auth.success.admin")


def require_miner_request(
    expected_hotkey: str,
    payload_bytes: bytes,
    x_miner_hotkey: str | None,
    x_miner_signature: str | None,
    x_miner_nonce: str | None,
    x_miner_timestamp: str | int | None,
    *,
    allow_unregistered: bool = False,
    registration_secret: str | None = None,
    x_miner_auth_mode: str | None = None,
) -> None:
    if not isinstance(x_miner_hotkey, str):
        x_miner_hotkey = None
    if not x_miner_hotkey:
        metrics.increment("auth.failure.missing_miner_header")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing miner hotkey header")
    if x_miner_hotkey != expected_hotkey:
        metrics.increment("auth.failure.miner_hotkey_mismatch")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="miner hotkey mismatch")
    if not isinstance(x_miner_signature, str) or not x_miner_signature:
        metrics.increment("auth.failure.missing_miner_signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing miner signature")
    if not isinstance(x_miner_nonce, str) or not x_miner_nonce:
        metrics.increment("auth.failure.missing_miner_nonce")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing miner nonce")
    try:
        timestamp = int(x_miner_timestamp) if x_miner_timestamp is not None else None
    except (TypeError, ValueError) as exc:
        metrics.increment("auth.failure.invalid_miner_timestamp")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid miner timestamp") from exc
    if timestamp is None:
        metrics.increment("auth.failure.missing_miner_timestamp")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing miner timestamp")

    auth_mode = x_miner_auth_mode or "hmac"
    signed = SignedRequest(
        actor_id=expected_hotkey,
        nonce=x_miner_nonce,
        timestamp=timestamp,
        signature=x_miner_signature,
        auth_mode=auth_mode,
    )

    if auth_mode == "hotkey":
        # Ed25519 verification — no shared secret needed
        result = verify_payload_hotkey(signed, payload_bytes, replay_store)
    else:
        # HMAC verification — needs shared secret from DB or registration
        miner = service.repository.get_miner(expected_hotkey)
        if not allow_unregistered and miner is None:
            metrics.increment("auth.failure.unknown_miner")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown miner")
        secret = registration_secret if allow_unregistered else (miner.auth_secret if miner is not None else None)
        if not secret:
            metrics.increment("auth.failure.missing_miner_secret")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing miner secret")
        result = verify_payload(secret, signed, payload_bytes, replay_store)

    if not result.valid:
        metrics.increment(f"auth.failure.miner_{result.reason or 'invalid'}")
        import hashlib
        body_sha = hashlib.sha256(payload_bytes).hexdigest()
        print(
            f"[AUTH FAIL] hotkey={expected_hotkey} auth_mode={auth_mode} "
            f"reason={result.reason!r} nonce={x_miner_nonce} ts={x_miner_timestamp} "
            f"body_len={len(payload_bytes)} body_sha256={body_sha} "
            f"body_repr={payload_bytes!r}",
            flush=True,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.reason or "invalid miner signature")

    if settings.whitelist_enabled and not service.repository.is_hotkey_whitelisted(expected_hotkey):
        metrics.increment("auth.failure.miner_not_whitelisted")
        print(f"[AUTH REJECT] hotkey={expected_hotkey} reason=not_whitelisted", flush=True)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="hotkey not whitelisted")

    metrics.increment("auth.success.miner")

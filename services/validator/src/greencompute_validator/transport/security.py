from __future__ import annotations

import os

from fastapi import HTTPException, status

from greencompute_persistence import CredentialStore, get_metrics_store
from greencompute_protocol import MemoryReplayStore, SignedRequest, verify_payload, verify_payload_hotkey
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_validator.application.services import service


def _env_master_admin_key() -> str:
    """Master admin key from env. Matches the gateway's break-glass logic so
    a single env var lets ops authenticate against both surfaces."""
    return (os.environ.get("GREENCOMPUTE_ADMIN_API_KEY") or "").strip()


credential_store = CredentialStore(
    engine=service.repository.engine,
    session_factory=service.repository.session_factory,
)
metrics = get_metrics_store("greencompute-validator")
replay_store = MemoryReplayStore()
control_plane_repository = ControlPlaneRepository()


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
    # Break-glass: env-var master admin key (matches gateway's path).
    master = _env_master_admin_key()
    if master and secret == master:
        metrics.increment("auth.success.master_admin")
        return
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
        verification = verify_payload_hotkey(signed, payload_bytes, replay_store)
    else:
        miner = control_plane_repository.get_miner(expected_hotkey)
        if miner is None:
            metrics.increment("auth.failure.unknown_miner")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown miner")
        verification = verify_payload(miner.auth_secret, signed, payload_bytes, replay_store)

    if not verification.valid:
        metrics.increment(f"auth.failure.miner_{verification.reason or 'invalid'}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=verification.reason or "invalid miner signature",
        )
    metrics.increment("auth.success.miner")


def verify_application_signature(
    hotkey: str,
    body: bytes,
    signature: str | None,
    nonce: str | None,
    timestamp: str | int | None,
    auth_mode: str | None,
) -> bool:
    """Non-raising: does the applicant prove they control `hotkey` by signing
    `body` (the raw `details` JSON) with their hotkey? Public applications must
    be hotkey-signed (no shared secret exists for an unknown applicant). A wide
    replay window accommodates the human paste-into-form flow. Returns True only
    on a valid ed25519 signature over the exact body bytes."""
    if not (isinstance(signature, str) and signature and isinstance(nonce, str) and nonce):
        return False
    try:
        ts = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        return False
    if ts is None:
        return False
    if (auth_mode or "hotkey") != "hotkey":
        return False
    signed = SignedRequest(actor_id=hotkey, nonce=nonce, timestamp=ts, signature=signature, auth_mode="hotkey")
    try:
        result = verify_payload_hotkey(signed, body, replay_store, window_seconds=600)
    except Exception:  # noqa: BLE001 — malformed input must never crash submit
        return False
    if result.valid:
        metrics.increment("auth.success.application_signature")
    else:
        metrics.increment(f"auth.failure.application_{result.reason or 'invalid'}")
    return result.valid

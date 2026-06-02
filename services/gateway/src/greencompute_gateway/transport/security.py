from __future__ import annotations

import contextvars
import hmac
import os

from fastapi import HTTPException, status

from greencompute_persistence import CredentialStore, FixedWindowRateLimiter, get_metrics_store
from greencompute_protocol import APIKeyRecord


credential_store = CredentialStore()
rate_limiter = FixedWindowRateLimiter()
metrics = get_metrics_store("greencompute-gateway")

# Per-IP auth-FAILURE rate limit. Each failed auth attempt (missing/invalid
# key, admin-required, bad provision secret) consumes one slot in a fixed
# window keyed on the client IP, so an attacker can make at most
# AUTH_FAILURE_LIMIT bad attempts per AUTH_FAILURE_WINDOW_SECONDS per IP per
# worker. Legitimate single-key callers never fail, so they never hit it.
# NOTE: FixedWindowRateLimiter is in-memory per-process, so with N uvicorn
# workers the effective ceiling is N*AUTH_FAILURE_LIMIT — an acceptable
# brute-force speed-bump; a shared Redis limiter is a follow-up.
AUTH_FAILURE_LIMIT = 20
AUTH_FAILURE_WINDOW_SECONDS = 60

# Populated by the request-context middleware (main.py) with the per-request
# client IP so the auth helpers can throttle without threading a `request`
# argument through ~40 route handlers.
current_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gateway_current_client_ip", default=None
)


def _resolve_client_ip(explicit: str | None) -> str | None:
    """Prefer an explicitly-passed IP, else fall back to the request-context
    ContextVar set by the middleware."""
    if explicit:
        return explicit
    try:
        return current_client_ip.get()
    except LookupError:
        return None


def _ct_eq(a: str | None, b: str | None) -> bool:
    """Constant-time string equality for secret comparison.

    Uses hmac.compare_digest so the comparison time does not leak how many
    leading characters matched. Treats None as the empty string so a missing
    value never matches a present one.
    """
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _check_auth_failure_throttle(client_ip: str | None) -> None:
    """Bump the per-IP auth-failure window and raise 429 once it's exhausted.

    Called at every auth-failure branch BEFORE the 401/403 is raised, so the
    attempt itself counts toward the limit. No-op when the caller IP is
    unknown (we never throttle on a missing key — that would let an attacker
    strip the IP to dodge the limit; better to fall through to the normal
    401/403)."""
    client_ip = _resolve_client_ip(client_ip)
    if not client_ip:
        return
    result = rate_limiter.check(
        "auth_fail",
        client_ip,
        limit=AUTH_FAILURE_LIMIT,
        window_seconds=AUTH_FAILURE_WINDOW_SECONDS,
    )
    if not result.allowed:
        metrics.increment("auth.failure.rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many auth failures",
        )


# Synthetic admin record returned when the caller presents the
# GREENCOMPUTE_ADMIN_API_KEY env-var value. Lets ops recover access without
# needing a row in api_keys — break-glass mechanism.
_MASTER_ADMIN_KEY_ID = "env-master"


def _env_master_admin_key() -> str:
    """Read the master admin key on every call so .env changes after restart
    take effect without re-importing."""
    return (os.environ.get("GREENCOMPUTE_ADMIN_API_KEY") or "").strip()


def _master_admin_record(secret: str) -> APIKeyRecord:
    from datetime import UTC, datetime

    return APIKeyRecord(
        key_id=_MASTER_ADMIN_KEY_ID,
        user_id=None,
        name="env master admin",
        admin=True,
        scopes=[],
        secret=secret,
        created_at=datetime.now(UTC),
    )


def extract_api_key_secret(authorization: str | None, x_api_key: str | None) -> str | None:
    if not isinstance(x_api_key, str):
        x_api_key = None
    if not isinstance(authorization, str):
        authorization = None
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def require_api_key(
    authorization: str | None,
    x_api_key: str | None,
    *,
    admin_required: bool = False,
    client_ip: str | None = None,
):
    secret = extract_api_key_secret(authorization, x_api_key)
    if not secret:
        metrics.increment("auth.failure.missing_api_key")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key")
    # Env-var master admin key — break-glass auth that survives DB wipes.
    # Checked BEFORE the DB lookup so it works even when the api_keys table
    # is unreachable (e.g. during incident recovery). Constant-time compare so
    # the break-glass key can't be brute-forced via timing.
    master = _env_master_admin_key()
    if master and _ct_eq(secret, master):
        metrics.increment("auth.success.master_admin")
        return _master_admin_record(secret)
    api_key = credential_store.get_api_key_by_secret(secret)
    if api_key is None:
        metrics.increment("auth.failure.invalid_api_key")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
    if admin_required and not api_key.admin:
        metrics.increment("auth.failure.admin_required")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin api key required")
    metrics.increment("auth.success")
    return api_key


def _provision_secret() -> str:
    """Server-side provisioning secret. Read on every call so .env changes
    take effect after restart without re-importing."""
    return (os.environ.get("GREENCOMPUTE_PROVISION_SECRET") or "").strip()


def require_provision_or_admin(
    authorization: str | None,
    x_api_key: str | None,
    *,
    client_ip: str | None = None,
):
    """Gate provisioning endpoints (register / api-key mint) to a privileged
    caller: EITHER the server-side GREENCOMPUTE_PROVISION_SECRET (used by the
    UI server to provision new users) OR a real admin api key (incl. the env
    master-admin break-glass key).

    Returns a tuple ``(caller, is_master_admin)`` where:
      - ``caller`` is an APIKeyRecord-like principal (the master-admin record
        for the provision secret / master key, or the matched admin key).
      - ``is_master_admin`` is True ONLY for the env master-admin key. The
        provision secret can bind keys to an explicit user_id but may NOT mint
        admin keys — only the master admin key may grant admin=true.

    FAIL CLOSED: if neither GREENCOMPUTE_PROVISION_SECRET nor an admin api key
    is presented, the request is rejected (401/403). If the provision secret
    env var is UNSET, the provision path is simply unavailable — we never fall
    back to honoring anonymous callers.
    """
    secret = extract_api_key_secret(authorization, x_api_key)
    if not secret:
        metrics.increment("auth.failure.missing_api_key")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key")
    # 1) Server-side provision secret — non-admin provisioning principal.
    provision = _provision_secret()
    if provision and _ct_eq(secret, provision):
        metrics.increment("auth.success.provision_secret")
        return _master_admin_record(secret), False
    # 2) Env master-admin key — full admin (may grant admin=true).
    master = _env_master_admin_key()
    if master and _ct_eq(secret, master):
        metrics.increment("auth.success.master_admin")
        return _master_admin_record(secret), True
    # 3) A real admin api key from the DB.
    api_key = credential_store.get_api_key_by_secret(secret)
    if api_key is None:
        metrics.increment("auth.failure.invalid_api_key")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
    if not api_key.admin:
        metrics.increment("auth.failure.admin_required")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin api key required")
    metrics.increment("auth.success")
    return api_key, True


def enforce_rate_limit(subject: str, key: str, *, limit: int, window_seconds: int) -> None:
    result = rate_limiter.check(subject, key, limit=limit, window_seconds=window_seconds)
    if not result.allowed:
        metrics.increment(f"rate_limit.hit.{subject}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": "rate limit exceeded", "subject": subject, "reset_at": result.reset_at},
        )

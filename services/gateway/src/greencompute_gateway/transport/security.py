from __future__ import annotations

import contextvars
import hmac
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC

from fastapi import HTTPException, status

from greencompute_persistence import (
    CredentialStore,
    FixedWindowRateLimiter,
    TokenBucketRateLimiter,
    get_metrics_store,
    key_expired,
)
from greencompute_protocol import APIKeyRecord


credential_store = CredentialStore()
rate_limiter = FixedWindowRateLimiter()
metrics = get_metrics_store("greencompute-gateway")

# --- Per-key request rate (every authenticated /platform route and /mcp) --------
# A token bucket per API key: GREENCOMPUTE_KEY_RATE_PER_SECOND requests a
# second on average (default 20), in bursts of up to GREENCOMPUTE_KEY_RATE_BURST
# (default 40); 0 per second turns it off. Per key, never per IP: every
# client reaches the API from one address (the host's NAT). Admin keys (and
# the env master key) are exempt. In memory, in this process: no database
# round-trip; the gateway runs as one process, so the limit is exact (with N
# workers it would be N times as much). Each response says where the key
# stands: X-RateLimit-Limit (the burst), X-RateLimit-Remaining (requests
# that may be sent now) and X-RateLimit-Reset (seconds until it is full
# again); a 429 `rate_limited` adds Retry-After.
key_rate_limiter = TokenBucketRateLimiter()
KEY_RATE_PER_SECOND_DEFAULT = 20.0
KEY_RATE_BURST_DEFAULT = 40

# Set per HTTP request by the middleware (main.py): {"path", "counted",
# "headers"}. The first successful key check of a request takes one token
# and leaves the headers here for the middleware to add; later checks in the
# same request (an MCP tool runs its REST calls in-process) take none.
# Outside a request (tests, in-process callers, the terminal WebSocket) it is
# None and nothing is limited.
current_rate_limit: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "gateway_current_rate_limit", default=None
)


def key_rate_settings() -> tuple[float, int]:
    """(requests per second, burst) from the environment."""
    def number(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, "") or default)
        except ValueError:
            return default
        return value if math.isfinite(value) and value >= 0 else default

    rate = number("GREENCOMPUTE_KEY_RATE_PER_SECOND", KEY_RATE_PER_SECOND_DEFAULT)
    burst = int(number("GREENCOMPUTE_KEY_RATE_BURST", KEY_RATE_BURST_DEFAULT))
    return rate, max(1, burst)


def _rate_limited_path(path: str) -> bool:
    return path.startswith("/platform/") or path.rstrip("/") == "/mcp"


def _apply_key_rate_limit(api_key) -> None:
    """Take one request from the key's allowance (once per HTTP request) and
    raise 429 rate_limited when there is none left."""
    state = current_rate_limit.get()
    if state is None or state.get("counted") or not _rate_limited_path(state.get("path") or ""):
        return
    state["counted"] = True
    if getattr(api_key, "admin", False):
        return
    rate, burst = key_rate_settings()
    if rate <= 0:
        return
    result = key_rate_limiter.take(api_key.key_id, rate=rate, burst=burst)
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(math.ceil(result.reset_seconds)),
    }
    state["headers"] = headers
    if result.allowed:
        return
    from greencompute_gateway.transport.errors import coded

    retry_after = max(1, math.ceil(result.retry_after_seconds))
    metrics.increment("rate_limit.hit.api_key")
    raise coded(429, "rate_limited", {
        "message": (f"too many requests with this API key: at most {rate:g} a second on average, in bursts of "
                    f"up to {burst}. Wait {retry_after} s, then send it again"),
        "limit_per_second": rate,
        "burst": burst,
        "retry_after_seconds": retry_after,
        "hint": "space requests out, or poll GET /platform/events instead of each rental",
    }, headers={**headers, "Retry-After": str(retry_after)})

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
# The request's (method, path), also set by that middleware, so the auth check
# can enforce a key's scopes without each route declaring what it needs.
current_request_route: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "gateway_current_request_route", default=None
)


@dataclass(frozen=True)
class SignedRequest:
    """A request signed with a Bittensor hotkey (btauth/1): the verified
    hotkey, or why it didn't verify (an infrastructure.btauth.BtAuthError)."""

    hotkey: str | None = None
    error: Exception | None = None


# Set by the request-context middleware for a request carrying X-Bittensor-*
# headers (transport/signed_requests.py); None for every other request.
current_signed_request: contextvars.ContextVar[SignedRequest | None] = contextvars.ContextVar(
    "gateway_current_signed_request", default=None
)

_SIGNED_HELP = "https://www.green-compute.com/docs/gamma"


def _signed_request_key(signed: SignedRequest, client_ip: str | None):
    """The key a signed request acts as: its hotkey account's (gamma.py)."""
    from greencompute_gateway.transport.errors import coded

    if signed.error is not None or not signed.hotkey:
        metrics.increment("auth.failure.signed_request")
        _check_auth_failure_throttle(client_ip)
        reason = getattr(signed.error, "reason", "malformed")
        message = getattr(signed.error, "message", "the signed request is not valid")
        raise coded(status.HTTP_401_UNAUTHORIZED, "signed_request_invalid", {
            "message": f"signed request refused: {message}",
            "reason": reason,
            "docs": _SIGNED_HELP,
        })
    from greencompute_gateway.application.gamma import signed_request_key

    return signed_request_key(credential_store.session_factory, signed.hotkey)


def _check_scopes(api_key) -> None:
    """Refuse a scoped key a request its scopes don't cover. Keys with no
    scopes (full access) and admin keys are not restricted; outside a request
    (tests, in-process callers) there is nothing to check against."""
    if api_key.admin or not api_key.scopes:
        return
    route = current_request_route.get()
    if route is None:
        return
    from greencompute_gateway.domain.scopes import required_scope

    method, path = route
    needed = required_scope(method, path)
    if needed == "*":  # any valid key (the MCP endpoint; its tools check their own)
        return
    if needed is None or needed not in api_key.scopes:
        metrics.increment("auth.failure.scope")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "this API key's scopes don't allow this request",
                "required_scope": needed or "full access",
                "key_scopes": list(api_key.scopes),
                "hint": "use a key with that scope, or create one under Settings or POST /platform/api-keys",
            },
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
        exc = HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="too many auth failures")
        exc.error_code = "auth_throttled"  # see transport/errors.py
        raise exc


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


# A 401 is often the very first response an agent gets from us, so it has to
# say how to fix itself. The detail stays a plain string starting with the old
# text ("missing api key" / "invalid api key") so nothing matching on it breaks.
_AUTH_HELP = (
    ". Send `Authorization: Bearer <key>`. Create a key at "
    "https://www.green-compute.com/settings , or get one by signing with a wallet "
    "(POST /platform/auth/wallet/challenge, no email needed); docs: https://www.green-compute.com/docs"
)
_AUTH_HEADERS = {
    "WWW-Authenticate": 'Bearer realm="green-compute"',
    "Link": '<https://www.green-compute.com/docs>; rel="help", '
            '<https://www.green-compute.com/llms.txt>; rel="describedby"',
}


def _refuse_expired_key(api_key) -> None:
    """401 api_key_expired for a key past its `expires_at`. Not an auth
    failure for the per-IP throttle: the key is real, only old, and every
    client behind the host's NAT shares one IP, so counting these would lock
    everyone out over one agent's stale key."""
    if not key_expired(api_key):
        return
    from greencompute_gateway.transport.errors import coded

    metrics.increment("auth.failure.expired_api_key")
    expires = api_key.expires_at if api_key.expires_at.tzinfo else api_key.expires_at.replace(tzinfo=UTC)
    when = expires.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise coded(
        status.HTTP_401_UNAUTHORIZED,
        "api_key_expired",
        f"expired api key: it expired at {when}. Create a new key at https://www.green-compute.com/settings",
        headers={**_AUTH_HEADERS,
                 "WWW-Authenticate": 'Bearer realm="green-compute", error="invalid_token", '
                                     'error_description="the API key expired"'},
    )


def require_api_key(
    authorization: str | None,
    x_api_key: str | None,
    *,
    admin_required: bool = False,
    client_ip: str | None = None,
):
    signed = current_signed_request.get()
    if signed is not None:
        # A hotkey-signed request (btauth/1): it acts as the hotkey's account,
        # whatever Authorization header came with it, and never as an admin.
        api_key = _signed_request_key(signed, client_ip)
        _apply_key_rate_limit(api_key)
        if admin_required:
            metrics.increment("auth.failure.admin_required")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin api key required")
        _check_scopes(api_key)
        metrics.increment("auth.success.signed_request")
        return api_key
    secret = extract_api_key_secret(authorization, x_api_key)
    if not secret:
        metrics.increment("auth.failure.missing_api_key")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing api key" + _AUTH_HELP,
            headers=_AUTH_HEADERS,
        )
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api key" + _AUTH_HELP,
            headers=_AUTH_HEADERS,
        )
    _refuse_expired_key(api_key)
    _apply_key_rate_limit(api_key)
    if admin_required and not api_key.admin:
        metrics.increment("auth.failure.admin_required")
        _check_auth_failure_throttle(client_ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin api key required")
    _check_scopes(api_key)
    metrics.increment("auth.success")
    return api_key


def _provision_secret() -> str:
    """Server-side provisioning secret. Read on every call so .env changes
    take effect after restart without re-importing."""
    return (os.environ.get("GREENCOMPUTE_PROVISION_SECRET") or "").strip()


def is_provision_secret(authorization: str | None, x_api_key: str | None) -> bool:
    """True when the caller presented GREENCOMPUTE_PROVISION_SECRET (the
    website server). Not presenting it is not an auth failure, so unlike
    require_provision_or_admin this never counts toward the per-IP throttle:
    the website's server-side calls with a user's own key share a few egress
    IPs."""
    secret = extract_api_key_secret(authorization, x_api_key)
    provision = _provision_secret()
    return bool(secret and provision and _ct_eq(secret, provision))


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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing api key" + _AUTH_HELP,
            headers=_AUTH_HEADERS,
        )
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api key" + _AUTH_HELP,
            headers=_AUTH_HEADERS,
        )
    _refuse_expired_key(api_key)
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
        retry_after = max(1, math.ceil(result.reset_at - time.time()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": "rate limit exceeded",
                "subject": subject,
                "limit": limit,
                "window_seconds": window_seconds,
                "retry_after_seconds": retry_after,
                "reset_at": result.reset_at,
            },
            headers={"Retry-After": str(retry_after)},
        )

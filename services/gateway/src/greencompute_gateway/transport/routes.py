import base64
import contextlib
import hashlib
import http.client
import json
import logging
import math
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from greencompute_protocol.ssh_host_keys import known_hosts_line, ssh_key_fingerprint
from greencompute_protocol import (
    DeploymentState,
    BareMetalInquiryCreateRequest,
    BuildContextUploadRequest,
    BuildRequest,
    ChatCompletionRequest,
    CommercialInquiryCreateRequest,
    DeploymentCreateRequest,
    DeploymentUpdateRequest,
    GpuCapacityOverride,
    ProviderServerCreateRequest,
    UserProfileUpdateRequest,
    UserSecretCreateRequest,
    UserRegistrationRequest,
    WorkloadCreateRequest,
    WorkloadShareCreateRequest,
    WorkloadUpdateRequest,
)
from greencompute_control_plane.config import settings as control_plane_settings
from greencompute_control_plane.domain.metering import is_catalog_workload
from greencompute_gateway.application import account_deletion
from greencompute_gateway.application.services import (
    WEBSITE_ADMIN_KEY_NAME,
    GatewayDeploymentCreateRequest,
    model_url,
    service,
)
from greencompute_gateway.domain.routing import NoReadyDeploymentError
from greencompute_gateway.infrastructure.inference_client import (
    InferenceBadResponseError,
    InferenceConnectionError,
    InferenceTimeoutError,
    InferenceUpstreamError,
)
from greencompute_gateway.transport import schemas
from greencompute_gateway.transport.schemas import ok
from greencompute_gateway.transport.errors import SUPPORT, coded
from greencompute_gateway.transport.security import (
    current_client_ip,
    enforce_rate_limit,
    is_provision_secret,
    metrics,
    require_api_key,
    require_provision_or_admin,
)

router = APIRouter()
log = logging.getLogger(__name__)


# --- Top-up amount bounds (single source of truth for UI + backend) --------
# Keep the default minimum at $1 (don't reject existing low-value flows). The
# UI references MIN_TOPUP_USD so the preset/custom-input minimum can never
# drift from what the backend enforces (BILL-M3). MAX is env-overridable so
# ops can raise it for a known big customer without a redeploy.
MIN_TOPUP_USD = 5.0
try:
    MAX_TOPUP_USD = float(os.environ.get("GREENCOMPUTE_MAX_TOPUP_USD", "50000"))
except (TypeError, ValueError):
    MAX_TOPUP_USD = 50000.0


def _validate_topup_amount(raw) -> float:
    """Validate a user-supplied top-up amount: numeric, finite, within
    [MIN_TOPUP_USD, MAX_TOPUP_USD]. Rounds to cents server-side. Raises
    HTTPException(400) on any violation (NaN/Inf/non-numeric/out-of-range)."""
    try:
        amt = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount_usd must be a number")
    if not math.isfinite(amt):
        raise HTTPException(status_code=400, detail="amount_usd must be finite")
    if amt < MIN_TOPUP_USD:
        raise HTTPException(
            status_code=400, detail=f"amount_usd must be >= {MIN_TOPUP_USD:g}"
        )
    if amt > MAX_TOPUP_USD:
        raise HTTPException(
            status_code=400, detail=f"amount_usd must be <= {MAX_TOPUP_USD:g}"
        )
    return round(amt, 2)


def _audit_client_ip() -> str | None:
    """Best-effort client IP for audit logs, read from the request-context
    ContextVar populated by the middleware."""
    try:
        return current_client_ip.get()
    except LookupError:
        return None


def _feature_disabled(feature: str) -> None:
    raise coded(501, "not_implemented", {
        "status": "disabled",
        "feature": feature,
        "reason": "not implemented in greencompute-api",
    })


# Accounts sanctions screening blocked, by user id, re-checked once a minute:
# paid inference is a service too, and asking the database on every request
# would cost more than the request.
_BLOCKED_CACHE: dict[str, tuple[float, bool]] = {}


def _refuse_blocked_account(api_key) -> None:
    if api_key.user_id is None or api_key.admin:
        return
    import time as _time

    from greencompute_gateway.application import sanctions

    now = _time.monotonic()
    cached = _BLOCKED_CACHE.get(api_key.user_id)
    if cached is None or now - cached[0] > 60:
        try:
            blocked = bool(sanctions.account_blocked(service.repository.session_factory, api_key.user_id))
        except Exception:  # noqa: BLE001 - a screening fault mustn't take inference down
            blocked = bool(cached and cached[1])
        if len(_BLOCKED_CACHE) > 100_000:
            _BLOCKED_CACHE.clear()
        _BLOCKED_CACHE[api_key.user_id] = (now, blocked)
        cached = _BLOCKED_CACHE[api_key.user_id]
    if cached[1]:
        raise coded(403, "wallet_blocked", sanctions.BLOCKED_MESSAGE)


def _inherit_wallet_tag(user_id: str | None, parent_key_id: str | None, child_key_id: str | None) -> bool:
    """A key made with a key a wallet's sign-in issued is that wallet's too, so
    signing in again, revoking, unlinking the wallet or turning its sign-in off
    ends it as well: a phished signature can't leave a key behind that outlives
    every way of shutting the wallet out.

    Under the account's row lock, which every shut-out takes too: if the
    parent was shut out while this request ran, the new key is deleted and
    False returned (its secret never leaves). Only a database the lock and
    table don't exist in (a test store) skips it."""
    if not parent_key_id or not child_key_id or not user_id:
        return True
    from greencompute_persistence import session_scope
    from greencompute_persistence.orm import APIKeyORM, UserORM
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    try:
        with session_scope(service.repository.session_factory) as session:
            session.get(UserORM, user_id, with_for_update=True)
            parent = session.get(APIKeyORM, parent_key_id)
            child = session.get(APIKeyORM, child_key_id)
            if parent is None or not BillingRepository.caller_still_in(session, user_id, parent_key_id):
                if child is not None:
                    session.delete(child)
                return False
            if parent.issued_by_wallet and child is not None:
                child.issued_by_wallet = parent.issued_by_wallet
            return True
    except Exception:  # noqa: BLE001 - a store without these tables: nothing to tag
        log.exception("api keys: couldn't carry the wallet tag from %s to %s", parent_key_id, child_key_id)
        return True


def _api_key_out(key) -> dict:
    """An API key without its secret, as list/get/delete return it."""
    from greencompute_persistence import key_expired

    return schemas.APIKeyOut(
        key_id=key.key_id,
        name=key.name,
        user_id=key.user_id,
        admin=key.admin,
        scopes=key.scopes,
        max_active_rentals=key.max_active_rentals,
        daily_spend_limit_usd=key.daily_spend_limit_usd,
        created_at=key.created_at,
        expires_at=getattr(key, "expires_at", None),
        expired=key_expired(key),
    ).model_dump(mode="json")


def _created_key_out(created) -> dict:
    """The new key, with its secret (shown only now)."""
    return {**created.model_dump(mode="json"), "expires_at": _api_key_out(created)["expires_at"], "expired": False}


@router.post("/platform/api-keys", include_in_schema=False)
def create_api_key(
    payload: schemas.APIKeyCreate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Mint an api key. AUTHENTICATED — anonymous callers are rejected.

    Caller tiers (most → least privileged):
      - env master-admin key: may set an arbitrary user_id and admin=true.
      - GREENCOMPUTE_PROVISION_SECRET (UI server provisioning): may bind the
        key to an explicit user_id, but admin is forced false.
      - a normal user api key: may only mint a key bound to its OWN user_id;
        client-supplied user_id/admin/scopes are ignored.

    Client-supplied `admin`/`user_id`/`scopes` are NEVER trusted from a
    non-admin caller — privilege is derived from the authenticated principal.
    """
    try:
        caller, is_master_admin = require_provision_or_admin(authorization, x_api_key)
    except HTTPException as exc:
        # Not a privileged caller — fall back to self-service: a normal,
        # valid user key may mint a key for its own user_id only. A truly
        # anonymous/invalid caller still gets rejected here.
        if exc.status_code not in (401, 403):
            raise
        api_key = require_api_key(authorization, x_api_key)
        try:
            created = service.create_api_key(
                payload,
                caller_user_id=api_key.user_id,
                allow_user_id_override=False,  # bound to own user_id
                allow_admin_grant=False,
                caller_scopes=list(api_key.scopes or []),
                caller_limits=api_key,
            )
            if not _inherit_wallet_tag(api_key.user_id, api_key.key_id, created.key_id):
                raise HTTPException(status_code=401, detail="invalid api key")
            return _created_key_out(created)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    # Privileged caller. Both the env master admin AND the provision secret may
    # bind the key to an explicit user_id (the UI provisions a key for the
    # newly-registered user). Only the master admin may grant admin=true.
    return _created_key_out(service.create_api_key(
        payload,
        caller_user_id=caller.user_id,
        allow_user_id_override=True,
        allow_admin_grant=is_master_admin,
    ))


@router.get("/platform/api-keys")
def list_api_keys(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    keys = service.list_api_keys(user_id=api_key.user_id, admin=api_key.admin)
    if not api_key.admin:
        # An admin's managed website key isn't one of "their" keys to manage.
        keys = [k for k in keys if not (k.admin and k.name == WEBSITE_ADMIN_KEY_NAME)]
    return [_api_key_out(k) for k in keys]


@router.get("/platform/api-keys/{key_id}")
def get_api_key(
    key_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    key = service.get_api_key(key_id, user_id=api_key.user_id, admin=api_key.admin)
    if key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    return _api_key_out(key)


@router.delete("/platform/api-keys/{key_id}")
def delete_api_key(
    key_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.scopes and not api_key.admin:
        # A scoped key may only remove keys no broader than itself, so a
        # leaked one can't lock the owner out by deleting their full keys.
        target = service.get_api_key(key_id, user_id=api_key.user_id, admin=False)
        if target is not None and (not target.scopes or not set(target.scopes) <= set(api_key.scopes)):
            raise HTTPException(status_code=403, detail="a scoped key can't delete a key broader than itself")
    try:
        deleted = service.delete_api_key(key_id, user_id=api_key.user_id, admin=api_key.admin)
        return _api_key_out(deleted)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/platform/admin/admins", include_in_schema=False)
def list_admins(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — accounts with admin access to the website."""
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.list_admins()


@router.post("/platform/admin/admins", include_in_schema=False)
def add_admin(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — give an existing account admin access: {"email": "..."}."""
    api_key = require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.add_admin(str(payload.get("email") or ""), added_by=api_key.key_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc


@router.delete("/platform/admin/admins/{ident}", include_in_schema=False)
def remove_admin(
    ident: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — remove admin access (by email or user id); deletes their
    website admin key, so it takes effect immediately."""
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.remove_admin(ident)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc


@router.post("/platform/admin/website-key", include_in_schema=False)
def website_admin_key(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """For the website server (provision secret): the admin key of a signed-in
    account if it is an admin, else {"admin": false}. Never callable with a
    user's own key, so an admin's other keys can't be turned into admin ones."""
    require_provision_or_admin(authorization, x_api_key)
    user_id = str(payload.get("user_id") or "")
    key = service.website_admin_key(user_id) if user_id else None
    return {"admin": key is not None, "key": key}


@router.post("/platform/register", include_in_schema=False)
def register_user(
    payload: UserRegistrationRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Register (or idempotently return) a user. AUTHENTICATED — requires the
    provisioning secret or an admin key (the UI server sends the provision
    secret). register is idempotent-by-email so re-running is safe."""
    require_provision_or_admin(authorization, x_api_key)
    return service.register_user(payload).model_dump(mode="json")


# --- Signing in with a wallet ---------------------------------------------------
# No email, password or browser: challenge -> sign -> verify -> an API key.
# application/wallet_auth.py has the rules; these routes add rate limits and
# what an agent needs next (where to pay, the account's limits).

# Challenges, and verifies, per minute per client IP. Not also per wallet: a
# limit keyed on the address would let anyone lock a wallet out of signing in
# by asking for its challenges, and protects nothing the per-IP one doesn't.
#
# In production the host's network rewrites every incoming connection's source
# to its gateway (172.16.110.1: nginx sees nothing else), so this is in effect
# one limit for the whole internet. Sized for that: high enough that a busy
# launch or one noisy client can't lock everyone out of wallet sign-in, still
# a cap on challenge rows and signature checks. Lower it per IP if the host
# ever passes real client addresses through.
WALLET_AUTH_PER_IP = int(os.environ.get("GREENCOMPUTE_WALLET_AUTH_PER_MINUTE", "600") or 600)


def _wallet_auth_error(exc) -> HTTPException:
    retry = exc.extra.get("retry_after_seconds")
    return coded(exc.status, exc.code, exc.detail(), {"Retry-After": str(retry)} if retry else None)


def _wallet_auth_rate_limit(subject: str, authorization: str | None, x_api_key: str | None) -> None:
    """Per client IP. The website server's calls carry the provision secret
    and are limited per visitor there instead: through it, every visitor
    shares a few egress IPs."""
    if is_provision_secret(authorization, x_api_key):
        return
    enforce_rate_limit(subject, _audit_client_ip() or "unknown", limit=WALLET_AUTH_PER_IP, window_seconds=60)


def _wallet_deposit_addresses(chain: str) -> list[dict]:
    from greencompute_gateway.application.billing_service import BONUS_RATES, _base_currency, _deposit_address_for
    from greencompute_gateway.infrastructure.wallet_signatures import CURRENCY_CHAIN

    return [
        {"currency": c, "deposit_address": _deposit_address_for(c),
         "bonus_pct": BONUS_RATES.get(_base_currency(c), 0.0)}
        for c, ch in CURRENCY_CHAIN.items() if ch == chain and _deposit_address_for(c)
    ]


def _claim_wallet_payments(user_id: str, chain: str, address: str) -> list[dict]:
    """Credit what this wallet sent before it had an account (or before
    this account verified it), as linking a wallet does. Best effort: a
    failure leaves them claimable, and must not fail the sign-in."""
    try:
        return _get_billing().claim_wallet_deposits(user_id, chain, address)
    except Exception:  # noqa: BLE001
        log.exception("wallet sign-in: claiming earlier payments for %s failed", user_id)
        return []


@router.post("/platform/auth/wallet/challenge", response_model=None,
             responses=ok(schemas.WalletChallenge, "The message to sign, valid for 5 minutes"))
def wallet_auth_challenge(
    payload: schemas.WalletChallengeRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Sign in with a wallet, step 1 of 2 (no API key, email or browser
    needed): a one-time message to sign with the wallet, bound to
    green-compute.com and valid for 5 minutes. EVM wallets sign it with
    `personal_sign` (it is a Sign-In with Ethereum message); Bittensor
    wallets sign it with the coldkey (sr25519). Then send the signature to
    POST /platform/auth/wallet/verify."""
    from greencompute_gateway.application import wallet_auth

    _wallet_auth_rate_limit("wallet_challenge", authorization, x_api_key)
    try:
        return wallet_auth.issue_challenge(
            service.repository.session_factory, address=payload.address, chain=payload.chain,
            action=payload.action, chain_id=payload.chain_id, client_ip=_audit_client_ip(),
            subject=payload.subject,
        )
    except wallet_auth.WalletAuthError as exc:
        raise _wallet_auth_error(exc) from exc


@router.post("/platform/auth/wallet/verify", response_model=None,
             responses=ok(schemas.WalletSignIn, "Signed in: the new API key (shown only now)"))
def wallet_auth_verify(
    payload: schemas.WalletVerifyRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Sign in with a wallet, step 2 of 2: the signed challenge in, an API
    key out. The first time, this creates the account: no email, no
    password, the wallet is the account. A wallet already verified on an
    account (Billing > Verified wallets) signs in to that account.

    Each sign-in revokes the keys this wallet's earlier sign-ins issued
    (`keep_existing_keys: true` keeps them), never other keys: lost a key?
    Sign in again. `scopes`, `max_active_rentals` and `daily_spend_limit_usd`
    limit the new key; `expires_in_days` or `expires_at` make it expire. Payments this wallet sends to `deposit_addresses` are
    credited automatically. A `revoke_keys` challenge revokes and issues
    nothing."""
    from greencompute_gateway.application import wallet_auth
    from greencompute_persistence.key_limits import new_account_limits

    _wallet_auth_rate_limit("wallet_verify", authorization, x_api_key)
    sf = service.repository.session_factory
    try:
        result = wallet_auth.verify(
            sf, nonce=payload.nonce, signature=payload.signature, key_name=payload.key_name,
            scopes=payload.scopes, max_active_rentals=payload.max_active_rentals,
            daily_spend_limit_usd=payload.daily_spend_limit_usd, keep_existing_keys=payload.keep_existing_keys,
            expires_at=payload.requested_expiry(),
        )
    except wallet_auth.WalletAuthError as exc:
        raise _wallet_auth_error(exc) from exc
    if result["api_key"] is None:
        return {**result, "next_steps": ["No key was issued. Sign a new `sign_in` challenge to get one."]}
    chain, address, user_id = result["wallet"]["chain"], result["wallet"]["address"], result["user_id"]
    limits = new_account_limits(sf, user_id)
    if limits:
        limits = {**limits, "until": limits["until"].isoformat()}
    deposits = _wallet_deposit_addresses(chain)
    pay = ("TAO or alpha (subnet 110)" if chain == "bittensor" else "USDC or USDT on Base or Ethereum")
    result.update(
        account_limits=limits,
        deposit_addresses=deposits,
        credited=_claim_wallet_payments(user_id, chain, address),
        next_steps=[
            "Send the key as `Authorization: Bearer <api_key>` on every request.",
            f"Add credit: send {pay} from THIS wallet to the matching `deposit_addresses` entry. It is "
            "credited within a few minutes, with no invoice (event `payment.credited`; check "
            "GET /platform/billing/balance).",
            "Rent: GET /platform/pricing, then POST /platform/workloads and POST /platform/deployments "
            "(https://www.green-compute.com/docs/agents).",
        ],
    )
    if result["has_email"]:
        _email_wallet_sign_in(user_id, chain, address, result["key_name"])
    return result


def _email_wallet_sign_in(user_id: str, chain: str, address: str, key_name: str, *, event: str = "signed_in") -> None:
    """Tell an account with an email that a wallet just got a key for it
    (`event` "signed_in"), or may now sign in to it ("sign_in_on"), the way
    a new-device notice would. notify_user skips accounts with no email."""
    from greencompute_gateway.infrastructure.notify_website import notify_user

    notify_user(service.repository.session_factory, user_id, "wallet_sign_in", {
        "chain": chain, "address": address, "key_name": key_name or None, "event": event,
        "signed_in_at": datetime.now(UTC).isoformat(),
    })


@router.post("/platform/auth/wallet/website-session", include_in_schema=False)
def wallet_auth_website_session(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """For the website's "Connect wallet" sign-in (provision secret only):
    the same checks as verify, the account back, and a key for this website
    session alone (`api_key`, tagged "<chain>:<address>#web": it ends when
    the wallet is unlinked, its sign-in turned off or its keys revoked, and
    an agent signing in again with the wallet doesn't touch it). Body:
    {"nonce", "signature"}."""
    from greencompute_gateway.application import wallet_auth

    require_provision_or_admin(authorization, x_api_key)
    if not is_provision_secret(authorization, x_api_key):
        raise HTTPException(status_code=403, detail="only the website server may call this")
    try:
        result = wallet_auth.verify_for_website(
            service.repository.session_factory,
            nonce=str(payload.get("nonce") or ""), signature=str(payload.get("signature") or ""),
        )
    except wallet_auth.WalletAuthError as exc:
        raise _wallet_auth_error(exc) from exc
    result["credited"] = _claim_wallet_payments(result["user_id"], result["chain"], result["address"])
    if result["email"]:
        _email_wallet_sign_in(result["user_id"], result["chain"], result["address"], "website sign-in")
    return result


@router.post("/platform/auth/wallet/website-session/check", include_in_schema=False)
def wallet_auth_website_session_check(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """For the website (provision secret only): may a session that began
    with this wallet carry on? Body: {"user_id", "chain", "address"}. The
    website asks every few minutes and ends the session on false."""
    from greencompute_gateway.application import wallet_auth

    require_provision_or_admin(authorization, x_api_key)
    if not is_provision_secret(authorization, x_api_key):
        raise HTTPException(status_code=403, detail="only the website server may call this")
    return {"ok": wallet_auth.website_session_ok(
        service.repository.session_factory,
        user_id=str(payload.get("user_id") or ""),
        chain=str(payload.get("chain") or ""),
        address=str(payload.get("address") or ""),
    )}


@router.post("/platform/account/email-approval", include_in_schema=False)
def approve_account_email(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """For the website (provision secret only), before it emails a link to
    add an email to an account: a wallet account needs a signature from one
    of its sign-in wallets over an `add_email` challenge naming the email
    (claimed here, once), and so does any account when the request comes from
    a wallet session ("wallet_session": true). Body: {"user_id", "email",
    "nonce", "signature", "wallet_session"}. Otherwise none is needed:
    {"approved": true, "needed": false}."""
    from greencompute_gateway.application import wallet_auth

    require_provision_or_admin(authorization, x_api_key)
    if not is_provision_secret(authorization, x_api_key):
        raise HTTPException(status_code=403, detail="only the website server may call this")
    try:
        return wallet_auth.approve_email(
            service.repository.session_factory, user_id=str(payload.get("user_id") or ""),
            email=str(payload.get("email") or ""), nonce=payload.get("nonce"), signature=payload.get("signature"),
            wallet_session=payload.get("wallet_session") is True,
        )
    except wallet_auth.WalletAuthError as exc:
        raise _wallet_auth_error(exc) from exc


@router.post("/platform/account/verified-email", include_in_schema=False)
def set_verified_email(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """For the website (provision secret only), after the person clicked
    the link it emailed: record the account's confirmed email. A wallet
    account's new-account limits end with it. Body: {"user_id", "email"}."""
    require_provision_or_admin(authorization, x_api_key)
    if not is_provision_secret(authorization, x_api_key):
        raise HTTPException(status_code=403, detail="only the website server may call this")
    from greencompute_gateway.application.services import EmailTakenError

    try:
        return service.set_verified_email(str(payload.get("user_id") or ""), str(payload.get("email") or ""))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    except EmailTakenError as exc:
        raise coded(409, "conflict", str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Account-wide limits (request #19: "stop everything at $X") ----------------------

_RAISE_LIMIT = ("raise or clear it with PUT /platform/account/limits (a full-access key) or under Settings > "
                "Account limits")


def _account_limits_out(user_id: str) -> dict:
    from greencompute_persistence.key_limits import (
        account_active_rentals,
        account_limits,
        account_own_limits,
        account_spend_cents,
    )

    sf = service.repository.session_factory
    own = account_own_limits(sf, user_id)
    limits = account_limits(sf, user_id) or {
        "daily_spend_limit_cents": None, "daily_spend_limit_source": None,
        "max_active_rentals": None, "max_active_rentals_source": None, "new_account": None,
    }
    new = limits["new_account"]
    cents = own["daily_spend_limit_cents"]
    in_force_cents = limits["daily_spend_limit_cents"]
    return schemas.AccountLimits(
        daily_spend_limit_usd=cents / 100 if cents is not None else None,
        max_active_rentals=own["max_active_rentals"],
        spent_last_24h_usd=round(account_spend_cents(sf, user_id) / 100, 2),
        active_rentals=account_active_rentals(sf, user_id),
        new_account_limits={**new, "until": new["until"].isoformat()} if new else None,
        in_force=schemas.AccountLimitsInForce(
            daily_spend_limit_usd=in_force_cents / 100 if in_force_cents is not None else None,
            daily_spend_limit_source=limits["daily_spend_limit_source"],
            max_active_rentals=limits["max_active_rentals"],
            max_active_rentals_source=limits["max_active_rentals_source"],
        ),
    ).model_dump(mode="json")


def _set_account_limits(payload: schemas.AccountLimitsUpdate, api_key, *, replace: bool) -> dict:
    """Only a full-access key may change them, as only one may turn on a
    wallet's sign-in: a rent-only or read-only key (a leaked agent's, say)
    must not be able to lift the cap that stops it."""
    if api_key.scopes and not api_key.admin:
        raise coded(403, "scope_required", {
            "message": "changing the account's limits needs a full-access key",
            "required_scope": "full access", "key_scopes": list(api_key.scopes),
        })
    from greencompute_persistence import session_scope
    from greencompute_persistence.orm import UserORM
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    sent = payload.model_fields_set
    with session_scope(service.repository.session_factory) as session:
        user = session.get(UserORM, api_key.user_id, with_for_update=True)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if not BillingRepository.caller_still_in(session, api_key.user_id, api_key.key_id):
            raise HTTPException(status_code=401, detail="invalid api key")
        if replace or "daily_spend_limit_usd" in sent:
            usd = payload.daily_spend_limit_usd
            user.daily_spend_limit_cents = round(usd * 100) if usd is not None else None
        if replace or "max_active_rentals" in sent:
            user.max_active_rentals = payload.max_active_rentals
        cents, rentals = user.daily_spend_limit_cents, user.max_active_rentals
    log.info("account limits: user_id=%s key_id=%s daily_spend_limit_cents=%s max_active_rentals=%s",
             api_key.user_id, api_key.key_id, cents, rentals)
    return _account_limits_out(api_key.user_id)


@router.get("/platform/account/limits", response_model=None,
            responses=ok(schemas.AccountLimits, "The account's limits and where it stands"))
def get_account_limits(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """The limits set for the whole account, every key together: spend in
    any 24 hours and rentals running at once, with what was spent in the
    last 24 hours and how many are running. Any key of the account may read
    them; `in_force` is what applies (for a new wallet account, the stricter
    of these and its first-week limits)."""
    api_key = _user_key(authorization, x_api_key)
    return _account_limits_out(api_key.user_id)


@router.put("/platform/account/limits", response_model=None,
            responses=ok(schemas.AccountLimits, "The account's limits, as now set"))
def put_account_limits(
    payload: schemas.AccountLimitsUpdate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Set both account-wide limits: {"daily_spend_limit_usd": 100,
    "max_active_rentals": 10}; one left out or null means no limit. At the
    spend limit, new rentals and paid model requests get 403
    account_spend_limit_reached and running rentals are stopped; at the
    rentals limit, new rentals get 403 account_rental_limit_reached (lowering
    it stops nothing). Needs a full-access key."""
    return _set_account_limits(payload, _user_key(authorization, x_api_key), replace=True)


@router.patch("/platform/account/limits", response_model=None,
              responses=ok(schemas.AccountLimits, "The account's limits, as now set"))
def patch_account_limits(
    payload: schemas.AccountLimitsUpdate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Change only the limits sent, e.g. {"max_active_rentals": null} to
    clear one. Needs a full-access key."""
    return _set_account_limits(payload, _user_key(authorization, x_api_key), replace=False)


def _account_limit_error(reason: str, numbers: dict) -> HTTPException:
    """403 account_rental_limit_reached / account_spend_limit_reached: the
    account is at a limit its owner set, every key together. Worded like the
    key's own limits (key_limit)."""
    if reason == "active":
        return coded(403, "account_rental_limit_reached", {
            "message": "this account already has as many rentals running as its account limit allows",
            "code": "account_rental_limit_reached",
            "max_active_rentals": numbers.get("max_active_rentals"),
            "active_rentals": numbers.get("active_rentals"),
            "hint": "stop one with DELETE /platform/deployments/{id}, or " + _RAISE_LIMIT,
        })
    return coded(403, "account_spend_limit_reached", {
        "message": "this account has reached its spend limit for the last 24 hours",
        "code": "account_spend_limit_reached",
        "daily_spend_limit_usd": numbers.get("daily_spend_limit_usd"),
        "spent_last_24h_usd": round(numbers.get("spent_cents", 0) / 100, 2),
        "hint": "wait for earlier spending to fall out of the 24-hour window, or " + _RAISE_LIMIT,
    })


def _enforce_account_spend_limit(api_key) -> None:
    """Refuse a paid model request once the account is at the spend limit
    its owner set (rentals are checked by create_deployment). Admin keys and
    keys without an account pass."""
    if getattr(api_key, "admin", False) or not getattr(api_key, "user_id", None):
        return
    from greencompute_persistence.key_limits import account_own_limits, account_spend_cents

    sf = service.repository.session_factory
    limit = account_own_limits(sf, api_key.user_id)["daily_spend_limit_cents"]
    if limit is None:
        return
    spent = account_spend_cents(sf, api_key.user_id)
    if spent >= limit:
        raise _account_limit_error("spend", {"daily_spend_limit_usd": limit / 100, "spent_cents": spent})


@router.get("/platform/users", include_in_schema=False)
def list_users(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [u.model_dump(mode="json") for u in service.list_users()]


@router.get("/platform/users/{user_id}")
def get_user(
    user_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if not api_key.admin and api_key.user_id != user_id:
        raise HTTPException(status_code=403, detail="user access denied")
    user = service.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user.model_dump(mode="json")


@router.get("/platform/users/{user_id}/balance")
def get_user_balance(
    user_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if not api_key.admin and api_key.user_id != user_id:
        raise HTTPException(status_code=403, detail="user access denied")
    user = service.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return {
        "user_id": user_id,
        "balance_credits": user.balance_credits,
        "balance_usd": round(user.balance_credits / 100.0, 2),
    }


@router.patch("/platform/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserProfileUpdateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if not api_key.admin and api_key.user_id != user_id:
        raise HTTPException(status_code=403, detail="user access denied")
    if payload.email is not None and not api_key.admin:
        # An account's email decides which account a website sign-up with that
        # address joins (register_user), so it can't be set by typing it in:
        # a free wallet account could otherwise claim someone's address and be
        # handed their sign-up. The website confirms an email by a link.
        raise coded(
            403, "forbidden",
            "an account's email can't be changed over the API; add or change it on the website (Settings), "
            "which confirms it by email",
        )
    try:
        return service.update_user_profile(user_id, payload).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- Deleting your own account (website Settings → Danger zone) -------------
# Hidden from the spec: it is a website flow, and an agent working through
# the API should never find "delete the account" among its options.


def _account_to_delete(authorization: str | None, x_api_key: str | None, user_id: str | None) -> str:
    """Whose account a deletion request is about. The website server names
    the signed-in account with the provision secret (it falls back to that
    when the account's website key was revoked); anyone else must use a key
    of the account itself, and not an admin key."""
    if is_provision_secret(authorization, x_api_key):
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        return user_id
    api_key = require_api_key(authorization, x_api_key)
    if api_key.admin or not api_key.user_id:
        raise HTTPException(status_code=403, detail="use a key of the account being deleted")
    return api_key.user_id


@router.get("/platform/account/deletion", include_in_schema=False)
def account_deletion_preview(
    user_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """What deleting the account would do now: the credit that would be lost
    and anything still active that has to be stopped first."""
    target = _account_to_delete(authorization, x_api_key, user_id)
    preview = account_deletion.deletion_preview(service.repository.session_factory, target)
    if preview is None:
        raise HTTPException(status_code=404, detail="account not found")
    return preview


@router.post("/platform/account/delete", include_in_schema=False)
def delete_account(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Delete the account: {"confirm": "DELETE"}. Refused (409) while a rental
    or deployment is pending, running or saved. Revokes every API key, removes
    verified wallets and secrets, erases the profile and forfeits remaining
    credit; ledger and payment records stay, under the anonymised user id.
    See application/account_deletion.py for the full list."""
    target = _account_to_delete(authorization, x_api_key, str(payload.get("user_id") or "") or None)
    if str(payload.get("confirm") or "") != account_deletion.CONFIRM_WORD:
        raise HTTPException(status_code=400, detail='send {"confirm": "DELETE"} to delete the account')
    try:
        result = account_deletion.delete_account(service.repository.session_factory, target)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc
    except account_deletion.AccountHasActiveDeployments as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "stop or delete your running GPUs and deployments before deleting your account",
                "code": "active_deployments",
                "deployments": exc.deployments,
            },
        ) from exc
    if not result.get("already_deleted"):
        metrics.increment("account.deleted")
        log.info(
            "account deleted user_id=%s keys=%s wallets=%s credit_forfeited_cents=%s",
            target, result["api_keys_revoked"], result["wallets_removed"], result["credit_forfeited_cents"],
        )
    return result


@router.post("/platform/images")
def build_image(
    payload: BuildRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("build_image", api_key.key_id, limit=30, window_seconds=60)
    return service.start_build(payload, owner_user_id=api_key.user_id).model_dump(mode="json")


@router.post("/platform/images/contexts")
def upload_build_context(
    payload: BuildContextUploadRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("upload_build_context", api_key.key_id, limit=30, window_seconds=60)
    return service.upload_build_context(payload).model_dump(mode="json")


@router.get("/platform/images")
def list_images(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        build.model_dump(mode="json")
        for build in service.list_builds(user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/images/{image:path}/history")
def image_history(
    image: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        build.model_dump(mode="json")
        for build in service.list_image_history(image, user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/builds")
def list_build_attempts(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        build.model_dump(mode="json")
        for build in service.list_builds(user_id=api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/builds/{build_id}")
def get_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    build = service.get_build(build_id, user_id=api_key.user_id, admin=api_key.admin)
    if build is None:
        raise HTTPException(status_code=404, detail="build not found")
    return build.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/context", include_in_schema=False)
def get_build_context(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    context = service.get_build_context(build_id)
    if context is None:
        raise HTTPException(status_code=404, detail="build context not found")
    return context.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/events", include_in_schema=False)
def get_build_events(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [event.model_dump(mode="json") for event in service.list_build_events(build_id)]


@router.get("/platform/builds/{build_id}/attempts", include_in_schema=False)
def get_build_attempts(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.build_attempts(build_id)


@router.get("/platform/builds/{build_id}/jobs", include_in_schema=False)
def get_build_jobs(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [job.model_dump(mode="json") for job in service.list_build_jobs(build_id)]


@router.get("/platform/builds/{build_id}/jobs/latest", include_in_schema=False)
def get_latest_build_job(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    job = service.get_build_job(build_id)
    if job is None:
        raise HTTPException(status_code=404, detail="build job not found")
    return job.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/jobs/latest/timeline", include_in_schema=False)
def get_latest_build_job_timeline(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    if service.get_build_job(build_id) is None:
        raise HTTPException(status_code=404, detail="build job not found")
    return [entry.model_dump(mode="json") for entry in service.latest_build_job_timeline(build_id)]


@router.post("/platform/builds/{build_id}/jobs/latest/cancel", include_in_schema=False)
def cancel_latest_build_job(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.cancel_latest_build_job(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/builds/{build_id}/jobs/latest/restart", include_in_schema=False)
def restart_latest_build_job(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.restart_latest_build_job(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/platform/builds/recovery/status", include_in_schema=False)
def build_recovery_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.build_recovery_status()


@router.post("/platform/builds/recovery", include_in_schema=False)
def recover_build_jobs(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.recover_build_jobs()


@router.get("/platform/builds/{build_id}/recovery-summary", include_in_schema=False)
def build_recovery_summary(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.build_recovery_summary(build_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/platform/builds/{build_id}/attempts/{attempt}", include_in_schema=False)
def get_build_attempt(
    build_id: str,
    attempt: int,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    record = service.get_build_attempt(build_id, attempt)
    if record is None:
        raise HTTPException(status_code=404, detail="build attempt not found")
    return record.model_dump(mode="json")


@router.get("/platform/builds/{build_id}/logs", include_in_schema=False)
def get_build_logs(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [log.model_dump(mode="json") for log in service.list_build_logs(build_id)]


@router.get("/platform/builds/{build_id}/logs/stream", include_in_schema=False)
def stream_build_logs(
    build_id: str,
    follow: bool = False,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StreamingResponse:
    require_api_key(authorization, x_api_key, admin_required=True)
    return StreamingResponse(
        service.stream_build_logs(build_id, follow=follow),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.post("/platform/builds/{build_id}/retry", include_in_schema=False)
def retry_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.retry_build(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/builds/{build_id}/cleanup", include_in_schema=False)
def cleanup_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.cleanup_build(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/platform/builds/{build_id}/cancel", include_in_schema=False)
def cancel_build(
    build_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        return service.cancel_build(build_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _wallet_blocked() -> HTTPException:
    """403 for an account whose wallet failed sanctions screening: the same
    neutral sentence wherever it shows."""
    from greencompute_gateway.application.sanctions import BLOCKED_MESSAGE

    return coded(403, "wallet_blocked", {"code": "wallet_blocked", "message": BLOCKED_MESSAGE})


def _new_account_limit(reason: str, numbers: dict) -> HTTPException:
    """403 new_account_limit: a wallet account in its first week is at one
    of the limits new wallet accounts have."""
    until = numbers["until"].isoformat() if numbers.get("until") else None
    if reason == "active":
        message = (
            f"New wallet accounts can have {numbers['max_active_rentals']} rentals running at once for their "
            "first 7 days"
        )
        hint = "stop one with DELETE /platform/deployments/{id}, or wait until `until`"
    else:
        message = (
            f"New wallet accounts can spend ${numbers['daily_spend_limit_usd']:.2f} in any 24 hours for their "
            "first 7 days"
        )
        hint = "wait for earlier spending to fall out of the 24-hour window, or until `until`"
    return coded(403, "new_account_limit", {
        "code": "new_account_limit",
        "message": message,
        "limit": reason,
        "max_active_rentals": numbers.get("max_active_rentals"),
        "active_rentals": numbers.get("active_rentals"),
        "daily_spend_limit_usd": numbers.get("daily_spend_limit_usd"),
        "spent_last_24h_usd": round(numbers.get("spent_cents", 0) / 100, 2) if "spent_cents" in numbers else None,
        "until": until,
        "hint": hint + ". Adding an email address to the account on the website (Settings) lifts these limits.",
    })


def _enforce_key_limits(api_key, *, starting_rental: bool = False) -> None:
    """Refuse a request once its API key is at one of its limits: rentals
    running at once (when starting a rental) or spend in the last 24 hours
    (rentals and inference). Keys without limits, and admin keys, pass.
    (A new wallet account's own limits are checked by create_deployment.)"""
    max_active = getattr(api_key, "max_active_rentals", None)
    daily_limit = getattr(api_key, "daily_spend_limit_usd", None)
    if getattr(api_key, "admin", False) or (max_active is None and daily_limit is None):
        return
    from greencompute_persistence.key_limits import key_rental_room

    _room, why, numbers = key_rental_room(
        service.repository.session_factory, api_key.key_id, max_active if starting_rental else None, daily_limit,
    )
    if why == "active":
        raise HTTPException(
            status_code=403,
            detail={
                "message": "this API key already has as many rentals running as it is allowed",
                "code": "key_limit",
                "max_active_rentals": max_active,
                "active_rentals": numbers["active_rentals"],
                "hint": "stop one with DELETE /platform/deployments/{id}, or use a key with a higher limit",
            },
        )
    if why == "spend":
        raise HTTPException(
            status_code=403,
            detail={
                "message": "this API key has reached its spend limit for the last 24 hours",
                "code": "key_limit",
                "daily_spend_limit_usd": daily_limit,
                "spent_last_24h_usd": round(numbers["spent_cents"] / 100, 2),
                "hint": "wait for earlier spending to fall out of the 24-hour window, or use a key with a higher limit",
            },
        )

def _with_idempotency(api_key, endpoint: str, idempotency_key: str | None, body: dict, response: Response, run) -> dict:
    """Run a create at most once per account, endpoint and Idempotency-Key.

    A client that timed out can resend the same request with the same key and
    gets the first result back (with `Idempotent-Replayed: true`) instead of a
    second rental. Only successes are remembered, for 24 hours: a request that
    failed (e.g. 402) can be retried with the same key."""
    if idempotency_key is None:
        return run()
    key = idempotency_key.strip()
    if not key or len(key) > 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key must be 1 to 255 characters")
    scope = api_key.user_id or api_key.key_id
    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    repo = service.repository
    outcome, stored = repo.idempotency_begin(scope, endpoint, key, request_hash)
    if outcome == "replay":
        response.headers["Idempotent-Replayed"] = "true"
        return stored
    if outcome == "mismatch":
        raise coded(
            422, "idempotency_key_reused",
            "this Idempotency-Key was already used with a different request body; use a new key",
        )
    if outcome == "in_progress":
        raise coded(
            409, "idempotency_in_progress",
            "a request with this Idempotency-Key is still being processed; retry in a moment",
            {"Retry-After": "2"},
        )
    try:
        result = run()
    except BaseException:
        repo.idempotency_release(scope, endpoint, key)
        raise
    repo.idempotency_complete(scope, endpoint, key, request_hash, result)
    return result


@router.post("/platform/workloads", response_model=None, responses=ok(schemas.Workload, "The new workload"))
def create_workload(
    payload: WorkloadCreateRequest | dict,
    response: Response,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("create_workload", api_key.key_id, limit=30, window_seconds=60)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    payload_data = payload if isinstance(payload, dict) else payload.model_dump(mode="json")
    return _with_idempotency(
        api_key, "create_workload", idempotency_key, payload_data, response,
        lambda: _create_workload(payload_data, api_key),
    )


def _create_workload(payload_data: dict, api_key) -> dict:
    template = payload_data.get("template")
    # NOTE: the route accepts `WorkloadCreateRequest | dict`, so an invalid body
    # (e.g. gpu_count > 16) silently falls back to `dict` at parse time instead
    # of FastAPI's 422 — then re-validating here would raise an *uncaught*
    # ValidationError and surface as a 500. Catch it and return a clean 422 so
    # clients see what they got wrong (this is what made `gpu_count:10` a 500).
    try:
        if template == "vllm":
            from greencompute_gateway.domain.templates import build_vllm_workload
            model = payload_data.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model required for vllm template")
            request = build_vllm_workload(
                model,
                **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
            )
        elif template == "vllm-vision":
            from greencompute_gateway.domain.templates import build_vllm_vision_workload
            model = payload_data.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model required for vllm-vision template")
            request = build_vllm_vision_workload(
                model,
                **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
            )
        elif template == "diffusion":
            from greencompute_gateway.domain.templates import build_diffusion_workload
            model = payload_data.get("model")
            if not model:
                raise HTTPException(status_code=400, detail="model required for diffusion template")
            request = build_diffusion_workload(
                model,
                **{k: v for k, v in payload_data.items() if k not in ("template", "model")},
            )
        else:
            request = WorkloadCreateRequest(**payload_data)
    except ValidationError as exc:
        detail = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors(include_url=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from exc
    except ValueError as exc:
        # Template builders raise ValueError (e.g. "model must be org/model
        # format"); without this it would surface as a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        workload = service.create_workload(request, api_key.user_id)
    except ValueError as exc:  # e.g. an ingress_host outside the model zone
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redact_workload(workload.model_dump(mode="json"))


def _is_secret_meta_key(key: str) -> bool:
    k = key.lower().replace("_", "")
    return any(s in k for s in ("token", "secret", "password", "apikey", "privatekey"))


def _redact_workload(d: dict) -> dict:
    """Strip owner-supplied secrets (HF tokens, API keys, passwords) from a
    workload's metadata before it leaves the gateway — these must never reach
    any client, owner or not. Public keys / ports / other config stay intact.
    (A workload is visible cross-user when public, so an un-redacted hf_token
    would otherwise leak to every authenticated user.)"""
    meta = d.get("metadata")
    if isinstance(meta, dict):
        d["metadata"] = {
            k: ("***redacted***" if _is_secret_meta_key(k) else v)
            for k, v in meta.items()
        }
        # A pod's environment usually carries tokens: return the names only.
        if isinstance(d["metadata"].get("env"), dict):
            d["metadata"]["env"] = {name: "***redacted***" for name in d["metadata"]["env"]}
    return d


@router.get("/platform/workloads", response_model=None, responses=ok(list[schemas.Workload]))
def list_workloads(
    mine: bool = Query(
        default=False,
        description="Only workloads you own, without the shared catalog models or ones others made public",
    ),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        _redact_workload(workload.model_dump(mode="json"))
        for workload in service.list_workloads(user_id=api_key.user_id, admin=api_key.admin, mine=mine)
    ]


@router.get("/platform/workloads/{workload_id}", response_model=None, responses=ok(schemas.Workload))
def get_workload(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    workload = service.get_workload(workload_id, user_id=api_key.user_id, admin=api_key.admin)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")
    return _redact_workload(workload.model_dump(mode="json"))


@router.patch("/platform/workloads/{workload_id}")
def update_workload(
    workload_id: str,
    payload: WorkloadUpdateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.update_workload(
            workload_id,
            payload,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/platform/workloads/{workload_id}/shares")
def share_workload(
    workload_id: str,
    payload: WorkloadShareCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.share_workload(
            workload_id,
            payload,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/workloads/{workload_id}")
def delete_workload(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.delete_workload(
            workload_id,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/platform/workloads/{workload_id}/utilization")
def get_workload_utilization(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    workload = service.get_workload(workload_id, user_id=api_key.user_id, admin=api_key.admin)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")
    return service.workload_utilization(workload_id)


@router.get("/platform/workloads/{workload_id}/warmup")
def workload_warmup(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StreamingResponse:
    api_key = require_api_key(authorization, x_api_key)
    workload = service.get_workload(workload_id, user_id=api_key.user_id, admin=api_key.admin)
    if workload is None:
        raise HTTPException(status_code=404, detail="workload not found")

    def _warmup_stream():
        yield f"data: {json.dumps({'workload_id': workload_id, 'status': 'warmup_started'})}\n\n"
        yield f"data: {json.dumps({'workload_id': workload_id, 'status': 'warmup_complete'})}\n\n"

    return StreamingResponse(
        _warmup_stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


@router.get("/platform/workloads/{workload_id}/shares")
def list_workload_shares(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return [
            share.model_dump(mode="json")
            for share in service.list_workload_shares(
                workload_id,
                actor_user_id=api_key.user_id,
                admin=api_key.admin,
            )
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


#: Never serialised into a deployment object. A pod's SSH private key used to
#: ride along on create/get/list/update/delete, so anything that logged API
#: responses -- agent transcripts, proxies, CI output -- ended up storing
#: long-lived credentials (reported by an external tester, 2026-10). The key is
#: still available, deliberately, from GET /platform/deployments/{id}/ssh, which
#: is owner/admin-gated and audit-logged per access.
DEPLOYMENT_SECRET_FIELDS = {"ssh_private_key"}
LIST_ERROR_CHARS = 500


def _deployment_out(deployment, admin: bool, workloads: dict | None = None, *, brief: bool = False) -> dict:
    """A deployment as the API returns it: no secrets and, for anyone but an
    admin, a model's endpoint is the model's own address rather than the box
    serving it (that one is firewalled and names the machine). Rentals keep
    their ssh:// endpoint. `workloads` caches lookups across a list.

    A suspended rental also says what it costs per day while it waits and when
    it will be deleted, so nobody has to work out the retention rules.

    `brief` (lists) caps last_error: a model that dies on start stores its
    whole container log, and thousands of deployments made the admin list
    ~20 MB. GET /platform/deployments/{id} still has the full text."""
    cache = workloads if workloads is not None else {}

    def workload():
        if deployment.workload_id not in cache:
            cache[deployment.workload_id] = service.control_plane.repository.get_workload(deployment.workload_id)
        return cache[deployment.workload_id]

    out = deployment.model_dump(mode="json", exclude=DEPLOYMENT_SECRET_FIELDS | ({"ssh_host_keys"} if brief else set()))
    if brief and out.get("last_error") and len(out["last_error"]) > LIST_ERROR_CHARS:
        out["last_error"] = out["last_error"][:LIST_ERROR_CHARS] + " …"
    storage_cents, deletes_at = (
        service.control_plane.suspension_terms(deployment, workload())
        if out.get("state") == DeploymentState.SUSPENDED.value
        else (0, None)
    )
    out["storage_cents_per_day"] = storage_cents
    out["deletes_at"] = deletes_at.isoformat() if deletes_at else None
    if admin or not str(out.get("endpoint") or "").startswith(("http://", "https://")):
        return out
    host = workload().ingress_host if workload() else None
    out["endpoint"] = model_url(host)
    return out

@router.post("/platform/deployments", response_model=None, responses=ok(schemas.Deployment, "The new deployment"))
def create_deployment(
    payload: GatewayDeploymentCreateRequest,
    response: Response,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("create_deployment", api_key.key_id, limit=30, window_seconds=60)
    return _with_idempotency(
        api_key, "create_deployment", idempotency_key, json.loads(payload.model_dump_json()), response,
        lambda: _create_deployment(payload, api_key),
    )


def _create_deployment(payload: DeploymentCreateRequest, api_key) -> dict:
    from greencompute_gateway.application.services import (
        AccountLimitError,
        InsufficientBalanceForRentalError,
        KeyLimitError,
        NewAccountBusyError,
        NewAccountLimitError,
        NoCapacityError,
        SanctionedAccountError,
        UnsupportedGPUError,
    )

    _enforce_key_limits(api_key, starting_rental=True)
    try:
        return _deployment_out(service.create_deployment(
            payload,
            user_id=api_key.user_id,
            admin=api_key.admin,
            created_by_key_id=api_key.key_id,
        ), api_key.admin)
    except SanctionedAccountError as exc:
        raise _wallet_blocked() from exc
    except NewAccountLimitError as exc:
        raise _new_account_limit(exc.reason, exc.numbers) from exc
    except AccountLimitError as exc:
        raise _account_limit_error(exc.reason, exc.numbers) from exc
    except NewAccountBusyError as exc:
        raise coded(
            409, "conflict",
            "another rental is being started for this account right now; send this one again in a moment",
            headers={"Retry-After": "2"},
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UnsupportedGPUError as exc:
        raise coded(
            400, "gpu_unavailable",
            {
                "message": "none of the requested GPU models are available to rent",
                "requested": exc.requested,
                "available": exc.available,
                "hint": "set supported_gpu_models to one of `available` (any spelling); "
                        "see GET /platform/pricing",
            },
        ) from exc
    except InsufficientBalanceForRentalError as exc:
        # 402 Payment Required — UI reads the numeric fields to render a
        # friendly "Add $X to start" prompt.
        raise HTTPException(status_code=402, detail=_hour_of_credit_detail(exc, "start")) from exc
    except KeyLimitError as exc:
        # Normally caught by _enforce_key_limits above; this is the race
        # where another request took the last slot in between.
        _enforce_key_limits(api_key, starting_rental=True)
        raise HTTPException(status_code=403, detail={"message": str(exc), "code": "key_limit"}) from exc
    except NoCapacityError as exc:
        # 503 + Retry-After: "busy", as distinct from the 400 "never" above.
        # `code` is for programs; `message` is for people. No free counts:
        # the owner keeps those private, so the way out is the waitlist.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "no_capacity",
                "message": exc.message,
                "gpu_models": exc.gpu_models,
                "requested_gpus": exc.gpu_count,
                "retry_after_seconds": exc.retry_after_seconds,
                "waitlist": "POST /platform/capacity-alerts",
                "contact": SUPPORT,
                "hint": "nothing was created. POST /platform/capacity-alerts with this gpu_model and gpu_count "
                        "to be told (email and a capacity.available event) when there is room, or resend after "
                        "Retry-After; GET /platform/pricing says when a GPU's availability is no longer "
                        "fully_rented. For more capacity, write to " + SUPPORT,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc


def _hour_of_credit_detail(exc, action: str) -> dict:
    """The 402 body when a balance can't cover the first hour of a deployment
    billed by the hour. For a private model the message says so in plain
    words: that it is charged by the hour at all is new (it used to be per
    token)."""
    if exc.private_model:
        hourly = exc.rate_cents_per_hour * exc.gpu_count
        gpus = "1 GPU" if exc.gpu_count == 1 else f"{exc.gpu_count} GPUs"
        message = (
            f"insufficient balance to {action} this private model: it costs ${hourly / 100:.2f}/hour "
            f"({gpus} at ${exc.rate_cents_per_hour / 100:.2f}/hr) while it runs, billed by the minute, "
            f"and {'starting' if action == 'start' else 'resuming'} it needs at least one hour "
            f"(${exc.required_cents / 100:.2f}) of credit; your balance is ${exc.current_cents / 100:.2f}"
        )
    elif action == "start":
        message = "insufficient balance to start this rental"
    else:
        message = "insufficient balance to resume this deployment"
    return {
        "message": message,
        "required_cents": exc.required_cents,
        "current_cents": exc.current_cents,
        "rate_cents_per_hour": exc.rate_cents_per_hour,
        "gpu_count": exc.gpu_count,
        "requested_instances": exc.requested_instances,
    }


def _attach_owner_info(rows: list[dict], users_by_id: dict) -> list[dict]:
    """Enrich admin deployment rows with the owner's email + username so a
    sales/admin investigator can identify the customer behind each deployment.
    owner_user_id (a UUID) is already on every row; this adds the
    human-readable fields. Admin-only by caller — never attach owner identity
    to a regular user's own-deployments list."""
    for row in rows:
        owner = users_by_id.get(row.get("owner_user_id"))
        if owner is not None:
            row["owner_email"] = owner.email
            row["owner_username"] = owner.username
    return rows


def _attach_workload_info(rows: list[dict], workloads_by_id: dict) -> list[dict]:
    """Enrich deployment rows with what each one runs: the workload's kind
    (pod/vm = a rental, inference = a model), its display name, and the GPU it
    asked for. Lists can then say "RTX 4090 x 1" or the model's name instead
    of a workload UUID. The admin list passes every workload; a user's list
    passes only the workloads behind their own deployments."""
    for row in rows:
        wl = workloads_by_id.get(row.get("workload_id"))
        if wl is not None:
            row["workload_kind"] = getattr(wl.kind, "value", str(wl.kind))
            row["workload_name"] = wl.display_name or wl.name
            gpus = wl.requirements.supported_gpu_models
            row["gpu_model"] = gpus[0] if gpus else None
            row["gpu_count"] = wl.requirements.gpu_count
    return rows


def _fleet_call(fn):
    """Fleet errors as HTTP: not found 404, not yours 403, can't 400."""
    from greencompute_gateway.application.fleets import FleetError

    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FleetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _user_key(authorization: str | None, x_api_key: str | None):
    """The caller's API key, which must belong to an account."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return api_key


def _webhook_call(fn):
    from greencompute_gateway.application.webhooks import WebhookError, WebhookNotFound

    try:
        return fn()
    except WebhookNotFound as exc:
        raise HTTPException(status_code=404, detail="webhook not found") from exc
    except WebhookError as exc:
        raise coded(400, "invalid_request", str(exc)) from exc


@router.get("/platform/events", response_model=None, responses=ok(schemas.EventPage, "Events after `after`"))
def list_account_events(
    after: int = 0,
    limit: int = 100,
    types: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Everything that happened on your account after event `after`, oldest
    first: deployments becoming ready, failing, being suspended or ending,
    and the balance running low. Instead of polling each deployment, poll
    this with the `next_after` it returned last time. `types` filters, e.g.
    `deployment.ready,deployment.failed`. Events are kept 30 days."""
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application import webhooks

    return _webhook_call(lambda: webhooks.list_events(
        service.repository.session_factory, user_id, after=after, types=types, limit=limit))


@router.post("/platform/webhooks", response_model=None, responses=ok(schemas.WebhookCreated, "The new webhook, with its secret"))
def create_webhook(
    payload: schemas.WebhookCreate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Send your account's events to your HTTPS endpoint as they happen,
    signed with the `secret` returned here (only now). At most 5 per
    account. Use POST /platform/webhooks/{id}/test to try it."""
    api_key = _user_key(authorization, x_api_key)
    enforce_rate_limit("create_webhook", api_key.key_id, limit=10, window_seconds=60)
    from greencompute_gateway.application import webhooks

    return _webhook_call(lambda: webhooks.create_webhook(
        service.repository.session_factory, api_key.user_id, payload.url, payload.events))


@router.get("/platform/webhooks", response_model=None, responses=ok(list[schemas.Webhook], "Your webhooks"))
def list_webhooks(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application import webhooks

    return webhooks.list_webhooks(service.repository.session_factory, user_id)


@router.patch("/platform/webhooks/{webhook_id}", response_model=None, responses=ok(schemas.Webhook, "The webhook"))
def update_webhook(
    webhook_id: str,
    payload: schemas.WebhookUpdate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Change its URL or event types, or turn it off and on again."""
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application import webhooks

    return _webhook_call(lambda: webhooks.update_webhook(
        service.repository.session_factory, user_id, webhook_id,
        url=payload.url, events=payload.events, active=payload.active))


@router.delete("/platform/webhooks/{webhook_id}", response_model=None)
def delete_webhook(
    webhook_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application import webhooks

    _webhook_call(lambda: webhooks.delete_webhook(service.repository.session_factory, user_id, webhook_id))
    return {"deleted": webhook_id}


@router.post("/platform/webhooks/{webhook_id}/test", response_model=None,
             responses=ok(schemas.WebhookTestResult, "How the test delivery went"))
def test_webhook(
    webhook_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """POST a `webhook.test` event to the endpoint now, signed like the real
    ones, and say whether it answered 2xx."""
    api_key = _user_key(authorization, x_api_key)
    enforce_rate_limit("test_webhook", api_key.key_id, limit=10, window_seconds=60)
    from greencompute_gateway.application import webhooks

    return _webhook_call(lambda: webhooks.send_test(service.repository.session_factory, api_key.user_id, webhook_id))


def _alert_call(fn):
    from greencompute_gateway.application.capacity_alerts import CapacityAlertError, CapacityAlertNotFound

    try:
        return fn()
    except CapacityAlertNotFound as exc:
        raise HTTPException(status_code=404, detail="alert not found") from exc
    except CapacityAlertError as exc:
        raise coded(400, "invalid_request", str(exc)) from exc


@router.post("/platform/capacity-alerts", response_model=None,
             responses=ok(schemas.CapacityAlert, "The alert, waiting"))
def create_capacity_alert(
    payload: schemas.CapacityAlertCreate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Be told once when there is room for `count` rentals of `gpu_count`
    GPUs of `gpu_model`: a `capacity.available` event (events feed and
    webhooks) and an email. Expires after 7 days. Asking again for the same
    returns the alert already waiting."""
    api_key = _user_key(authorization, x_api_key)
    enforce_rate_limit("capacity_alert", api_key.key_id, limit=20, window_seconds=60)
    from greencompute_gateway.application import capacity_alerts

    return _alert_call(lambda: capacity_alerts.create_alert(
        service.repository.session_factory, api_key.user_id, payload.gpu_model, payload.gpu_count, payload.count))


@router.get("/platform/capacity-alerts", response_model=None,
            responses=ok(list[schemas.CapacityAlert], "Waiting alerts, and those from the last 7 days"))
def list_capacity_alerts(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application import capacity_alerts

    return capacity_alerts.list_alerts(service.repository.session_factory, user_id)


@router.delete("/platform/capacity-alerts/{alert_id}", response_model=None,
               responses=ok(schemas.CapacityAlert, "The alert, cancelled"))
def cancel_capacity_alert(
    alert_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application import capacity_alerts

    return _alert_call(lambda: capacity_alerts.cancel_alert(service.repository.session_factory, user_id, alert_id))


@router.post("/platform/fleets", response_model=None, responses=ok(schemas.Fleet, "The new fleet"))
def create_fleet(
    payload: schemas.FleetCreate,
    response: Response,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Keep `target` rentals of a workload running (`replace: true`, the
    default): any that stops or fails is replaced, under the same name. Or,
    with `replace: false`, start `target` of them as GPUs free up and stop
    there. Members are ordinary deployments named `<name_prefix>-01`, `-02`, …
    and are billed as usual; the fleet waits when GPUs, credit or this key's
    limits run out, and pauses if three in a row fail before they are ready."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    enforce_rate_limit("create_fleet", api_key.key_id, limit=10, window_seconds=60)
    from greencompute_gateway.application import fleets

    def run() -> dict:
        fleet = _fleet_call(lambda: fleets.create_fleet(
            service, user_id=api_key.user_id, key_id=None if api_key.admin else api_key.key_id,
            admin=api_key.admin, body=payload.model_dump(),
        ))
        return fleets.view(service, fleet)

    # A retry with the same Idempotency-Key returns the first fleet, never a second one.
    return _with_idempotency(api_key, "create_fleet", idempotency_key, payload.model_dump(), response, run)


@router.get("/platform/fleets", response_model=None, responses=ok(list[schemas.Fleet], "Your fleets"))
def list_fleets(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Your fleets: all but those stopped more than a week ago."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    from greencompute_gateway.application import fleets

    online = service.control_plane.online_gpu_models()  # once, not per fleet
    return [fleets.view(service, f, online=online) for f in fleets.list_fleets(service, api_key.user_id)]


@router.get("/platform/fleets/{fleet_id}", response_model=None, responses=ok(schemas.Fleet, "The fleet and its members"))
def get_fleet(
    fleet_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    from greencompute_gateway.application import fleets

    fleet = _fleet_call(lambda: fleets.get_fleet(service, fleet_id, api_key.user_id, admin=api_key.admin))
    return fleets.view(service, fleet)


@router.patch("/platform/fleets/{fleet_id}", response_model=None, responses=ok(schemas.Fleet, "The fleet"))
def update_fleet(
    fleet_id: str,
    payload: schemas.FleetUpdate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Change `target` (raising starts more, lowering stops the newest
    not-yet-ready ones first), or `paused` true/false. Resuming a fleet that
    paused itself after failures clears that count."""
    api_key = require_api_key(authorization, x_api_key)
    from greencompute_gateway.application import fleets

    fleet = _fleet_call(lambda: fleets.update_fleet(
        service, fleet_id, user_id=api_key.user_id, admin=api_key.admin,
        body=payload.model_dump(exclude_none=True),
    ))
    return fleets.view(service, fleet)


@router.delete("/platform/fleets/{fleet_id}", response_model=None, responses=ok(schemas.Fleet, "The stopped fleet"))
def stop_fleet(
    fleet_id: str,
    keep_running: bool = Query(default=False, description="true: stop the fleet but leave its rentals running."),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Stop the fleet and its rentals (or only the fleet, with `keep_running=true`)."""
    api_key = require_api_key(authorization, x_api_key)
    from greencompute_gateway.application import fleets

    fleet = _fleet_call(lambda: fleets.stop_fleet(
        service, fleet_id, user_id=api_key.user_id, admin=api_key.admin, keep_running=keep_running,
    ))
    return fleets.view(service, fleet)


#: Rows per page of GET /platform/deployments when a cursor comes without a limit.
DEPLOYMENTS_PAGE_DEFAULT = 100
DEPLOYMENTS_PAGE_MAX = 500


def _encode_deployments_cursor(deployment) -> str:
    """Opaque to clients: where the next page starts (the last row's
    created_at and id), urlsafe base64."""
    raw = f"1|{deployment.created_at.isoformat()}|{deployment.deployment_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_deployments_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        version, created, deployment_id = raw.split("|", 2)
        if version != "1" or not deployment_id or len(deployment_id) > 64:
            raise ValueError(raw)
        return datetime.fromisoformat(created), deployment_id
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise coded(400, "invalid_cursor", {
            "message": "this cursor isn't one this API gave out, or it was changed",
            "cursor": cursor[:200],
            "hint": "send the X-Next-Cursor header (or the Link rel=\"next\" URL) of the previous page "
                    "unchanged, or start again without a cursor",
        }) from exc


def _list_deployments_page(api_key, wanted: set[str], state: str | None, limit: int, cursor: str | None,
                           response: Response | None) -> list[dict]:
    """GET /platform/deployments with `limit`/`cursor`: newest first, the state
    filter and the limit applied in the database. When there are more, the
    X-Next-Cursor and Link rel="next" headers say where the next page starts."""
    if not 1 <= limit <= DEPLOYMENTS_PAGE_MAX:
        raise coded(400, "invalid_request", f"limit must be from 1 to {DEPLOYMENTS_PAGE_MAX}")
    after = _decode_deployments_cursor(cursor) if cursor else None
    page = service.list_deployments_page(
        user_id=api_key.user_id, admin=api_key.admin, states=wanted or None, limit=limit + 1, after=after,
    )
    more, page = len(page) > limit, page[:limit]
    workloads: dict = {}
    rows = [_deployment_out(deployment, api_key.admin, workloads, brief=True) for deployment in page]
    if api_key.admin:
        # Only the owners on this page, not every user.
        _attach_owner_info(rows, service.repository.get_users({row.get("owner_user_id") for row in rows}))
    _attach_workload_info(rows, service.control_plane.repository.get_workloads({row["workload_id"] for row in rows}))
    if more and response is not None:
        next_cursor = _encode_deployments_cursor(page[-1])
        query = {"state": state.strip()} if state and state.strip() else {}
        query.update(limit=limit, cursor=next_cursor)
        response.headers["X-Next-Cursor"] = next_cursor
        # Relative, so it is right whatever host and scheme the client used.
        response.headers["Link"] = (
            f'</platform/deployments?{urllib_parse.urlencode(query, safe=",")}>; rel="next"'
        )
    return rows


@router.get("/platform/deployments", response_model=None, responses=ok(list[schemas.Deployment], "Your deployments, including stopped ones"))
def list_deployments(
    state: str | None = Query(
        default=None,
        description="Only deployments in these states, comma-separated, e.g. `ready` or `terminated,failed`",
    ),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    limit: Annotated[int | None, Query(
        ge=1, le=DEPLOYMENTS_PAGE_MAX,
        description=("Page through them: at most this many, newest first. When there are more, the "
                     "`X-Next-Cursor` header holds the next page's `cursor` and `Link` its URL (rel=\"next\"). "
                     "Without `limit` or `cursor`, every deployment comes back at once, as before."),
    )] = None,
    cursor: Annotated[str | None, Query(
        max_length=512,
        description="The previous page's `X-Next-Cursor`, unchanged (with `state` and `limit` as before).",
    )] = None,
    response: Response = None,  # type: ignore[assignment]  # injected by FastAPI; None in-process
) -> list[dict]:
    """Your deployments, including stopped ones. By default all of them, in
    no particular order; with `limit` (and then `cursor`), a page at a time,
    newest first."""
    api_key = require_api_key(authorization, x_api_key)
    wanted = {s.strip().lower() for s in (state or "").split(",") if s.strip()}
    known = {s.value for s in DeploymentState}
    if wanted - known:
        raise HTTPException(
            status_code=400,
            detail=f"unknown state {sorted(wanted - known)}; use any of {sorted(known)}",
        )
    if limit is not None or cursor is not None:
        return _list_deployments_page(api_key, wanted, state, limit or DEPLOYMENTS_PAGE_DEFAULT, cursor, response)
    workloads: dict = {}
    rows = [
        _deployment_out(deployment, api_key.admin, workloads, brief=True)
        for deployment in service.list_deployments(user_id=api_key.user_id, admin=api_key.admin)
        if not wanted or deployment.state.value in wanted
    ]
    if api_key.admin:
        # Batch-map users + workloads once so each row can show the customer
        # behind it and whether it's a rental (pod/vm) or a model (inference).
        users_by_id = {u.user_id: u for u in service.list_users()}
        _attach_owner_info(rows, users_by_id)
        workloads_by_id = {w.workload_id: w for w in service.list_workloads(admin=True)}
    else:
        # Only the workloads behind the caller's own deployments, in one query.
        workloads_by_id = service.control_plane.repository.get_workloads(
            {row["workload_id"] for row in rows}
        )
    _attach_workload_info(rows, workloads_by_id)
    return rows


@router.get("/platform/deployments/{deployment_id}", response_model=None, responses=ok(schemas.Deployment))
def get_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    return _deployment_out(deployment, api_key.admin)


@router.get("/platform/deployments/{deployment_id}/ssh", response_model=None, responses=ok(schemas.SSHAccess))
def get_deployment_ssh(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if not deployment.endpoint or not deployment.endpoint.startswith("ssh://"):
        raise HTTPException(status_code=404, detail="SSH not available for this deployment")
    # Parse ssh://user@host:port
    parts = deployment.endpoint.replace("ssh://", "").split("@", 1)
    user = parts[0] if len(parts) == 2 else "root"
    host_port = parts[-1].rsplit(":", 1)
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) == 2 else 22
    private_key = None if _workload_has_own_ssh_keys(deployment.workload_id) else deployment.ssh_private_key
    if private_key:
        # Audit: a long-lived SSH private key is being handed out. Authz
        # already passed (owner/admin), but record WHO pulled it.
        metrics.increment("secret_access.deployment_ssh")
        log.info(
            "secret_access event=deployment_ssh_key deployment_id=%s key_id=%s user_id=%s ip=%s",
            deployment_id,
            api_key.key_id,
            api_key.user_id,
            _audit_client_ip(),
        )
    host_keys = list(deployment.ssh_host_keys or [])
    return {
        "ssh_host": host,
        "ssh_port": port,
        "ssh_username": user,
        "ssh_command": f"ssh {user}@{host} -p {port}",
        "private_key": private_key,
        "host_keys": [
            {"type": key.split()[0], "key": key, "fingerprint": ssh_key_fingerprint(key)} for key in host_keys
        ],
        "known_hosts": [known_hosts_line(host, port, key) for key in host_keys],
    }


def _workload_has_own_ssh_keys(workload_id: str) -> bool:
    """Did the workload bring its own SSH public keys? Then the platform's
    generated key is only for clients that didn't, and is never returned:
    handing it out would be a second root key to the pod nobody asked for."""
    workload = service.get_workload(workload_id, admin=True)
    keys = ((workload.metadata or {}) if workload else {}).get("ssh_public_keys") or []
    return any(isinstance(k, str) and k.strip() for k in keys)


def _control_plane_base() -> str:
    """Internal base URL for control-plane HTTP calls. Inside compose we use
    the container hostname; the env var lets ops override for e.g. smoke tests."""
    return os.environ.get("GREENCOMPUTE_CONTROL_PLANE_URL", "http://control-plane:8001").rstrip("/")


def _admin_api_key() -> str | None:
    return os.environ.get("GREENCOMPUTE_ADMIN_API_KEY") or None


def _lookup_miner_base_url(hotkey: str, *, timeout: float = 3.0) -> str | None:
    """Query control-plane for a miner's api_base_url by hotkey."""
    url = f"{_control_plane_base()}/platform/v1/servers/{hotkey}"
    req = urllib_request.Request(url, method="GET")
    admin_key = _admin_api_key()
    if admin_key:
        req.add_header("X-API-Key", admin_key)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode() or "{}")
            base = body.get("api_base_url")
            return base.rstrip("/") if isinstance(base, str) and base else None
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def _node_base_url(deployment) -> str | None:
    """The node agent's address for the box `deployment` runs on, by node:
    several boxes share a hotkey and the miner record holds one address. The
    hotkey's address is the fallback for nodes that don't report their own
    yet."""
    if not deployment.hotkey:
        return None
    miner_base = None
    if getattr(deployment, "node_id", None):
        try:
            miner_base = service.control_plane.repository.node_api_base_url(deployment.node_id)
        except Exception:  # noqa: BLE001
            miner_base = None
    return miner_base or _lookup_miner_base_url(deployment.hotkey)


def _node_request(deployment, path: str, *, timeout: float, max_bytes: int,
                  body: dict | None = None) -> tuple[str, int | None, Any]:
    """Call the node agent running `deployment` (gateway → control plane for
    the machine's address → node agent), with the node secret: GET, or POST
    `body` as JSON. Returns (outcome, status, data):
    - ("ok", 200, data);
    - ("refused", status, detail): the node answered with an error;
    - ("unreachable", None, None): no address, or no connection was made;
    - ("unknown", None, None): sent, but no answer in time (or a broken one),
      so a POST may or may not have taken effect."""
    miner_base = _node_base_url(deployment)
    if not miner_base:
        return "unreachable", None, None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib_request.Request(f"{miner_base}{path}", data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    agent_secret = os.environ.get("GREENCOMPUTE_INFERENCE_AUTH_SECRET")
    if agent_secret:
        req.add_header("X-Agent-Auth", agent_secret)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return "unknown", None, None
            out = json.loads(raw.decode() or "{}")
            return "ok", resp.status, out
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(65536).decode() or "{}").get("detail")
        except (ValueError, AttributeError, OSError):
            detail = None
        return "refused", exc.code, detail
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        return ("unknown" if isinstance(reason, TimeoutError) else "unreachable"), None, None
    except (TimeoutError, OSError, ValueError, http.client.HTTPException):
        # incl. a reply cut short (IncompleteRead) by a restarting node
        return "unknown", None, None


def _node_get(deployment, path: str, *, timeout: float, max_bytes: int) -> dict | None:
    """GET from the node agent: its JSON object, or None on any failure."""
    outcome, _, data = _node_request(deployment, path, timeout=timeout, max_bytes=max_bytes)
    return data if outcome == "ok" and isinstance(data, dict) else None


@router.get("/platform/deployments/{deployment_id}/stats", response_model=None,
            responses=ok(schemas.PodStats, "Live readings; {} while there are none"))
def get_deployment_stats(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Live readings of a running rental: CPU, memory, network, disk, and
    each GPU's use, memory, temperature, power and clocks (`gpus`). `{}`
    while it isn't running or its machine doesn't answer, so a dashboard can
    keep polling."""
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    # Under the dashboard's 5 s poll, plus a little: a slow machine must not
    # pile up requests (the node takes its readings side by side, ~2 s).
    return _node_get(deployment, f"/pods/{deployment_id}/stats", timeout=6.0, max_bytes=256 * 1024) or {}


@router.get("/platform/deployments/{deployment_id}/logs", response_model=None,
            responses=ok(schemas.PodLogs, "The last lines of the rental's output"))
def get_deployment_logs(
    deployment_id: str,
    lines: int = 200,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """A running rental's recent output: the last `lines` lines (at most
    2000) of its start command's log (/workspace/greencompute-command.log)
    and of its container log (the image's own start command, and the pod's
    set-up). When either is null, its `_note` says why."""
    api_key = require_api_key(authorization, x_api_key)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=api_key.admin)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if deployment.state != DeploymentState.READY:
        raise coded(409, "conflict", f"the rental is {deployment.state.value}; logs are there while it runs")
    lines = max(1, min(int(lines), 2000))
    # The node keeps 256 KiB of each source's end; JSON escaping can double it.
    out = _node_get(deployment, f"/pods/{deployment_id}/logs?lines={lines}", timeout=15.0, max_bytes=8 * 1024 * 1024)
    if out is None:
        raise coded(503, "unavailable", "the machine running this rental didn't answer; try again shortly",
                    headers={"Retry-After": "10"})
    return out


#: Commands waited on at once: by this gateway, and per customer. Each holds
#: a worker thread for up to two minutes, so they are capped.
_EXEC_TOTAL = threading.BoundedSemaphore(16)
_EXEC_PER_USER = 4
_exec_running: dict[str, int] = {}
_exec_lock = threading.Lock()


@contextlib.contextmanager
def _exec_slot(user_id: str):
    with _exec_lock:
        if _exec_running.get(user_id, 0) >= _EXEC_PER_USER:
            raise coded(429, "rate_limited", f"at most {_EXEC_PER_USER} commands can run at once; wait for one to finish",
                        headers={"Retry-After": "5"})
        if not _EXEC_TOTAL.acquire(blocking=False):
            raise coded(429, "rate_limited", "too many commands are running right now; try again in a few seconds",
                        headers={"Retry-After": "5"})
        _exec_running[user_id] = _exec_running.get(user_id, 0) + 1
    try:
        yield
    finally:
        with _exec_lock:
            _exec_running[user_id] -= 1
            if not _exec_running[user_id]:
                del _exec_running[user_id]
        _EXEC_TOTAL.release()


@router.post("/platform/deployments/{deployment_id}/exec", response_model=None,
             responses=ok(schemas.ExecResult, "How the command ran"))
def run_in_deployment(
    deployment_id: str,
    payload: schemas.ExecRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Run a shell command in your running rental, as root, from /workspace,
    without SSH: e.g. update the software on every card in a loop. Returns
    its exit code and the last 64 KiB of its output. After `timeout_seconds`
    (at most 120) we stop waiting, but the command may still be running.
    A job it starts in the background keeps running and doesn't hold this
    up. Needs the `rentals` scope, like SSH access, and only in your own
    rentals (an admin key too). Up to 4 at once per account."""
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("deployment_exec", api_key.key_id, limit=60, window_seconds=60)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=False)
    # Root in a customer's pod is theirs alone: an admin key gets no more
    # here than over SSH, where a customer's own keys shut it out.
    if deployment is None or api_key.user_id is None or deployment.owner_user_id != api_key.user_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    if deployment.state != DeploymentState.READY:
        raise coded(409, "conflict", f"the rental is {deployment.state.value}; commands run while it is ready")
    if not str(deployment.endpoint or "").startswith("ssh://"):
        raise coded(409, "conflict", "commands run only in rentals (pods), not in models")
    # Who ran something where: like the SSH key hand-out, recorded (the
    # command's hash and length, not its text, which may hold a secret).
    log.info(
        "secret_access event=deployment_exec deployment_id=%s key_id=%s user_id=%s ip=%s command_sha256=%s len=%d",
        deployment_id, api_key.key_id, api_key.user_id, _audit_client_ip(),
        hashlib.sha256(payload.command.encode()).hexdigest()[:16], len(payload.command),
    )
    with _exec_slot(api_key.user_id):
        outcome, status, data = _node_request(
            deployment, f"/pods/{deployment_id}/exec", timeout=payload.timeout_seconds + 15.0, max_bytes=1024 * 1024,
            body={"command": payload.command, "timeout_seconds": payload.timeout_seconds})
    if outcome == "ok" and isinstance(data, dict):
        return data
    if outcome == "refused":
        code = {400: "invalid_request", 409: "conflict", 429: "rate_limited"}.get(status or 0, "unavailable")
        headers = {"Retry-After": "5"} if status == 429 else None
        raise coded(status if status in (400, 409, 429) else 503, code,
                    data or "the machine running this rental refused the command", headers=headers)
    if outcome == "unknown":
        # Sent, but no answer: running it again could run it twice.
        raise coded(504, "exec_unknown", "no answer from the machine in time: the command may or may not have run. "
                                         "Check before running it again (e.g. its log, or a quick read-only command)")
    raise coded(503, "unavailable", "the machine running this rental didn't answer; nothing was run. Try again shortly",
                headers={"Retry-After": "10"})


# --- the browser terminal (request #34) ------------------------------------------

_HOST_HEADER = re.compile(r"[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")


def _terminal_ws_base(request: Request) -> str:
    """ws(s)://host the caller reached us at: GREENCOMPUTE_PUBLIC_API_URL, else
    from the request (behind nginx, its X-Forwarded-Proto, when trusted)."""
    from greencompute_gateway import main

    base = (os.environ.get("GREENCOMPUTE_PUBLIC_API_URL") or "").strip().rstrip("/")
    if not base:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip() if main._TRUST_PROXY else ""
        host = request.headers.get("host") or ""
        if not _HOST_HEADER.fullmatch(host):
            host = request.url.netloc
        base = f"{'https' if (proto or request.url.scheme) == 'https' else 'http'}://{host}"
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def _rental_terminal_problem(deployment) -> HTTPException | None:
    if deployment.state != DeploymentState.READY:
        return coded(409, "conflict", f"the rental is {deployment.state.value}; a terminal opens while it is ready")
    if not str(deployment.endpoint or "").startswith("ssh://"):
        return coded(409, "conflict", "terminals open only in rentals (pods), not in models")
    return None


@router.post("/platform/deployments/{deployment_id}/terminal", response_model=None,
             responses=ok(schemas.TerminalTicket, "A one-time ticket for the terminal's WebSocket"))
def open_deployment_terminal(
    deployment_id: str,
    request: Request,
    payload: schemas.TerminalTicketRequest | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """A terminal in your running rental, without SSH keys: a root login
    shell in the pod, from /workspace, as SSH gives you. This returns a
    ticket; open `websocket_url` with it within 60 seconds (it works once).
    Needs the `rentals` scope, and only in your own rentals. At most 3 open
    at once per account; each closes after 15 minutes with no input or
    output, or 4 hours in all. Closing it ends the shell, as closing SSH
    does."""
    api_key = require_api_key(authorization, x_api_key)
    from greencompute_gateway.application import sanctions, terminal

    if not terminal.enabled():
        raise coded(501, "not_implemented", "the browser terminal is turned off")
    enforce_rate_limit("deployment_terminal", api_key.key_id, limit=30, window_seconds=60)
    deployment = service.get_deployment(deployment_id, user_id=api_key.user_id, admin=False)
    # Root in a customer's pod is theirs alone, as for exec and SSH.
    if deployment is None or api_key.user_id is None or deployment.owner_user_id != api_key.user_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    if sanctions.account_blocked(service.repository.session_factory, api_key.user_id):
        raise _wallet_blocked()
    problem = _rental_terminal_problem(deployment)
    if problem is not None:
        raise problem
    if terminal.open_count(api_key.user_id) >= terminal.PER_USER:
        raise coded(429, "terminal_limit", f"at most {terminal.PER_USER} terminals can be open at once on an "
                                           "account; close one first", headers={"Retry-After": "5"})
    ticket, expires = terminal.issue_ticket(
        service.repository.session_factory, user_id=api_key.user_id, key_id=api_key.key_id,
        deployment_id=deployment.deployment_id, shell=(payload.shell if payload else "auto"))
    log.info("secret_access event=deployment_terminal_ticket deployment_id=%s key_id=%s user_id=%s ip=%s",
             deployment.deployment_id, api_key.key_id, api_key.user_id, _audit_client_ip())
    path = f"/platform/deployments/{urllib_parse.quote(deployment.deployment_id, safe='')}/terminal"
    return {
        "ticket": ticket,
        "expires_in_seconds": terminal.TICKET_TTL_SECONDS,
        "expires_at": expires.isoformat(),
        "websocket_url": f"{_terminal_ws_base(request)}{path}?ticket={ticket}",
        "idle_timeout_seconds": terminal.IDLE_SECONDS,
        "max_session_seconds": terminal.MAX_SESSION_SECONDS,
    }


def _terminal_still_allowed(ticket, deployment_id: str) -> tuple[int, str, str] | None:
    """While a terminal is open: its key, the account and the rental, as at
    the start. None to carry on (also when the database can't be asked: the
    check at the start was made, and a blip shouldn't cut everyone off)."""
    from greencompute_gateway.application import sanctions, terminal
    from greencompute_gateway.transport import security

    try:
        if not terminal.key_still_allows(security.credential_store.session_factory, ticket.key_id, ticket.user_id):
            return 4403, "forbidden", "the API key this terminal was opened with was deleted"
        if sanctions.account_blocked(service.repository.session_factory, ticket.user_id):
            return 4403, "wallet_blocked", sanctions.BLOCKED_MESSAGE
        deployment = service.get_deployment(deployment_id, user_id=ticket.user_id, admin=False)
    except Exception:  # noqa: BLE001
        log.warning("terminal: couldn't re-check %s; keeping it open", deployment_id, exc_info=True)
        return None
    if deployment is None or deployment.owner_user_id != ticket.user_id:
        return 4404, "not_found", "deployment not found"
    problem = _rental_terminal_problem(deployment)
    return (4409, "conflict", str(problem.detail)) if problem is not None else None


def _terminal_admission(ticket_value: str, deployment_id: str, presented_secret: str | None):
    """Everything a terminal needs checked before it opens, in one trip to
    the thread pool: the ticket (taken, so it can't be used again), its key,
    the account, the rental. Returns (ticket, deployment, None) or (ticket or
    None, None, (close code, error code, message))."""
    from greencompute_gateway.application import sanctions, terminal
    from greencompute_gateway.transport import security

    invalid = (4401, "terminal_ticket_invalid",
               "this terminal ticket is unknown, already used, expired or for another rental; request a new one")
    ticket = terminal.consume_ticket(service.repository.session_factory, ticket_value)
    if ticket is None or ticket.deployment_id != deployment_id:
        return ticket, None, invalid
    # A client that can send a key (an agent) must send the one the ticket
    # was made with: someone else's ticket is no good with your key.
    if presented_secret is not None:
        presented = security.credential_store.get_api_key_by_secret(presented_secret)
        if presented is None or presented.key_id != ticket.key_id:
            return ticket, None, invalid
    if not terminal.key_still_allows(security.credential_store.session_factory, ticket.key_id, ticket.user_id):
        return ticket, None, (4403, "forbidden", "the API key this ticket was made with was deleted, expired or "
                                                  "can't open terminals any more")
    if sanctions.account_blocked(service.repository.session_factory, ticket.user_id):
        from greencompute_gateway.application.sanctions import BLOCKED_MESSAGE

        return ticket, None, (4403, "wallet_blocked", BLOCKED_MESSAGE)
    deployment = service.get_deployment(deployment_id, user_id=ticket.user_id, admin=False)
    if deployment is None or deployment.owner_user_id != ticket.user_id:
        return ticket, None, (4404, "not_found", "deployment not found")
    problem = _rental_terminal_problem(deployment)
    if problem is not None:
        return ticket, None, (4409, "conflict", str(problem.detail))
    return ticket, deployment, None


@router.websocket("/platform/deployments/{deployment_id}/terminal")
async def deployment_terminal(websocket: WebSocket, deployment_id: str) -> None:
    """The terminal's WebSocket: ?ticket=<from the POST>&cols=<n>&rows=<n>.
    Relayed to the node agent running the pod. Never blocks the event loop:
    the checks run in the thread pool, the relay is async I/O."""
    from greencompute_gateway import main
    from greencompute_gateway.application import terminal
    from greencompute_gateway.transport.security import extract_api_key_secret

    ticket_value = websocket.query_params.get("ticket") or ""

    async def spend() -> None:
        # A connection refused before its ticket is checked still uses it
        # up: the URL, ticket and all, may be in a log someone reads.
        if terminal.well_formed(ticket_value):
            with contextlib.suppress(Exception):
                await run_in_threadpool(terminal.consume_ticket, service.repository.session_factory, ticket_value)

    origin = websocket.headers.get("origin")
    if not terminal.origin_allowed(origin):
        log.warning("terminal refused: origin %r deployment_id=%s", (origin or "")[:200], deployment_id)
        await spend()
        await websocket.close(code=1008)  # before accepting: the handshake is refused (HTTP 403)
        return
    await websocket.accept()
    ip = main._client_ip_from_request(websocket)
    if not terminal.enabled():
        await spend()
        await terminal.refuse(websocket, 4501, "not_implemented", "the browser terminal is turned off")
        return
    # No limit per address here: behind the host's NAT every visitor has the
    # same one, so it would let anyone shut the terminal for everybody. A
    # ticket is 256 random bits, made at most 30 a minute per key, and
    # anything not shaped like one never reaches the database.
    if not terminal.well_formed(ticket_value):  # not worth a trip to the database
        await terminal.refuse(websocket, 4401, "terminal_ticket_invalid", "this terminal ticket is unknown, already "
                                                                           "used, expired or for another rental; "
                                                                           "request a new one")
        return
    presented = extract_api_key_secret(websocket.headers.get("authorization"), websocket.headers.get("x-api-key"))
    ticket, deployment, problem = await run_in_threadpool(
        _terminal_admission, ticket_value, deployment_id, presented)
    if problem is not None:
        log.info("secret_access event=deployment_terminal_refused deployment_id=%s user_id=%s ip=%s code=%s",
                 deployment_id, ticket.user_id if ticket else None, ip, problem[1])
        await terminal.refuse(websocket, *problem)
        return
    refused = terminal.take_slot(ticket.user_id)
    if refused:
        await terminal.refuse(websocket, 4429, "terminal_limit", refused)
        return
    session = terminal.Session()
    node = None
    try:
        cols, rows = terminal.clamp_size(websocket.query_params.get("cols"), websocket.query_params.get("rows"))
        base = await run_in_threadpool(_node_base_url, deployment)
        if not base:
            session.end_reason = "no_node_address"
            await terminal.refuse(websocket, 4503, "unavailable", "the machine running this rental didn't answer; "
                                                                   "try again shortly")
            return
        try:
            node = await terminal.open_node_link(
                terminal.node_terminal_url(base, deployment.deployment_id, cols=cols, rows=rows, shell=ticket.shell),
                os.environ.get("GREENCOMPUTE_INFERENCE_AUTH_SECRET"))
        except terminal.NodeUnavailableError as exc:
            log.warning("terminal %s: %s (deployment_id=%s node_id=%s)", session.session_id, exc,
                        deployment.deployment_id, getattr(deployment, "node_id", None))
            session.end_reason = "node_unavailable"
            await terminal.refuse(websocket, 4503, "unavailable", "the machine running this rental didn't answer; "
                                                                   "try again shortly")
            return
        not_ready = await terminal.wait_ready(node)
        if not_ready is not None:
            session.end_reason = f"node_refused_{not_ready[1]}"
            await terminal.refuse(websocket, *not_ready)
            return
        session.touch()
        log.info("secret_access event=deployment_terminal_start session=%s deployment_id=%s key_id=%s user_id=%s "
                 "ip=%s", session.session_id, deployment.deployment_id, ticket.key_id, ticket.user_id, ip)
        await websocket.send_text(json.dumps({
            "type": "ready", "idle_timeout_seconds": terminal.IDLE_SECONDS,
            "max_session_seconds": terminal.MAX_SESSION_SECONDS,
            "session_ends_at": (datetime.now(UTC) + timedelta(seconds=terminal.MAX_SESSION_SECONDS)).isoformat()}))

        async def recheck():
            return await run_in_threadpool(_terminal_still_allowed, ticket, deployment.deployment_id)
        ending = await terminal.relay(websocket, node, session, recheck=recheck)
        if ending is not None:
            close_code, code, message = ending
            if close_code == 1000:
                with contextlib.suppress(Exception):
                    await websocket.close(code=1000)
            else:
                await terminal.refuse(websocket, close_code, code, message)
    finally:
        terminal.release_slot(ticket.user_id)  # first: closing the node's end can be cut short
        if node is not None:
            await node.close()
        log.info("secret_access event=deployment_terminal_end session=%s deployment_id=%s key_id=%s user_id=%s "
                 "ip=%s reason=%s duration_s=%d bytes_in=%d bytes_out=%d", session.session_id,
                 deployment.deployment_id, ticket.key_id, ticket.user_id, ip, session.end_reason,
                 int(time.monotonic() - session.started), session.bytes_in, session.bytes_out)


@router.patch("/platform/deployments/{deployment_id}")
def update_deployment(
    deployment_id: str,
    payload: DeploymentUpdateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return _deployment_out(service.update_deployment(
            deployment_id,
            payload,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ), api_key.admin)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/platform/deployments/{deployment_id}", response_model=None, responses=ok(schemas.Deployment, "The deployment, now terminated"))
def terminate_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return _deployment_out(service.terminate_deployment(
            deployment_id,
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ), api_key.admin)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/platform/deployments/{deployment_id}/resume")
def resume_deployment(
    deployment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Pod-safe in-place resume of a SUSPENDED deployment — restarts the SAME
    container (preserves the user's saved work) after a 1-hour credit gate.
    Returns the same deployment record (SUSPENDED until the miner re-reports
    READY). 402 if balance can't cover an hour, 409 if the original node is
    full / its placement is gone.
    """
    from greencompute_gateway.application.services import InsufficientBalanceForRentalError, SanctionedAccountError

    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("resume_deployment", api_key.key_id, limit=30, window_seconds=60)
    try:
        return _deployment_out(service.resume_deployment(
            deployment_id,
            user_id=api_key.user_id,
            admin=api_key.admin,
        ), api_key.admin)
    except SanctionedAccountError as exc:
        raise _wallet_blocked() from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        # Not in SUSPENDED state.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InsufficientBalanceForRentalError as exc:
        raise HTTPException(status_code=402, detail=_hour_of_credit_detail(exc, "resume")) from exc


@router.post("/platform/secrets")
def create_secret(
    payload: UserSecretCreateRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return service.create_secret(api_key.user_id, payload).model_dump(mode="json")


@router.get("/platform/secrets")
def list_secrets(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return [secret.model_dump(mode="json") for secret in service.list_secrets(api_key.user_id)]


@router.delete("/platform/secrets/{secret_id}")
def delete_secret(
    secret_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return service.delete_secret(secret_id, user_id=api_key.user_id, admin=api_key.admin).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Commercial inquiries — public /contact-sales lead form
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """The real client address, as the request middleware worked it out
    (X-Real-IP from nginx when GREENCOMPUTE_TRUSTED_PROXY is on; the first
    X-Forwarded-For entry is whatever the client wrote, so it is never used)."""
    return (current_client_ip.get() or (request.client.host if request.client else "") or "")[:64]


# Allowed sales-inquiry statuses (free-string column, validated here).
# "attempted" = outreach has been attempted but the contact has NOT yet been
# reached — distinct from "contacted". Order reflects the funnel:
# new → attempted → contacted → won/lost.
_ALLOWED_INQUIRY_STATUSES = {"new", "attempted", "contacted", "won", "lost"}


@router.post("/platform/sales/inquiries", status_code=201)
def create_commercial_inquiry(
    payload: CommercialInquiryCreateRequest,
    request: Request,
) -> dict:
    """Public — no auth. Rate-limited by IP (5 / hour) and honeypotted."""
    enforce_rate_limit("commercial_inquiry", _client_ip(request), limit=10, window_seconds=3600)
    try:
        inquiry = service.submit_commercial_inquiry(
            payload,
            source_ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    # Only return the inquiry id and a friendly status — never leak source_ip
    # or other server-side metadata back to the caller.
    return {"inquiry_id": inquiry.inquiry_id, "status": inquiry.status}


@router.get("/platform/sales/inquiries", include_in_schema=False)
def list_commercial_inquiries(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [
        inquiry.model_dump(mode="json")
        for inquiry in service.list_commercial_inquiries(
            status=status, limit=limit, offset=offset
        )
    ]


@router.patch("/platform/sales/inquiries/{inquiry_id}", include_in_schema=False)
def update_commercial_inquiry(
    inquiry_id: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    # `status` is OPTIONAL: omit it (or send "") to save notes only without
    # changing the funnel stage — lets sales log context before reaching a
    # contact. When present it must be a known status.
    raw_status = body.get("status")
    status = str(raw_status).strip() if raw_status is not None else None
    if not status:
        status = None
    elif status not in _ALLOWED_INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be one of " + "|".join(sorted(_ALLOWED_INQUIRY_STATUSES)),
        )
    notes = body.get("notes")
    if notes is not None:
        notes = str(notes)[:5000]
    if status is None and notes is None:
        raise HTTPException(status_code=400, detail="provide status and/or notes")
    try:
        return service.update_commercial_inquiry_status(
            inquiry_id, status=status, notes=notes
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Bare-metal inquiries — dedicated /rental sales form
# ---------------------------------------------------------------------------


@router.post("/platform/sales/bare-metal-inquiries", status_code=201)
def create_bare_metal_inquiry(
    payload: BareMetalInquiryCreateRequest,
    request: Request,
) -> dict:
    """Public — no auth. Same hardening as commercial inquiries."""
    enforce_rate_limit(
        "bare_metal_inquiry", _client_ip(request), limit=10, window_seconds=3600
    )
    try:
        inquiry = service.submit_bare_metal_inquiry(
            payload,
            source_ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"inquiry_id": inquiry.inquiry_id, "status": inquiry.status}


@router.get("/platform/sales/bare-metal-inquiries", include_in_schema=False)
def list_bare_metal_inquiries(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [
        inq.model_dump(mode="json")
        for inq in service.list_bare_metal_inquiries(
            status=status, limit=limit, offset=offset
        )
    ]


@router.patch("/platform/sales/bare-metal-inquiries/{inquiry_id}", include_in_schema=False)
def update_bare_metal_inquiry(
    inquiry_id: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    # `status` optional — omit (or "") to save review_notes only. See the
    # commercial PATCH above for the rationale.
    raw_status = body.get("status")
    status = str(raw_status).strip() if raw_status is not None else None
    if not status:
        status = None
    elif status not in _ALLOWED_INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be one of " + "|".join(sorted(_ALLOWED_INQUIRY_STATUSES)),
        )
    review_notes = body.get("review_notes")
    if review_notes is not None:
        review_notes = str(review_notes)[:5000]
    if status is None and review_notes is None:
        raise HTTPException(status_code=400, detail="provide status and/or review_notes")
    try:
        return service.update_bare_metal_inquiry_status(
            inquiry_id, status=status, review_notes=review_notes
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# GPU capacity overrides — admin-controlled cluster size, shown in the admin
# view of the GPU pool only (the public sees availability from the scheduler)
# ---------------------------------------------------------------------------


@router.get("/platform/admin/capacity-overrides", include_in_schema=False)
def list_capacity_overrides(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [
        ov.model_dump(mode="json") for ov in service.list_gpu_capacity_overrides()
    ]


@router.put("/platform/admin/capacity-overrides/{gpu_model}", include_in_schema=False)
def upsert_capacity_override(
    gpu_model: str,
    body: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key, admin_required=True)
    try:
        total = int(body.get("total_gpus") or 0)
        avail = int(body.get("available_gpus") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="total_gpus and available_gpus must be integers") from exc
    if total < 0 or avail < 0:
        raise HTTPException(status_code=400, detail="counts must be non-negative")
    if avail > total:
        raise HTTPException(status_code=400, detail="available_gpus cannot exceed total_gpus")
    override = GpuCapacityOverride(
        gpu_model=gpu_model,
        total_gpus=total,
        available_gpus=avail,
        note=str(body.get("note") or "")[:255],
        updated_by=(api_key.user_id or api_key.name or "admin"),
    )
    saved = service.upsert_gpu_capacity_override(override)
    return saved.model_dump(mode="json")


@router.delete("/platform/admin/capacity-overrides/{gpu_model}", include_in_schema=False)
def delete_capacity_override(
    gpu_model: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    deleted = service.delete_gpu_capacity_override(gpu_model)
    if not deleted:
        raise HTTPException(status_code=404, detail="override not found")
    return {"deleted": True, "gpu_model": gpu_model}


# ---------------------------------------------------------------------------
# Provider server onboarding — UI-driven SSH provisioning of a node-agent
# onto a whitelisted provider's own hardware. The SSH credential is used by
# the background job only and never persisted.
# ---------------------------------------------------------------------------


def _server_owner_view(rec) -> dict:
    """Strip nothing secret here (no creds are stored), but cap the log on
    the wire and present a clean dict."""
    d = rec.model_dump(mode="json")
    return d


@router.post("/platform/provider/servers", status_code=201)
def create_provider_server(
    payload: ProviderServerCreateRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None and not api_key.admin:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    enforce_rate_limit("provider_server_create", api_key.key_id, limit=20, window_seconds=3600)
    try:
        record, inputs = service.create_provider_server(
            payload, user_id=api_key.user_id, admin=api_key.admin,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Provision in the background (paramiko is blocking; BackgroundTasks runs
    # sync callables in the threadpool, off the event loop).
    background_tasks.add_task(service.run_provisioning, inputs)
    return _server_owner_view(record)


@router.get("/platform/provider/servers")
def list_provider_servers(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    return [
        _server_owner_view(s)
        for s in service.list_provider_servers(api_key.user_id, admin=api_key.admin)
    ]


@router.get("/platform/provider/servers/{server_id}")
def get_provider_server(
    server_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        return _server_owner_view(
            service.get_provider_server(server_id, user_id=api_key.user_id, admin=api_key.admin)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/provider/servers/{server_id}")
def delete_provider_server(
    server_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    try:
        deleted = service.delete_provider_server(server_id, user_id=api_key.user_id, admin=api_key.admin)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"deleted": True, "server_id": deleted.server_id}


@router.get("/v1/models")
def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """OpenAI-compatible model listing.

    Not decorative: OpenAI-compatible clients (Cursor, aider, Continue, LibreChat)
    call this to discover models and to validate a custom base URL. Returning 404
    made those tools report the endpoint as broken even though inference worked
    perfectly -- the failure looked like "your API is down", not "discovery is
    unimplemented".

    Lists inference workloads the gateway can actually route to, so a client that
    picks an id from here can use it verbatim as `model`.
    """
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("list_models", api_key.key_id, limit=60, window_seconds=60)

    seen: dict[str, int] = {}
    for workload in service.control_plane.list_workloads():
        if getattr(workload, "kind", None) is not None and str(workload.kind).lower().rsplit(".", 1)[-1] != "inference":
            continue
        # `public` is the same visibility flag the model catalog uses. Without it
        # this endpoint published every inference workload ever created -- 21 of
        # 25 were internal test artifacts (ssrf-poc-workload, sqli-test2,
        # file-env-leak...), which would have appeared in every customer's model
        # picker in Cursor.
        if not getattr(workload, "public", False):
            continue
        name = (workload.name or "").strip()
        if not name or name in seen:
            continue
        created = getattr(workload, "created_at", None)
        seen[name] = int(created.timestamp()) if created is not None else 0

    # Externally-hosted models have no workload row, so they must be added
    # explicitly or discovery-driven clients (Cursor, aider) never see them and
    # report the endpoint as not offering the model.
    from greencompute_gateway.application.services import _external_model_upstreams
    for model_id in _external_model_upstreams():
        seen.setdefault(model_id, 0)

    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": seen[name], "owned_by": "greencompute"}
            for name in sorted(seen)
        ],
    }


@router.post("/v1/chat/completions")
def chat_completions(
    payload: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    host: str | None = Header(default=None, alias="Host"),
) -> dict:
    from greencompute_gateway.application.services import InsufficientBalanceForInferenceError
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository

    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("chat_completions", api_key.key_id, limit=60, window_seconds=60)
    _enforce_key_limits(api_key)
    _enforce_account_spend_limit(api_key)
    _refuse_blocked_account(api_key)

    # Pre-flight balance gate: require any positive balance. Per-request cost
    # is fractions of a cent for small prompts, so we don't need a large
    # floor — just "not broke". Admin keys bypass. Anonymous (user_id=None)
    # also bypasses so internal / system callers are not blocked. A private
    # model costs its caller nothing (its owner pays by the hour), so it is
    # only gated when the request is charged per token.
    if api_key.user_id is not None and not api_key.admin:
        current = BillingRepository().get_balance(api_key.user_id)
        if current <= 0 and service.bills_per_token(
            payload, routed_host=host, user_id=api_key.user_id, admin=api_key.admin
        ):
            raise HTTPException(
                status_code=402,
                detail={
                    "message": "insufficient balance for inference",
                    "current_cents": current,
                },
            )

    try:
        if payload.stream:
            metrics.increment("invoke.stream")
            return StreamingResponse(
                service.stream_chat_completion(
                    payload,
                    api_key_id=api_key.key_id,
                    user_id=api_key.user_id,
                    routed_host=host,
                    admin=api_key.admin,
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache"},
            )
        response = service.invoke_chat_completion(
            payload,
            api_key_id=api_key.key_id,
            user_id=api_key.user_id,
            routed_host=host,
            admin=api_key.admin,
        ).model_dump(mode="json")
        metrics.increment("invoke.success")
        return response
    except InsufficientBalanceForInferenceError as exc:
        metrics.increment("invoke.failure.insufficient_balance")
        raise HTTPException(
            status_code=402,
            detail={"message": "insufficient balance for inference", "reason": str(exc)},
        ) from exc
    except NoReadyDeploymentError as exc:
        metrics.increment("invoke.failure.no_ready_deployment")
        raise coded(409, "model_not_ready", str(exc), {"Retry-After": "10"}) from exc
    except InferenceTimeoutError as exc:
        metrics.increment("invoke.failure.timeout")
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except InferenceConnectionError as exc:
        metrics.increment("invoke.failure.connection")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InferenceBadResponseError as exc:
        metrics.increment("invoke.failure.bad_response")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except InferenceUpstreamError as exc:
        metrics.increment("invoke.failure.upstream")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/v1/completions")
def completions(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    host: str | None = Header(default=None, alias="Host"),
) -> dict:
    if not payload.get("model"):
        raise HTTPException(status_code=400, detail="model required")
    try:
        request = ChatCompletionRequest(
            model=payload["model"],
            messages=[{"role": "user", "content": payload.get("prompt", "")}],
            max_tokens=payload.get("max_tokens", 128),
            temperature=payload.get("temperature", 0.7),
            stream=payload.get("stream", False),
        )
    except ValidationError as exc:
        detail = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors(include_url=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from exc
    return chat_completions(request, authorization=authorization, x_api_key=x_api_key, host=host)


@router.post("/v1/embeddings")
def embeddings(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    enforce_rate_limit("embeddings", api_key.key_id, limit=60, window_seconds=60)
    _enforce_key_limits(api_key)
    _enforce_account_spend_limit(api_key)
    text = payload.get("input", "")
    vector = [round(((ord(char) % 32) / 31.0), 6) for char in str(text)[:16]]
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "model": payload.get("model", "greencompute-embedding"),
    }


from greencompute_gateway.domain.gpu_catalog import normalize_gpu_model as _normalize_gpu_model
from greencompute_gateway.domain.gpu_catalog import public_gpu_models as _public_gpu_models


def _rentable_now() -> list[str]:
    """Public GPU ids with a node the scheduler would use right now."""
    return service.control_plane.online_gpu_models()


def _capacity_by_model() -> dict[str, dict[str, int]]:
    """Normalised GPU model -> the scheduler's counts (free, most free on one
    machine, live fleet). Internal: only their availability state is public."""
    return service.control_plane.capacity_by_model()


@router.get("/platform/nodes/supported", response_model=None, responses=ok(list[str], "GPU ids you can rent right now"))
def list_supported_gpus() -> list[str]:
    """Public -- the GPU ids with a machine online and taking rentals.

    No API key required: an agent needs this to decide whether to sign up at
    all. Any spelling is accepted on input ("RTX 4090", "rtx-4090"); these are
    the canonical forms. A GPU we price but have no usable node for is omitted
    -- advertising it made agents create deployments that could never
    schedule. Whether any are FREE is `available_now` in /platform/pricing.
    """
    return _rentable_now()


@router.get("/platform/pricing", response_model=None, responses=ok(schemas.Pricing))
def public_pricing() -> dict:
    """Public, machine-readable price list. No API key required.

    Agents budget from the API, not the marketing page, so this must be the
    numbers billing actually charges: GPU rates come from the same table that
    locks onto a deployment at placement, and model rates are resolved through
    the same `rates_for_model` the inference meter uses, for exactly the models
    /v1/models currently lists.
    """
    from greencompute_protocol import (
        GPU_RATE_CENTS_PER_HOUR,
        GPU_VRAM_GB,
        INFERENCE_MIN_CHARGE_CENTS,
        rates_for_model,
    )

    # How rented out each model is, from the scheduler's count (net of
    # reservations, on nodes it would use), as a state and never a number:
    # the owner keeps fleet and free counts private. One snapshot gives
    # `online` (a node of the model exists), `availability` and
    # `available_now`, so they always agree with each other, with the GPU
    # pool, and with what a create is told.
    from greencompute_control_plane.domain.availability import RENTABLE, availability_of
    from greencompute_gateway.application.billing_service import BONUS_RATES

    alpha_bonus = BONUS_RATES.get("alpha", 0.0)
    capacity = _capacity_by_model()
    gpus = []
    for m in _public_gpu_models():
        c = capacity.get(_normalize_gpu_model(m))
        state = availability_of(c)
        gpus.append({
            "gpu_model": m,
            "online": c is not None,
            "available_now": state in RENTABLE,
            "availability": state,
            "vram_gb": GPU_VRAM_GB.get(m),
            "cents_per_gpu_hour": GPU_RATE_CENTS_PER_HOUR[m],
            "usd_per_gpu_hour": round(GPU_RATE_CENTS_PER_HOUR[m] / 100, 2),
            # An external API user had to work this out by hand (Oct 2026).
            "usd_per_gpu_hour_paid_in_alpha": round(GPU_RATE_CENTS_PER_HOUR[m] / 100 / (1 + alpha_bonus), 4),
        })
    models = []
    for name in sorted(_public_inference_model_names()):
        cin, cout = rates_for_model(name)
        models.append({
            "model": name,
            "usd_per_million_input_tokens": round(cin / 100, 2),
            "usd_per_million_output_tokens": round(cout / 100, 2),
        })
    return {
        "currency": "USD",
        "payment_bonuses": {k: v for k, v in BONUS_RATES.items() if v > 0},
        "gpu_rental": {
            "billing": "per minute while running, per GPU",
            "deployment_fee_usd": 0,
            "gpus": gpus,
        },
        "inference": {
            "billing": "per token; minimum charge per request",
            "minimum_charge_usd": round(INFERENCE_MIN_CHARGE_CENTS / 100, 2),
            "models": models,
        },
        "private_model": {
            "billing": (
                "per minute while running, at the gpu_rental rate per GPU; requests to it are not "
                "charged per token. Starting or resuming one needs one hour of that rate in credit"
            ),
            "idle_pause_minutes": control_plane_settings.idle_private_endpoint_timeout_seconds // 60,
        },
        "docs": "https://www.green-compute.com/docs",
    }


def _public_inference_model_names() -> list[str]:
    """Names of the public inference workloads -- the same filter /v1/models
    uses -- less customers' own models, which are paid for by the hour by
    their owner rather than per token."""
    names = []
    for workload in service.control_plane.list_workloads():
        kind = getattr(workload, "kind", None)
        if kind is not None and str(kind).lower().rsplit(".", 1)[-1] != "inference":
            continue
        if not getattr(workload, "public", False):
            continue
        if getattr(workload, "owner_user_id", None) and not is_catalog_workload(
            workload.workload_id, getattr(workload, "metadata", None)
        ):
            continue
        name = (workload.name or "").strip()
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


@router.get("/platform/v1/debug/route/{model}", include_in_schema=False)
def debug_route(
    model: str,
    host: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    try:
        workload, routing = service.resolve_workload_reference(model, routed_host=host)
        workload_id = workload.workload_id
        deployment = service.control_plane.resolve_ready_deployment(workload_id)
        return {
            "model": model,
            "routing": routing,
            "workload_id": workload_id,
            "workload_alias": workload.workload_alias,
            "ingress_host": workload.ingress_host,
            "deployment": deployment.model_dump(mode="json", exclude=DEPLOYMENT_SECRET_FIELDS) if deployment else None,
        }
    except NoReadyDeploymentError as exc:
        return {"model": model, "host": host, "error": str(exc), "deployment": None}


@router.get("/platform/v1/debug/routing-decisions", include_in_schema=False)
def debug_routing_decisions(
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.list_routing_decisions(limit=limit)


@router.get("/platform/v1/metrics", include_in_schema=False)
def platform_metrics(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return metrics.snapshot()


@router.get("/platform/v1/payment/summary", include_in_schema=False)
def payment_summary(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.payment_summary()


@router.get("/platform/v1/invocations", include_in_schema=False)
def list_invocations(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [record.model_dump(mode="json") for record in service.list_invocations(limit=limit)]


@router.get("/platform/v1/invocations/exports/recent", include_in_schema=False)
def export_recent_invocations(
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.export_recent_invocations(limit=limit)


@router.get("/platform/model-aliases")
def list_model_aliases(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    workloads = service.list_workloads(user_id=api_key.user_id, admin=api_key.admin)
    return [
        {"alias": w.workload_alias, "workload_id": w.workload_id}
        for w in workloads
        if w.workload_alias
    ]


@router.post("/platform/model-aliases")
def create_or_update_model_alias(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    alias = payload.get("alias")
    workload_id = payload.get("workload_id")
    if not alias or not workload_id:
        raise HTTPException(status_code=400, detail="alias and workload_id required")
    try:
        from greencompute_protocol import WorkloadUpdateRequest

        return service.update_workload(
            workload_id,
            WorkloadUpdateRequest(workload_alias=alias),
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="alias must be a 1–100 char string") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/platform/model-aliases/{alias}")
def delete_model_alias(
    alias: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    workloads = service.list_workloads(user_id=api_key.user_id, admin=api_key.admin)
    target = next((w for w in workloads if w.workload_alias == alias), None)
    if target is None:
        raise HTTPException(status_code=404, detail="model alias not found")
    try:
        from greencompute_protocol import WorkloadUpdateRequest

        return service.update_workload(
            target.workload_id,
            WorkloadUpdateRequest(clear_workload_alias=True),
            actor_user_id=api_key.user_id,
            admin=api_key.admin,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/platform/v1/invocations/{invocation_id}", include_in_schema=False)
def get_invocation(
    invocation_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    record = service.get_invocation(invocation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="invocation not found")
    return record.model_dump(mode="json")


@router.get("/platform/v1/debug/invocation-failures", include_in_schema=False)
def debug_invocation_failures(
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return service.control_plane.invocation_failure_report(limit=limit)


@router.get("/registry/auth")
def registry_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    # The registry password is a shared infra credential. Scope-tighten: only
    # hand it to a user-bound key or an admin — never to an anonymous/system
    # key (user_id None and not admin).
    if api_key.user_id is None and not api_key.admin:
        raise HTTPException(status_code=403, detail="registry auth requires a user-bound api key")
    # Audit: a shared infra credential is being handed out.
    metrics.increment("secret_access.registry_auth")
    log.info(
        "secret_access event=registry_auth key_id=%s user_id=%s ip=%s",
        api_key.key_id,
        api_key.user_id,
        _audit_client_ip(),
    )
    import base64
    from greencompute_persistence.runtime import load_runtime_settings
    settings = load_runtime_settings("greencompute-gateway")
    registry_password = getattr(settings, "registry_password", "greencompute-registry")
    auth_string = base64.b64encode(f":{registry_password}".encode()).decode()
    return {"authenticated": True, "auth_header": f"Basic {auth_string}"}


@router.get("/guess/vllm_config")
def guess_vllm_config(
    model: str = Query(..., description="HuggingFace model id (org/model)"),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    from greencompute_gateway.infrastructure.guesser import analyze_model
    try:
        req = analyze_model(model)
        return req.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/logos")
def upload_logo(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("logos")


@router.get("/logos/{logo_id}.{ext}")
def get_logo(
    logo_id: str,
    ext: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("logos")


@router.get("/bounties")
def list_bounties(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key)
    _feature_disabled("bounties")


@router.post("/audit/miner_data", include_in_schema=False)
def audit_miner_data(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    _feature_disabled("audit")


@router.get("/audit/", include_in_schema=False)
def list_audit(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    _feature_disabled("audit")


@router.get("/audit/download", include_in_schema=False)
def audit_download(
    path: str = Query(""),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    _feature_disabled("audit")


@router.get("/misc/proxy")
def misc_proxy(
    url: str = Query(""),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("misc_proxy")


@router.get("/misc/hf_repo_info")
def misc_hf_repo_info(
    repo: str = Query(""),
    path: str = Query(""),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("misc_hf_repo_info")


@router.get("/e2e/instances/{workload_id}")
def e2e_instances(
    workload_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key)
    _feature_disabled("e2e_instances")


@router.get("/idp/scopes")
def idp_scopes() -> list[str]:
    _feature_disabled("idp")


@router.get("/idp/authorize")
def idp_authorize(
    client_id: str = Query(""),
    redirect_uri: str = Query(""),
    response_type: str = Query(""),
    scope: str = Query(""),
) -> dict:
    _feature_disabled("idp")


@router.post("/idp/token")
def idp_token(payload: dict) -> dict:
    _feature_disabled("idp")


@router.post("/e2e/invoke")
def e2e_invoke(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key)
    _feature_disabled("e2e_invoke")


@router.get("/platform/v1/debug/build-failures", include_in_schema=False)
def debug_build_failures(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    require_api_key(authorization, x_api_key, admin_required=True)
    return [build.model_dump(mode="json") for build in service.list_failed_builds()]


# ---------------------------------------------------------------------------
# Billing endpoints
# ---------------------------------------------------------------------------


def _get_billing():
    from greencompute_gateway.application.billing_service import get_billing_service
    return get_billing_service()


@router.get("/platform/billing/balance", response_model=None, responses=ok(schemas.Balance))
def billing_balance(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return _get_billing().get_balance(api_key.user_id)


@router.get("/platform/billing/forecast", response_model=None, responses=ok(schemas.Forecast, "How long your balance lasts at the current rate"))
def billing_forecast(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """What your running rentals and private models cost per hour together,
    and how long your balance lasts at that rate. You are emailed once when it
    drops under 24 hours and once more under 2 hours; a rental created with
    `resume_on_topup: true` comes back by itself after you top up."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    from greencompute_gateway.application.credit_watch import forecast

    return forecast(api_key.user_id, control_plane=service.control_plane)


@router.get("/platform/billing/spending", response_model=None, responses=ok(schemas.SpendingSummary, "What you spent, per day and per deployment"))
def billing_spending(
    from_: str | None = Query(default=None, alias="from", description="First day, YYYY-MM-DD (UTC). Default: 29 days before `to`."),
    to: str | None = Query(default=None, description="Last day, YYYY-MM-DD (UTC), included. Default: today."),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Your charges added up per day and per deployment (with its name, GPUs
    and hours), and your top-ups, for up to 400 days. The ledger has one row
    per metered minute; this is the readable version."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    from greencompute_gateway.application.statements import parse_range, spending_summary

    try:
        first, last = parse_range(from_, to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return spending_summary(_get_billing().repo, service.control_plane, api_key.user_id, first, last)


@router.get("/platform/billing/statement", response_model=None, responses=ok(schemas.Statement, "One month's statement"))
def billing_statement(
    month: str | None = Query(default=None, description="YYYY-MM (UTC). Default: this month."),
    format: str = Query(default="json", description="`json`, or `csv` for a file to download."),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """A month as statement lines (one per deployment per day per kind of
    charge, plus each top-up, refund or adjustment), with opening and closing
    balance. `format=csv` returns the same as a CSV file."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be json or csv")
    from greencompute_gateway.application.statements import monthly_statement, statement_csv

    try:
        statement = monthly_statement(_get_billing().repo, service.control_plane, api_key.user_id, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if format == "csv":
        return Response(
            content=statement_csv(statement),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="green-compute-statement-{statement["month"]}.csv"'},
        )
    return statement


@router.get("/platform/billing/ledger", response_model=None, responses=ok(list[schemas.LedgerItem], "Top-ups and charges, newest first"))
def billing_ledger(
    limit: int = 50,
    offset: int = 0,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    entries = _get_billing().list_ledger(api_key.user_id, limit=limit, offset=offset)
    return [e.model_dump(mode="json") for e in entries]


@router.post("/platform/billing/topup/stripe")
def billing_topup_stripe(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    amount_usd = _validate_topup_amount(payload.get("amount_usd"))
    try:
        return _get_billing().create_stripe_topup(api_key.user_id, amount_usd)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/platform/billing/topup/crypto", response_model=None, responses=ok(schemas.CryptoInvoiceCreated))
def billing_topup_crypto(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    currency = payload.get("currency", "").lower()
    allowed = (
        "usdt-eth", "usdt-base",
        "usdc-eth", "usdc-base",
        "tao",
        # Alpha (subnet token) — auto-credited by scan_bittensor in the deposit
        # watcher, which matches SubtensorModule.StakeTransferred events to our
        # deposit coldkey on the configured netuid. Priced via price_feed.
        "alpha",
    )
    if currency in ("usdt", "usdc"):
        # A bare stablecoin code names no chain. The deposit watcher only
        # credits chain-qualified invoices, so this one could never be paid.
        raise HTTPException(
            status_code=400,
            detail=f"{currency} needs a chain: use {currency}-base or {currency}-eth",
        )
    if currency not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"currency must be one of: {', '.join(allowed)}",
        )
    amount_usd = _validate_topup_amount(payload.get("amount_usd"))
    from greencompute_gateway.application.billing_service import (
        MAX_PENDING_INVOICES_PER_USER,
        TooManyOpenInvoices,
    )
    try:
        return _get_billing().create_crypto_invoice(api_key.user_id, currency, amount_usd)
    except TooManyOpenInvoices as exc:
        raise coded(
            429, "too_many_invoices",
            f"you already have {MAX_PENDING_INVOICES_PER_USER} unpaid invoices open; pay one "
            "or wait for it to expire (30 minutes), or pay from a verified wallet without an invoice",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/platform/billing/webhook/stripe", include_in_schema=False)
async def billing_stripe_webhook(request: Request) -> dict:
    """Stripe webhook — called by Stripe when a checkout session completes,
    or when a charge is refunded / disputed (BILL-M1 Part B)."""
    from greencompute_gateway.infrastructure.stripe_client import (
        _payment_intent_id,
        verify_webhook_signature,
    )
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook_signature(payload, sig)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"webhook verification failed: {exc}") from exc
    event_type = event.get("type")
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session_data = event["data"]["object"]
        stripe_session_id = session_data["id"]
        # The completed session carries its PaymentIntent id — record it so a
        # later refund/dispute can map back to this top-up (BILL-M1 Part B).
        payment_intent_id = _payment_intent_id(session_data.get("payment_intent"))
        # Gate the credit on a PAID session. `checkout.session.completed` can
        # fire with payment_status in {'unpaid','no_payment_required'} for
        # async/delayed payment methods — crediting then books money we never
        # captured. Only credit when paid/complete; otherwise leave the session
        # pending for the reconcile job or a later async_payment_succeeded
        # event. confirm_stripe_payment is idempotent (BILL-C2), so a later
        # async event after a borderline 'complete' is safe.
        payment_status = session_data.get("payment_status")
        # MUST be payment_status=='paid'. `status=='complete'` is true for EVERY
        # checkout.session.completed event (incl. unpaid async sessions), so
        # OR-ing it in would credit money we never captured. A delayed payment
        # arrives later as checkout.session.async_payment_succeeded (paid).
        if payment_status == "paid":
            _get_billing().confirm_stripe_payment(
                stripe_session_id, payment_intent_id=payment_intent_id
            )
        else:
            log.info(
                "Stripe %s for session %s not credited (payment_status=%r) — "
                "awaiting capture / reconcile",
                event_type, stripe_session_id, payment_status,
            )
    elif event_type in ("charge.refunded", "charge.dispute.created"):
        # BILL-M1 Part B: reverse a prior top-up on a verified (signature-
        # checked above) refund or dispute. The event's `object` is a Charge
        # (refund) or a Dispute (chargeback); both carry the `payment_intent`,
        # which is our join key back to the originating checkout session — the
        # event does NOT carry our checkout session id. We resolve via the
        # session's recorded payment_intent_id and idempotently debit by the
        # amount actually refunded/disputed, keyed on the Stripe event id so a
        # re-delivered event reverses at most once. If the session can't be
        # resolved (historical session with NULL payment_intent_id), we log +
        # skip for manual reconcile rather than guess.
        obj = event["data"]["object"]
        event_id = event.get("id") or ""
        payment_intent_id = _payment_intent_id(obj.get("payment_intent"))
        if event_type == "charge.refunded":
            # A charge can be partially refunded across several events; reverse
            # the incremental amount of THIS refund. Prefer the most recent
            # refund's amount; fall back to amount_refunded.
            amount_cents = _latest_refund_amount_cents(obj)
        else:  # charge.dispute.created — the Dispute object carries `amount`
            amount_cents = int(obj.get("amount") or 0)
        try:
            _get_billing().reverse_stripe_topup_for_event(
                event_id=event_id,
                payment_intent_id=payment_intent_id,
                amount_cents=amount_cents,
                reason=("refund" if event_type == "charge.refunded" else "dispute"),
            )
        except Exception:
            # Never 200-swallow a genuine processing failure silently into a
            # lost reversal: log loudly and let Stripe retry (we re-raise as a
            # 500 so the event is redelivered; the reversal is idempotent).
            log.exception(
                "Stripe %s reversal failed for event %s", event_type, event_id
            )
            raise HTTPException(status_code=500, detail="reversal processing failed")
    return {"received": True}


def _latest_refund_amount_cents(charge: dict) -> int:
    """Best-effort amount (in cents) for the most recent refund on a Charge.

    `charge.refunded` fires once per refund. Stripe includes a `refunds` list
    (data ordered newest-first) whose latest entry is this refund's amount; if
    that's unavailable we fall back to the charge's cumulative
    `amount_refunded`. Both are integer minor units (cents) already.
    """
    refunds = charge.get("refunds")
    if isinstance(refunds, dict):
        data = refunds.get("data")
        if isinstance(data, list) and data:
            amt = data[0].get("amount")
            if amt is not None:
                return int(amt)
    return int(charge.get("amount_refunded") or 0)


def _request_review(invoice_id: str, user_id: str, tx_hash: str, note: str | None) -> dict:
    tx_hash = (tx_hash or "").strip()
    if not tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required")
    billing = _get_billing()
    # One transaction is reviewed once (the same hash filed on two invoices
    # left an admin to spot the duplicate).
    if billing.repo.tx_credited_to(user_id, tx_hash):
        raise HTTPException(status_code=409, detail="This transaction has already been added to your balance.")
    already = billing.repo.open_review_for_tx(user_id, tx_hash)
    if already and already != invoice_id:
        raise HTTPException(
            status_code=409,
            detail=f"You've already asked us to check this transaction (invoice {already[:8]}). "
            "We'll email you when it's done.",
        )
    result = billing.repo.request_invoice_review(invoice_id, user_id, tx_hash, note)
    if result is None:
        # Either the invoice doesn't exist or the caller doesn't own it; same
        # response either way so we don't leak invoice ids.
        raise HTTPException(status_code=404, detail="invoice not found")
    if result.get("refused"):
        raise HTTPException(
            status_code=409, detail=f"invoice is {result['status']}; there is nothing to review"
        )
    from greencompute_gateway.infrastructure.alerts import send_payment_alert

    username = _get_billing().repo.get_username(user_id) or user_id
    send_payment_alert(
        f"Payment review requested by {username}: invoice {invoice_id[:8]} ({result['status']}), "
        f"tx {tx_hash}. Resolve it under Billing admin > Review requests.",
        email_support=False,  # the website emails support its own review request email
    )
    _get_billing().email_review_received(invoice_id)
    return result


@router.post("/platform/billing/crypto/{invoice_id}/review")
def billing_request_review(
    invoice_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """User-side: a payment for this invoice wasn't credited (wrong amount,
    sent after it expired, paid from an exchange...). Body: {"tx_hash", "note"
    (optional)}. Works on pending and expired invoices; support checks the
    transaction on-chain and credits it."""
    user_id = _require_user(authorization, x_api_key)
    return _request_review(invoice_id, user_id, str(payload.get("tx_hash") or ""), payload.get("note"))


@router.post("/platform/billing/crypto/{invoice_id}/report-tx")
def billing_report_crypto_tx(
    invoice_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Older name for /review: attaches the tx hash and opens a review."""
    user_id = _require_user(authorization, x_api_key)
    return _request_review(invoice_id, user_id, str(payload.get("tx_hash") or ""), None)


@router.post("/platform/billing/crypto/{invoice_id}/confirm", include_in_schema=False)
def billing_confirm_crypto(
    invoice_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — confirm a crypto deposit."""
    require_api_key(authorization, x_api_key, admin_required=True)
    tx_hash = payload.get("tx_hash", "")
    if not tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required")
    from greencompute_gateway.application.billing_service import AlreadyCreditedError, BlockedPaymentError

    try:
        result = _get_billing().confirm_crypto_deposit(invoice_id, tx_hash)
    except (AlreadyCreditedError, BlockedPaymentError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if result.get("refused"):
        raise HTTPException(
            status_code=409,
            detail=f"invoice is {result.get('status')} and can no longer be credited",
        )
    return result


@router.post("/platform/billing/crypto/{invoice_id}/reject", include_in_schema=False)
def billing_reject_crypto(
    invoice_id: str,
    payload: dict | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — reject a crypto invoice. Does NOT credit the user. Marks
    status as 'rejected' so it drops out of the pending queue, and emails the
    customer why. Body: {"reason": a code from
    GET /platform/billing/admin/crypto/reasons, "note": your own words
    (required for "other")}. Idempotent. Rejecting a confirmed invoice
    returns 404 — use admin debit to reverse that flow instead.
    """
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.domain.payment_decisions import DecisionError

    payload = payload or {}
    try:
        result = _get_billing().reject_invoice(invoice_id, payload.get("reason"), payload.get("note"))
    except DecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        # Either doesn't exist or already confirmed — both look like 409/404.
        # Using 404 for simplicity; admin can refresh to see current state.
        raise HTTPException(
            status_code=404,
            detail="invoice not found or already confirmed",
        )
    return result


@router.get("/platform/billing/crypto/invoices", response_model=None, responses=ok(list[schemas.CryptoInvoiceListed]))
def billing_list_crypto_invoices(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """User-side — the caller's own crypto top-up invoices, newest first, so
    they can track status (pending / confirmed / expired / rejected) after
    generating one. Scoped to the api key's user_id."""
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    from greencompute_gateway.domain.payment_decisions import describe_invoice

    repo = _get_billing().repo
    outcomes = repo.invoice_outcomes(api_key.user_id)
    out = []
    for inv in repo.list_crypto_invoices(api_key.user_id):
        extra = outcomes.get(inv.invoice_id, {})
        item = {
            **inv.model_dump(mode="json"),
            "amount_to_send": f"{inv.amount_crypto:.6f}",
            "review_status": extra.get("review_status"),
            "decision_reason": extra.get("decision_reason"),
        }
        message, next_step = describe_invoice(
            {**item, "amount_crypto": inv.amount_crypto, "decision_note": extra.get("decision_note")},
            extra.get("received"),
        )
        item["status_message"] = message
        item["next_step"] = next_step
        out.append(item)
    return out


def _require_user(authorization: str | None, x_api_key: str | None) -> str:
    api_key = require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise HTTPException(status_code=403, detail="api key must be bound to a user")
    return api_key.user_id


@router.get("/platform/billing/crypto/addresses")
def billing_crypto_addresses(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Where to send each currency, and which wallet type can be verified
    for it. A verified wallet can pay these addresses without an invoice."""
    _require_user(authorization, x_api_key)
    from greencompute_gateway.application.billing_service import (
        BONUS_RATES,
        _base_currency,
        _deposit_address_for,
    )
    from greencompute_gateway.infrastructure.wallet_signatures import CURRENCY_CHAIN

    currencies = []
    for currency, chain in CURRENCY_CHAIN.items():
        address = _deposit_address_for(currency)
        if address:
            currencies.append({
                "currency": currency,
                "deposit_address": address,
                "wallet_chain": chain,
                "bonus_pct": BONUS_RATES.get(_base_currency(currency), 0.0),
            })
    return {"currencies": currencies}


@router.get("/platform/billing/crypto/deposits")
def billing_crypto_deposits(
    limit: int = 50,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """The caller's credited on-chain deposits, newest first."""
    user_id = _require_user(authorization, x_api_key)
    keep = ("deposit_ref", "currency", "tx_hash", "sender", "amount", "status",
            "matched_by", "invoice_id", "credited_cents", "observed_at", "credited_at")
    return [
        {k: d[k] for k in keep}
        for d in _get_billing().repo.list_user_deposits(user_id, limit=max(1, min(limit, 200)))
    ]


@router.get("/platform/billing/wallets")
def billing_list_wallets(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """The caller's verified wallets."""
    user_id = _require_user(authorization, x_api_key)
    return _get_billing().repo.list_verified_wallets(user_id)


@router.post("/platform/billing/wallets/challenge")
def billing_wallet_challenge(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Step 1 of verifying a wallet: the message to sign with it.
    Body: {"chain": "bittensor" | "evm", "address": "..."}."""
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.infrastructure.wallet_signatures import InvalidAddress

    try:
        return _get_billing().wallet_challenge(
            user_id, str(payload.get("chain") or "").lower(), str(payload.get("address") or "")
        )
    except InvalidAddress as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/platform/billing/wallets")
def billing_link_wallet(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Step 2: link the wallet with a signature over the challenge message.
    Body: {"chain", "address", "issued_at" (from the challenge), "signature"}.
    Also credits any unmatched payments the wallet sent in the last 30 days."""
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.application.billing_service import WalletBlocked, WalletProofError
    from greencompute_gateway.infrastructure.billing_repository import (
        MAX_WALLETS_PER_USER,
        TooManyWallets,
        WalletLinkedElsewhere,
    )
    from greencompute_gateway.infrastructure.wallet_signatures import InvalidAddress

    try:
        return _get_billing().link_wallet(
            user_id,
            str(payload.get("chain") or "").lower(),
            str(payload.get("address") or ""),
            str(payload.get("issued_at") or ""),
            str(payload.get("signature") or ""),
        )
    except (InvalidAddress, WalletProofError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WalletBlocked as exc:
        raise _wallet_blocked() from exc
    except WalletLinkedElsewhere as exc:
        raise HTTPException(
            status_code=409, detail="this wallet is already verified by another account"
        ) from exc
    except TooManyWallets as exc:
        raise HTTPException(
            status_code=409, detail=f"an account can verify at most {MAX_WALLETS_PER_USER} wallets"
        ) from exc


@router.patch("/platform/billing/wallets/{wallet_id}")
def billing_update_wallet(
    wallet_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Turn signing in with a verified wallet on or off: {"sign_in": true}.
    A wallet linked to receive payments can't sign in to the account until
    this is on; turning it off revokes the API keys its sign-ins issued and
    ends the website sessions it began.

    Turning it on gives that wallet a full-access way in, so it needs a
    full-access key, and when the account already has wallets that sign in,
    a signature from one of them: ask for a challenge with `action`
    `approve_sign_in` and `subject` `<chain>:<address>` of this wallet, and
    send {"sign_in": true, "nonce", "signature"}. Without it: 403
    approval_required, naming the wallets that can sign."""
    api_key = _user_key(authorization, x_api_key)
    user_id = api_key.user_id
    enabling = payload.get("sign_in")
    if not isinstance(enabling, bool):
        raise coded(400, "invalid_request", 'send {"sign_in": true} or {"sign_in": false}')
    from greencompute_gateway.application import wallet_auth
    from greencompute_gateway.infrastructure.billing_repository import ApprovalNeeded, CallerShutOut, WalletIsTheSignIn
    from greencompute_gateway.infrastructure.wallet_signatures import wallet_tag

    repo = _get_billing().repo
    wallets = repo.list_verified_wallets(user_id)
    target = next((w for w in wallets if w["wallet_id"] == wallet_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    approved_by = None
    if enabling and not target["sign_in"]:
        if api_key.scopes and not api_key.admin:
            # A limited key (say billing only) must not mint itself a way to
            # a full-access key by letting a wallet it controls sign in.
            raise coded(403, "scope_required", {
                "message": "turning on sign-in for a wallet needs a full-access key",
                "required_scope": "full access", "key_scopes": list(api_key.scopes),
            })
        subject = wallet_tag(target["chain"], target["address"])
        if any(w["sign_in"] for w in wallets if w["wallet_id"] != wallet_id):
            try:
                approved_by = wallet_auth.approve(
                    service.repository.session_factory, user_id=user_id, action="approve_sign_in",
                    subject=subject, nonce=str(payload.get("nonce") or ""),
                    signature=str(payload.get("signature") or ""),
                )
            except wallet_auth.WalletAuthError as exc:
                raise _wallet_auth_error(exc) from exc
    try:
        result = repo.set_wallet_sign_in(user_id, wallet_id, enabling, approved_by=approved_by,
                                         caller_key_id=api_key.key_id)
    except CallerShutOut as exc:
        raise HTTPException(status_code=401, detail="invalid api key") from exc
    except WalletIsTheSignIn as exc:
        raise coded(
            409, "conflict",
            "this wallet is how this account signs in, and the account has no email: add an email on the "
            "website (Settings) or turn on signing in with another wallet first",
        ) from exc
    except ApprovalNeeded as exc:
        # Another wallet was turned on in the meantime, and it didn't sign this.
        raise _wallet_auth_error(wallet_auth.approval_required(
            "approve_sign_in", wallet_tag(target["chain"], target["address"]),
            wallet_auth.sign_in_wallets(service.repository.session_factory, user_id),
        )) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    if enabling and result.get("changed"):
        # A new way into the account: tell its owner, as for a sign-in.
        _email_wallet_sign_in(user_id, target["chain"], target["address"], "", event="sign_in_on")
    return result


@router.delete("/platform/billing/wallets/{wallet_id}")
def billing_unlink_wallet(
    wallet_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Unlink a verified wallet. Later payments from it are no longer
    credited to this account automatically, it can no longer sign in to the
    account, and the API keys its sign-ins issued stop working. The only
    wallet of an account with no email can't be unlinked: nothing else
    signs in to it."""
    user_id = _require_user(authorization, x_api_key)
    from greencompute_gateway.infrastructure.billing_repository import WalletIsTheSignIn, WalletUnderSanctions

    try:
        result = _get_billing().repo.unlink_wallet(user_id, wallet_id)
    except WalletIsTheSignIn as exc:
        raise coded(
            409, "conflict",
            "this wallet is how this account signs in, and the account has no email: add an email on the "
            "website (Settings) or verify another wallet first",
        ) from exc
    except WalletUnderSanctions as exc:
        raise coded(
            409, "conflict", "This wallet can't be removed from the account. Contact support@green-compute.com.",
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return result


@router.get("/platform/billing/admin/crypto/invoices", include_in_schema=False)
def billing_admin_list_crypto_invoices(
    status: str | None = None,
    limit: int = 100,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — list crypto invoices (optionally filtered by status) with the
    user's email/username joined in so the admin can tell who each deposit
    is for at a glance. Used by the admin billing UI."""
    require_api_key(authorization, x_api_key, admin_required=True)
    return _get_billing().repo.list_all_crypto_invoices_for_admin(
        status=status,
        limit=max(1, min(limit, 500)),
    )


@router.get("/platform/billing/admin/monitor", include_in_schema=False)
def billing_admin_monitor(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — what the billing monitor sees right now: problems, notices and
    today's numbers. Read-only; alerts are sent by the background loop."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.application.billing_monitor import snapshot

    return snapshot(_get_billing().repo.session_factory)


@router.get("/platform/billing/admin/crypto/deposits", include_in_schema=False)
def billing_admin_list_deposits(
    status: str | None = None,
    limit: int = 200,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — on-chain deposits the watcher logged; status=unmatched lists
    payments nobody has been credited for yet."""
    require_api_key(authorization, x_api_key, admin_required=True)
    return _get_billing().repo.list_deposits_for_admin(status=status, limit=max(1, min(limit, 500)))


@router.get("/platform/admin/sanctions", include_in_schema=False)
def admin_sanctions_state(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — sanctions screening: each list's last download, how many
    addresses it holds and its last error. Blocked payments are listed by
    GET /platform/billing/admin/crypto/deposits?status=blocked."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.application import sanctions

    return {
        "enabled": sanctions.enabled(),
        "oracle_enabled": sanctions.oracle_enabled(),
        "lists": sanctions.list_state(service.repository.session_factory),
    }


@router.post("/platform/admin/sanctions/refresh", include_in_schema=False)
def admin_sanctions_refresh(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — download the lists now (normally daily). A list that fails
    keeps its last good copy."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.application import sanctions

    return sanctions.refresh(service.repository.session_factory)


@router.get("/platform/admin/sanctions/check", include_in_schema=False)
def admin_sanctions_check(
    address: str = Query(...),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — what screening says about one address: the list naming it
    (if any) and, for EVM, the oracle's answer (null if it can't be asked)."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.application import sanctions
    from greencompute_gateway.infrastructure.wallet_signatures import InvalidAddress, detect_chain, normalize_address

    chain = detect_chain(address)
    try:
        canonical = normalize_address(chain, address)
    except InvalidAddress as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "chain": chain,
        "address": canonical,
        "listed": sanctions.listed(service.repository.session_factory, chain, canonical),
        "oracle": sanctions.oracle_says(canonical) if chain == "evm" else None,
    }


@router.get("/platform/billing/admin/crypto/reasons", include_in_schema=False)
def billing_admin_payment_reasons(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — the reasons a review can be dismissed ("review") or an invoice
    rejected ("reject") with; the customer is emailed the matching text."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.domain.payment_decisions import reason_list

    return reason_list()


@router.get("/platform/billing/admin/crypto/reviews", include_in_schema=False)
def billing_admin_list_reviews(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict]:
    """Admin — open review requests, oldest first, each with any logged
    deposit carrying the transaction hash the user reported."""
    require_api_key(authorization, x_api_key, admin_required=True)
    return _get_billing().repo.list_open_reviews()


@router.post("/platform/billing/admin/crypto/reviews/{invoice_id}/resolve", include_in_schema=False)
def billing_admin_resolve_review(
    invoice_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — close a review. Body: {"action": "credit_deposit" (with
    "deposit_ref") | "confirm_invoice" | "dismiss" (with "reason" from
    GET /platform/billing/admin/crypto/reasons, and "note", required for
    "other")}. A dismissal emails the customer the reason."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.application.billing_service import ReviewError
    from greencompute_gateway.infrastructure.price_feed import PriceUnavailable

    try:
        return _get_billing().resolve_review(
            invoice_id, str(payload.get("action") or ""), payload.get("deposit_ref"),
            reason=payload.get("reason"), note=payload.get("note"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="invoice not found") from exc
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PriceUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"{exc}: the deposit can't be valued yet") from exc


@router.post("/platform/billing/admin/crypto/deposits/{deposit_ref}/assign", include_in_schema=False)
def billing_admin_assign_deposit(
    deposit_ref: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — credit an unmatched deposit to a user, at its value when it
    arrived. Body: {"user": user id, username or email}. Refuses a deposit
    that was already credited."""
    require_api_key(authorization, x_api_key, admin_required=True)
    ident = str(payload.get("user") or payload.get("user_id") or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="user required (id, username or email)")
    billing = _get_billing()
    user_id = billing.repo.find_user_id(ident)
    if user_id is None:
        raise HTTPException(status_code=404, detail=f"no single account matches {ident!r}")
    from greencompute_gateway.infrastructure.price_feed import PriceUnavailable

    try:
        result = billing.assign_deposit(deposit_ref, user_id)
    except PriceUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"{exc}: it can't be valued yet") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="deposit not found")
    if result.get("already_credited"):
        raise HTTPException(status_code=409, detail="deposit was already credited")
    if result.get("blocked"):
        raise HTTPException(status_code=409, detail="deposit's sender is on a sanctions list: it can't be credited")
    if result.get("stray"):
        raise HTTPException(
            status_code=409, detail="deposit is alpha of another subnet (or its amount couldn't be read): it can't be credited",
        )
    return result


@router.post("/platform/billing/admin/crypto/deposits/{deposit_ref}/ignore", include_in_schema=False)
def billing_admin_ignore_deposit(
    deposit_ref: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — mark an unmatched deposit as nobody's top-up (our own transfer,
    a refund, dust): it is no longer alerted or offered for assignment."""
    require_api_key(authorization, x_api_key, admin_required=True)
    result = _get_billing().repo.ignore_deposit(deposit_ref)
    if result is None:
        raise HTTPException(status_code=404, detail="deposit not found")
    if result.get("refused"):
        raise HTTPException(status_code=409, detail=f"deposit is {result['status']}, not unmatched")
    return result


@router.get("/platform/billing/admin/miner-revenue", include_in_schema=False)
def billing_admin_miner_revenue(
    window_hours: int = 168,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — miner revenue accrued from successful inference requests.
    Feeds the admin /flux dashboard revenue tile."""
    require_api_key(authorization, x_api_key, admin_required=True)
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository
    rows = BillingRepository().aggregate_miner_revenue(window_hours=window_hours)
    total_cents = sum(r["cents_earned"] for r in rows)
    return {
        "window_hours": window_hours,
        "total_cents": total_cents,
        "rows": rows,
    }


@router.get("/platform/billing/bonus-rates")
def billing_bonus_rates() -> dict:
    """Public — returns the bonus rates for each payment method."""
    from greencompute_gateway.application.billing_service import BONUS_RATES
    return {k: f"+{int(v*100)}%" for k, v in BONUS_RATES.items()}


# ---------------------------------------------------------------------------
# Admin commercial analytics
#
# All routes here require an admin API key. Aggregations are bounded by
# `days` (capped server-side in AnalyticsRepository) so curling silly windows
# can't take the DB down. Each panel has its own endpoint — UI fetches in
# parallel so one slow query doesn't block the rest.
# ---------------------------------------------------------------------------


def _analytics_repo():
    """Cached per-process instance — same DB engine reuse pattern as
    BillingRepository elsewhere in this file."""
    from greencompute_gateway.infrastructure.analytics_repository import (
        AnalyticsRepository,
    )

    return AnalyticsRepository()


@router.get("/platform/admin/analytics/revenue-by-miner", include_in_schema=False)
def analytics_revenue_by_miner(
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    rows = _analytics_repo().revenue_by_miner(days=days, limit=limit)
    return {
        "window_days": days,
        "total_cents_earned": sum(r["cents_earned"] for r in rows),
        "rows": rows,
    }


@router.get("/platform/admin/analytics/top-renters", include_in_schema=False)
def analytics_top_renters(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=25, ge=1, le=200),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    rows = _analytics_repo().top_renters(days=days, limit=limit)
    return {
        "window_days": days,
        "total_cents_spent": sum(r["cents_spent"] for r in rows),
        "rows": rows,
    }


@router.get("/platform/admin/analytics/gross-revenue", include_in_schema=False)
def analytics_gross_revenue(
    days: int = Query(default=30, ge=1, le=365),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return _analytics_repo().gross_revenue(days=days)


@router.get("/platform/admin/analytics/active-rentals", include_in_schema=False)
def analytics_active_rentals(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    require_api_key(authorization, x_api_key, admin_required=True)
    return _analytics_repo().active_rentals()


@router.get("/platform/admin/analytics/gpu-usage", include_in_schema=False)
def analytics_gpu_usage(
    days: int = 7,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — per GPU class: online/offline, rented, serving models, free,
    and requests turned away for lack of capacity in the last `days`."""
    require_api_key(authorization, x_api_key, admin_required=True)
    return _analytics_repo().gpu_usage(days=days)


@router.post("/platform/billing/admin/credit", include_in_schema=False)
def billing_admin_credit(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — manually credit a user's balance (for testing or manual adjustments)."""
    require_api_key(authorization, x_api_key, admin_required=True)
    user_id = payload.get("user_id")
    amount_usd = payload.get("amount_usd")
    description = payload.get("description", "Manual admin credit")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not amount_usd or amount_usd <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    amount_cents = int(round(float(amount_usd) * 100))
    billing = _get_billing()
    entry = billing.repo.credit_user(
        user_id=user_id,
        amount_cents=amount_cents,
        kind="topup",
        description=description,
    )
    return {
        "credited": True,
        "user_id": user_id,
        "amount_cents": amount_cents,
        "balance_after": entry.balance_after,
        "balance_usd": round(entry.balance_after / 100.0, 2),
    }


@router.post("/platform/billing/admin/debit", include_in_schema=False)
def billing_admin_debit(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Admin — manually debit a user's balance."""
    require_api_key(authorization, x_api_key, admin_required=True)
    user_id = payload.get("user_id")
    amount_usd = payload.get("amount_usd")
    description = payload.get("description", "Manual admin debit")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not amount_usd or amount_usd <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")
    amount_cents = int(round(float(amount_usd) * 100))
    billing = _get_billing()
    # Admin claw-back is trusted and may push the balance negative — this is
    # the fraud/chargeback case where the user already spent the funds, so a
    # strict debit (which would 400) is exactly wrong here. Usage/inference
    # debits stay strict (they call debit_user without allow_negative).
    entry = billing.repo.debit_user(
        user_id=user_id,
        amount_cents=amount_cents,
        kind="refund",
        description=description,
        allow_negative=True,
    )
    return {
        "debited": True,
        "user_id": user_id,
        "amount_cents": amount_cents,
        "balance_after": entry.balance_after,
        "balance_usd": round(entry.balance_after / 100.0, 2),
    }

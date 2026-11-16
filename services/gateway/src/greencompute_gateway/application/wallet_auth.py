"""Signing in with a wallet: no email, no password, no browser.

1. challenge: we issue a one-time message (domain/wallet_auth.py) naming the
   wallet, and keep its exact text.
2. The wallet signs it: EIP-4361 personal_sign (EVM) or sr25519 (Bittensor).
3. verify: the nonce is claimed (once, before the signature is even looked
   at), the signature is checked against the text WE issued, the wallet is
   screened, and its account is found or created. An agent gets an API key;
   the website gets the account id and keeps its own session key.

The account is the wallet: its verified_wallets row, unique per (chain,
address). A wallet an email account already verified for payments signs in
to THAT account; a new wallet gets a new account with no email. Either way
payments from the wallet are credited to the account automatically, since
that row is what the deposit watcher matches senders against.

Keys issued here carry api_keys.issued_by_wallet. Signing in again revokes
those (a lost key is a new signature, not a support ticket), and only those:
never the website's session key or keys made with POST /platform/api-keys.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    AdminAccountORM,
    APIKeyORM,
    UserORM,
    VerifiedWalletORM,
    WalletAuthChallengeORM,
)
from greencompute_gateway.application import sanctions
from greencompute_gateway.domain import wallet_auth as msg
from greencompute_gateway.infrastructure.wallet_signatures import (
    CHAINS,
    InvalidAddress,
    checksum_address,
    detect_chain,
    normalize_address_strict,
    signature_problem,
    verify_signature,
    wallet_tag,
)

log = logging.getLogger(__name__)

DEFAULT_KEY_NAME = "wallet sign-in"
# Expired challenges are kept this long (for support questions), then pruned.
# Short, so the table stays small whatever rate challenges are asked for at.
_KEEP_EXPIRED = timedelta(hours=1)
_NONCE = re.compile(r"^[0-9A-Za-z]{8,64}$")
# Wallets a sanctions hit was already alerted on, in this process.
_ALERTED: set[str] = set()


class WalletAuthError(Exception):
    """A wallet sign-in request that can't go ahead. `code` is the API's
    error code; `message` says what to do next."""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


# --- 1. the challenge -------------------------------------------------------------


def issue_challenge(
    session_factory,
    *,
    address: str,
    chain: str | None = None,
    action: str = "sign_in",
    chain_id: int | None = None,
    client_ip: str | None = None,
) -> dict:
    """A one-time message for this wallet to sign. The same answer whether
    or not the wallet has an account: nothing here looks one up."""
    chain = (chain or detect_chain(address)).strip().lower()
    if chain not in CHAINS:
        raise WalletAuthError(400, "invalid_request", "chain must be `evm` (Ethereum or Base) or `bittensor`.")
    action = (action or "sign_in").strip().lower()
    if action not in msg.ACTIONS:
        raise WalletAuthError(400, "invalid_request", f"action must be one of: {', '.join(msg.ACTIONS)}.")
    try:
        canonical = normalize_address_strict(chain, address)
    except InvalidAddress as exc:
        raise WalletAuthError(400, "invalid_request", f"Not a valid {chain} address: {exc}.") from exc
    if chain == "evm":
        chain_id = 1 if chain_id is None else chain_id
        if chain_id not in msg.EVM_CHAIN_IDS:
            raise WalletAuthError(400, "invalid_request", "chain_id must be 1 (Ethereum) or 8453 (Base).")
        display = checksum_address(canonical)
    else:
        chain_id, display = None, canonical
    now = _now().replace(microsecond=0)
    expires = now + timedelta(seconds=msg.challenge_ttl_seconds())
    nonce = msg.new_nonce()
    domain, uri = msg.auth_domain(), msg.auth_uri()
    text = msg.sign_in_message(
        chain=chain, display_address=display, action=action, nonce=nonce, issued_at=now, expires_at=expires,
        chain_id=chain_id or 1, domain=domain, uri=uri,
    )
    with session_scope(session_factory) as session:
        session.execute(delete(WalletAuthChallengeORM).where(WalletAuthChallengeORM.expires_at < now - _KEEP_EXPIRED))
        session.add(WalletAuthChallengeORM(
            nonce=nonce, chain=chain, address=canonical, action=action, message=text,
            client_ip=(client_ip or None) and client_ip[:64], created_at=now, expires_at=expires,
        ))
    return {
        "nonce": nonce,
        "message": text,
        "chain": chain,
        "address": display,
        "action": action,
        "domain": domain,
        "uri": uri,
        "chain_id": chain_id,
        "issued_at": _iso(now),
        "expires_at": _iso(expires),
        "expires_in_seconds": msg.challenge_ttl_seconds(),
        "sign_with": (
            "personal_sign (EIP-191) over `message`, exactly as given"
            if chain == "evm" else
            "the wallet's coldkey (sr25519) over `message`, exactly as given, e.g. "
            "`btcli wallet sign --no-use-hotkey --message '<message>'`"
        ),
        "next": "POST /platform/auth/wallet/verify with {\"nonce\": ..., \"signature\": \"0x...\"}",
    }


# --- 2. claiming and checking it ----------------------------------------------------


def _claim(session_factory, nonce: str) -> WalletAuthChallengeORM:
    """Mark the challenge used, in its own committed transaction, before the
    signature is checked: each nonce gets exactly one attempt, and two
    requests racing with the same one can't both pass (the UPDATE is
    conditional, so only one changes the row)."""
    if not _NONCE.match(nonce or ""):
        raise _unknown_challenge()
    now = _now()
    with session_scope(session_factory) as session:
        claimed = session.execute(
            update(WalletAuthChallengeORM)
            .where(
                WalletAuthChallengeORM.nonce == nonce,
                WalletAuthChallengeORM.used_at.is_(None),
                WalletAuthChallengeORM.expires_at > now,
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        row = session.get(WalletAuthChallengeORM, nonce)
        if row is None:
            raise _unknown_challenge()
        if claimed != 1:
            if row.used_at is not None:
                raise WalletAuthError(
                    400, "challenge_invalid",
                    "This sign-in message was already used. Each one works once: request a new one "
                    "from POST /platform/auth/wallet/challenge and sign that.",
                )
            raise WalletAuthError(
                400, "challenge_invalid",
                f"This sign-in message expired (they last {msg.challenge_ttl_seconds() // 60} minutes). "
                "Request a new one from POST /platform/auth/wallet/challenge and sign it straight away.",
            )
        session.expunge(row)
        return row


def _unknown_challenge() -> WalletAuthError:
    return WalletAuthError(
        400, "challenge_invalid",
        "No sign-in message has this nonce. Send the `nonce` from POST /platform/auth/wallet/challenge.",
    )


def _check_signature(challenge: WalletAuthChallengeORM, signature: str) -> None:
    problem = signature_problem(challenge.chain, signature)
    if problem:
        raise WalletAuthError(
            400, "signature_invalid",
            f"{problem[0].upper()}{problem[1:]}. This message can't be used again: request a new challenge and sign it.",
        )
    if not verify_signature(challenge.chain, challenge.address, challenge.message, signature, strict=True):
        who = "the coldkey of" if challenge.chain == "bittensor" else "the key of"
        raise WalletAuthError(
            401, "signature_invalid",
            f"The signature doesn't match the message for this wallet. Sign the exact `message` with {who} "
            f"{challenge.address}, without changing it. This message can't be used again: request a new challenge.",
        )


# --- 3. the account ---------------------------------------------------------------


def wallet_owner(session_factory, chain: str, address: str) -> str | None:
    with session_scope(session_factory) as session:
        return session.scalar(select(VerifiedWalletORM.user_id).where(
            VerifiedWalletORM.chain == chain, VerifiedWalletORM.address == address))


def _free_username(session, address: str) -> str:
    # The address itself (unique already: one account per wallet), unless an
    # account somehow took that name first.
    for candidate in (address, f"{address[:52]}-{secrets.token_hex(4)}"):
        if session.scalar(select(UserORM.user_id).where(UserORM.username == candidate)) is None:
            return candidate
    return f"wallet-{uuid4().hex}"


def _create_account(session_factory, chain: str, address: str) -> str | None:
    """A new account and its wallet, in one transaction. None when another
    request created this wallet's account in the meantime (the unique index
    on verified_wallets decides; the caller then signs in to that one)."""
    now = _now()
    user_id = str(uuid4())
    try:
        with session_scope(session_factory) as session:
            session.add(UserORM(
                user_id=user_id, username=_free_username(session, address), email=None,
                display_name=short_address(address), profile_metadata={}, balance_credits=0,
                created_at=now, signup_method="wallet",
            ))
            session.add(VerifiedWalletORM(
                wallet_id=str(uuid4()), user_id=user_id, chain=chain, address=address, created_at=now,
                sanctions_checked_at=now,
            ))
    except IntegrityError:
        return None
    log.info("wallet sign-in: new account %s for %s %s", user_id, chain, short_address(address))
    return user_id


def _blocked(session_factory, chain: str, address: str, source: str, user_id: str | None) -> WalletAuthError:
    """Refuse a sanctioned wallet: flag it, tell staff once, tell the caller
    only the neutral sentence."""
    sanctions.flag_wallet(session_factory, chain, address, source)
    key = f"{chain}:{address}"
    if key not in _ALERTED:
        _ALERTED.add(key)
        from greencompute_gateway.infrastructure.alerts import send_payment_alert

        send_payment_alert(
            f"Sanctions screening refused a wallet sign-in: {chain} {address} (named by {source}). "
            + (f"It belongs to account {user_id}, which can no longer rent." if user_id else "No account was created.")
        )
    return WalletAuthError(403, "wallet_blocked", sanctions.BLOCKED_MESSAGE)


def _account_for(session_factory, chain: str, address: str) -> tuple[str, bool]:
    """(user_id, created) for a wallet that just proved itself, screened."""
    owner = wallet_owner(session_factory, chain, address)
    try:
        hit = sanctions.screen(session_factory, chain, address, new_account=owner is None)
    except sanctions.ScreeningUnavailable as exc:
        raise WalletAuthError(
            503, "screening_unavailable",
            "We can't check new wallets right now, so we can't create an account for this one yet. "
            "Request a new challenge and try again in a few minutes.",
            retry_after_seconds=300,
        ) from exc
    if hit:
        raise _blocked(session_factory, chain, address, hit, owner)
    if owner is not None:
        return owner, False
    created = _create_account(session_factory, chain, address)
    if created is None:
        owner = wallet_owner(session_factory, chain, address)
        if owner is None:
            raise WalletAuthError(409, "conflict", "This wallet's account is being set up. Try again in a moment.")
        return owner, False
    return created, True


def _user_or_conflict(session_factory, user_id: str) -> UserORM:
    with session_scope(session_factory) as session:
        user = session.get(UserORM, user_id)
        if user is None or (user.username or "").startswith("deleted-"):
            raise WalletAuthError(
                409, "conflict",
                "This wallet is linked to an account that no longer exists. Contact support@green-compute.com.",
            )
        session.expunge(user)
        return user


# --- 4. keys ----------------------------------------------------------------------


def _key_out(row: APIKeyORM) -> dict:
    return {
        "key_id": row.key_id,
        "key_name": row.name,
        "scopes": list(row.scopes or []),
        "max_active_rentals": row.max_active_rentals,
        "daily_spend_limit_usd": row.daily_spend_limit_cents / 100 if row.daily_spend_limit_cents is not None else None,
    }


def _issue_key(
    session_factory, user_id: str, tag: str, *, name: str, scopes: list[str], max_active_rentals: int | None,
    daily_spend_limit_usd: float | None, keep_existing_keys: bool,
) -> tuple[str, dict, list[str]]:
    """(secret, key fields, revoked key ids): the new key, and unless
    `keep_existing_keys` the earlier keys this wallet's sign-ins issued
    revoked in the same transaction."""
    now = _now()
    with session_scope(session_factory) as session:
        revoked: list[str] = []
        if not keep_existing_keys:
            revoked = list(session.scalars(select(APIKeyORM.key_id).where(
                APIKeyORM.user_id == user_id, APIKeyORM.issued_by_wallet == tag)).all())
            if revoked:
                session.execute(delete(APIKeyORM).where(APIKeyORM.key_id.in_(revoked)))
        row = APIKeyORM(
            key_id=str(uuid4()), user_id=user_id, name=name, admin=False, scopes=scopes,
            secret=f"gk_{secrets.token_urlsafe(24)}", created_at=now,
            max_active_rentals=max_active_rentals,
            daily_spend_limit_cents=round(daily_spend_limit_usd * 100) if daily_spend_limit_usd is not None else None,
            issued_by_wallet=tag,
        )
        session.add(row)
        session.flush()
        return row.secret, _key_out(row), revoked


def revoke_wallet_keys(session_factory, user_id: str, chain: str, address: str) -> list[str]:
    """Delete every key this wallet's sign-ins issued on this account."""
    tag = wallet_tag(chain, address)
    with session_scope(session_factory) as session:
        ids = list(session.scalars(select(APIKeyORM.key_id).where(
            APIKeyORM.user_id == user_id, APIKeyORM.issued_by_wallet == tag)).all())
        if ids:
            session.execute(delete(APIKeyORM).where(APIKeyORM.key_id.in_(ids)))
    return ids


# --- verify -----------------------------------------------------------------------


def verify(
    session_factory,
    *,
    nonce: str,
    signature: str,
    key_name: str | None = None,
    scopes: list[str] | None = None,
    max_active_rentals: int | None = None,
    daily_spend_limit_usd: float | None = None,
    keep_existing_keys: bool = False,
) -> dict:
    """Step 3 for an agent: a signed challenge in, an API key out (or, for
    a revoke_keys challenge, the keys revoked)."""
    from greencompute_gateway.domain.scopes import normalize_scopes

    try:
        scopes = normalize_scopes(scopes)
    except ValueError as exc:
        raise WalletAuthError(400, "invalid_request", f"{exc}.") from exc
    name = (key_name or DEFAULT_KEY_NAME).strip()[:64] or DEFAULT_KEY_NAME

    challenge = _claim(session_factory, (nonce or "").strip())
    _check_signature(challenge, signature)
    chain, address = challenge.chain, challenge.address
    wallet = {"chain": chain, "address": address}

    if challenge.action == "revoke_keys":
        # Revoking needs no screening and never creates an account.
        owner = wallet_owner(session_factory, chain, address)
        revoked = revoke_wallet_keys(session_factory, owner, chain, address) if owner else []
        return {"api_key": None, "user_id": owner, "wallet": wallet, "revoked_key_ids": revoked}

    user_id, created = _account_for(session_factory, chain, address)
    user = _user_or_conflict(session_factory, user_id)
    secret, key, revoked = _issue_key(
        session_factory, user_id, wallet_tag(chain, address), name=name, scopes=scopes,
        max_active_rentals=max_active_rentals, daily_spend_limit_usd=daily_spend_limit_usd,
        keep_existing_keys=keep_existing_keys,
    )
    log.info("wallet sign-in: key %s for account %s (%s), revoked %d", key["key_id"], user_id, chain, len(revoked))
    return {
        "api_key": secret,
        **key,
        "user_id": user_id,
        "account_created": created,
        "has_email": bool((user.email or "").strip()),
        "wallet": wallet,
        "revoked_key_ids": revoked,
    }


def verify_for_website(session_factory, *, nonce: str, signature: str) -> dict:
    """Step 3 for the website's sign-in: the same checks, the account id
    back and no key issued or revoked (the website keeps its own key per
    account, which a wallet's sign-ins never touch)."""
    challenge = _claim(session_factory, (nonce or "").strip())
    if challenge.action != "sign_in":
        raise WalletAuthError(400, "invalid_request", "This message is for revoking keys, not signing in.")
    _check_signature(challenge, signature)
    user_id, created = _account_for(session_factory, challenge.chain, challenge.address)
    user = _user_or_conflict(session_factory, user_id)
    with session_scope(session_factory) as session:
        is_admin = session.get(AdminAccountORM, user_id) is not None
    if is_admin:
        # The website hands an admin account its admin key. A wallet verified
        # for payments isn't a strong enough way in for that: a phished
        # signature (Bittensor extensions don't check the site) would be one.
        raise WalletAuthError(
            403, "forbidden", "Admin accounts sign in with email or Google, not a wallet.",
        )
    return {
        "user_id": user_id,
        "account_created": created,
        "chain": challenge.chain,
        "address": challenge.address,
        "username": user.username,
        "display_name": user.display_name or short_address(challenge.address),
        "email": (user.email or "").strip() or None,
        # Made on the website, or a wallet account whose email the website
        # confirmed: only then may the email tie it to a website user.
        "email_confirmed": user.signup_method != "wallet" or user.email_verified_at is not None,
    }

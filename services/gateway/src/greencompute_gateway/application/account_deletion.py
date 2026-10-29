"""Deleting an account at its owner's request (UK GDPR right to erasure).

The website asks first (preview: the balance that will be lost and anything
still running), then deletes. Deletion is refused while the account has a
rental or model deployment that is pending, running or saved, so nothing is
left running, or holding the person's files, on a node with no owner.

What is erased, in one transaction:
  - the profile: email, display name, bio, website and metadata; the username
    becomes "deleted-<id>";
  - every API key (the website's own included), so nothing can sign in as
    the account again;
  - verified wallets, saved secrets (e.g. HF_TOKEN), workload shares, website
    admin access, provider-server onboarding records and in-flight inference
    holds;
  - on the account's workloads: environment variables, start commands, SSH
    public keys, secret-named settings, readme, display name, logo, tags,
    alias and model address (the workload is made private); on its
    deployments: the platform-generated SSH private key.

What is kept, attached to the anonymised user id, because the accounts need
it: the ledger (every top-up and charge), crypto invoices and deposits, Stripe
checkout records, usage and invocation records, and the deployment and
workload rows themselves (state, GPU, rate, times) that explain the charges.

Remaining credit is forfeited: the balance goes to zero with an
"account_closed" ledger entry recording the amount. A negative balance is left
as it is, so a debt stays on the books. Open crypto invoices are rejected, so a
payment sent afterwards shows up as an unmatched deposit to refund rather than
crediting an account nobody can use.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    AccountEventORM,
    WebhookORM,
    AdminAccountORM,
    APIKeyORM,
    CryptoInvoiceORM,
    DeploymentORM,
    InferenceBalanceHoldORM,
    LedgerEntryORM,
    ProviderServerORM,
    UserORM,
    UserSecretORM,
    VerifiedWalletORM,
    WorkloadORM,
    WorkloadShareORM,
)
from sqlalchemy import delete, func, or_, select

# Pending, running or saved: the owner has to stop (or delete) these first.
BLOCKING_STATES = ("pending", "scheduled", "pulling", "starting", "ready", "draining", "suspended")
FORFEIT_KIND = "account_closed"
CONFIRM_WORD = "DELETE"

# Workload settings that can carry secrets or the person's own data.
_PERSONAL_META_KEYS = ("env", "command", "ssh_public_keys")
_SECRET_MARKERS = ("token", "secret", "password", "apikey", "privatekey")


class AccountHasActiveDeployments(Exception):
    def __init__(self, deployments: list[dict]) -> None:
        super().__init__(f"{len(deployments)} deployment(s) still active")
        self.deployments = deployments


def _is_deleted(user: UserORM) -> bool:
    return bool((user.profile_metadata or {}).get("deleted_at"))


def _blockers(session, user_id: str) -> list[dict]:
    rows = session.execute(
        select(DeploymentORM, WorkloadORM)
        .outerjoin(WorkloadORM, WorkloadORM.workload_id == DeploymentORM.workload_id)
        .where(DeploymentORM.owner_user_id == user_id, DeploymentORM.state.in_(BLOCKING_STATES))
        .order_by(DeploymentORM.created_at)
    ).all()
    return [
        {
            "deployment_id": dep.deployment_id,
            "workload_id": dep.workload_id,
            "name": (wl.display_name or wl.name) if wl else None,
            "kind": wl.kind if wl else None,
            "state": dep.state,
        }
        for dep, wl in rows
    ]


def deletion_preview(session_factory, user_id: str) -> dict | None:
    """What deleting the account would do right now; None if there is no
    such account (or it is already deleted)."""
    with session_scope(session_factory) as session:
        user = session.get(UserORM, user_id)
        if user is None or _is_deleted(user):
            return None
        blocking = _blockers(session, user_id)
        balance = int(user.balance_credits or 0)

        def count(column) -> int:
            return session.scalar(select(func.count()).where(column == user_id)) or 0

        return {
            "user_id": user_id,
            "balance_cents": balance,
            "balance_usd": round(balance / 100, 2),
            # What is lost: positive credit only (a debt is not written off).
            "credit_lost_usd": round(max(0, balance) / 100, 2),
            "active_deployments": blocking,
            "can_delete": not blocking,
            "api_keys": count(APIKeyORM.user_id),
            "verified_wallets": count(VerifiedWalletORM.user_id),
        }


def _scrub_metadata(meta: dict | None) -> dict:
    out = {}
    for key, value in (meta or {}).items():
        flat = str(key).lower().replace("_", "")
        if key in _PERSONAL_META_KEYS or any(m in flat for m in _SECRET_MARKERS):
            continue
        out[key] = value
    return out


def delete_account(session_factory, user_id: str, *, now: datetime | None = None) -> dict:
    """Erase the account's personal data and access, keeping its billing
    records. Raises KeyError for an unknown account and
    AccountHasActiveDeployments while anything is still active. Deleting an
    already-deleted account is a no-op, so the website can safely retry."""
    now = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        user = session.get(UserORM, user_id, with_for_update=True)
        if user is None:
            raise KeyError(f"user not found: {user_id}")
        if _is_deleted(user):
            return {"deleted": True, "already_deleted": True, "user_id": user_id}
        blocking = _blockers(session, user_id)
        if blocking:
            raise AccountHasActiveDeployments(blocking)

        forfeited = max(0, int(user.balance_credits or 0))
        if forfeited:
            user.balance_credits = 0
            session.add(LedgerEntryORM(
                entry_id=str(uuid4()),
                user_id=user_id,
                amount_cents=-forfeited,
                balance_after=0,
                kind=FORFEIT_KIND,
                description="Account deleted by its owner; remaining credit forfeited",
                created_at=now,
            ))

        def remove(model, *where) -> int:
            return session.execute(delete(model).where(*where)).rowcount or 0

        summary = {
            "api_keys_revoked": remove(APIKeyORM, APIKeyORM.user_id == user_id),
            "wallets_removed": remove(VerifiedWalletORM, VerifiedWalletORM.user_id == user_id),
            "secrets_deleted": remove(UserSecretORM, UserSecretORM.user_id == user_id),
            "shares_removed": remove(
                WorkloadShareORM,
                or_(
                    WorkloadShareORM.owner_user_id == user_id,
                    WorkloadShareORM.shared_with_user_id == user_id,
                ),
            ),
            "provider_servers_removed": remove(
                ProviderServerORM, ProviderServerORM.owner_user_id == user_id
            ),
        }
        summary["webhooks_removed"] = remove(WebhookORM, WebhookORM.user_id == user_id)
        remove(AccountEventORM, AccountEventORM.user_id == user_id)
        remove(AdminAccountORM, AdminAccountORM.user_id == user_id)
        remove(InferenceBalanceHoldORM, InferenceBalanceHoldORM.user_id == user_id)

        open_invoices = session.scalars(
            select(CryptoInvoiceORM).where(
                CryptoInvoiceORM.user_id == user_id, CryptoInvoiceORM.status == "pending"
            )
        ).all()
        for invoice in open_invoices:
            invoice.status = "rejected"

        workloads = session.scalars(
            select(WorkloadORM).where(WorkloadORM.owner_user_id == user_id)
        ).all()
        for wl in workloads:
            wl.metadata_json = _scrub_metadata(wl.metadata_json)
            wl.display_name = None
            wl.readme = None
            wl.logo_uri = None
            wl.tags = []
            wl.workload_alias = None
            wl.ingress_host = None
            wl.public = False

        ssh_keys = 0
        for dep in session.scalars(
            select(DeploymentORM).where(
                DeploymentORM.owner_user_id == user_id, DeploymentORM.ssh_private_key.is_not(None)
            )
        ).all():
            dep.ssh_private_key = None
            ssh_keys += 1

        user.username = f"deleted-{user_id.replace('-', '')[:12]}"
        user.email = None
        user.display_name = None
        user.bio = None
        user.website = None
        user.profile_metadata = {"deleted_at": now.isoformat()}

        return {
            "deleted": True,
            "already_deleted": False,
            "user_id": user_id,
            "credit_forfeited_cents": forfeited,
            "credit_forfeited_usd": round(forfeited / 100, 2),
            **summary,
            "invoices_cancelled": len(open_invoices),
            "workloads_scrubbed": len(workloads),
            "ssh_keys_removed": ssh_keys,
        }

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from greencompute_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greencompute_persistence.db import needs_bootstrap
from greencompute_persistence.orm import (
    CryptoInvoiceORM,
    InferenceDemandStatsORM,
    LedgerEntryORM,
    MinerPayoutAccrualORM,
    StripeSessionORM,
    UserORM,
)
from greencompute_protocol import CryptoInvoice, LedgerEntry, StripeSession


class InsufficientBalanceError(Exception):
    pass


class BillingRepository:
    def __init__(self, database_url: str | None = None, bootstrap: bool | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    # --- Balance operations (atomic with SELECT FOR UPDATE) ---

    def get_balance(self, user_id: str) -> int:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user_id)
            return row.balance_credits if row else 0

    def credit_user(
        self,
        user_id: str,
        amount_cents: int,
        kind: str,
        reference_id: str | None = None,
        description: str = "",
    ) -> LedgerEntry:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user_id, with_for_update=True)
            if row is None:
                raise KeyError(f"user {user_id} not found")
            row.balance_credits += amount_cents
            # Populate created_at explicitly — the ORM's default=utcnow fires
            # at flush time, but we return the pydantic LedgerEntry before the
            # session commits, so reading it before then would be None.
            entry = LedgerEntryORM(
                entry_id=str(uuid4()),
                user_id=user_id,
                amount_cents=amount_cents,
                balance_after=row.balance_credits,
                kind=kind,
                reference_id=reference_id,
                description=description,
                created_at=datetime.now(UTC),
            )
            session.add(entry)
            return self._to_ledger_entry(entry)

    def debit_user(
        self,
        user_id: str,
        amount_cents: int,
        kind: str,
        reference_id: str | None = None,
        description: str = "",
    ) -> LedgerEntry:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user_id, with_for_update=True)
            if row is None:
                raise KeyError(f"user {user_id} not found")
            if row.balance_credits < amount_cents:
                raise InsufficientBalanceError(
                    f"balance {row.balance_credits} < requested {amount_cents}"
                )
            row.balance_credits -= amount_cents
            entry = LedgerEntryORM(
                entry_id=str(uuid4()),
                user_id=user_id,
                amount_cents=-amount_cents,
                balance_after=row.balance_credits,
                kind=kind,
                reference_id=reference_id,
                description=description,
                created_at=datetime.now(UTC),
            )
            session.add(entry)
            return self._to_ledger_entry(entry)

    def list_ledger(self, user_id: str, limit: int = 50, offset: int = 0) -> list[LedgerEntry]:
        with session_scope(self.session_factory) as session:
            stmt = (
                select(LedgerEntryORM)
                .where(LedgerEntryORM.user_id == user_id)
                .order_by(LedgerEntryORM.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = session.scalars(stmt).all()
            return [self._to_ledger_entry(r) for r in rows]

    # --- Miner payout accrual (Phase 2E) ---

    def accrue_miner_payout(
        self,
        *,
        hotkey: str,
        deployment_id: str,
        workload_id: str,
        request_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cents_earned: int,
    ) -> None:
        """Capture one row per billable inference request. Idempotent via
        request_id unique constraint — retries from the gateway won't
        double-credit the miner."""
        with session_scope(self.session_factory) as session:
            session.add(MinerPayoutAccrualORM(
                accrual_id=str(uuid4()),
                hotkey=hotkey,
                deployment_id=deployment_id,
                workload_id=workload_id,
                request_id=request_id,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cents_earned=cents_earned,
                created_at=datetime.now(UTC),
            ))

    # --- Miner revenue aggregation (Phase 2J) ---

    def aggregate_miner_revenue(self, window_hours: int = 168) -> list[dict]:
        """Return [{hotkey, requests, cents_earned, ...}] aggregated over the
        last `window_hours`. Used by the admin /flux dashboard."""
        from datetime import timedelta

        from sqlalchemy import func

        cutoff = datetime.now(UTC) - timedelta(hours=max(1, min(window_hours, 24 * 60)))
        with session_scope(self.session_factory) as session:
            rows = session.execute(
                select(
                    MinerPayoutAccrualORM.hotkey,
                    func.count(MinerPayoutAccrualORM.accrual_id).label("requests"),
                    func.coalesce(func.sum(MinerPayoutAccrualORM.cents_earned), 0).label("cents_earned"),
                    func.coalesce(func.sum(MinerPayoutAccrualORM.prompt_tokens), 0).label("prompt_tokens"),
                    func.coalesce(func.sum(MinerPayoutAccrualORM.completion_tokens), 0).label("completion_tokens"),
                )
                .where(MinerPayoutAccrualORM.created_at >= cutoff)
                .group_by(MinerPayoutAccrualORM.hotkey)
                .order_by(func.sum(MinerPayoutAccrualORM.cents_earned).desc())
            ).all()
            return [
                {
                    "hotkey": r.hotkey,
                    "requests": int(r.requests),
                    "cents_earned": int(r.cents_earned),
                    "prompt_tokens": int(r.prompt_tokens),
                    "completion_tokens": int(r.completion_tokens),
                }
                for r in rows
            ]

    # --- Inference demand stats (Phase 2I) ---

    def record_demand_tick(
        self,
        *,
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        now: datetime | None = None,
    ) -> None:
        """Bump the current-minute bucket for `model_id`. Upsert — race-safe
        via session_scope's transaction. Granularity is per-minute, which
        caps table growth at ~1440 rows/model/day."""
        ts = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
        lat = int(max(0.0, latency_ms))
        with session_scope(self.session_factory) as session:
            row = session.get(InferenceDemandStatsORM, (model_id, ts))
            if row is None:
                session.add(InferenceDemandStatsORM(
                    model_id=model_id,
                    window_start=ts,
                    invocations=1,
                    prompt_tokens_sum=prompt_tokens,
                    completion_tokens_sum=completion_tokens,
                    latency_ms_sum=lat,
                    updated_at=ts,
                ))
            else:
                row.invocations += 1
                row.prompt_tokens_sum += prompt_tokens
                row.completion_tokens_sum += completion_tokens
                row.latency_ms_sum += lat
                row.updated_at = datetime.now(UTC)
                session.add(row)

    # --- Crypto invoices ---

    def create_crypto_invoice(self, invoice: CryptoInvoice) -> CryptoInvoice:
        with session_scope(self.session_factory) as session:
            row = CryptoInvoiceORM(
                invoice_id=invoice.invoice_id,
                user_id=invoice.user_id,
                currency=invoice.currency,
                amount_crypto=invoice.amount_crypto,
                amount_usd=invoice.amount_usd,
                bonus_pct=invoice.bonus_pct,
                total_credits=invoice.total_credits,
                deposit_address=invoice.deposit_address,
                status=invoice.status,
                expires_at=invoice.expires_at,
                created_at=invoice.created_at,
            )
            session.add(row)
        return invoice

    def get_crypto_invoice(self, invoice_id: str) -> CryptoInvoice | None:
        with session_scope(self.session_factory) as session:
            row = session.get(CryptoInvoiceORM, invoice_id)
            return self._to_crypto_invoice(row) if row else None

    def confirm_crypto_invoice(self, invoice_id: str, tx_hash: str) -> CryptoInvoice | None:
        from datetime import UTC, datetime

        with session_scope(self.session_factory) as session:
            row = session.get(CryptoInvoiceORM, invoice_id)
            if row is None:
                return None
            row.status = "confirmed"
            row.tx_hash = tx_hash
            row.confirmed_at = datetime.now(UTC)
            session.add(row)
            return self._to_crypto_invoice(row)

    def confirm_and_credit_invoice(
        self,
        invoice_id: str,
        tx_hash: str,
        description: str,
        deposit_ref: str | None = None,
    ) -> dict | None:
        """Atomically confirm an invoice and credit the owning user.

        Runs inside a single session so partial failures (e.g. a ledger
        insert blowing up after we've flipped the invoice to `confirmed`)
        roll back everything.

        Two layers of idempotency:
          1. Per-INVOICE: an existing topup ledger row for this invoice_id
             makes a retry a no-op (covers re-runs of the same invoice).
          2. Per-ON-CHAIN-TRANSFER: `deposit_ref` (currency:tx_hash:log_index
             for EVM, chain:block:event_index for TAO, or manual:... for
             admin confirms) is the GLOBAL idempotency key. If any topup
             ledger row already carries this deposit_ref, the transfer has
             already credited *some* invoice and we refuse to credit again —
             this is what stops "create N same-amount invoices, send ONE tx,
             get credited N times". The ledger column is UNIQUE, so even a
             concurrent racing replica that slips past the SELECT loses at
             COMMIT (IntegrityError → treated as already-confirmed).

        Returns:
          - None if the invoice doesn't exist
          - {"already_confirmed": True, ...} if invoice or transfer already processed
          - {"credited": True, ...} on a fresh confirm+credit
        """
        with session_scope(self.session_factory) as session:
            inv = session.get(CryptoInvoiceORM, invoice_id, with_for_update=True)
            if inv is None:
                return None

            # Layer 1 — per-invoice idempotency (retry of same invoice).
            existing = session.scalar(
                select(LedgerEntryORM).where(
                    LedgerEntryORM.reference_id == invoice_id,
                    LedgerEntryORM.kind == "topup",
                )
            )
            if existing is not None:
                if inv.status != "confirmed":
                    inv.status = "confirmed"
                    if tx_hash and not inv.tx_hash:
                        inv.tx_hash = tx_hash
                    if inv.confirmed_at is None:
                        inv.confirmed_at = datetime.now(UTC)
                return {"already_confirmed": True, "invoice_id": invoice_id}

            # Layer 2 — per-transfer idempotency. If this on-chain transfer
            # has already credited ANY invoice, do not credit a second one.
            if deposit_ref:
                already = session.scalar(
                    select(LedgerEntryORM).where(
                        LedgerEntryORM.kind == "topup",
                        LedgerEntryORM.deposit_ref == deposit_ref,
                    )
                )
                if already is not None:
                    return {
                        "already_confirmed": True,
                        "invoice_id": invoice_id,
                        "duplicate_deposit": True,
                        "credited_invoice_id": already.reference_id,
                    }

            user = session.get(UserORM, inv.user_id, with_for_update=True)
            if user is None:
                raise KeyError(f"user {inv.user_id} not found for invoice {invoice_id}")

            # 1. flip invoice
            inv.status = "confirmed"
            inv.tx_hash = tx_hash
            inv.confirmed_at = datetime.now(UTC)
            # 2. credit user
            user.balance_credits += inv.total_credits
            # 3. ledger entry (explicit created_at — see credit_user comment)
            session.add(
                LedgerEntryORM(
                    entry_id=str(uuid4()),
                    user_id=inv.user_id,
                    amount_cents=inv.total_credits,
                    balance_after=user.balance_credits,
                    kind="topup",
                    reference_id=invoice_id,
                    deposit_ref=deposit_ref or None,
                    description=description,
                    created_at=datetime.now(UTC),
                )
            )
            # Force the INSERT now so a UNIQUE(deposit_ref) violation from a
            # concurrent replica surfaces here, inside our try, rather than
            # at commit. The loser treats it as already-confirmed.
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                return {
                    "already_confirmed": True,
                    "invoice_id": invoice_id,
                    "duplicate_deposit": True,
                }
            return {
                "credited": True,
                "invoice_id": invoice_id,
                "total_credits": inv.total_credits,
            }

    def expire_stale_pending_invoices(self, grace_seconds: int) -> int:
        """Bulk-mark pending invoices whose pay-by deadline passed more than
        `grace_seconds` ago as `expired`.

        The grace window beyond `expires_at` is deliberate: it covers
        on-chain confirmation lag + watcher catch-up so we NEVER expire an
        invoice the watcher is about to credit. The caller runs this AFTER
        the per-tick credit scan, and the WHERE clause only touches rows
        still `pending`, so an invoice credited earlier in the same tick is
        already `confirmed` and won't be expired. Returns the count expired.
        """
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(seconds=max(0, grace_seconds))
        with session_scope(self.session_factory) as session:
            result = session.execute(
                update(CryptoInvoiceORM)
                .where(
                    CryptoInvoiceORM.status == "pending",
                    CryptoInvoiceORM.expires_at < cutoff,
                )
                .values(status="expired")
            )
            return result.rowcount or 0

    def reject_crypto_invoice(self, invoice_id: str) -> CryptoInvoice | None:
        """Admin action: mark invoice as rejected and never credit it.

        Idempotent — rejecting a rejected invoice is a no-op. Rejecting a
        confirmed invoice returns None (can't undo a credited invoice here;
        use admin debit if you need to claw back funds).
        """
        with session_scope(self.session_factory) as session:
            row = session.get(CryptoInvoiceORM, invoice_id)
            if row is None:
                return None
            if row.status == "confirmed":
                return None
            row.status = "rejected"
            session.add(row)
            return self._to_crypto_invoice(row)

    def report_invoice_tx_hash(
        self,
        invoice_id: str,
        user_id: str,
        tx_hash: str,
    ) -> CryptoInvoice | None:
        """User-side: attach the tx hash they sent. Does NOT credit the
        user — admin still has to verify and confirm. The hash is stored on
        the invoice so the admin UI can surface it as a starting point.
        Ownership is enforced: only the invoice's own user can report a tx.
        """
        with session_scope(self.session_factory) as session:
            row = session.get(CryptoInvoiceORM, invoice_id)
            if row is None:
                return None
            if row.user_id != user_id:
                return None  # caller doesn't own this invoice
            if row.status == "confirmed":
                return self._to_crypto_invoice(row)  # already settled, no-op
            row.tx_hash = tx_hash
            session.add(row)
            return self._to_crypto_invoice(row)

    def list_crypto_invoices(self, user_id: str) -> list[CryptoInvoice]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(CryptoInvoiceORM)
                .where(CryptoInvoiceORM.user_id == user_id)
                .order_by(CryptoInvoiceORM.created_at.desc())
            ).all()
            return [self._to_crypto_invoice(r) for r in rows]

    def list_all_crypto_invoices_for_admin(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Admin view — list every invoice joined with the user row so the
        admin knows *who* this deposit is for without a second round-trip.
        Returns plain dicts (not the CryptoInvoice model) because we want
        the user email/username alongside each row.
        """
        with session_scope(self.session_factory) as session:
            stmt = (
                select(CryptoInvoiceORM, UserORM)
                .join(UserORM, UserORM.user_id == CryptoInvoiceORM.user_id)
                .order_by(CryptoInvoiceORM.created_at.desc())
                .limit(limit)
            )
            if status:
                stmt = stmt.where(CryptoInvoiceORM.status == status)
            out: list[dict] = []
            for inv, user in session.execute(stmt).all():
                out.append({
                    "invoice_id": inv.invoice_id,
                    "user_id": inv.user_id,
                    "username": user.username,
                    "email": user.email,
                    "display_name": user.display_name,
                    "currency": inv.currency,
                    "amount_crypto": inv.amount_crypto,
                    "amount_usd": inv.amount_usd,
                    "bonus_pct": inv.bonus_pct,
                    "total_credits": inv.total_credits,
                    "deposit_address": inv.deposit_address,
                    "status": inv.status,
                    "tx_hash": inv.tx_hash,
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                    "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
                    "confirmed_at": inv.confirmed_at.isoformat() if inv.confirmed_at else None,
                })
            return out

    # --- Stripe sessions ---

    def create_stripe_session(self, ss: StripeSession) -> StripeSession:
        with session_scope(self.session_factory) as session:
            row = StripeSessionORM(
                session_id=ss.session_id,
                user_id=ss.user_id,
                stripe_session_id=ss.stripe_session_id,
                amount_usd=ss.amount_usd,
                amount_cents=ss.amount_cents,
                status=ss.status,
                created_at=ss.created_at,
            )
            session.add(row)
        return ss

    def get_stripe_session_by_stripe_id(self, stripe_session_id: str) -> StripeSession | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(StripeSessionORM).where(StripeSessionORM.stripe_session_id == stripe_session_id)
            )
            return self._to_stripe_session(row) if row else None

    def complete_stripe_session(self, stripe_session_id: str) -> StripeSession | None:
        from datetime import UTC, datetime

        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(StripeSessionORM).where(StripeSessionORM.stripe_session_id == stripe_session_id)
            )
            if row is None:
                return None
            row.status = "paid"
            row.completed_at = datetime.now(UTC)
            session.add(row)
            return self._to_stripe_session(row)

    # --- Mappers ---

    @staticmethod
    def _to_ledger_entry(row: LedgerEntryORM) -> LedgerEntry:
        return LedgerEntry(
            entry_id=row.entry_id,
            user_id=row.user_id,
            amount_cents=row.amount_cents,
            balance_after=row.balance_after,
            kind=row.kind,
            reference_id=row.reference_id,
            description=row.description or "",
            created_at=row.created_at,
        )

    @staticmethod
    def _to_crypto_invoice(row: CryptoInvoiceORM) -> CryptoInvoice:
        return CryptoInvoice(
            invoice_id=row.invoice_id,
            user_id=row.user_id,
            currency=row.currency,
            amount_crypto=row.amount_crypto,
            amount_usd=row.amount_usd,
            bonus_pct=row.bonus_pct,
            total_credits=row.total_credits,
            deposit_address=row.deposit_address,
            status=row.status,
            tx_hash=row.tx_hash,
            expires_at=row.expires_at,
            confirmed_at=row.confirmed_at,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_stripe_session(row: StripeSessionORM) -> StripeSession:
        return StripeSession(
            session_id=row.session_id,
            user_id=row.user_id,
            stripe_session_id=row.stripe_session_id,
            amount_usd=row.amount_usd,
            amount_cents=row.amount_cents,
            status=row.status,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

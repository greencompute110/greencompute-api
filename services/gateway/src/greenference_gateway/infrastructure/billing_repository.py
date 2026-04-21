from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.db import needs_bootstrap
from greenference_persistence.orm import (
    CryptoInvoiceORM,
    LedgerEntryORM,
    StripeSessionORM,
    UserORM,
)
from greenference_protocol import CryptoInvoice, LedgerEntry, StripeSession


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
            entry = LedgerEntryORM(
                entry_id=str(uuid4()),
                user_id=user_id,
                amount_cents=amount_cents,
                balance_after=row.balance_credits,
                kind=kind,
                reference_id=reference_id,
                description=description,
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

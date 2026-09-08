"""Orchestration for platform-custodied provider wallets.

Drives a wallet through provision → funding → registration → active → payout by
calling the deterministic planners in ``domain.managed_wallet`` and executing
whatever they authorise. **No policy lives here.** If you find yourself writing
an `if` about money in this file, it belongs in the domain module where it can
be unit-tested without a chain.

Three ticks, each safe to run on every worker iteration and safe to interrupt:
  * ``tick_funding``      — awaiting_funding → funded / funding_expired
  * ``tick_registration`` — funded → registering → registered → active
  * ``tick_payouts``      — active|suspended → transfer accrued alpha

The ordering guarantee that matters: a wallet is only whitelisted (and so only
starts earning) AFTER its neuron is confirmed on chain. Whitelisting earlier
would put a hotkey in the weight vector that `_commit_weights_to_chain` cannot
resolve to a uid, which it silently skips — the provider would appear onboarded
and earn nothing, with no error anywhere.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from greencompute_protocol import MinerWhitelistEntry

from greencompute_validator.domain.managed_wallet import (
    DEFAULT_NETUID,
    ManagedWallet,
    WalletState,
    check_funding,
    plan_payout,
    plan_registration,
    provision_wallet,
    quote_funding,
)

logger = logging.getLogger(__name__)


class ManagedWalletService:
    """Custody lifecycle for provider miner wallets.

    Every dependency is injected so the whole lifecycle can be exercised against
    fakes — the alternative is testing money-moving code against mainnet.
    """

    def __init__(
        self,
        repository,
        wallet_factory,
        secret_box,
        chain_ops,
        *,
        netuid: int = DEFAULT_NETUID,
    ) -> None:
        self.repository = repository
        self.factory = wallet_factory
        self.secrets = secret_box
        self.chain = chain_ops
        self.netuid = netuid

    # --- Provisioning --------------------------------------------------------

    def provision_for_application(
        self, application_id: str, payout_address: str
    ) -> ManagedWallet:
        """Create a custodied wallet for an approved applicant.

        Idempotent per application: a second call returns the existing wallet
        rather than minting a second keypair. Re-provisioning would orphan the
        first coldkey — and if the provider had already funded it, orphan their
        TAO with it.
        """
        existing = self.repository.get_managed_wallet_by_application(application_id)
        if existing is not None:
            logger.info("application %s already has wallet %s", application_id, existing.wallet_id)
            return existing
        quote = quote_funding(self.chain, netuid=self.netuid)
        wallet = provision_wallet(
            self.factory,
            self.secrets,
            wallet_id=f"mw-{uuid.uuid4().hex[:16]}",
            application_id=application_id,
            payout_address=payout_address,
            quote=quote,
            netuid=self.netuid,
        )
        self.repository.create_managed_wallet(wallet)
        # coldkey_ss58 is safe to log — it is a public address, and operators
        # need it to answer "did my transfer arrive?". Mnemonics never are.
        logger.info(
            "provisioned managed wallet %s for application %s (coldkey=%s, needs %.4f TAO)",
            wallet.wallet_id, application_id, wallet.coldkey_ss58, wallet.required_funding_tao,
        )
        return wallet

    # --- Funding -------------------------------------------------------------

    def tick_funding(self) -> int:
        """Check every awaiting-funding coldkey for the provider's deposit."""
        advanced = 0
        for wallet in self.repository.list_managed_wallets_in_state(WalletState.AWAITING_FUNDING):
            try:
                balance = self.chain.coldkey_balance_tao(wallet.coldkey_ss58)
            except Exception:
                # A transient RPC failure must not expire a provider's window.
                logger.exception("balance check failed for wallet %s", wallet.wallet_id)
                continue
            result = check_funding(wallet, balance)
            if result.state is WalletState.AWAITING_FUNDING:
                continue
            self.repository.advance_managed_wallet(
                wallet.wallet_id,
                result.state,
                funded_tao=result.balance_tao,
                failure_reason=None if result.is_funded else result.reason,
            )
            advanced += 1
            logger.info("wallet %s -> %s (%s)", wallet.wallet_id, result.state, result.reason)
        return advanced

    # --- Registration --------------------------------------------------------

    def tick_registration(self) -> int:
        """Register funded wallets, then whitelist the confirmed ones."""
        registered = 0
        for wallet in self.repository.list_managed_wallets_in_state(WalletState.FUNDED):
            if self._register_one(wallet):
                registered += 1
        # Separate pass: a wallet that registered but crashed before being
        # whitelisted would otherwise sit REGISTERED forever, holding a paid-for
        # neuron that earns nothing.
        for wallet in self.repository.list_managed_wallets_in_state(WalletState.REGISTERED):
            self._activate_one(wallet)
        return registered

    def _register_one(self, wallet: ManagedWallet) -> bool:
        try:
            plan = plan_registration(wallet, self.chain)
        except Exception:
            logger.exception("registration planning failed for %s", wallet.wallet_id)
            return False

        if not plan.should_register:
            if plan.additional_funding_tao > 0:
                # Burn outran the deposit. Ask for the difference instead of
                # failing the provider — their TAO is already on our coldkey.
                self.repository.advance_managed_wallet(
                    wallet.wallet_id,
                    WalletState.AWAITING_FUNDING,
                    required_funding_tao=round(
                        wallet.funded_tao + plan.additional_funding_tao, 6
                    ),
                    failure_reason=plan.reason,
                )
                logger.warning("wallet %s needs more funding: %s", wallet.wallet_id, plan.reason)
            elif "already registered" in plan.reason:
                # Idempotent recovery: the extrinsic landed but we lost the
                # response. Adopt the existing uid rather than burning again.
                uid = self.chain.is_registered(wallet.hotkey_ss58, wallet.netuid)
                self.repository.advance_managed_wallet(
                    wallet.wallet_id, WalletState.REGISTERING,
                )
                self.repository.advance_managed_wallet(
                    wallet.wallet_id, WalletState.REGISTERED, uid=uid,
                )
                logger.info("wallet %s adopted existing uid %s", wallet.wallet_id, uid)
            return False

        # Mark REGISTERING before submitting. If we die mid-extrinsic the row
        # says so, and the FUNDED sweep won't pick it up and burn a second fee.
        if self.repository.advance_managed_wallet(wallet.wallet_id, WalletState.REGISTERING) is None:
            return False
        try:
            outcome = self.chain.register(
                coldkey_mnemonic=self.secrets.decrypt(
                    wallet.coldkey_mnemonic_enc, context=f"{wallet.wallet_id}:coldkey"
                ),
                hotkey_mnemonic=self.secrets.decrypt(
                    wallet.hotkey_mnemonic_enc, context=f"{wallet.wallet_id}:hotkey"
                ),
                netuid=wallet.netuid,
            )
        except Exception as exc:
            logger.exception("registration extrinsic raised for %s", wallet.wallet_id)
            self.repository.advance_managed_wallet(
                wallet.wallet_id, WalletState.REGISTRATION_FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            return False

        # Trust the CHAIN, not the response. An extrinsic can report failure
        # after landing, and marking a live neuron as failed would burn a second
        # registration on retry.
        uid = self.chain.is_registered(wallet.hotkey_ss58, wallet.netuid)
        if uid is None:
            self.repository.advance_managed_wallet(
                wallet.wallet_id, WalletState.REGISTRATION_FAILED,
                failure_reason=outcome.message or "registration did not produce a uid",
            )
            logger.error("registration failed for %s: %s", wallet.wallet_id, outcome.message)
            return False
        self.repository.advance_managed_wallet(
            wallet.wallet_id, WalletState.REGISTERED, uid=uid,
        )
        logger.info("wallet %s registered with uid %s", wallet.wallet_id, uid)
        return True

    def _activate_one(self, wallet: ManagedWallet) -> None:
        """Whitelist a confirmed neuron so it starts receiving weight."""
        try:
            self.repository.add_whitelist_entry(MinerWhitelistEntry(
                hotkey=wallet.hotkey_ss58,
                label=f"managed:{wallet.application_id}",
                notes=f"platform-managed wallet {wallet.wallet_id}",
            ))
            self.repository.advance_managed_wallet(wallet.wallet_id, WalletState.ACTIVE)
            logger.info(
                "wallet %s active — hotkey %s whitelisted", wallet.wallet_id, wallet.hotkey_ss58
            )
        except Exception:
            # Stays REGISTERED and is retried next tick.
            logger.exception("failed to whitelist wallet %s", wallet.wallet_id)

    # --- Payout --------------------------------------------------------------

    def tick_payouts(self) -> int:
        """Forward accrued alpha to each provider's own address."""
        paid = 0
        for state in (WalletState.ACTIVE, WalletState.SUSPENDED):
            for wallet in self.repository.list_managed_wallets_in_state(state):
                if self._pay_one(wallet):
                    paid += 1
        return paid

    def _pay_one(self, wallet: ManagedWallet) -> bool:
        # An unresolved submission means we do not know whether alpha already
        # moved. Reading the balance again and transferring would pay twice.
        if self.repository.has_inflight_payout(wallet.wallet_id):
            logger.warning(
                "wallet %s has an unresolved payout — skipping until reconciled",
                wallet.wallet_id,
            )
            return False
        try:
            accrued = self.chain.staked_alpha(
                wallet.coldkey_ss58, wallet.hotkey_ss58, wallet.netuid
            )
        except Exception:
            logger.exception("alpha balance check failed for %s", wallet.wallet_id)
            return False

        plan = plan_payout(wallet, accrued)
        if not plan.should_pay:
            return False

        payout_id = f"pay-{uuid.uuid4().hex[:16]}"
        # Write BEFORE submitting, so a crash mid-flight leaves evidence rather
        # than silence — that row is what `has_inflight_payout` trips on.
        self.repository.record_managed_payout(
            payout_id=payout_id,
            wallet_id=wallet.wallet_id,
            destination=plan.destination,
            alpha_amount=plan.alpha_amount,
            netuid=wallet.netuid,
            status="submitting",
        )
        try:
            outcome = self.chain.transfer_alpha(
                coldkey_mnemonic=self.secrets.decrypt(
                    wallet.coldkey_mnemonic_enc, context=f"{wallet.wallet_id}:coldkey"
                ),
                hotkey_mnemonic=self.secrets.decrypt(
                    wallet.hotkey_mnemonic_enc, context=f"{wallet.wallet_id}:hotkey"
                ),
                destination_coldkey_ss58=plan.destination,
                hotkey_ss58=wallet.hotkey_ss58,
                netuid=wallet.netuid,
                alpha_amount=plan.alpha_amount,
            )
        except Exception as exc:
            # Deliberately left as "failed", not deleted: an operator must be
            # able to see that a transfer was attempted and check the chain.
            logger.exception("alpha transfer raised for %s", wallet.wallet_id)
            self.repository.record_managed_payout(
                payout_id=payout_id,
                wallet_id=wallet.wallet_id,
                destination=plan.destination,
                alpha_amount=plan.alpha_amount,
                netuid=wallet.netuid,
                status="failed",
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            return False

        status = "confirmed" if outcome.success else "failed"
        self.repository.record_managed_payout(
            payout_id=payout_id,
            wallet_id=wallet.wallet_id,
            destination=plan.destination,
            alpha_amount=plan.alpha_amount,
            netuid=wallet.netuid,
            status=status,
            extrinsic_hash=outcome.extrinsic_hash,
            failure_reason=None if outcome.success else (outcome.message or "transfer failed"),
        )
        if not outcome.success:
            logger.error("alpha transfer failed for %s: %s", wallet.wallet_id, outcome.message)
            return False
        self.repository.credit_managed_payout(wallet.wallet_id, plan.alpha_amount)
        logger.info(
            "paid %.6f alpha from wallet %s to %s (%s)",
            plan.alpha_amount, wallet.wallet_id, plan.destination, outcome.extrinsic_hash,
        )
        return True

    # --- Read model for routes ----------------------------------------------

    def wallet_status(self, application_id: str) -> dict | None:
        """Provider-facing status. Deliberately excludes every secret column."""
        wallet = self.repository.get_managed_wallet_by_application(application_id)
        if wallet is None:
            return None
        outstanding = max(0.0, round(wallet.required_funding_tao - wallet.funded_tao, 6))
        return {
            "wallet_id": wallet.wallet_id,
            "state": str(wallet.state),
            "netuid": wallet.netuid,
            # The address the provider sends TAO to.
            "funding_address": wallet.coldkey_ss58,
            "required_funding_tao": wallet.required_funding_tao,
            "funded_tao": wallet.funded_tao,
            "outstanding_tao": outstanding,
            "hotkey": wallet.hotkey_ss58,
            "uid": wallet.uid,
            "payout_address": wallet.payout_address,
            "total_paid_alpha": wallet.total_paid_alpha,
            "last_payout_at": wallet.last_payout_at,
            "failure_reason": wallet.failure_reason,
            "created_at": wallet.created_at,
        }

"""Lazy construction of the ``ManagedWalletService`` singleton.

Separate from ``managed_wallets`` so that module stays importable — and
therefore unit-testable — in the gateway test environment, which has neither
`bittensor` nor a chain connection. Everything heavy is imported inside
``get_managed_wallet_service``.

The service is built ONCE and cached. It is None whenever the feature is
disabled or its prerequisites are missing, and every caller must handle that:
routes return 503, the worker skips its tick. Failing this way rather than
raising at import keeps a misconfigured managed-wallet feature from taking down
the whole validator, which also serves weights and audit reports.
"""
from __future__ import annotations

import logging

from greencompute_validator.config import settings as validator_settings

logger = logging.getLogger(__name__)

_service = None
_attempted = False


def get_managed_wallet_service():
    """The singleton, or None if managed wallets are unavailable.

    Construction is attempted once. A failure is logged and cached as None so a
    broken config produces one error line rather than a stack trace on every
    worker tick and every request.
    """
    global _service, _attempted
    if _attempted:
        return _service
    _attempted = True

    if not validator_settings.managed_wallets_enabled:
        logger.info("managed wallets are disabled (GREENCOMPUTE_MANAGED_WALLETS_ENABLED)")
        return None

    try:
        from greencompute_validator.application.managed_wallets import ManagedWalletService
        from greencompute_validator.application.services import service
        from greencompute_validator.infrastructure.bittensor_wallet import (
            BittensorChainOps,
            BittensorWalletFactory,
        )
        from greencompute_validator.infrastructure.secretbox import AesGcmSecretBox

        chain = getattr(service, "_chain", None)
        if chain is None:
            raise RuntimeError(
                "managed wallets need a chain client — set GREENCOMPUTE_BITTENSOR_ENABLED"
            )
        subtensor = chain._get_subtensor()
        _service = ManagedWalletService(
            repository=service.repository,
            wallet_factory=BittensorWalletFactory(),
            # Raises if GREENCOMPUTE_WALLET_MASTER_KEY is unset — deliberately
            # fatal to the feature rather than falling back to plaintext.
            secret_box=AesGcmSecretBox(),
            chain_ops=BittensorChainOps(subtensor),
            netuid=validator_settings.bittensor_netuid,
        )
        logger.info(
            "managed wallets ENABLED on netuid %s", validator_settings.bittensor_netuid
        )
    except Exception:
        logger.exception("managed wallets could not be initialised — feature is OFF")
        _service = None
    return _service


def reset_managed_wallet_service() -> None:
    """Test hook — drop the cached singleton so settings can be re-read."""
    global _service, _attempted
    _service, _attempted = None, False

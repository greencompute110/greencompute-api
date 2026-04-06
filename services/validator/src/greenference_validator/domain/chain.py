"""Bittensor chain client — metagraph sync, hotkey validation, set_weights."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from greenference_protocol import ChainWeightCommit, MetagraphEntry
from substrateinterface import SubstrateInterface, Keypair

logger = logging.getLogger(__name__)


def _restore_logging() -> None:
    """Re-attach our handler after bittensor wipes the root logger."""
    for noisy in ("bittensor", "urllib3", "websocket", "substrateinterface"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = logging.getLogger("greenference_validator")
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(h)
        root.propagate = False


class BittensorChainClient:
    """Wraps substrate-interface calls to the Bittensor chain."""

    def __init__(
        self,
        network: str = "test",
        netuid: int = 16,
        wallet_path: str | None = None,
    ) -> None:
        self.network = network
        self.netuid = netuid
        self.wallet_path = wallet_path
        self._subtensor = None
        self._bt = None

    def _get_bt(self):
        """Lazy-import bittensor so its logging init runs after uvicorn sets up logging."""
        if self._bt is not None:
            return self._bt
        import bittensor as bt  # noqa: PLC0415
        bt.logging.off()
        _restore_logging()
        self._bt = bt
        return self._bt

    def _get_subtensor(self):
        if self._subtensor is not None:
            return self._subtensor
        bt = self._get_bt()
        endpoint = self._resolve_endpoint()
        logger.info("connecting to subtensor: %s", endpoint)
        self._subtensor = bt.Subtensor(network=endpoint)
        logger.info("connected to %s subtensor", endpoint)
        return self._subtensor

    def _resolve_endpoint(self) -> str:
        endpoints = {
            "test": "wss://test.finney.opentensor.ai:443/",
            "finney": "wss://entrypoint-finney.opentensor.ai:443/",
            "local": "ws://127.0.0.1:9944",
        }
        return endpoints.get(self.network, self.network)

    def sync_metagraph(self) -> list[MetagraphEntry]:
        """Read all neurons registered on our netuid."""
        subtensor = self._get_subtensor()
        try:
            result = subtensor.neurons(netuid=self.netuid)
        except Exception:
            logger.exception("failed to query metagraph for netuid=%d", self.netuid)
            return []

        entries: list[MetagraphEntry] = []
        for neuron in result:
            entries.append(MetagraphEntry(
                netuid=self.netuid,
                uid=neuron.uid,
                hotkey=str(neuron.hotkey),
                coldkey=str(neuron.coldkey),
                stake=neuron.stake,
                incentive=neuron.incentive,
                emission=neuron.emission,
                synced_at=datetime.now(UTC),
            ))

        logger.info("synced metagraph: %d neurons on netuid=%d", len(entries), self.netuid)
        return entries

    def is_registered(self, hotkey: str) -> bool:
        """Check if a hotkey is registered on our netuid."""
        subtensor = self._get_subtensor()
        try:
            result = subtensor.get_uid_for_hotkey_on_subnet(hotkey=hotkey, netuid=self.netuid)
            return result is not None and result.value is not None
        except Exception:
            logger.exception("failed to check registration for %s", hotkey)
            return False

    def set_weights(
        self,
        uids: list[int],
        weights: list[float],
        wallet_name: str = "default",
        hotkey_name: str = "default",
    ) -> ChainWeightCommit:
        """Push weight vector to chain via set_weights extrinsic."""
        total = sum(weights) or 1.0
        normalized = [int((w / total) * 65535) for w in weights]

        try:
            substrate = SubstrateInterface(
                url=self._resolve_endpoint(),
                ss58_format=42,
                type_registry_preset="substrate-node-template",
                auto_reconnect=True,
            )

            if self.wallet_path:
                keypair = Keypair.create_from_uri(self.wallet_path)
            else:
                keypair = Keypair.create_from_uri(f"//{wallet_name}//{hotkey_name}")

            call = substrate.compose_call(
                call_module="SubtensorModule",
                call_function="set_weights",
                call_params={
                    "netuid": self.netuid,
                    "dests": uids,
                    "weights": normalized,
                    "version_key": 0,
                },
            )
            extrinsic = substrate.create_signed_extrinsic(call=call, keypair=keypair)
            receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)

            tx_hash = receipt.extrinsic_hash if hasattr(receipt, "extrinsic_hash") else str(receipt)
            logger.info("set_weights tx submitted: %s (uids=%d)", tx_hash, len(uids))

            return ChainWeightCommit(
                netuid=self.netuid,
                tx_hash=tx_hash,
                uids=uids,
                weights=weights,
                committed_at=datetime.now(UTC),
            )

        except Exception:
            logger.exception("failed to set_weights on netuid=%d", self.netuid)
            raise

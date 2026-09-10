"""Bittensor chain client — metagraph sync, hotkey validation, set_weights."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from greencompute_protocol import ChainWeightCommit, MetagraphEntry
from substrateinterface import SubstrateInterface, Keypair

logger = logging.getLogger(__name__)


def _load_keypair_from_wallet_file(wallet_path: str) -> Keypair:
    """Load a Bittensor wallet hotkey/coldkey file and return a Keypair.

    `Keypair.create_from_uri(path)` is WRONG here — it treats the string as
    an sr25519 derivation URI like `//Alice`, producing a fresh test keypair
    rather than the real wallet's keypair. The chain then rejects extrinsics
    signed by that wrong hotkey with `Custom error: 1` (NotRegistered).

    Bittensor wallet files are JSON with `secretSeed` (or `privateKey`) —
    read that, hand to `create_from_seed()`. Path examples:
      ~/.bittensor/wallets/<coldkey>/hotkeys/<name>     (hotkey)
      ~/.bittensor/wallets/<coldkey>/coldkey            (coldkey, signs stake)
    """
    p = Path(wallet_path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"wallet file not found: {p}")
    with p.open() as f:
        data = json.load(f)
    seed = data.get("secretSeed") or data.get("privateKey") or data.get("seed") or data.get("private_key")
    if not seed:
        raise ValueError(f"no secretSeed/privateKey in {p}")
    if isinstance(seed, str) and seed.startswith("0x"):
        seed = seed[2:]
    return Keypair.create_from_seed(seed)


def _restore_logging() -> None:
    """Re-attach our handler after bittensor wipes the root logger."""
    for noisy in ("bittensor", "urllib3", "websocket", "substrateinterface"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = logging.getLogger("greencompute_validator")
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(h)
        root.propagate = False


def _silence_bittensor_logging(bt) -> None:
    """Quieten the SDK across major versions.

    bittensor < 11 exposed ``bt.logging.off()``. v11 REMOVED it and raises on
    attribute access ("bittensor.logging was removed in v11"). That exception
    propagated out of every validator worker-loop iteration, which silently
    stopped audit-report publishing for ~30 hours: the loop catches per-iteration
    errors, so /healthz stayed "ok" and only `worker_last_error` on /readyz
    showed anything wrong.

    bittensor is not version-pinned, so a rebuild can move this under us again --
    hence feature detection rather than a version check.
    """
    try:
        bt.logging.off()
    except Exception:  # noqa: BLE001 - any failure just means "no legacy API"
        # v11+: the SDK logs through the standard 'bittensor' logger namespace.
        logging.getLogger("bittensor").setLevel(logging.CRITICAL)
        logging.getLogger("bittensor").propagate = False


class BittensorChainClient:
    """Wraps substrate-interface calls to the Bittensor chain.

    GreenCompute runs on two netuids depending on environment:
      - testnet (`network="test"`, endpoint test.finney)   → netuid 16
      - mainnet (`network="finney"`, endpoint finney)       → netuid 110

    Defaults below target testnet since that's where current prod lives;
    flip both `network` and `netuid` via the validator config env vars
    (`GREENCOMPUTE_BITTENSOR_NETWORK`, `_NETUID`) to cut over to mainnet."""

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
        _silence_bittensor_logging(bt)
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

    def current_block_number(self) -> int | None:
        """Return the current best block number. None on error — callers should
        treat a None return as 'skip this tick, try again'."""
        subtensor = self._get_subtensor()
        try:
            # bittensor v11 dropped Subtensor.get_current_block() in favour of the
            # `.block` property. Feature-detect rather than version-gate: the SDK
            # is range-pinned, not exact, and this call failing returns None --
            # which the audit hook treats as "skip this tick", so a rename here
            # silently stops audit-report publishing with nothing in the logs.
            getter = getattr(subtensor, "get_current_block", None)
            block = getter() if callable(getter) else getattr(subtensor, "block", None)
            return int(block) if block is not None else None
        except Exception:
            logger.exception("failed to fetch current block number")
            return None

    def set_commitment(
        self,
        commitment_bytes: bytes,
        wallet_name: str = "default",
        hotkey_name: str = "default",
    ) -> str | None:
        """Anchor a hash (SHA256 of our audit report JSON) on-chain for this
        netuid via the `Commitments.set_commitment` pallet. Called once per
        epoch by `generate_audit_report`. Returns tx_hash on success, None on
        failure. Input must be ≤ 128 bytes (substrate pallet cap)."""
        if len(commitment_bytes) > 128:
            raise ValueError(f"commitment exceeds 128-byte cap: {len(commitment_bytes)}")
        try:
            substrate = SubstrateInterface(
                url=self._resolve_endpoint(),
                ss58_format=42,
                type_registry_preset="substrate-node-template",
                auto_reconnect=True,
            )
            if self.wallet_path:
                keypair = _load_keypair_from_wallet_file(self.wallet_path)
            else:
                # Fallback for local dev — Subkey-style derivation URI.
                # Real production deployments must set BITTENSOR_WALLET_PATH.
                keypair = Keypair.create_from_uri(f"//{wallet_name}//{hotkey_name}")

            # Commitments pallet expects CommitmentInfo { fields: BoundedVec<Data> }
            # where Data is a variant including Raw0..Raw64. We use Raw64 for
            # 32-byte SHA256 digest (fits in Raw64's 0..64 byte payload).
            hex_payload = "0x" + commitment_bytes.hex()
            # Use Raw64 variant — most compatible across bittensor chain forks.
            # Fallback to Raw32 if the chain's metadata only supports that tag.
            raw_tag = f"Raw{max(32, ((len(commitment_bytes) + 31) // 32) * 32)}"
            call = substrate.compose_call(
                call_module="Commitments",
                call_function="set_commitment",
                call_params={
                    "netuid": self.netuid,
                    "info": {"fields": [[{raw_tag: hex_payload}]]},
                },
            )
            extrinsic = substrate.create_signed_extrinsic(call=call, keypair=keypair)
            receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True)
            tx_hash = receipt.extrinsic_hash if hasattr(receipt, "extrinsic_hash") else str(receipt)
            logger.info("set_commitment tx submitted: %s (%d bytes)", tx_hash, len(commitment_bytes))
            return tx_hash
        except Exception:
            logger.exception("failed to submit set_commitment on netuid=%d", self.netuid)
            return None

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
                keypair = _load_keypair_from_wallet_file(self.wallet_path)
            else:
                # Fallback for local dev — Subkey-style derivation URI.
                # Real production deployments must set BITTENSOR_WALLET_PATH.
                keypair = Keypair.create_from_uri(f"//{wallet_name}//{hotkey_name}")
            print(f"[CHAIN COMMIT] signing set_weights with ss58={keypair.ss58_address}", flush=True)

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

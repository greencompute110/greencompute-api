from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import uuid4

from greencompute_persistence import SubjectBus, WorkflowEventRepository, create_subject_bus, get_metrics_store
from greencompute_persistence.runtime import load_runtime_settings
from greencompute_protocol import (
    AuditReport,
    ChainWeightCommit,
    FluxRebalanceEvent,
    FluxState,
    MetagraphEntry,
    NodeCapability,
    ProbeChallenge,
    ProbeResult,
    RentalWaitEstimate,
    ScoreCard,
    WeightSnapshot,
)
from greencompute_validator.config import settings as validator_settings
from greencompute_validator.domain.chain import BittensorChainClient
from greencompute_validator.domain.demand import (
    DemandCollector,
    InferenceDemandSignal,
    RentalDemandSignal,
)
from greencompute_validator.domain.flux import FluxOrchestrator
from greencompute_validator.domain.metagraph import MetagraphCache
from greencompute_validator.domain.multinode import (
    CLUSTER_ADDRESS_LABEL,
    KEEP,
    REBUILD,
    NodeCandidate,
    build_replica_rows,
    group_by_replica,
    head_address,
    plan_multi_node_placement,
    replica_action,
    replica_is_ready,
    teardown_order,
    validate_topology,
)
from greencompute_validator.domain.scoring import ScoreEngine
from greencompute_validator.domain.wait_estimator import WaitEstimator
from greencompute_validator.infrastructure.repository import ValidatorRepository

logger = logging.getLogger(__name__)


def _flux_excluded_gpu_models() -> set[str]:
    """GPU models that Flux must NOT auto-assign catalog inference to.

    Defaults to the A4000: it has 16 GB VRAM and cannot run the 24 GB-class
    catalog models (vLLM OOMs at load), and the internal A4000 box is reserved
    for manual model-deploy testing — Flux placing replicas there caused an
    infinite fail/cooldown/respawn loop. Override with
    GREENCOMPUTE_FLUX_EXCLUDE_GPU_MODELS (comma-separated, e.g. "a4000,a2000")."""
    raw = os.getenv("GREENCOMPUTE_FLUX_EXCLUDE_GPU_MODELS", "a4000")
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


class UnknownCapabilityError(KeyError):
    pass


class UnknownProbeChallengeError(KeyError):
    pass


class InvalidProbeResultError(ValueError):
    pass


class ValidatorService:
    def __init__(
        self,
        repository: ValidatorRepository | None = None,
        workflow_repository: WorkflowEventRepository | None = None,
        bus: SubjectBus | None = None,
    ) -> None:
        self.repository = repository or ValidatorRepository()
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.repository.engine,
            session_factory=self.repository.session_factory,
        )
        runtime_settings = load_runtime_settings("greencompute-validator")
        self.bus = bus or create_subject_bus(
            engine=self.workflow_repository.engine,
            session_factory=self.workflow_repository.session_factory,
            workflow_repository=self.workflow_repository,
            nats_url=runtime_settings.nats_url,
            transport=runtime_settings.bus_transport,
        )
        self.scoring = ScoreEngine()
        self.metrics = get_metrics_store("greencompute-validator")
        self.flux = FluxOrchestrator(
            inference_floor_pct=validator_settings.flux_inference_floor_pct,
            rental_floor_pct=validator_settings.flux_rental_floor_pct,
        )
        self.demand = DemandCollector()
        self.wait_estimator = WaitEstimator()
        # Keyed by (hotkey, node_id) — supports the multi-node-per-hotkey
        # deployment pattern (a single miner identity controlling several
        # physical boxes). For per-hotkey lookups, use _aggregate_flux_state.
        self._flux_states: dict[tuple[str, str], FluxState] = {}
        # Reentrant lock guarding ALL reads/iterations/mutations of the Flux
        # in-memory maps (_flux_states, _replica_targets, _replica_cooldown_until,
        # _demand_last_hot_at, _blended_rpm). These are touched from two concurrent
        # contexts in the same process: the asyncio worker-loop (rebalance_all_miners)
        # and the sync HTTP rebalance route (runs in FastAPI's threadpool). RLock
        # (reentrant) is required because the public entry points nest — e.g.
        # rebalance_all_miners → rebalance_miner, register_capability → init_flux_state.
        self._flux_lock = threading.RLock()
        # Phase 2I hysteresis — last time a model's blended rpm was seen
        # above its scale-up threshold. Used to defer scale-down.
        self._demand_last_hot_at: dict[str, "datetime"] = {}
        # Cache of the latest computed replica targets so per-miner rebalance
        # can pass them to the orchestrator without recomputing.
        self._replica_targets: dict[str, int] = {}
        # Cache of the latest blended rpm per catalog model (captured during
        # compute_replica_targets) so the per-hotkey inference demand signal
        # can be derived without re-querying read_demand_windows.
        self._blended_rpm: dict[str, float] = {}
        # Failure cooldown: (hotkey, model_id) → datetime when deployment
        # last failed. Blocks respawning on the same miner for N minutes so
        # a broken miner (bad driver, OOM, missing weights, etc) doesn't burn
        # an infinite respawn loop.
        self._replica_cooldown_until: dict[tuple[str, str], "datetime"] = {}

        # Bittensor chain (lazy — only connects when enabled)
        self.metagraph = MetagraphCache()
        self._chain: BittensorChainClient | None = None
        if validator_settings.bittensor_enabled:
            self._chain = BittensorChainClient(
                network=validator_settings.bittensor_network,
                netuid=validator_settings.bittensor_netuid,
                wallet_path=validator_settings.bittensor_wallet_path,
            )

    def register_capability(self, capability: NodeCapability) -> NodeCapability:
        if validator_settings.bittensor_enabled and not self.metagraph.is_registered(capability.hotkey):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail=f"hotkey {capability.hotkey} not registered on chain")
        saved = self.repository.upsert_capability(capability)
        # Bootstrap Flux state so the first rebalance tick already considers
        # this miner. Re-seeds total_gpus if the miner changed capacity.
        self.init_flux_state(
            capability.hotkey,
            capability.node_id,
            capability.gpu_count,
            available_gpus=getattr(capability, "available_gpus", None),
        )
        return saved

    def create_probe(self, hotkey: str, node_id: str, kind: str = "latency") -> ProbeChallenge:
        capability = self.repository.get_capability(hotkey)
        if capability is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={hotkey}")
        if capability.node_id != node_id:
            raise InvalidProbeResultError(f"node mismatch for hotkey={hotkey}: expected={capability.node_id}")
        challenge = ProbeChallenge(hotkey=hotkey, node_id=node_id, kind=kind)
        return self.repository.save_challenge(challenge)

    @staticmethod
    def _score_window_start() -> datetime:
        """Trailing cutoff for probe-based scoring. Probes older than this are
        excluded so a recovered miner's score reflects recent behavior, not its
        entire lifetime (see score_probe_lookback_days)."""
        return datetime.now(UTC) - timedelta(
            days=validator_settings.score_probe_lookback_days
        )

    def submit_probe_result(self, result: ProbeResult) -> ScoreCard:
        challenge = self.repository.get_challenge(result.challenge_id)
        if challenge is None:
            raise UnknownProbeChallengeError(f"challenge not found: {result.challenge_id}")
        if challenge.hotkey != result.hotkey or challenge.node_id != result.node_id:
            raise InvalidProbeResultError(f"challenge mismatch for hotkey={result.hotkey} node={result.node_id}")
        if self.repository.get_result(result.challenge_id, result.hotkey) is not None:
            raise InvalidProbeResultError(f"duplicate result for challenge={result.challenge_id} hotkey={result.hotkey}")

        capability = self.repository.get_capability(result.hotkey)
        if capability is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={result.hotkey}")

        self.repository.add_result(result)
        flux = self._aggregate_flux_state(result.hotkey)
        scorecard = self.scoring.compute_scorecard(
            capability,
            self.repository.list_results(result.hotkey, since=self._score_window_start()),
            flux,
        )
        saved = self.repository.save_scorecard(scorecard)
        self.bus.publish(
            "probe.result.recorded",
            {
                "challenge_id": result.challenge_id,
                "hotkey": result.hotkey,
                "node_id": result.node_id,
                "final_score": saved.final_score,
            },
        )
        self.metrics.increment("probe.result.recorded")
        return saved

    def publish_weight_snapshot(
        self,
        netuid: int | None = None,
        epoch_id: str | None = None,
    ) -> WeightSnapshot:
        """Compute scorecards for every capable+whitelisted miner, persist them,
        publish a WeightSnapshot, optionally push to chain, and — if epoch_id is
        provided — append the scorecard vector to scorecard_history so audits
        can replay exactly what drove this epoch's weights.

        `netuid` defaults to GREENCOMPUTE_BITTENSOR_NETUID (16 on testnet,
        110 on mainnet) — pass explicitly only if you're cross-publishing."""
        if netuid is None:
            netuid = validator_settings.bittensor_netuid
        scorecards: dict[str, ScoreCard] = {}
        # Multi-node aware: each hotkey may control multiple physical nodes.
        # We aggregate gpu_count and vram across nodes to size the scorecard
        # correctly (capacity_weight = sum(gpu_count * vram_gb_per_gpu)).
        for hotkey, nodes in sorted(self.repository.list_node_capabilities().items()):
            if not nodes:
                continue
            if validator_settings.whitelist_enabled and not self.repository.is_whitelisted(hotkey):
                logger.info("skipping non-whitelisted miner %s", hotkey)
                continue
            results = self.repository.list_results(hotkey, since=self._score_window_start())
            if not results:
                continue
            # Synthesize a capability whose gpu_count * vram_gb_per_gpu
            # equals the sum of the hotkey's per-node products (the score
            # engine multiplies these to get capacity_weight). We keep
            # gpu_count = primary node's count and stretch vram to encode
            # the full fleet's total VRAM-GPU product (works for any
            # gpu_count within the NodeCapability bound).
            total_capacity_units = sum(
                n.gpu_count * n.vram_gb_per_gpu for n in nodes
            )
            primary = nodes[0]
            synthetic_vram = max(
                total_capacity_units // max(primary.gpu_count, 1), 1
            )
            avail_gpus = sum(n.available_gpus for n in nodes)
            agg_capability = primary.model_copy(
                update={
                    "vram_gb_per_gpu": synthetic_vram,
                    "available_gpus": min(avail_gpus, primary.gpu_count),
                }
            )
            flux = self._aggregate_flux_state(hotkey)
            scorecard = self.scoring.compute_scorecard(agg_capability, results, flux)
            scorecards[hotkey] = self.repository.save_scorecard(scorecard)
        weights = {
            hotkey: scorecard.final_score
            for hotkey, scorecard in sorted(scorecards.items())
        }
        snapshot = WeightSnapshot(netuid=netuid, weights=weights)
        saved = self.repository.save_snapshot(snapshot)
        self.bus.publish(
            "validator.weights.published",
            {
                "snapshot_id": saved.snapshot_id,
                "netuid": saved.netuid,
                "weights": saved.weights,
                "epoch_id": epoch_id,
            },
        )
        self.metrics.increment("weights.published")

        # Append-only scorecard history keyed by epoch (audit trail)
        if epoch_id:
            try:
                self.repository.save_scorecard_history(
                    epoch_id=epoch_id,
                    snapshot_id=saved.snapshot_id,
                    scorecards=scorecards,
                )
            except Exception:
                logger.exception("failed to save scorecard history for epoch %s", epoch_id)

        # Push to Bittensor chain if enabled
        logger.info(
            "[chain publish] scorecards=%s chain_init=%s bittensor_enabled=%s netuid=%s",
            len(scorecards), self._chain is not None,
            validator_settings.bittensor_enabled, netuid,
        )
        if self._chain and validator_settings.bittensor_enabled:
            try:
                commit = self._commit_weights_to_chain(scorecards)
                logger.info("[chain publish] _commit_weights_to_chain returned ok=%s", commit is not None)
            except Exception:
                logger.exception("failed to commit weights to chain")
        else:
            logger.info(
                "[chain publish] skipped — chain=%s bittensor_enabled=%s",
                self._chain is not None, validator_settings.bittensor_enabled,
            )

        return saved

    def _commit_weights_to_chain(self, scorecards: dict[str, ScoreCard]) -> ChainWeightCommit | None:
        """Convert scorecards to uid/weight vectors and call set_weights."""
        if not self._chain:
            logger.debug("[chain commit] no chain client, returning None")
            return None
        logger.info(
            "[chain commit] mapping %s scorecards to UIDs; metagraph_size=%s",
            len(scorecards), self.metagraph.size,
        )
        uids: list[int] = []
        weights: list[float] = []
        for hotkey, sc in sorted(scorecards.items()):
            uid = self.metagraph.hotkey_to_uid(hotkey)
            # Per-hotkey mapping at debug only — avoids dumping hotkeys+scores
            # to stdout/info on every epoch boundary.
            logger.debug("[chain commit]   hotkey=%s score=%.4f uid=%s", hotkey, sc.final_score, uid)
            if uid is None:
                logger.warning("hotkey not in metagraph, skipping weight")
                continue
            uids.append(uid)
            weights.append(sc.final_score)
        if not uids:
            logger.warning("no valid uids for set_weights — bailing")
            return None
        logger.info("[chain commit] calling set_weights for %s uids", len(uids))
        commit = self._chain.set_weights(uids, weights)
        logger.info("[chain commit] set_weights returned ok=%s", commit is not None)
        self.metrics.increment("chain.weights.committed")
        return commit

    # --- Audit (Chutes-style per-epoch signed reports) ---

    # Bittensor tempo — 360 blocks for most subnets including both of ours
    # (netuid 16 on testnet + netuid 110 on mainnet). At ~12s block time
    # this is one epoch / weight-setting window ≈ 72 min.
    AUDIT_EPOCH_LENGTH = 360

    @classmethod
    def _compute_epoch_window(cls, current_block: int, netuid: int) -> tuple[str, int, int]:
        """Return (epoch_id, start_block, end_block) for the epoch that
        **just closed** at `current_block`. end_block is exclusive."""
        end_block = (current_block // cls.AUDIT_EPOCH_LENGTH) * cls.AUDIT_EPOCH_LENGTH
        start_block = end_block - cls.AUDIT_EPOCH_LENGTH
        return f"{netuid}-{end_block}", start_block, end_block

    def generate_audit_report(
        self,
        epoch_id: str,
        start_block: int,
        end_block: int,
        netuid: int | None = None,
    ) -> AuditReport:
        """Build a canonical per-epoch audit report and anchor its SHA256
        on-chain via Commitments.set_commitment. The ScoreEngine formula,
        probe data, scorecards, and weight snapshot in the report are
        sufficient for an independent auditor (greencompute-audit) to replay
        our math and verify we didn't fudge weights."""
        import hashlib
        import json

        # Resolve netuid: explicit arg > chain client's configured netuid
        # > validator settings (16 on testnet, 110 on mainnet).
        if netuid is None:
            netuid = self._chain.netuid if self._chain else validator_settings.bittensor_netuid

        # Block-to-timestamp mapping is imperfect (chain block times drift) —
        # for MVP we bound the probe window by the audit tick's wall-clock
        # interval around the epoch boundary. At a 12s block time, an epoch
        # of 360 blocks = 72 minutes; we overshoot by 10 min on each side to
        # catch late-arriving probe results from the previous epoch.
        window_hint_seconds = self.AUDIT_EPOCH_LENGTH * 12
        now = datetime.now(UTC)
        window_start = now - timedelta(seconds=window_hint_seconds + 600)
        window_end = now + timedelta(seconds=60)

        challenges = self.repository.list_probe_challenges_since(window_start, window_end)
        results = self.repository.list_probe_results_since(window_start, window_end)
        snapshots = self.repository.list_weight_snapshots_in_block_range(
            netuid, window_start, window_end,
        )
        latest_snapshot = snapshots[-1] if snapshots else None

        # Collect every scorecard history row we wrote for this epoch_id —
        # these are the exact scorecards the weight snapshot was built from.
        # Query via raw ORM to avoid reshaping the whole repo API surface.
        from sqlalchemy import select as _select
        from greencompute_persistence import session_scope as _session_scope
        from greencompute_persistence.orm import ScoreCardHistoryORM

        with _session_scope(self.repository.session_factory) as session:
            rows = session.scalars(
                _select(ScoreCardHistoryORM)
                .where(ScoreCardHistoryORM.epoch_id == epoch_id)
                .order_by(ScoreCardHistoryORM.hotkey.asc())
            ).all()
            scorecard_rows = [{
                "hotkey": r.hotkey,
                "capacity_weight": r.capacity_weight,
                "reliability_score": r.reliability_score,
                "performance_score": r.performance_score,
                "security_score": r.security_score,
                "fraud_penalty": r.fraud_penalty,
                "utilization_score": r.utilization_score,
                "rental_revenue_bonus": r.rental_revenue_bonus,
                "final_score": r.final_score,
                "computed_at": r.computed_at.isoformat() if r.computed_at else None,
            } for r in rows]

        report_json = {
            "epoch_id": epoch_id,
            "netuid": netuid,
            "epoch_start_block": start_block,
            "epoch_end_block": end_block,
            "generated_at": now.isoformat(),
            "probes": [
                {
                    "challenge": {
                        "challenge_id": c.challenge_id,
                        "hotkey": c.hotkey,
                        "node_id": c.node_id,
                        "kind": c.kind,
                        "payload": c.payload,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                    },
                    "results": [
                        {
                            "hotkey": r.hotkey,
                            "latency_ms": r.latency_ms,
                            "throughput": r.throughput,
                            "success": r.success,
                            "benchmark_signature": r.benchmark_signature,
                            "proxy_suspected": r.proxy_suspected,
                            "readiness_failures": r.readiness_failures,
                            "prompt_sha256": r.prompt_sha256,
                            "response_sha256": r.response_sha256,
                            "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                        }
                        for r in results
                        if r.challenge_id == c.challenge_id
                    ],
                }
                for c in challenges
            ],
            "scorecards": scorecard_rows,
            "weight_snapshot": (
                {
                    "snapshot_id": latest_snapshot.snapshot_id,
                    "netuid": latest_snapshot.netuid,
                    "weights": latest_snapshot.weights,
                    "created_at": latest_snapshot.created_at.isoformat(),
                }
                if latest_snapshot else None
            ),
        }

        canonical = json.dumps(report_json, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        report_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        # Sign the canonical bytes with the validator's hotkey. Reuse the
        # existing auth.sign_payload_hotkey machinery if wallet is loaded;
        # otherwise skip signing (not fatal — auditors can still verify via
        # on-chain SHA256 anchor, signature is an extra convenience).
        signature = ""
        signer_hotkey = ""
        if self._chain and validator_settings.bittensor_wallet_path:
            try:
                from substrateinterface import Keypair as _Keypair
                kp = _Keypair.create_from_uri(validator_settings.bittensor_wallet_path)
                signature = kp.sign(canonical.encode("utf-8")).hex()
                signer_hotkey = kp.ss58_address
            except Exception:
                logger.exception("failed to sign audit report for epoch %s", epoch_id)

        # Anchor hash on-chain via Commitments.set_commitment
        chain_tx: str | None = None
        if self._chain and validator_settings.bittensor_enabled:
            try:
                chain_tx = self._chain.set_commitment(bytes.fromhex(report_sha256))
            except Exception:
                logger.exception("failed to anchor audit report %s on-chain", epoch_id)

        report = AuditReport(
            epoch_id=epoch_id,
            netuid=netuid,
            epoch_start_block=start_block,
            epoch_end_block=end_block,
            report_json=report_json,
            report_sha256=report_sha256,
            signature=signature,
            signer_hotkey=signer_hotkey,
            chain_commitment_tx=chain_tx,
            created_at=now,
        )
        self.repository.save_audit_report(report)
        self.bus.publish("audit.report.published", {
            "epoch_id": epoch_id,
            "report_sha256": report_sha256,
            "chain_commitment_tx": chain_tx,
        })
        self.metrics.increment("audit.report.published")
        return report

    # --- Metagraph sync ---

    def sync_metagraph(self) -> list[MetagraphEntry]:
        """Refresh metagraph from chain. Called periodically from worker loop."""
        if not self._chain:
            return []
        entries = self._chain.sync_metagraph()
        self.metagraph.update(entries)
        self.metrics.set_gauge("metagraph.size", float(self.metagraph.size))
        return entries

    def process_pending_events(self, limit: int = 10) -> list[dict]:
        events = self.bus.claim_pending(
            "validator-worker",
            ["probe.result.recorded", "validator.weights.published"],
            limit=limit,
        )
        processed: list[dict] = []
        for event in events:
            if event.subject == "probe.result.recorded":
                self.bus.mark_completed(event.delivery_id)
                self.metrics.increment("probe.result.delivered")
                processed.append({"subject": event.subject, "hotkey": event.payload["hotkey"]})
                continue
            if event.subject == "validator.weights.published":
                self.bus.mark_completed(event.delivery_id)
                self.metrics.increment("weights.delivered")
                processed.append({"subject": event.subject, "snapshot_id": event.payload["snapshot_id"]})
                continue
            self.bus.mark_failed(event.delivery_id, f"unsupported workflow subject={event.subject}")
        self.metrics.set_gauge(
            "workflow.pending.validator",
            float(
                len(
                    self.bus.list_deliveries(
                        consumer="validator-worker",
                        subjects=["probe.result.recorded", "validator.weights.published"],
                        statuses=["pending"],
                    )
                )
            ),
        )
        return processed


    # --- Flux orchestrator ---

    def get_flux_state(self, hotkey: str) -> FluxState | None:
        """Aggregated FluxState across all of this hotkey's nodes."""
        return self._aggregate_flux_state(hotkey)

    def init_flux_state(
        self,
        hotkey: str,
        node_id: str,
        total_gpus: int,
        available_gpus: int | None = None,
    ) -> FluxState:
        """Initialize or update a (hotkey, node_id) pair's Flux state.

        Only genuinely-free GPUs (available_gpus) seed the movable idle pool;
        the reserved remainder (total - available, e.g. running tenant rentals)
        is marked as locked rental_gpus so Flux never preempts a running pod.
        available_gpus defaults to total_gpus to preserve existing callers/tests.
        """
        key = (hotkey, node_id)
        with self._flux_lock:
            existing = self._flux_states.get(key)
            if existing and existing.total_gpus == total_gpus:
                return existing
            avail = total_gpus if available_gpus is None else max(0, min(available_gpus, total_gpus))
            reserved = total_gpus - avail
            state = FluxState(
                hotkey=hotkey,
                node_id=node_id,
                total_gpus=total_gpus,
                rental_gpus=reserved,
                idle_gpus=avail,
                inference_floor_pct=validator_settings.flux_inference_floor_pct,
                rental_floor_pct=validator_settings.flux_rental_floor_pct,
            )
            self._flux_states[key] = state
            return state

    @staticmethod
    def _is_cap_fresh(cap: NodeCapability, now_ts: datetime, timeout_seconds: float) -> bool:
        """True if this node's mirrored inventory is recent enough to schedule
        catalog replicas on. A missing or stale observed_at → treated as gone."""
        obs = getattr(cap, "observed_at", None)
        if obs is None:
            return False
        if obs.tzinfo is None:
            obs = obs.replace(tzinfo=UTC)
        return (now_ts - obs).total_seconds() <= timeout_seconds

    def _states_for_hotkey(self, hotkey: str) -> list[FluxState]:
        with self._flux_lock:
            return [s for (h, _), s in self._flux_states.items() if h == hotkey]

    def _aggregate_flux_state(self, hotkey: str) -> FluxState | None:
        """Synthesize a single FluxState by summing across all of this
        hotkey's per-node states. Used by scoring / dashboard / weight
        publishing — places that operate on the per-miner level."""
        with self._flux_lock:
            states = self._states_for_hotkey(hotkey)
            if not states:
                return None
            return FluxState(
                hotkey=hotkey,
                node_id=f"{hotkey}-aggregate",
                total_gpus=sum(s.total_gpus for s in states),
                inference_gpus=sum(s.inference_gpus for s in states),
                rental_gpus=sum(s.rental_gpus for s in states),
                idle_gpus=sum(s.idle_gpus for s in states),
                inference_floor_pct=validator_settings.flux_inference_floor_pct,
                rental_floor_pct=validator_settings.flux_rental_floor_pct,
            )

    def rebalance_miner(self, hotkey: str) -> tuple[FluxState, list[FluxRebalanceEvent]]:
        """Run Flux rebalance for every node belonging to this miner.

        For multi-node hotkeys (one identity, several physical boxes), this
        rebalances each node independently — each gets its own catalog
        assignments, idle/inference/rental split, etc. — and returns the
        aggregated state + combined events for the caller's convenience.

        Catalog-aware: pulls the current public catalog and each node's
        advertised VRAM, then lets the orchestrator both (a) pick the
        inf/rental split and (b) assign catalog models to inference GPUs.
        """
        with self._flux_lock:
            states = self._states_for_hotkey(hotkey)
            if not states:
                return FluxState(hotkey=hotkey, node_id="", total_gpus=0), []
            catalog = self.repository.list_catalog_entries(visibility="public")
            node_caps = {
                n.node_id: n for n in self.repository.get_node_capabilities(hotkey)
            }
            inf_score = self.demand.inference_score(hotkey)
            rent_score = self.demand.rental_score(hotkey)
            all_events: list[FluxRebalanceEvent] = []

            # Coordinate the FLEET-WIDE replica target across this hotkey's
            # nodes. Each node's rebalance is otherwise independent, so without
            # this every node fills its inference GPUs toward the SAME fleet
            # target — N nodes each running a replica of a target-1 model (the
            # phantom over-count that left the dashboard showing running=3 for a
            # single real replica). We seed `remaining` with the deficit after
            # what's already running, and hand each node only that shrinking
            # deficit, so only as many NEW replicas as the fleet still needs get
            # placed. (Per-node pins below keep existing replicas where they are.)
            live_states = {"ready", "starting", "scheduled", "provisioning", "pending"}
            fleet_running: dict[str, int] = {}
            for d in self.repository.list_flux_deployments(hotkey):
                mid = d.get("model_id")
                if mid and d.get("state") in live_states:
                    fleet_running[mid] = fleet_running.get(mid, 0) + 1
            remaining: dict[str, int] = {
                mid: max(0, tgt - fleet_running.get(mid, 0))
                for mid, tgt in (self._replica_targets or {}).items()
            }

            for state in states:
                primed = state.model_copy(update={
                    "inference_demand_score": inf_score,
                    "rental_demand_score": rent_score,
                })
                cap = node_caps.get(state.node_id)
                # Exclude configured GPU models from Flux catalog-inference
                # placement (default: a4000). The internal A4000 test box has
                # 16 GB VRAM — it can't run the 24 GB-class catalog models (OOM)
                # and is reserved for manual testing, so Flux must never
                # auto-assign inference replicas there. Manual/API deploys still
                # work (they go through the scheduler, not Flux).
                gpu_model = (getattr(cap, "gpu_model", "") or "").lower() if cap else ""
                if gpu_model and gpu_model in _flux_excluded_gpu_models():
                    continue
                vram = getattr(cap, "vram_gb_per_gpu", None) if cap else None
                # Pin the models THIS node already serves so its replica stays
                # in place rather than churning to another node on rebalance.
                pinned = {
                    d["model_id"]
                    for d in self.repository.list_flux_deployments(
                        hotkey, node_id=state.node_id
                    )
                    if d.get("model_id") and d.get("state") in live_states
                }
                new_state, events = self.flux.rebalance(
                    primed,
                    catalog=catalog,
                    vram_gb_per_gpu=vram,
                    replica_targets=remaining or None,
                    pinned_model_ids=pinned,
                )
                # A model this node NEWLY took (vs. one it already ran) consumes
                # one unit of the fleet-wide deficit so siblings don't re-add it.
                for mid, idxs in new_state.inference_assignments.items():
                    if idxs and mid not in pinned:
                        remaining[mid] = max(0, remaining.get(mid, 0) - 1)
                self._flux_states[(hotkey, state.node_id)] = new_state
                all_events.extend(events)
                self._reconcile_catalog_deployments(hotkey, new_state)
            self.metrics.increment("flux.rebalance", len(all_events))
            # Return aggregate state for callers that expect one — actual
            # per-node state lives in self._flux_states.
            return self._aggregate_flux_state(hotkey) or states[0], all_events

    def _reconcile_catalog_deployments(self, hotkey: str, new_state: FluxState) -> None:
        """Drive catalog replica deployments through the shared DB.
        For each model in the miner's inference_assignments, ensure a live
        Flux-managed deployment exists; terminate deployments for models no
        longer assigned. Miners pick up the new leases via sync_leases — no
        direct validator→miner HTTP needed."""
        # Pin replicas to the node Flux actually assigned this model to
        # (new_state.node_id). Using get_capability(hotkey) here pinned EVERY
        # replica for a multi-node hotkey onto an arbitrary "first" node — and
        # if a stale/renamed node_id sorted first, replicas were pinned to a
        # ghost node that no live box runs (the lease only executed via the
        # by-hotkey sync back-compat). new_state always carries the real node.
        target_node_id = new_state.node_id
        if not target_node_id:
            return

        # Detect deployments that failed/terminated since last reconcile and
        # add a cooldown so we don't instantly respawn on the same (miner,
        # model) pair. Without this, a miner that can't run a given model
        # (bad driver, CUDA version, OOM) burns an infinite respawn loop.
        now_ts = datetime.now(UTC)
        cooldown = timedelta(minutes=15)
        all_deps = self.repository.list_flux_deployments_incl_terminated(hotkey)
        for d in all_deps:
            if not (d["model_id"] and d["state"] in ("failed", "terminated")):
                continue
            # Only arm the cooldown for a RECENT failure, anchored to the
            # failure's own timestamp. Previously this used setdefault with
            # `now + 15min` over EVERY failed/terminated row — so on each
            # validator restart the reconcile re-read the full failure history
            # (hundreds of rows accumulate for a flaky model) and re-armed a
            # fresh 15-min cooldown off ancient failures, silently blocking
            # placement for 15 min after every restart. setdefault made it
            # worse — the first (often stale) value stuck permanently. Anchoring
            # to updated_at and keeping the max means ancient rows are ignored
            # and the window reflects the actual last failure.
            failed_at = d.get("updated_at")
            if failed_at is None or (now_ts - failed_at) >= cooldown:
                continue
            key = (hotkey, d["model_id"])
            until = failed_at + cooldown
            existing = self._replica_cooldown_until.get(key)
            self._replica_cooldown_until[key] = (
                max(until, existing) if existing else until
            )

        target_models = set(new_state.inference_assignments.keys())
        # Scope the existing-replica view to THIS node. Without the node filter
        # the termination loop below tore down healthy replicas living on the
        # hotkey's OTHER nodes (cross-node termination), and the provision branch
        # under-provisioned this node because a model already running on a
        # sibling node counted as 'existing' here. Pinning to target_node_id
        # makes each node reconcile only its own deficit/surplus.
        existing = self.repository.list_flux_deployments(hotkey, node_id=target_node_id)
        # Distributed-replica ranks are NOT this loop's business. A replica of a
        # too-large model spans several of the hotkey's nodes and is owned by
        # _reconcile_distributed_replicas. Left in, the termination branch below
        # would kill every rank sitting on a node whose own per-node
        # inference_assignments don't list that model — which is most of them —
        # tearing the replica down moments after it was placed.
        existing = [d for d in existing if not d.get("multi_node")]
        # Only LIVE deployments count as "already serving" this model. A failed /
        # unhealthy replica is NOT serving, yet list_flux_deployments still returns
        # it (it isn't 'terminated'). Counting it as existing deadlocks recovery:
        # the model stays targeted, so the terminate branch below leaves the dead
        # row in place, and the provision branch skips the model as already-present
        # — the catalog can never respawn a crashed replica. This is exactly what
        # wedged the whole catalog when a validator restart flipped live runtimes
        # to 'failed'. Count only live states here; reap the dead rows below.
        live_states = {"ready", "starting", "scheduled", "provisioning", "pending"}
        existing_models = {
            d["model_id"] for d in existing
            if d["model_id"] and d["state"] in live_states
        }

        # Terminate live replicas no longer targeted by Flux. A 'failed' row is
        # intentionally left as a historical record (terminate_flux_deployment
        # guards against re-terminating it) — the live-only existing_models set
        # above is what stops it blocking re-provision, which is the actual fix
        # for the catalog wedging after a restart flipped live runtimes to
        # 'failed' (the model stayed targeted, so the dead row was never cleaned
        # and the provision branch skipped it as already-present).
        for d in existing:
            if d["model_id"] and d["model_id"] not in target_models:
                if self.repository.terminate_flux_deployment(d["deployment_id"]):
                    self.bus.publish("flux.replica.terminated", {
                        "hotkey": hotkey,
                        "model_id": d["model_id"],
                        "deployment_id": d["deployment_id"],
                    })
                    self.metrics.increment("flux.replica.terminated")

        # Provision replicas for newly-assigned catalog models
        for model_id in target_models - existing_models:
            # Cooldown gate — skip if this (miner, model) pair has been in
            # a failed/terminated state within the cooldown window.
            cd = self._replica_cooldown_until.get((hotkey, model_id))
            if cd is not None and cd > now_ts:
                logger.info(
                    "flux reconcile: skipping %s on %s (cooldown until %s)",
                    model_id, hotkey, cd.isoformat(),
                )
                continue
            workload_id = self.repository.get_catalog_workload_id(model_id)
            if workload_id is None:
                logger.warning(
                    "flux reconcile: no canonical workload for catalog model %s (was it approved?)",
                    model_id,
                )
                continue
            dep_id = self.repository.create_flux_deployment(
                hotkey=hotkey,
                node_id=target_node_id,
                workload_id=workload_id,
            )
            self.bus.publish("flux.replica.provisioned", {
                "hotkey": hotkey,
                "model_id": model_id,
                "deployment_id": dep_id,
                "workload_id": workload_id,
            })
            self.metrics.increment("flux.replica.provisioned")

    def distributed_replica_status(self) -> list[dict]:
        """Fleet view of every distributed replica and its per-rank health.

        A distributed replica only serves when EVERY rank is ready, so the admin
        view reports readiness at replica level (not per row) — a replica that
        looks '7/8 ready' is serving nothing.
        """
        rows = self.repository.list_distributed_replica_rows()
        out: list[dict] = []
        for replica_id, ranks in sorted(group_by_replica(rows).items()):
            ordered = sorted(ranks, key=lambda r: (r.get("multi_node") or {}).get("rank", 0))
            first = (ordered[0].get("multi_node") or {}) if ordered else {}
            expected = int(first.get("node_count") or len(ordered))
            out.append({
                "replica_id": replica_id,
                "model_id": first.get("model_id"),
                "hotkey": ordered[0].get("hotkey") if ordered else None,
                "expected_ranks": expected,
                "live_ranks": len(ordered),
                "ready": replica_is_ready([r.get("state") for r in ordered])
                         and len(ordered) == expected,
                "action": replica_action(ordered, expected),
                "head_host": first.get("head_host"),
                "tensor_parallel_size": first.get("tensor_parallel_size"),
                "pipeline_parallel_size": first.get("pipeline_parallel_size"),
                "total_gpus": expected * int(first.get("gpus_per_node") or 0),
                "ranks": [
                    {
                        "rank": (r.get("multi_node") or {}).get("rank"),
                        "role": (r.get("multi_node") or {}).get("role"),
                        "node_id": r.get("node_id"),
                        "state": r.get("state"),
                        "deployment_id": r.get("deployment_id"),
                    }
                    for r in ordered
                ],
            })
        return out

    def _distributed_candidates(self) -> list[NodeCandidate]:
        """Fleet nodes eligible to host a rank of a distributed replica."""
        candidates: list[NodeCandidate] = []
        for hotkey, nodes in self.repository.list_node_capabilities().items():
            for cap in nodes:
                candidates.append(NodeCandidate(
                    hotkey=hotkey,
                    node_id=cap.node_id,
                    available_gpus=cap.available_gpus,
                    vram_gb_per_gpu=cap.vram_gb_per_gpu,
                    gpu_model=cap.gpu_model,
                    labels=dict(cap.labels or {}),
                ))
        return candidates

    def _teardown_replica(self, rank_rows: list[dict], reason: str) -> None:
        """Terminate every rank of a replica, workers before the head."""
        for row in teardown_order(rank_rows):
            if self.repository.terminate_flux_deployment(row["deployment_id"]):
                self.metrics.increment("flux.distributed.rank_terminated")
        mn = (rank_rows[0].get("multi_node") or {}) if rank_rows else {}
        logger.info(
            "distributed replica %s torn down (%s)", mn.get("replica_id"), reason
        )
        self.bus.publish("flux.distributed.replica_terminated", {
            "replica_id": mn.get("replica_id"),
            "model_id": mn.get("model_id"),
            "reason": reason,
        })

    def _reconcile_distributed_replicas(self) -> None:
        """Fleet-level reconcile for models too large for a single node.

        Deliberately separate from _reconcile_catalog_deployments, which is
        per-(hotkey, node): a distributed replica is one logical unit spanning
        several nodes, so it can only be planned and judged as a whole. The
        placement rules guarantee every rank lives under ONE hotkey, so this
        never splits a replica across operators (which would also break the
        per-hotkey emission model).
        """
        entries = [
            e for e in self.repository.list_catalog_entries()
            if e.multi_node is not None and e.multi_node.is_distributed
        ]
        if not entries:
            return

        candidates = self._distributed_candidates()
        for entry in entries:
            config = entry.multi_node
            problems = validate_topology(config)
            if problems:
                logger.error(
                    "catalog model %s has an unservable multi-node topology: %s",
                    entry.model_id, "; ".join(problems),
                )
                self.metrics.increment("flux.distributed.invalid_topology")
                continue

            rank_rows = self.repository.list_distributed_replica_rows(entry.model_id)
            groups = group_by_replica(rank_rows)
            healthy = 0
            for replica_id, rows in groups.items():
                action = replica_action(rows, config.node_count)
                if action == KEEP:
                    healthy += 1
                elif action == REBUILD:
                    self._teardown_replica(rows, f"incomplete replica {replica_id}")

            if healthy >= max(entry.min_replicas, 1):
                continue

            workload_id = self.repository.get_catalog_workload_id(entry.model_id)
            if workload_id is None:
                logger.warning(
                    "distributed model %s has no canonical workload (approved?)",
                    entry.model_id,
                )
                continue

            # Exclude nodes already hosting a rank of this model so a rebuild
            # doesn't try to reuse hardware that hasn't been released yet.
            busy = {(r["hotkey"], r["node_id"]) for r in rank_rows}
            free = [c for c in candidates if (c.hotkey, c.node_id) not in busy]

            plan = plan_multi_node_placement(
                model_id=entry.model_id,
                config=config,
                candidates=free,
                min_vram_gb=entry.min_vram_gb_per_gpu,
            )
            if plan is None:
                logger.info(
                    "no viable node group for distributed model %s (needs %dx%d GPUs, "
                    ">=%.0fGbps, same operator+fabric)",
                    entry.model_id, config.node_count, config.gpus_per_node,
                    config.min_interconnect_gbps,
                )
                self.metrics.increment("flux.distributed.unplaceable")
                continue

            # Workers must be told where to dial. The validator can't infer a
            # node's cluster address, so an unlabelled head makes the replica
            # unstartable — refuse rather than provision ranks that can never
            # find each other.
            head_node = next(
                (c for c in free
                 if c.hotkey == plan.head.hotkey and c.node_id == plan.head.node_id),
                None,
            )
            head_host = head_address(head_node) if head_node else ""
            if not head_host:
                logger.error(
                    "head node %s/%s has no '%s' label — cannot start distributed "
                    "model %s (workers would have no address to join)",
                    plan.head.hotkey, plan.head.node_id, CLUSTER_ADDRESS_LABEL,
                    entry.model_id,
                )
                self.metrics.increment("flux.distributed.missing_cluster_address")
                continue

            replica_id = f"{entry.model_id}-{uuid4().hex[:8]}"
            rows = build_replica_rows(
                plan=plan,
                replica_id=replica_id,
                head_host=head_host,
                gpus_per_node=config.gpus_per_node,
            )
            # Head first — workers dial its address, so its lease must be in
            # flight before theirs.
            for row in rows:
                self.repository.create_flux_deployment(
                    hotkey=row["hotkey"],
                    node_id=row["node_id"],
                    workload_id=workload_id,
                    multi_node=row["multi_node"],
                )
            logger.info(
                "provisioned distributed replica %s for %s across %d nodes (head %s)",
                replica_id, entry.model_id, len(rows), head_host,
            )
            self.metrics.increment("flux.distributed.replica_provisioned")
            self.bus.publish("flux.distributed.replica_provisioned", {
                "replica_id": replica_id,
                "model_id": entry.model_id,
                "node_count": len(rows),
                "head_host": head_host,
            })

    def rebalance_all_miners(self) -> dict[str, FluxState]:
        """Rebalance all tracked miners. Called from the worker loop.
        Computes the fleet-wide replica targets up-front so every per-miner
        rebalance sees the same target map. Lazily bootstraps Flux state
        for any registered capability we haven't seen yet — previously
        `_flux_states` only grew from the (unused) init_flux_state call
        site, so rebalance was a no-op after every restart."""
        # Mirror miners from control-plane inventory into validator
        # capabilities, since miners register once (with the control plane)
        # and bittensor on-chain sync is typically off. Without this, Flux
        # never sees real miners — even though they're healthy + scheduled
        # elsewhere in the system.
        try:
            self.repository.sync_from_control_plane()
        except Exception:
            logger.exception("failed to sync capabilities from control plane")

        # Only consider nodes whose mirrored inventory is fresh. A node that
        # stopped heartbeating (dead box, or an old node_id left behind after a
        # rename) must not receive catalog replicas — Flux would pin them to a
        # node no live box runs, draining the pool.
        now_ts = datetime.now(UTC)
        timeout = validator_settings.node_inventory_timeout_seconds
        fresh_caps: dict[str, list[NodeCapability]] = {}
        fresh_keys: set[tuple[str, str]] = set()
        for hotkey, nodes in self.repository.list_node_capabilities().items():
            live = [c for c in nodes if self._is_cap_fresh(c, now_ts, timeout)]
            if live:
                fresh_caps[hotkey] = live
            for c in live:
                fresh_keys.add((hotkey, c.node_id))

        # Single coarse critical section over all the in-memory Flux maps so a
        # concurrent admin /flux/rebalance (FastAPI threadpool) can't interleave
        # a half-updated target map, resurrect a just-deleted ghost node, or lose
        # a decrement against the worker-loop tick.
        with self._flux_lock:
            # Drop Flux state for nodes that have gone stale (ghost cleanup) so the
            # rebalance loop below stops allocating to them.
            for key in list(self._flux_states):
                if key not in fresh_keys:
                    logger.info("flux: dropping stale node %s from rebalance (inventory expired)", key)
                    del self._flux_states[key]

            # Bootstrap: ensure every FRESH (hotkey, node_id) has a Flux state so
            # rebalance iterates them. Noop for entries already present.
            for hotkey, nodes in fresh_caps.items():
                for cap in nodes:
                    key = (hotkey, cap.node_id)
                    if key in self._flux_states:
                        continue
                    # Seed the movable idle pool from genuinely-free GPUs only.
                    # available_gpus lumps rentals + inference replicas together;
                    # the reserved block (gpu_count - available_gpus) is marked as
                    # locked rental_gpus so Flux never provisions inference replicas
                    # on top of a running tenant rental. Rebalance only ever moves
                    # idle GPUs, so locked rental GPUs are never preempted. The true
                    # inf/rental split self-corrects on later state updates.
                    avail = max(0, min(cap.available_gpus, cap.gpu_count))
                    reserved = cap.gpu_count - avail
                    self._flux_states[key] = FluxState(
                        hotkey=hotkey,
                        node_id=cap.node_id,
                        total_gpus=cap.gpu_count,
                        rental_gpus=reserved,
                        idle_gpus=avail,
                        inference_floor_pct=validator_settings.flux_inference_floor_pct,
                        rental_floor_pct=validator_settings.flux_rental_floor_pct,
                    )

            self._replica_targets = self.compute_replica_targets()
            # Populate the per-hotkey demand collector so the Flux flex split is
            # demand-driven instead of a hardcoded 50/50. Without this, inference_score
            # / rental_score always returned 0.0 → total_demand == 0 → the orchestrator
            # split the idle flex pool evenly regardless of real load.
            self._update_demand_signals()
            results: dict[str, FluxState] = {}
            rebalanced_hotkeys: set[str] = set()
            for hotkey, _node_id in list(self._flux_states):
                if hotkey in rebalanced_hotkeys:
                    continue
                new_state, _ = self.rebalance_miner(hotkey)
                results[hotkey] = new_state
                rebalanced_hotkeys.add(hotkey)
            # Distributed replicas are planned across nodes, so they reconcile
            # once per fleet pass — after the per-node rebalances have settled
            # each node's available capacity. Never let a failure here break the
            # ordinary catalog rebalance.
            try:
                self._reconcile_distributed_replicas()
            except Exception:
                logger.exception("distributed replica reconcile failed")
            return results

    def _update_demand_signals(self) -> None:
        """Refresh the DemandCollector with the latest per-hotkey inference and
        rental demand. Inference demand = sum of blended rpm across the catalog
        models each hotkey currently serves (reuses the blended-rpm map captured
        in compute_replica_targets). Rental demand = count of owner-bound rental
        deployments pinned to that hotkey still awaiting placement. Both signals
        only influence how the IDLE flex pool is split — no running pod is moved.
        Must be called while holding the flux lock (iterates _flux_states)."""
        # Aggregate each hotkey's currently-served models across all its nodes.
        served_by_hotkey: dict[str, set[str]] = {}
        for (hotkey, _node_id), state in self._flux_states.items():
            served = served_by_hotkey.setdefault(hotkey, set())
            for model_id, idxs in state.inference_assignments.items():
                if idxs:
                    served.add(model_id)
        for hotkey, models in served_by_hotkey.items():
            inf_rpm = sum(self._blended_rpm.get(m, 0.0) for m in models)
            self.demand.update_inference(
                InferenceDemandSignal(
                    hotkey=hotkey,
                    pending_requests=int(inf_rpm),
                    avg_queue_depth=0.0,
                )
            )
            try:
                pending = self.repository.count_pending_rentals(hotkey)
            except Exception:
                logger.exception("failed to count pending rentals for hotkey")
                pending = 0
            self.demand.update_rental(
                RentalDemandSignal(hotkey=hotkey, pending_deployments=pending)
            )

    # --- Demand-reactive replica targets (Phase 2I) --------------------

    # --- Inference attestation canary (Phase 2F) -----------------------

    def run_inference_canary(self, hotkey: str, model_id: str) -> ProbeResult:
        """Fire a canary chat-completion against the gateway and score the
        response.

        A.5 hardening: the prompt is now **nonce-bearing** — we instruct the
        miner to echo back a fresh random token. An honest miner serving the
        actual model emits the nonce verbatim; a miner returning canned
        responses or proxying to a cache/OpenAI key without the nonce gets
        flagged as failed. The exact prompt + response SHA256 go into the
        ProbeResult so independent auditors can re-verify the check."""
        import hashlib
        import json
        import secrets
        import time as _time
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        cap = self.repository.get_capability(hotkey)
        if cap is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={hotkey}")

        # Generate a fresh 8-byte nonce for this probe. Present both as a
        # prompt token the miner must echo + a commit value the miner must
        # include. Both are embedded in challenge.payload so auditors see
        # the exact test we asked.
        nonce = secrets.token_hex(8)
        prompt = (
            f"You will receive a token. Echo it back verbatim as the first "
            f"word of your reply. Your reply must start exactly with the "
            f"token followed by a space and the word DONE. Token: {nonce}"
        )
        expected_prefix = f"{nonce} DONE"

        challenge = ProbeChallenge(
            hotkey=hotkey,
            node_id=cap.node_id,
            kind="inference_verification",
            payload={
                "model_id": model_id,
                "prompt": prompt,
                "nonce": nonce,
                "expected_prefix": expected_prefix,
            },
        )
        challenge = self.repository.save_challenge(challenge)

        gw = validator_settings.gateway_url
        key = validator_settings.inference_canary_api_key
        if not gw or not key:
            result = ProbeResult(
                challenge_id=challenge.challenge_id,
                hotkey=hotkey,
                node_id=cap.node_id,
                latency_ms=0.0,
                throughput=0.0,
                success=False,
                readiness_failures=1,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            )
            return self.repository.add_result(result)

        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32,
            "stream": False,
        }
        data = json.dumps(body).encode()
        req = Request(
            gw.rstrip("/") + "/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json", "X-API-Key": key},
            method="POST",
        )
        started = _time.perf_counter()
        latency_ms = 0.0
        success = False
        signature: str | None = None
        tokens = 0
        readiness_failures = 0
        response_text = ""
        proxy_suspected = False
        try:
            with urlopen(req, timeout=validator_settings.inference_canary_timeout_seconds) as resp:
                payload = json.loads(resp.read())
            latency_ms = (_time.perf_counter() - started) * 1000.0
            choices = payload.get("choices") or []
            content = (choices[0].get("message") or {}).get("content") if choices else None
            if content and isinstance(content, str):
                response_text = content.strip()
                # Hardened check: miner must echo the nonce verbatim. Case-
                # insensitive match on the expected prefix — some models add
                # leading whitespace or casing quirks. But the nonce itself
                # (hex) must appear literally.
                if nonce in response_text:
                    success = True
                    # De-nonced cross-probe fingerprint, used by the fraud check
                    # (scoring._fraud_penalty: >1 distinct signature across a
                    # hotkey's probes == inconsistent backend). It MUST be stable
                    # across every honest probe of one hotkey, or honest miners
                    # eat a permanent 0.75 penalty. Two things would otherwise
                    # break that — both fixed here:
                    #   (1) the random nonce — stripped out before hashing, so
                    #       two honest answers to different nonces collapse.
                    #   (2) model_id — _fraud_penalty aggregates ALL of a
                    #       hotkey's probes, and a miner serving MULTIPLE catalog
                    #       models (the whole point of the shared catalog pool)
                    #       would get one signature per model -> >1 -> penalty.
                    #       Since this canary is a model-independent ECHO test
                    #       ("reply {nonce} DONE"), model_id carries no real
                    #       signal here, so it is intentionally excluded.
                    # Anti-proxy is unaffected — that is the separate verbatim
                    # nonce-echo check above + the proxy_suspected flag.
                    norm = response_text.upper()
                    denonced = norm.replace(nonce.upper(), "")[:64]
                    signature = hashlib.sha256(
                        denonced.encode()
                    ).hexdigest()[:16]
                else:
                    # Nonce missing = response did not come from a real
                    # inference on THIS prompt. Either cached, pre-computed,
                    # or proxied from a different prompt. Flag as proxy.
                    proxy_suspected = True
                    logger.warning(
                        "probe %s: miner %s response missing nonce %s (response=%r)",
                        challenge.challenge_id, hotkey, nonce, response_text[:80],
                    )
            usage = payload.get("usage") or {}
            tokens = int(usage.get("completion_tokens", 0) or 0)
        except HTTPError as exc:
            latency_ms = (_time.perf_counter() - started) * 1000.0
            readiness_failures = 1
            logger.warning("inference canary http %s for %s: %s", exc.code, model_id, exc.reason)
        except Exception as exc:  # noqa: BLE001 — probe-scoped catch
            latency_ms = (_time.perf_counter() - started) * 1000.0
            readiness_failures = 1
            logger.warning("inference canary error for %s: %s", model_id, exc)

        throughput = (tokens / max(latency_ms, 1.0)) * 1000.0 if success else 0.0
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        response_hash = hashlib.sha256(response_text.encode()).hexdigest() if response_text else None
        result = ProbeResult(
            challenge_id=challenge.challenge_id,
            hotkey=hotkey,
            node_id=cap.node_id,
            latency_ms=latency_ms,
            throughput=throughput,
            success=success,
            benchmark_signature=signature,
            proxy_suspected=proxy_suspected,
            readiness_failures=readiness_failures,
            prompt_sha256=prompt_hash,
            response_sha256=response_hash,
        )
        saved = self.repository.add_result(result)
        self.metrics.increment(f"probe.inference.{'success' if success else 'failure'}")
        if proxy_suspected:
            self.metrics.increment("probe.inference.proxy_suspected")
        return saved

    def run_attestation_tick(self) -> ProbeResult | None:
        """Periodic worker hook — fires a canary against one live
        (miner, catalog model) pair per call. Round-robins across the
        fleet so every replica gets probed eventually. Skips silently if
        no catalog replicas are running."""
        pairs: list[tuple[str, str]] = []
        # Per-(hotkey, node) iteration — multi-node hotkeys generate one
        # pair per node serving the catalog model. Snapshot under the lock so a
        # concurrent rebalance can't mutate the dict mid-iteration; release
        # before the (slow, network-bound) canary call below.
        with self._flux_lock:
            for (hotkey, _node_id), state in self._flux_states.items():
                for model_id, idxs in state.inference_assignments.items():
                    if idxs:
                        pairs.append((hotkey, model_id))
        if not pairs:
            return None
        # Deterministic round-robin without storing a pointer: hash on
        # the current minute so we spread probes without thrashing on
        # restart.
        idx = (int(datetime.now(UTC).timestamp()) // 60) % len(pairs)
        hotkey, model_id = pairs[idx]
        try:
            return self.run_inference_canary(hotkey, model_id)
        except Exception:
            logger.exception("attestation tick failed for %s / %s", hotkey, model_id)
            return None

    # --- Dashboard snapshot (Phase 2J) ---------------------------------

    def build_flux_dashboard(self) -> dict:
        """Single-shot snapshot bundle for the admin /flux dashboard.
        Returns fleet-wide tiles, per-model catalog pool status, and a
        per-miner summary. UI polls this every 5s."""
        now = datetime.now(UTC)
        capabilities = self.repository.list_capabilities()
        scorecards = self.repository.list_scorecards()

        # Take a consistent snapshot of the in-memory Flux maps under the lock,
        # then build the (DB-heavy) dashboard off the snapshot so a concurrent
        # rebalance can't mutate the dicts mid-iteration.
        with self._flux_lock:
            flux_states = list(self._flux_states.values())
            replica_targets = dict(self._replica_targets)
            online_hotkeys = {h for (h, _n) in self._flux_states}

        # Fleet strip — derived from the in-memory Flux state map
        total_gpus = 0
        inference_gpus = 0
        rental_gpus = 0
        idle_gpus = 0
        active_catalog_replicas = 0
        for state in flux_states:
            total_gpus += state.total_gpus
            inference_gpus += state.inference_gpus
            rental_gpus += state.rental_gpus
            idle_gpus += state.idle_gpus
            for gpu_idxs in state.inference_assignments.values():
                active_catalog_replicas += 1 if gpu_idxs else 0

        # Catalog pool — per-model replica counts and demand
        catalog_pool: list[dict] = []
        running_by_model: dict[str, int] = {}
        for state in flux_states:
            for model_id, idxs in state.inference_assignments.items():
                if idxs:
                    running_by_model[model_id] = running_by_model.get(model_id, 0) + 1
        for entry in self.repository.list_catalog_entries(visibility="public"):
            windows = self.repository.read_demand_windows(entry.model_id, now=now)
            running = running_by_model.get(entry.model_id, 0)
            target = replica_targets.get(entry.model_id, entry.min_replicas)
            serving_miners = [
                state.hotkey
                for state in flux_states
                if state.inference_assignments.get(entry.model_id)
            ]
            if windows["rpm_10m"] > validator_settings.target_rpm_per_replica:
                status = "hot"
            elif windows["rpm_10m"] > 0:
                status = "warm"
            else:
                status = "cold"
            catalog_pool.append({
                "model_id": entry.model_id,
                "display_name": entry.display_name,
                "target_replicas": target,
                "running_replicas": running,
                "rpm_10m": round(windows["rpm_10m"], 2),
                "rpm_1h": round(windows["rpm_1h"], 2),
                "status": status,
                "serving_miners": serving_miners,
            })

        # Miner fleet summary — aggregate across all of each hotkey's nodes
        miner_fleet: list[dict] = []
        for hotkey, cap in sorted(capabilities.items()):
            state = self._aggregate_flux_state(hotkey)
            nodes = self.repository.get_node_capabilities(hotkey)
            assigned: list[str] = []
            for s in self._states_for_hotkey(hotkey):
                assigned.extend(s.inference_assignments.keys())
            assigned = sorted(set(assigned))
            sc = scorecards.get(hotkey)
            total_gpus = sum(n.gpu_count for n in nodes) if nodes else cap.gpu_count
            miner_fleet.append({
                "hotkey": hotkey,
                "node_id": cap.node_id,
                "gpu_model": cap.gpu_model,
                "gpu_count": total_gpus,
                "inference_gpus": state.inference_gpus if state else 0,
                "rental_gpus": state.rental_gpus if state else 0,
                "idle_gpus": state.idle_gpus if state else total_gpus,
                "assigned_models": assigned,
                "reliability_score": sc.reliability_score if sc else None,
                "final_score": sc.final_score if sc else None,
                "last_rebalanced_at": state.last_rebalanced_at.isoformat() if state and state.last_rebalanced_at else None,
            })

        return {
            "observed_at": now.isoformat(),
            "fleet": {
                "total_gpus": total_gpus,
                "inference_gpus": inference_gpus,
                "rental_gpus": rental_gpus,
                "idle_gpus": idle_gpus,
                "miners_online": len(online_hotkeys),
                "miners_registered": len(capabilities),
                "active_catalog_replicas": active_catalog_replicas,
                "catalog_models": len(catalog_pool),
            },
            "catalog_pool": catalog_pool,
            "miner_fleet": miner_fleet,
        }

    def demand_timeseries(self, *, model_id: str | None = None, window_minutes: int = 60) -> list[dict]:
        """Return per-minute rows from `inference_demand_stats`. If model_id
        is None, returns rows for every catalog model."""
        from sqlalchemy import select as _select

        from greencompute_persistence import session_scope as _session_scope
        from greencompute_persistence.orm import InferenceDemandStatsORM

        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
        stmt = _select(InferenceDemandStatsORM).where(
            InferenceDemandStatsORM.window_start >= cutoff
        )
        if model_id:
            stmt = stmt.where(InferenceDemandStatsORM.model_id == model_id)
        stmt = stmt.order_by(InferenceDemandStatsORM.window_start.asc())
        with _session_scope(self.repository.session_factory) as session:
            rows = session.scalars(stmt).all()
            return [
                {
                    "model_id": r.model_id,
                    "window_start": r.window_start.isoformat(),
                    "invocations": r.invocations,
                    "prompt_tokens_sum": r.prompt_tokens_sum,
                    "completion_tokens_sum": r.completion_tokens_sum,
                }
                for r in rows
            ]

    def flux_events(self, limit: int = 50) -> list[dict]:
        """Merged feed of recent bus events relevant to the /flux dashboard.
        Pulls from the workflow event store, filtered to Flux + catalog
        subjects."""
        subjects = [
            "flux.replica.provisioned",
            "flux.replica.terminated",
            "probe.result.recorded",
            "validator.weights.published",
        ]
        events = self.workflow_repository.list_events(subjects=subjects)
        events = events[-limit:] if limit and len(events) > limit else events
        return [
            {
                "event_id": e.event_id,
                "subject": e.subject,
                "payload": e.payload,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]

    def compute_replica_targets(self, now: datetime | None = None) -> dict[str, int]:
        """For every public catalog model, derive the target replica count
        from recent demand. Uses a blended 10-min / 60-min EMA (hot-biased)
        divided by `target_rpm_per_replica`. Scale-down is guarded by a
        hysteresis window — a model that was hot within the last
        `flux_cooldown_seconds` is never dropped below its previous target."""
        now = now or datetime.now(UTC)
        targets: dict[str, int] = {}
        blended_rpm: dict[str, float] = {}
        catalog = self.repository.list_catalog_entries(visibility="public")
        # Guard the hysteresis/target maps. Reentrant — this is normally already
        # held via rebalance_all_miners, but the lock makes a direct call safe too.
        with self._flux_lock:
            for entry in catalog:
                windows = self.repository.read_demand_windows(entry.model_id, now=now)
                rpm_10 = windows["rpm_10m"]
                rpm_60 = windows["rpm_1h"]
                blended = 0.7 * rpm_10 + 0.3 * rpm_60
                blended_rpm[entry.model_id] = blended
                raw_target = ceil(blended / validator_settings.target_rpm_per_replica)
                target = max(entry.min_replicas, raw_target)
                if entry.max_replicas is not None:
                    target = min(target, entry.max_replicas)

                # Hysteresis — if the model was above its scale-up floor
                # recently, block scale-down until cooldown elapses.
                scale_up_floor = validator_settings.target_rpm_per_replica
                if blended > scale_up_floor:
                    self._demand_last_hot_at[entry.model_id] = now
                last_hot = self._demand_last_hot_at.get(entry.model_id)
                in_cooldown = (
                    last_hot is not None
                    and (now - last_hot).total_seconds() < validator_settings.flux_cooldown_seconds
                )
                if in_cooldown:
                    prev = self._replica_targets.get(entry.model_id, target)
                    target = max(target, prev)

                targets[entry.model_id] = target
                self.metrics.set_gauge(f"flux.target_replicas.{entry.model_id}", float(target))
                self.metrics.set_gauge(f"flux.rpm_10m.{entry.model_id}", rpm_10)
                self.metrics.set_gauge(f"flux.rpm_1h.{entry.model_id}", rpm_60)
            # Stash the blended rpm so rebalance_all_miners can build per-hotkey
            # inference demand signals without re-querying.
            self._blended_rpm = blended_rpm
            return targets

    def estimate_rental_wait(self, deployment_id: str, hotkey: str) -> RentalWaitEstimate:
        """Estimate wait time for a rental deployment on a specific miner."""
        state = self._aggregate_flux_state(hotkey)
        if state is None:
            return RentalWaitEstimate(
                deployment_id=deployment_id,
                estimated_wait_seconds=0.0,
                position_in_queue=0,
            )
        self.wait_estimator.enqueue(deployment_id)
        return self.wait_estimator.estimate(deployment_id, state)


service = ValidatorService()

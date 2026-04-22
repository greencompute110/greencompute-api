from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from math import ceil

from greenference_persistence import SubjectBus, WorkflowEventRepository, create_subject_bus, get_metrics_store
from greenference_persistence.runtime import load_runtime_settings
from greenference_protocol import (
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
from greenference_validator.config import settings as validator_settings
from greenference_validator.domain.chain import BittensorChainClient
from greenference_validator.domain.demand import DemandCollector
from greenference_validator.domain.flux import FluxOrchestrator
from greenference_validator.domain.metagraph import MetagraphCache
from greenference_validator.domain.scoring import ScoreEngine
from greenference_validator.domain.wait_estimator import WaitEstimator
from greenference_validator.infrastructure.repository import ValidatorRepository

logger = logging.getLogger(__name__)


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
        runtime_settings = load_runtime_settings("greenference-validator")
        self.bus = bus or create_subject_bus(
            engine=self.workflow_repository.engine,
            session_factory=self.workflow_repository.session_factory,
            workflow_repository=self.workflow_repository,
            nats_url=runtime_settings.nats_url,
            transport=runtime_settings.bus_transport,
        )
        self.scoring = ScoreEngine()
        self.metrics = get_metrics_store("greenference-validator")
        self.flux = FluxOrchestrator(
            inference_floor_pct=validator_settings.flux_inference_floor_pct,
            rental_floor_pct=validator_settings.flux_rental_floor_pct,
        )
        self.demand = DemandCollector()
        self.wait_estimator = WaitEstimator()
        self._flux_states: dict[str, FluxState] = {}
        # Phase 2I hysteresis — last time a model's blended rpm was seen
        # above its scale-up threshold. Used to defer scale-down.
        self._demand_last_hot_at: dict[str, "datetime"] = {}
        # Cache of the latest computed replica targets so per-miner rebalance
        # can pass them to the orchestrator without recomputing.
        self._replica_targets: dict[str, int] = {}

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
        return self.repository.upsert_capability(capability)

    def create_probe(self, hotkey: str, node_id: str, kind: str = "latency") -> ProbeChallenge:
        capability = self.repository.get_capability(hotkey)
        if capability is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={hotkey}")
        if capability.node_id != node_id:
            raise InvalidProbeResultError(f"node mismatch for hotkey={hotkey}: expected={capability.node_id}")
        challenge = ProbeChallenge(hotkey=hotkey, node_id=node_id, kind=kind)
        return self.repository.save_challenge(challenge)

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
        flux = self._flux_states.get(result.hotkey)
        scorecard = self.scoring.compute_scorecard(capability, self.repository.list_results(result.hotkey), flux)
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

    def publish_weight_snapshot(self, netuid: int = 16) -> WeightSnapshot:
        scorecards: dict[str, ScoreCard] = {}
        for hotkey, capability in sorted(self.repository.list_capabilities().items()):
            if validator_settings.whitelist_enabled and not self.repository.is_whitelisted(hotkey):
                logger.info("skipping non-whitelisted miner %s", hotkey)
                continue
            results = self.repository.list_results(hotkey)
            if not results:
                continue
            flux = self._flux_states.get(hotkey)
            scorecard = self.scoring.compute_scorecard(capability, results, flux)
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
            },
        )
        self.metrics.increment("weights.published")

        # Push to Bittensor chain if enabled
        if self._chain and validator_settings.bittensor_enabled:
            try:
                self._commit_weights_to_chain(scorecards)
            except Exception:
                logger.exception("failed to commit weights to chain")

        return saved

    def _commit_weights_to_chain(self, scorecards: dict[str, ScoreCard]) -> ChainWeightCommit | None:
        """Convert scorecards to uid/weight vectors and call set_weights."""
        if not self._chain:
            return None
        uids: list[int] = []
        weights: list[float] = []
        for hotkey, sc in sorted(scorecards.items()):
            uid = self.metagraph.hotkey_to_uid(hotkey)
            if uid is None:
                logger.warning("hotkey %s not in metagraph, skipping weight", hotkey)
                continue
            uids.append(uid)
            weights.append(sc.final_score)
        if not uids:
            logger.warning("no valid uids for set_weights")
            return None
        commit = self._chain.set_weights(uids, weights)
        self.metrics.increment("chain.weights.committed")
        return commit

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
        return self._flux_states.get(hotkey)

    def init_flux_state(self, hotkey: str, node_id: str, total_gpus: int) -> FluxState:
        """Initialize or update a miner's Flux state when capacity is registered."""
        existing = self._flux_states.get(hotkey)
        if existing and existing.total_gpus == total_gpus:
            return existing
        state = FluxState(
            hotkey=hotkey,
            node_id=node_id,
            total_gpus=total_gpus,
            idle_gpus=total_gpus,
            inference_floor_pct=validator_settings.flux_inference_floor_pct,
            rental_floor_pct=validator_settings.flux_rental_floor_pct,
        )
        self._flux_states[hotkey] = state
        return state

    def rebalance_miner(self, hotkey: str) -> tuple[FluxState, list[FluxRebalanceEvent]]:
        """Run Flux rebalance for a single miner.

        Catalog-aware: pulls the current public catalog and the miner's
        advertised VRAM, then lets the orchestrator both (a) pick the
        inf/rental split and (b) assign catalog models to the inference GPUs.
        """
        state = self._flux_states.get(hotkey)
        if state is None:
            return FluxState(hotkey=hotkey, node_id="", total_gpus=0), []
        # Inject latest demand scores
        state = state.model_copy(update={
            "inference_demand_score": self.demand.inference_score(hotkey),
            "rental_demand_score": self.demand.rental_score(hotkey),
        })
        # Catalog + miner VRAM for model-assignment math
        catalog = self.repository.list_catalog_entries(visibility="public")
        vram = None
        cap = self.repository.get_capability(hotkey)
        if cap is not None:
            vram = getattr(cap, "vram_gb_per_gpu", None)
        new_state, events = self.flux.rebalance(
            state,
            catalog=catalog,
            vram_gb_per_gpu=vram,
            replica_targets=self._replica_targets or None,
        )
        self._flux_states[hotkey] = new_state
        self.metrics.increment("flux.rebalance", len(events))
        self._reconcile_catalog_deployments(hotkey, new_state)
        return new_state, events

    def _reconcile_catalog_deployments(self, hotkey: str, new_state: FluxState) -> None:
        """Drive catalog replica deployments through the shared DB.
        For each model in the miner's inference_assignments, ensure a live
        Flux-managed deployment exists; terminate deployments for models no
        longer assigned. Miners pick up the new leases via sync_leases — no
        direct validator→miner HTTP needed."""
        cap = self.repository.get_capability(hotkey)
        if cap is None:
            return
        target_models = set(new_state.inference_assignments.keys())
        existing = self.repository.list_flux_deployments(hotkey)
        existing_models = {d["model_id"] for d in existing if d["model_id"]}

        # Terminate replicas no longer targeted by Flux
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
            workload_id = self.repository.get_catalog_workload_id(model_id)
            if workload_id is None:
                logger.warning(
                    "flux reconcile: no canonical workload for catalog model %s (was it approved?)",
                    model_id,
                )
                continue
            dep_id = self.repository.create_flux_deployment(
                hotkey=hotkey,
                node_id=cap.node_id,
                workload_id=workload_id,
            )
            self.bus.publish("flux.replica.provisioned", {
                "hotkey": hotkey,
                "model_id": model_id,
                "deployment_id": dep_id,
                "workload_id": workload_id,
            })
            self.metrics.increment("flux.replica.provisioned")

    def rebalance_all_miners(self) -> dict[str, FluxState]:
        """Rebalance all tracked miners. Called from the worker loop.
        Computes the fleet-wide replica targets up-front so every per-miner
        rebalance sees the same target map."""
        self._replica_targets = self.compute_replica_targets()
        results: dict[str, FluxState] = {}
        for hotkey in list(self._flux_states):
            new_state, _ = self.rebalance_miner(hotkey)
            results[hotkey] = new_state
        return results

    # --- Demand-reactive replica targets (Phase 2I) --------------------

    # --- Inference attestation canary (Phase 2F) -----------------------

    def run_inference_canary(self, hotkey: str, model_id: str) -> ProbeResult:
        """Fire a canary chat-completion against the gateway and score the
        response. Result gets persisted as a ProbeResult so existing
        reliability / fraud-penalty scoring picks it up — no new scoring
        math needed. Records latency, shape-compliance signature, and
        whether the serving miner matched the expected hotkey."""
        import hashlib
        import json
        import time as _time
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        cap = self.repository.get_capability(hotkey)
        if cap is None:
            raise UnknownCapabilityError(f"capability not found for hotkey={hotkey}")

        challenge = self.create_probe(hotkey, cap.node_id, kind="inference_verification")

        gw = validator_settings.gateway_url
        key = validator_settings.inference_canary_api_key
        if not gw or not key:
            # No gateway wired → record a no-op failure rather than raising,
            # so admin triggering the endpoint sees a clear signal.
            result = ProbeResult(
                challenge_id=challenge.challenge_id,
                hotkey=hotkey,
                node_id=cap.node_id,
                latency_ms=0.0,
                throughput=0.0,
                success=False,
                readiness_failures=1,
            )
            return self.repository.add_result(result)

        body = {
            "model": model_id,
            "messages": [
                {"role": "user", "content": "Reply exactly: OK"},
            ],
            "max_tokens": 8,
            "stream": False,
        }
        data = json.dumps(body).encode()
        req = Request(
            gw.rstrip("/") + "/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": key,
            },
            method="POST",
        )
        started = _time.perf_counter()
        latency_ms = 0.0
        success = False
        signature: str | None = None
        tokens = 0
        readiness_failures = 0
        try:
            with urlopen(req, timeout=validator_settings.inference_canary_timeout_seconds) as resp:
                payload = json.loads(resp.read())
            latency_ms = (_time.perf_counter() - started) * 1000.0
            choices = payload.get("choices") or []
            content = (choices[0].get("message") or {}).get("content") if choices else None
            if content and isinstance(content, str):
                success = True
                norm = content.strip().upper()[:32]
                signature = hashlib.sha256(f"{model_id}:{norm}".encode()).hexdigest()[:16]
            usage = payload.get("usage") or {}
            tokens = int(usage.get("completion_tokens", 0) or 0)
        except HTTPError as exc:
            latency_ms = (_time.perf_counter() - started) * 1000.0
            readiness_failures = 1
            logger.warning("inference canary http %s for %s: %s", exc.code, model_id, exc.reason)
        except Exception as exc:  # noqa: BLE001 — broad is fine, we're scoring a probe
            latency_ms = (_time.perf_counter() - started) * 1000.0
            readiness_failures = 1
            logger.warning("inference canary error for %s: %s", model_id, exc)

        throughput = (tokens / max(latency_ms, 1.0)) * 1000.0 if success else 0.0
        result = ProbeResult(
            challenge_id=challenge.challenge_id,
            hotkey=hotkey,
            node_id=cap.node_id,
            latency_ms=latency_ms,
            throughput=throughput,
            success=success,
            benchmark_signature=signature,
            readiness_failures=readiness_failures,
        )
        saved = self.repository.add_result(result)
        self.metrics.increment(f"probe.inference.{'success' if success else 'failure'}")
        return saved

    # --- Dashboard snapshot (Phase 2J) ---------------------------------

    def build_flux_dashboard(self) -> dict:
        """Single-shot snapshot bundle for the admin /flux dashboard.
        Returns fleet-wide tiles, per-model catalog pool status, and a
        per-miner summary. UI polls this every 5s."""
        now = datetime.now(UTC)
        capabilities = self.repository.list_capabilities()
        scorecards = self.repository.list_scorecards()

        # Fleet strip — derived from the in-memory Flux state map
        total_gpus = 0
        inference_gpus = 0
        rental_gpus = 0
        idle_gpus = 0
        active_catalog_replicas = 0
        for state in self._flux_states.values():
            total_gpus += state.total_gpus
            inference_gpus += state.inference_gpus
            rental_gpus += state.rental_gpus
            idle_gpus += state.idle_gpus
            for gpu_idxs in state.inference_assignments.values():
                active_catalog_replicas += 1 if gpu_idxs else 0

        # Catalog pool — per-model replica counts and demand
        catalog_pool: list[dict] = []
        running_by_model: dict[str, int] = {}
        for state in self._flux_states.values():
            for model_id, idxs in state.inference_assignments.items():
                if idxs:
                    running_by_model[model_id] = running_by_model.get(model_id, 0) + 1
        for entry in self.repository.list_catalog_entries(visibility="public"):
            windows = self.repository.read_demand_windows(entry.model_id, now=now)
            running = running_by_model.get(entry.model_id, 0)
            target = self._replica_targets.get(entry.model_id, entry.min_replicas)
            serving_miners = [
                state.hotkey
                for state in self._flux_states.values()
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

        # Miner fleet summary
        miner_fleet: list[dict] = []
        for hotkey, cap in sorted(capabilities.items()):
            state = self._flux_states.get(hotkey)
            assigned = list(state.inference_assignments.keys()) if state else []
            sc = scorecards.get(hotkey)
            miner_fleet.append({
                "hotkey": hotkey,
                "node_id": cap.node_id,
                "gpu_model": cap.gpu_model,
                "gpu_count": cap.gpu_count,
                "inference_gpus": state.inference_gpus if state else 0,
                "rental_gpus": state.rental_gpus if state else 0,
                "idle_gpus": state.idle_gpus if state else cap.gpu_count,
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
                "miners_online": len(self._flux_states),
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

        from greenference_persistence import session_scope as _session_scope
        from greenference_persistence.orm import InferenceDemandStatsORM

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
        catalog = self.repository.list_catalog_entries(visibility="public")
        for entry in catalog:
            windows = self.repository.read_demand_windows(entry.model_id, now=now)
            rpm_10 = windows["rpm_10m"]
            rpm_60 = windows["rpm_1h"]
            blended = 0.7 * rpm_10 + 0.3 * rpm_60
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
        return targets

    def estimate_rental_wait(self, deployment_id: str, hotkey: str) -> RentalWaitEstimate:
        """Estimate wait time for a rental deployment on a specific miner."""
        state = self._flux_states.get(hotkey)
        if state is None:
            return RentalWaitEstimate(
                deployment_id=deployment_id,
                estimated_wait_seconds=0.0,
                position_in_queue=0,
            )
        self.wait_estimator.enqueue(deployment_id)
        return self.wait_estimator.estimate(deployment_id, state)


service = ValidatorService()

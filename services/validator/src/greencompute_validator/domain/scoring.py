from __future__ import annotations

from math import sqrt
from statistics import median

from greencompute_protocol import FluxState, NodeCapability, ProbeResult, ScoreCard
from greencompute_validator.config import settings


class ScoreEngine:
    def compute_scorecard(
        self,
        capability: NodeCapability,
        results: list[ProbeResult],
        flux_state: FluxState | None = None,
    ) -> ScoreCard:
        capacity_weight = capability.gpu_count * capability.vram_gb_per_gpu
        reliability = self._reliability_score(capability, results)
        performance = self._performance_score(capability, results)
        security = 1.0
        fraud_penalty = self._fraud_penalty(results)
        utilization = self._utilization_factor(flux_state)
        rental_bonus = self._rental_revenue_bonus(flux_state)
        final_score = (
            capacity_weight
            * (security**settings.score_alpha)
            * (reliability**settings.score_beta)
            * (performance**settings.score_gamma)
            * fraud_penalty
            * (utilization**settings.score_delta)
            * (1.0 + rental_bonus)
        )
        return ScoreCard(
            hotkey=capability.hotkey,
            capacity_weight=capacity_weight,
            reliability_score=reliability,
            performance_score=performance,
            security_score=security,
            fraud_penalty=fraud_penalty,
            utilization_score=utilization,
            rental_revenue_bonus=rental_bonus,
            final_score=round(final_score, 6),
        )

    @staticmethod
    def _utilization_factor(flux_state: FluxState | None) -> float:
        """(inference_gpus + rental_gpus) / total_gpus — how busy the node is."""
        if flux_state is None or flux_state.total_gpus == 0:
            return 1.0  # No flux data — neutral
        used = flux_state.inference_gpus + flux_state.rental_gpus
        return round(max(used / flux_state.total_gpus, 0.01), 6)

    @staticmethod
    def _rental_revenue_bonus(flux_state: FluxState | None) -> float:
        """Small bonus for nodes actively serving rentals, capped."""
        if flux_state is None or flux_state.total_gpus == 0:
            return 0.0
        rental_ratio = flux_state.rental_gpus / flux_state.total_gpus
        return round(min(rental_ratio * 0.2, settings.rental_revenue_bonus_cap), 6)

    def _reliability_score(self, capability: NodeCapability, results: list[ProbeResult]) -> float:
        if not results:
            return round(max(capability.reliability_score * 0.5, 0.01), 6)
        success_rate = sum(1 for result in results if result.success) / len(results)
        readiness_penalty = max(0.2, 1.0 - (sum(result.readiness_failures for result in results) * 0.04))
        return round(max(capability.reliability_score * success_rate * readiness_penalty, 0.01), 6)

    def _performance_score(self, capability: NodeCapability, results: list[ProbeResult]) -> float:
        if not results:
            return max(capability.performance_score * 0.5, 0.01)
        latencies = [result.latency_ms for result in results if result.success]
        throughputs = [result.throughput for result in results if result.success]
        if not latencies or not throughputs:
            return 0.01
        median_latency = median(latencies)
        median_throughput = median(throughputs)
        latency_component = min(1.0, 1000.0 / max(median_latency, 1.0))
        throughput_component = min(2.0, median_throughput / 100.0)
        return round(max((latency_component * 0.5) + (throughput_component * 0.5), 0.01), 6)

    def _fraud_penalty(self, results: list[ProbeResult]) -> float:
        if not results:
            # No probe data → neutral fraud multiplier. fraud_penalty is a
            # MULTIPLIER (1.0 == no fraud detected); returning 0.0 here would
            # zero the entire final_score. The no-data penalty is already
            # carried by reliability/performance (which halve when empty).
            return 1.0
        signature_set = {result.benchmark_signature for result in results if result.benchmark_signature}
        signature_penalty = 0.75 if len(signature_set) > 1 else 1.0
        proxy_penalty = 0.4 if any(result.proxy_suspected for result in results) else 1.0
        consistency_penalty = self._consistency_penalty(results)
        readiness_penalty = max(0.2, 1.0 - (sum(result.readiness_failures for result in results) * 0.03))
        success_penalty = max(0.2, sum(1 for result in results if result.success) / len(results))
        return round(signature_penalty * proxy_penalty * consistency_penalty * readiness_penalty * success_penalty, 6)

    def _consistency_penalty(self, results: list[ProbeResult]) -> float:
        successful = [result for result in results if result.success]
        if len(successful) < 2:
            return 1.0
        latencies = [result.latency_ms for result in successful]
        throughputs = [result.throughput for result in successful]
        latency_spread = self._coefficient_of_variation(latencies)
        throughput_spread = self._coefficient_of_variation(throughputs)
        spread = max(latency_spread, throughput_spread)
        if spread >= 0.6:
            return 0.7
        if spread >= 0.3:
            return 0.85
        return 1.0

    @staticmethod
    def _coefficient_of_variation(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        if mean <= 0:
            return 0.0
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return sqrt(variance) / mean

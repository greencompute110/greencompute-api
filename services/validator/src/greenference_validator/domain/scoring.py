from __future__ import annotations

from statistics import median

from greenference_protocol import NodeCapability, ProbeResult, ScoreCard
from greenference_validator.config import settings


class ScoreEngine:
    def compute_scorecard(self, capability: NodeCapability, results: list[ProbeResult]) -> ScoreCard:
        capacity_weight = capability.gpu_count * capability.vram_gb_per_gpu
        reliability = capability.reliability_score
        performance = self._performance_score(capability, results)
        security = 1.0 if capability.security_tier.value == "standard" else 1.1
        fraud_penalty = self._fraud_penalty(results)
        final_score = (
            capacity_weight
            * (security**settings.score_alpha)
            * (reliability**settings.score_beta)
            * (performance**settings.score_gamma)
            * fraud_penalty
        )
        return ScoreCard(
            hotkey=capability.hotkey,
            capacity_weight=capacity_weight,
            reliability_score=reliability,
            performance_score=performance,
            security_score=security,
            fraud_penalty=fraud_penalty,
            final_score=round(final_score, 6),
        )

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
            return 0.0
        signature_set = {result.benchmark_signature for result in results if result.benchmark_signature}
        signature_penalty = 0.8 if len(signature_set) > 1 else 1.0
        proxy_penalty = 0.4 if any(result.proxy_suspected for result in results) else 1.0
        readiness_penalty = max(0.2, 1.0 - (sum(result.readiness_failures for result in results) * 0.05))
        success_penalty = sum(1 for result in results if result.success) / len(results)
        return round(signature_penalty * proxy_penalty * readiness_penalty * success_penalty, 6)


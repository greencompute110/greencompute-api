"""Per-model upstream timeouts.

The 120s default is sized for 7B-class models. Kimi K3 is a reasoning model on 72
consumer GPUs: it emits chain-of-thought before the answer, so a normal completion
runs 1-3 minutes and every non-streaming request died at exactly 120.2s with a 502,
regardless of the client's own timeout. Streaming masked it because the socket keeps
reading -- so the failure only hit the non-streaming shape most agent frameworks use.
"""
from greencompute_gateway.infrastructure.inference_client import (
    MODEL_UPSTREAM_TIMEOUT_SECONDS,
    HttpInferenceClient,
)


class _Payload:
    def __init__(self, model):
        self.model = model


def _client(**kw):
    return HttpInferenceClient(**kw)


def test_k3_gets_long_enough_to_finish_thinking():
    """A K3 completion runs 1-3 minutes; 120s guarantees a 502. Long-context
    requests are far slower still -- a ~104k-token prefill alone exceeded 340s --
    so the budget has to cover prefill AND generation, not just generation."""
    assert _client()._timeout_for(_Payload("kimi-k3")) == 1800.0


def test_vendor_prefixed_id_gets_the_same_timeout():
    """Clients send either the catalog id or the HF repo. If the prefixed form
    missed the table, the identical request would 502 purely because of how the
    caller spelled the model -- the same normalisation bug already fixed in billing."""
    c = _client()
    assert c._timeout_for(_Payload("moonshotai/Kimi-K3")) == 1800.0
    assert c._timeout_for(_Payload("MoonshotAI/KIMI-K3")) == 1800.0


def test_other_models_keep_the_short_default():
    """The long timeout must stay scoped. /v1/chat/completions is a sync endpoint
    served from a 40-thread pool; letting every model hold a thread for 10 minutes
    would trade a K3 bug for a fleet-wide outage."""
    assert _client()._timeout_for(_Payload("qwen2.5-7b-instruct")) == 120.0
    assert _client()._timeout_for(_Payload(None)) == 120.0


def test_operator_configured_timeout_is_never_shortened():
    """An operator who sets a longer global timeout keeps it -- the per-model
    entry raises the floor, it does not cap."""
    assert _client(upstream_timeout_seconds=2400.0)._timeout_for(_Payload("kimi-k3")) == 2400.0
    assert _client(upstream_timeout_seconds=2400.0)._timeout_for(_Payload("qwen2.5-7b")) == 2400.0


def test_health_checks_stay_fast():
    """Health probes must not inherit the long timeout: a hung upstream would
    otherwise stall liveness detection for ten minutes instead of two seconds."""
    assert _client().health_timeout_seconds == 2.0


def test_every_override_exceeds_the_default():
    """An override below the default is a typo and would shorten, not lengthen."""
    c = _client()
    for model, seconds in MODEL_UPSTREAM_TIMEOUT_SECONDS.items():
        assert seconds > c.upstream_timeout_seconds, f"{model} override is shorter than the default"

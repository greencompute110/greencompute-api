"""Externally-hosted models on the public OpenAI-compatible surface.

Some models run on hardware the platform does not manage (the 48-GPU SGLang
cluster). Routing them through the gateway -- rather than exposing a second,
unmetered endpoint -- means they inherit API-key auth, the balance gate,
per-token billing and the streaming path for free, and callers select them by
`model` against ONE base URL, OpenRouter-style.
"""
import os

from greencompute_gateway.application.services import _external_model_upstreams


def _with(raw):
    os.environ["GREENCOMPUTE_EXTERNAL_MODEL_UPSTREAMS"] = raw
    try:
        return _external_model_upstreams()
    finally:
        os.environ.pop("GREENCOMPUTE_EXTERNAL_MODEL_UPSTREAMS", None)


def test_parses_a_single_entry():
    assert _with("k3-coder=http://127.0.0.1:31000") == {"k3-coder": "http://127.0.0.1:31000"}


def test_parses_multiple_entries():
    got = _with("a=http://x:1,b=http://y:2")
    assert got == {"a": "http://x:1", "b": "http://y:2"}


def test_model_ids_are_case_insensitive_and_urls_lose_trailing_slash():
    """The client appends '/v1/chat/completions', so a trailing slash would
    produce a double slash against some servers."""
    assert _with("K3-Coder=http://h:1/") == {"k3-coder": "http://h:1"}


def test_unset_or_malformed_config_is_inert():
    """A missing or fat-fingered value must not break normal model routing."""
    assert _with("") == {}
    assert _with("no-equals-sign") == {}
    assert _with("=http://x:1") == {}
    assert _with("id=") == {}


def test_whitespace_is_tolerated():
    assert _with(" a = http://x:1 , b = http://y:2 ") == {"a": "http://x:1", "b": "http://y:2"}


def test_external_models_are_checked_before_workload_lookup():
    """They have no workload row, deployment, or health probe -- resolving them
    through the normal path would 404 as an unknown model."""
    import inspect
    from greencompute_gateway.application.services import GatewayService
    src = inspect.getsource(GatewayService._select_healthy_deployments)
    assert src.index("_external_model_upstreams") < src.index("resolve_workload_reference")


def test_external_route_is_billed_like_any_other_model():
    """Routing through the gateway is what preserves metering; a model with no
    rate entry would silently bill at the 7B default."""
    from greencompute_protocol import rates_for_model
    from greencompute_protocol.billing_rates import INFERENCE_OUTPUT_CENTS_PER_MTOK
    assert rates_for_model("k3-coder")[1] > INFERENCE_OUTPUT_CENTS_PER_MTOK

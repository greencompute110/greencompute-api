"""Per-model inference pricing.

The flat $0.20/$0.60 rate is sized for 7B-class models that run on ONE GPU
($0.70/hr, >1k tok/s => ~3x margin). Kimi K3 pins 72 GPUs for a single replica,
so on the flat rate it billed $0.60 per 1M output tokens against a measured
cost of ~$162 — publishing it to the catalog created a live ~270x-underwater
SKU reachable by 219 API keys.
"""
from greencompute_protocol import inference_cost_cents, rates_for_model
from greencompute_protocol.billing_rates import (
    INFERENCE_INPUT_CENTS_PER_MTOK,
    INFERENCE_OUTPUT_CENTS_PER_MTOK,
    MODEL_RATE_CENTS_PER_MTOK,
)

MTOK = 1_000_000


def test_kimi_k3_bills_at_market_parity():
    """$3.00 in / $15.00 out — the price every other K3 provider charges."""
    assert inference_cost_cents(MTOK, 0, model="kimi-k3") == 300      # $3.00
    assert inference_cost_cents(0, MTOK, model="kimi-k3") == 1500     # $15.00


def test_vendor_prefixed_model_id_bills_the_same():
    """Clients may send the catalog id or the HF repo. If the prefixed form
    missed the table it would silently fall through to the 7B default — a
    25x under-bill triggered purely by how the caller spelled the model."""
    assert rates_for_model("moonshotai/Kimi-K3") == rates_for_model("kimi-k3")
    assert rates_for_model("MoonshotAI/KIMI-K3") == rates_for_model("kimi-k3")


def test_unlisted_models_keep_the_flat_default():
    """The default is CORRECT for single-GPU models — this must not change it."""
    assert rates_for_model("qwen2.5-7b-instruct") == (
        INFERENCE_INPUT_CENTS_PER_MTOK, INFERENCE_OUTPUT_CENTS_PER_MTOK
    )
    assert inference_cost_cents(0, MTOK, model="qwen2.5-7b-instruct") == 60  # $0.60


def test_omitting_the_model_is_backward_compatible():
    # Old callers keep working, at the default rate.
    assert inference_cost_cents(0, MTOK) == 60
    assert rates_for_model(None) == (
        INFERENCE_INPUT_CENTS_PER_MTOK, INFERENCE_OUTPUT_CENTS_PER_MTOK
    )


def test_k3_output_costs_25x_the_default():
    """Guards the specific mistake: K3 quietly billed at 7B rates."""
    k3_out = rates_for_model("kimi-k3")[1]
    assert k3_out == 25 * INFERENCE_OUTPUT_CENTS_PER_MTOK


def test_every_override_is_dearer_than_the_default():
    """An override exists to price a model ABOVE the single-GPU baseline. One
    cheaper than the default is almost certainly a typo (e.g. cents vs dollars)
    and would under-bill silently."""
    for model, (inp, out) in MODEL_RATE_CENTS_PER_MTOK.items():
        assert inp >= INFERENCE_INPUT_CENTS_PER_MTOK, f"{model} input below default"
        assert out >= INFERENCE_OUTPUT_CENTS_PER_MTOK, f"{model} output below default"
        assert out > inp, f"{model}: output must cost more than input"


def test_a_realistic_k3_reasoning_request():
    # K3 thinks before answering, so completions dominate: 500 in / 800 out.
    # 500*300/1M = 0.15c, 800*1500/1M = 1.20c => 1.35c, rounds to 1c.
    assert inference_cost_cents(500, 800, model="kimi-k3") == 1


def test_rounding_is_symmetric_not_systematically_in_the_users_favour():
    """Integer-cent billing must not bleed revenue on every request."""
    # 1.35c -> 1c (down), 1.65c -> 2c (up): half-up, so it averages out.
    below = inference_cost_cents(500, 800, model="kimi-k3")       # 1.35c
    above = inference_cost_cents(500, 1000, model="kimi-k3")      # 1.65c
    assert below == 1 and above == 2


def test_large_k3_request_bills_proportionally():
    # 100k output at $15/1M = $1.50 = 150 cents.
    assert inference_cost_cents(0, 100_000, model="kimi-k3") == 150


def test_gateway_passes_the_model_on_both_billing_paths():
    """A hold reserved at 7B rates for a K3 request would let a near-empty
    balance fan out into expensive completions."""
    import inspect
    from greencompute_gateway.application import services
    charge = inspect.getsource(services.GatewayService._charge_inference_tokens)
    assert "model=model" in charge, "settle path must bill at the model's rate"
    hold = inspect.getsource(services.GatewayService._reserve_inference_budget)
    assert "model=request.model" in hold, "hold must be estimated at the model's rate"

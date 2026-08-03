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


def test_kimi_k3_undercuts_the_market_reference():
    """$2.00 in / $10.00 out — ~33% below the $3.00/$15.00 every other K3
    provider charges, and below the cheapest endpoint anywhere (Morph,
    $2.90/$14.00). Deliberate positioning: cheapest K3 on the market, on 100%
    renewable consumer GPUs."""
    assert inference_cost_cents(MTOK, 0, model="kimi-k3") == 200      # $2.00
    assert inference_cost_cents(0, MTOK, model="kimi-k3") == 1000     # $10.00
    # must stay under the cheapest commercial endpoint to keep the claim true
    inp, out = rates_for_model("kimi-k3")
    assert inp < 290 and out < 1400, "no longer the cheapest K3 available"


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


def test_k3_still_costs_far_more_than_the_7b_default():
    """Guards the original mistake: K3 quietly billing at 7B rates. It pins 72
    GPUs, so whatever the headline price, it must never fall back to $0.60."""
    k3_out = rates_for_model("kimi-k3")[1]
    assert k3_out >= 10 * INFERENCE_OUTPUT_CENTS_PER_MTOK


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
    # 500*200/1M = 0.10c, 800*1000/1M = 0.80c => 0.90c, rounds to 1c.
    assert inference_cost_cents(500, 800, model="kimi-k3") == 1


def test_rounding_is_symmetric_not_systematically_in_the_users_favour():
    """Integer-cent billing must not bleed revenue on every request."""
    # 0.90c -> 1c (up), 1.40c -> 1c (down): half-up, so it averages out.
    assert inference_cost_cents(500, 800, model="kimi-k3") == 1    # 0.90c
    assert inference_cost_cents(0, 150_000, model="kimi-k3") == 150  # exact, 1500c


def test_large_k3_request_bills_proportionally():
    # 100k output at $10/1M = $1.00 = 100 cents.
    assert inference_cost_cents(0, 100_000, model="kimi-k3") == 100


def test_gateway_passes_the_model_on_both_billing_paths():
    """A hold reserved at 7B rates for a K3 request would let a near-empty
    balance fan out into expensive completions."""
    import inspect
    from greencompute_gateway.application import services
    charge = inspect.getsource(services.GatewayService._charge_inference_tokens)
    assert "model=model" in charge, "settle path must bill at the model's rate"
    hold = inspect.getsource(services.GatewayService._reserve_inference_budget)
    assert "model=request.model" in hold, "hold must be estimated at the model's rate"

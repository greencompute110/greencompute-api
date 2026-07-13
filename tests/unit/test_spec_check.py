"""Deterministic provider-spec conformance evaluator (Phase 1 automated
onboarding). It must confidently PASS in-spec declarations, FAIL clearly
out-of-spec ones, and return UNCLEAR (never a false fail) on fuzzy free text.
"""
from greencompute_validator.domain.spec_check import evaluate_provider_specs


def _details(gpu="8x RTX 4090", cpu="Dual Intel Xeon Gold", ram="512GB",
             storage="8TB NVMe", up="10 Gbps", down="10 Gbps"):
    return {
        "nodes": [{"chassis": "Supermicro", "cpu": cpu, "ram": ram,
                   "storage": storage, "gpu": gpu, "quantity": "1"}],
        "infrastructure": {"upload_speed": up, "download_speed": down},
    }


def test_fully_in_spec_is_conformant():
    a = evaluate_provider_specs(_details())
    assert a["conformant"] is True
    assert set(a["checks"].values()) == {"pass"}
    assert a["flags"] == []


def test_5090_also_passes():
    a = evaluate_provider_specs(_details(gpu="8 x RTX 5090"))
    assert a["checks"]["gpu"] == "pass"


def test_wrong_gpu_model_fails():
    a = evaluate_provider_specs(_details(gpu="8x RTX 3090"))
    assert a["conformant"] is False
    assert a["checks"]["gpu"] == "fail"
    assert any("not an RTX 4090/5090" in f for f in a["flags"])


def test_wrong_gpu_count_fails():
    a = evaluate_provider_specs(_details(gpu="4x RTX 4090"))
    assert a["checks"]["gpu"] == "fail"


def test_insufficient_ram_and_storage_fail():
    a = evaluate_provider_specs(_details(ram="256 GB", storage="4 TB"))
    assert a["checks"]["ram"] == "fail"
    assert a["checks"]["storage"] == "fail"
    assert a["conformant"] is False


def test_ram_in_tb_passes():
    a = evaluate_provider_specs(_details(ram="1 TB"))
    assert a["checks"]["ram"] == "pass"


def test_single_cpu_fails_dual_passes():
    assert evaluate_provider_specs(_details(cpu="Single AMD EPYC"))["checks"]["cpu"] == "fail"
    assert evaluate_provider_specs(_details(cpu="2x AMD EPYC 9004"))["checks"]["cpu"] == "pass"


def test_sub_10g_network_fails():
    a = evaluate_provider_specs(_details(up="1 Gbps", down="1 Gbps"))
    assert a["checks"]["network"] == "fail"


def test_mbps_symmetric_below_threshold_fails():
    a = evaluate_provider_specs(_details(up="1000 Mbps", down="1000 Mbps"))  # 1 Gbps
    assert a["checks"]["network"] == "fail"


def test_ambiguous_free_text_is_unclear_not_fail():
    # A legit provider phrasing things vaguely must NOT be auto-failed.
    a = evaluate_provider_specs({
        "nodes": [{"gpu": "eight high-end GPUs", "cpu": "enterprise CPUs",
                   "ram": "half a terabyte", "storage": "plenty", "quantity": ""}],
        "infrastructure": {"upload_speed": "fast", "download_speed": "fast"},
    })
    assert a["conformant"] is None  # routes to human, never a confident fail
    assert "fail" not in a["checks"].values()


def test_missing_nodes_is_unclear():
    a = evaluate_provider_specs({"infrastructure": {}})
    assert a["conformant"] is None
    assert a["checks"]["gpu"] == "unclear"


def test_never_raises_on_junk():
    for junk in [{}, {"nodes": "not-a-list"}, {"nodes": [None, 5]}, None if False else {"nodes": [{}]}]:
        evaluate_provider_specs(junk)  # must not raise


# --- regressions for the confirmed spec_check defects (2026-07-13) ------------


def _node_details(**over):
    node = {"gpu": "RTX 4090", "cpu": "Dual Xeon", "ram": "512GB", "storage": "8TB", "quantity": "8"}
    node.update(over)
    return {"nodes": [node], "infrastructure": {"upload_speed": "10 Gbps", "download_speed": "10 Gbps"}}


def test_rtx_4090_with_quantity_field_passes():
    # The 'x' in "RTX" must NOT be read as the GPU-count multiplier.
    a = evaluate_provider_specs(_node_details(gpu="RTX 4090", quantity="8"))
    assert a["checks"]["gpu"] == "pass"
    assert a["conformant"] is True


def test_rtx_4090_x8_suffix_passes():
    a = evaluate_provider_specs(_node_details(gpu="RTX 4090 x8", quantity=""))
    assert a["checks"]["gpu"] == "pass"


def test_dual_cpu_2x_does_not_bleed_into_gpu_count():
    a = evaluate_provider_specs(_node_details(gpu="RTX 4090", cpu="2x AMD EPYC 9004", quantity="8"))
    assert a["checks"]["gpu"] == "pass"
    assert a["checks"]["cpu"] == "pass"


def test_disallowed_gpu_masked_with_4090_still_fails():
    a = evaluate_provider_specs(_details(gpu="8x RTX 3090 (near 4090-class)"))
    assert a["checks"]["gpu"] == "fail"


def test_8000gb_storage_is_treated_as_8tb():
    assert evaluate_provider_specs(_details(storage="8000 GB"))["checks"]["storage"] == "pass"


def test_oversized_digit_run_does_not_raise():
    huge = "9" * 5000
    evaluate_provider_specs({"nodes": [{"gpu": huge + "x RTX 4090", "quantity": huge}],
                             "infrastructure": {}})  # must not raise

"""On-chain weight planning under the manual-quarterly-payout policy.

Accumulation ON (default): 100% weight to the team accumulator hotkey; miners
do NOT earn on-chain. Accumulation OFF: per-miner distribution by final_score.
"""
from greencompute_validator.domain.payout import plan_chain_weights

ACC = "5FmpATtoNvMUisuqPNeanXxXDkhaDpYJShCNi4Xm4cXkwWKH"


def _uid_map(mapping):
    return lambda hk: mapping.get(hk)


def test_accumulation_routes_all_weight_to_accumulator():
    plan = plan_chain_weights(
        accumulation_enabled=True,
        accumulator_hotkey=ACC,
        scores={"5MINER_A": 100.0, "5MINER_B": 50.0},
        hotkey_to_uid=_uid_map({ACC: 155, "5MINER_A": 42, "5MINER_B": 7}),
    )
    assert plan == ([155], [1.0])  # miners get nothing on-chain


def test_accumulator_missing_sets_no_weights():
    # Fail-safe: never fall back to paying miners if the accumulator isn't registered.
    plan = plan_chain_weights(
        accumulation_enabled=True,
        accumulator_hotkey=ACC,
        scores={"5MINER_A": 100.0},
        hotkey_to_uid=_uid_map({"5MINER_A": 42}),  # ACC absent
    )
    assert plan is None


def test_distribution_mode_weights_miners_by_score():
    plan = plan_chain_weights(
        accumulation_enabled=False,
        accumulator_hotkey=ACC,
        scores={"5A": 0.9, "5B": 0.4},
        hotkey_to_uid=_uid_map({"5A": 1, "5B": 2}),
    )
    assert plan == ([1, 2], [0.9, 0.4])  # sorted by hotkey


def test_distribution_skips_unregistered_miners():
    plan = plan_chain_weights(
        accumulation_enabled=False,
        accumulator_hotkey=ACC,
        scores={"5A": 0.9, "5GHOST": 0.4},
        hotkey_to_uid=_uid_map({"5A": 1}),  # 5GHOST not in metagraph
    )
    assert plan == ([1], [0.9])


def test_distribution_none_when_no_registered_miners():
    plan = plan_chain_weights(
        accumulation_enabled=False,
        accumulator_hotkey=ACC,
        scores={"5GHOST": 0.4},
        hotkey_to_uid=_uid_map({}),
    )
    assert plan is None

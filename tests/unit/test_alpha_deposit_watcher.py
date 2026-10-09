"""Decode tests for the Alpha (subnet token) deposit scanner.

Customers top up with alpha by `transfer_stake`-ing it to our deposit
coldkey. That extrinsic emits, among others:

    StakeTransferred(origin_coldkey, destination_coldkey, hotkey,
                     origin_netuid, destination_netuid, tao_moved)
    StakeAdded(destination_coldkey, hotkey, tao_equivalent, alpha, netuid, fee)

StakeTransferred's amount is TAO, not alpha, so the credit must come from
StakeAdded. The fixtures below are the real events of a $5 alpha top-up on
mainnet (block 9174691, 2026-09-29) that was never credited because the
scanner read the TAO figure. (The credit/dedup machinery is shared with
scan_tao and covered by test_deposit_double_credit.)
"""
from greencompute_gateway.infrastructure.deposit_watcher import (
    _alpha_deposits,
    _extract_alpha_transfer,
)

OUR = "5FFzLoiNKzXVgG4Rvr4wR2NeuymvFZTptCujX6abGPaUgqwN"
SENDER = "5H3CPzYJnjQKxsM8QGWnMZMwn8h641fW34jqhBy84KpLnNR8"
HOTKEY = "5GKH9FPPnWSUoeeTJp19wVtd84XqFW4pyK2ijV2GsFbhTrP1"
ADDRS = {OUR}
NETUID = 110


def _ev(event, attributes, xidx=5):
    return {"module_id": "SubtensorModule", "event_id": event, "attributes": attributes, "extrinsic_idx": xidx}


def _real_topup(xidx=5):
    return [
        _ev("StakeAdded", [OUR, HOTKEY, 16_486_440, 985_006_000, NETUID, 0], xidx),
        _ev("StakeTransferred", [SENDER, OUR, HOTKEY, NETUID, NETUID, 16_486_440], xidx),
    ]


def test_real_mainnet_topup_is_valued_in_alpha_not_tao():
    deposits = _alpha_deposits(_real_topup(), ADDRS, NETUID)
    # 0.985006 alpha was invoiced and received; 0.01648644 is its TAO value.
    assert deposits == [(1, OUR, 0.985006, SENDER)]


def test_dict_attributes_decode_the_same():
    events = [
        _ev("StakeAdded", {"coldkey": OUR, "hotkey": HOTKEY, "tao": 16_486_440,
                           "alpha": 985_006_000, "netuid": NETUID, "fee": 0}),
        _ev("StakeTransferred", {"origin_coldkey": SENDER, "destination_coldkey": OUR,
                                 "hotkey": HOTKEY, "origin_netuid": NETUID,
                                 "destination_netuid": NETUID, "amount": 16_486_440}),
    ]
    assert _alpha_deposits(events, ADDRS, NETUID) == [(1, OUR, 0.985006, SENDER)]


def test_stake_added_from_another_extrinsic_is_not_borrowed():
    events = [
        _ev("StakeAdded", [OUR, HOTKEY, 1, 777_000_000, NETUID, 0], xidx=2),
        _ev("StakeTransferred", [SENDER, OUR, HOTKEY, NETUID, NETUID, 16_486_440], xidx=5),
    ]
    assert _alpha_deposits(events, ADDRS, NETUID) == []


def test_our_own_staking_is_not_a_deposit():
    # We stake to a validator ourselves: StakeAdded for our coldkey, no transfer.
    assert _alpha_deposits([_ev("StakeAdded", [OUR, HOTKEY, 1, 5, NETUID, 0])], ADDRS, NETUID) == []


def test_wrong_subnet_is_skipped():
    # Alpha of another subnet is priced differently — must NOT credit.
    events = [
        _ev("StakeAdded", [OUR, HOTKEY, 16_486_440, 985_006_000, 42, 0]),
        _ev("StakeTransferred", [SENDER, OUR, HOTKEY, 42, 42, 16_486_440]),
    ]
    assert _alpha_deposits(events, ADDRS, NETUID) == []


def test_transfer_to_other_address_is_skipped():
    events = [
        _ev("StakeAdded", ["5SomeoneElse", HOTKEY, 16_486_440, 985_006_000, NETUID, 0]),
        _ev("StakeTransferred", [SENDER, "5SomeoneElse", HOTKEY, NETUID, NETUID, 16_486_440]),
    ]
    assert _alpha_deposits(events, ADDRS, NETUID) == []


def test_extractor_ignores_other_events_and_malformed_input():
    assert _extract_alpha_transfer(
        {"module_id": "Balances", "event_id": "Transfer", "attributes": ["a", OUR, 1]}, ADDRS, NETUID
    ) is None
    assert _extract_alpha_transfer({"module_id": "SubtensorModule"}, ADDRS, NETUID) is None
    assert _extract_alpha_transfer(_ev("StakeTransferred", ["too", "short"]), ADDRS, NETUID) is None
    assert _extract_alpha_transfer("not-a-dict", ADDRS, NETUID) is None
    assert _alpha_deposits(["not-a-dict", None], ADDRS, NETUID) == []

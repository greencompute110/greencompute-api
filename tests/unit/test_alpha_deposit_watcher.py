"""Decode tests for the Alpha (subnet token) deposit scanner.

Customers top up with alpha by `transfer_stake`-ing it to our deposit
coldkey, emitting SubtensorModule.StakeTransferred(origin_coldkey,
destination_coldkey, hotkey, origin_netuid, destination_netuid, amount_rao).
scan_alpha must credit only transfers to OUR address on OUR subnet, valued
at 9-decimal RAO. (The credit/dedup machinery is shared with scan_tao and
covered by test_deposit_double_credit.)
"""
from greencompute_gateway.infrastructure.deposit_watcher import _extract_alpha_transfer

OUR = "5DepositColdkeyAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ADDRS = {OUR}
NETUID = 110


def _ev(attributes, module="SubtensorModule", event="StakeTransferred"):
    return {"module_id": module, "event_id": event, "attributes": attributes}


def test_list_attrs_transfer_to_us_on_our_subnet():
    # (origin, dest, hotkey, origin_netuid, dest_netuid, amount_rao)
    ev = _ev(["5Sender", OUR, "5Hotkey", 0, NETUID, 2_500_000_000])
    out = _extract_alpha_transfer(ev, ADDRS, NETUID)
    assert out == (OUR, 2.5, "5Sender")


def test_dict_attrs_named_fields():
    ev = _ev({
        "origin_coldkey": "5Sender",
        "destination_coldkey": OUR,
        "hotkey": "5Hotkey",
        "origin_netuid": 0,
        "destination_netuid": NETUID,
        "amount": 1_000_000_000,
    })
    out = _extract_alpha_transfer(ev, ADDRS, NETUID)
    assert out == (OUR, 1.0, "5Sender")


def test_wrong_subnet_is_skipped():
    # Alpha of another subnet is priced differently — must NOT credit.
    ev = _ev(["5Sender", OUR, "5Hotkey", 0, 42, 2_500_000_000])
    assert _extract_alpha_transfer(ev, ADDRS, NETUID) is None


def test_transfer_to_other_address_is_skipped():
    ev = _ev(["5Sender", "5SomeoneElse", "5Hotkey", 0, NETUID, 2_500_000_000])
    assert _extract_alpha_transfer(ev, ADDRS, NETUID) is None


def test_non_stake_transfer_events_ignored():
    assert _extract_alpha_transfer(
        {"module_id": "Balances", "event_id": "Transfer", "attributes": ["a", OUR, 1]},
        ADDRS, NETUID,
    ) is None
    assert _extract_alpha_transfer(
        _ev(["5S", OUR, "5H", 0, NETUID, 1], event="StakeAdded"), ADDRS, NETUID
    ) is None


def test_malformed_event_returns_none():
    assert _extract_alpha_transfer({"module_id": "SubtensorModule"}, ADDRS, NETUID) is None
    assert _extract_alpha_transfer(_ev(["too", "short"]), ADDRS, NETUID) is None
    assert _extract_alpha_transfer("not-a-dict", ADDRS, NETUID) is None

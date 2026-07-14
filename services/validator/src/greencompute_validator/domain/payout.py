"""On-chain weight planning for the payout policy.

Pure, dependency-free so it can be unit-tested without the chain/metagraph
stack. See docs/PAYOUTS.md for the full manual-quarterly-payout policy.
"""
from __future__ import annotations

from collections.abc import Callable


def plan_chain_weights(
    *,
    accumulation_enabled: bool,
    accumulator_hotkey: str,
    scores: dict[str, float],
    hotkey_to_uid: Callable[[str], int | None],
) -> tuple[list[int], list[float]] | None:
    """Decide the (uids, weights) vector to set on-chain, or None to set none.

    Manual quarterly payout (accumulation_enabled=True): route 100% of weight to
    the single accumulator hotkey so all alpha collects there; the team pays
    miners out off-chain by their persisted scorecards. If the accumulator isn't
    registered (uid is None) we return None — the caller sets NO weights rather
    than fall back to paying miners, which would leak the withheld emissions.

    On-chain distribution (accumulation_enabled=False): weight each miner by its
    final_score. Hotkeys not in the metagraph are skipped; None if none remain.
    """
    if accumulation_enabled:
        uid = hotkey_to_uid(accumulator_hotkey)
        if uid is None:
            return None
        return ([uid], [1.0])

    uids: list[int] = []
    weights: list[float] = []
    for hotkey, score in sorted(scores.items()):
        uid = hotkey_to_uid(hotkey)
        if uid is None:
            continue
        uids.append(uid)
        weights.append(score)
    if not uids:
        return None
    return (uids, weights)

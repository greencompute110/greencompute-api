"""How rented out each GPU model is, as one of four words the public may see.

The review team's "Demand and Availability Marketing" doc: the site and the API
say whether a model is available, almost fully rented or fully rented, never
how many GPUs are free or how big the fleet is. Every public answer (the price
list, the GPU pool, the website's banners and badges) is this one function of
the scheduler's own count, so they can't disagree, and `available_now` is false
exactly when nothing of that model can be rented.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

AVAILABLE = "available"
ALMOST_FULLY_RENTED = "almost_fully_rented"
FULLY_RENTED = "fully_rented"
# No live fleet at all (the 5090 while it is paused, every node of a model
# drained). Saying "rented out" then would be scarcity we made up.
ON_REQUEST = "on_request"

STATES = (AVAILABLE, ALMOST_FULLY_RENTED, FULLY_RENTED, ON_REQUEST)
RENTABLE = frozenset({AVAILABLE, ALMOST_FULLY_RENTED})

DEFAULT_ALMOST_PERCENT = 20.0


def almost_fully_rented_percent() -> float:
    """GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT: at or below this share of a
    model's live fleet free, it reads "almost fully rented". Read on every call
    so a change applies without a restart; a bad value falls back to 20."""
    raw = os.getenv("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT", "").strip()
    if not raw:
        return DEFAULT_ALMOST_PERCENT
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 0 <= value <= 100:
        logger.warning("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT=%r is not 0-100; using %s", raw, DEFAULT_ALMOST_PERCENT)
        return DEFAULT_ALMOST_PERCENT
    return value


def availability_state(free_gpus: int, fleet_gpus: int, percent: float | None = None) -> str:
    """`free_gpus` of `fleet_gpus` (both the scheduler's: nodes it would place
    on, net of reservations) as a public state."""
    if fleet_gpus <= 0:
        return ON_REQUEST
    if free_gpus <= 0:
        return FULLY_RENTED
    share = almost_fully_rented_percent() if percent is None else percent
    # Whole numbers, so exactly 20% of 10 is 2 and not 2.0000000000000004.
    if free_gpus * 100 <= share * fleet_gpus:
        return ALMOST_FULLY_RENTED
    return AVAILABLE


def availability_of(entry: Mapping[str, int] | None) -> str:
    """The state of one model's entry in ControlPlaneService.capacity_by_model()
    (None: no node of it the scheduler would use)."""
    if not entry:
        return ON_REQUEST
    return availability_state(int(entry.get("available_gpus", 0)), int(entry.get("total_gpus", 0)))

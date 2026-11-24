"""Paying in gamma, the way Bittensor's "Subnet services" standard describes
(https://www.bittensor.com/docs/guides/services, /gamma, /signed-requests) and
Lium (SN51), engy (SN53) and SayGM (SN28) already take it:

1. A customer mints SN110 gamma into a deposit at a hotkey of theirs (their
   "API key"): `mint_gamma` buys SN110 alpha with TAO and burns it, 1 gamma
   per $1 burned.
2. The hotkey spends it at our service hotkey (`spend_gamma`, no fee). The
   chain destroys the gamma and adds it to GammaSpent(110, service, hotkey).
   Nothing is sent to us.
3. We read GammaSpent at the finalized head and credit the growth to the
   hotkey's account, $1 per gamma (application/gamma.py).
4. The hotkey then signs its requests (btauth/1, infrastructure/btauth.py)
   with our service hotkey as the receiver, or signs in with the hotkey
   (wallet sign-in) for an API key.

So every dollar paid in gamma is a dollar of SN110 alpha already bought and
burned. The planned buyback gives card and crypto revenue the same
treatment (cadence still the owner's call): the owner mints gamma with that
much TAO into a treasury hotkey and spends it at the same service, so
GammaSpent at our service is all revenue, which anyone can read on chain.
Treasury spends are recorded as buybacks and never credited to anyone.

Settings (gateway env):
- GREENCOMPUTE_GAMMA_SERVICE_HOTKEYS: our service hotkeys, comma-separated;
  the first is where customers spend and the receiver of signed requests.
  Default: SN110's subnet-owner hotkey, the one `set_service` is published
  for (the owner coldkey signs it; no validator permit needed).
- GREENCOMPUTE_GAMMA_TREASURY_HOTKEYS: hotkeys whose spends are our own
  buybacks (default none).
- GREENCOMPUTE_GAMMA_SERVICES_URL: where services.json is served (default
  https://api.green-compute.com/gamma/services.json), the url `set_service`
  publishes.
- GREENCOMPUTE_BITTENSOR_NETUID: the subnet (default 110), as for alpha.
"""
from __future__ import annotations

import os

#: SN110's subnet-owner hotkey on mainnet (SubnetOwnerHotkey[110], read
#: 2026-11-23). The service is published for it.
SN110_OWNER_HOTKEY = "5CwckYmAV3fET8V9KTKfqds6BnRGACAgofECQqdYfa2Q9rvp"

#: Gamma's smallest unit: GammaBalance has 9 decimals.
RAW_PER_GAMMA = 10**9
#: The chain's smallest spend, unless the spend empties the deposit.
MIN_SPEND_GAMMA = 0.1

API_BASE = "https://api.green-compute.com"
DOCS_URL = "https://www.green-compute.com/docs/gamma"

#: What a signed request may do: rentals, inference and reading the account,
#: as Lium allows (read + rent) plus inference, as engy allows. Not adding
#: credit, not managing API keys: sign in with the hotkey for a full key.
SIGNED_REQUEST_SCOPES = ["inference", "read", "rentals"]
#: The name of the account's API key that stands for its signed requests.
SIGNED_REQUEST_KEY_NAME = "signed requests (btauth/1)"


def _hotkeys(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def netuid() -> int:
    from greencompute_gateway.infrastructure.price_feed import _subnet_netuid

    return _subnet_netuid()


def service_hotkeys() -> list[str]:
    """Our service hotkeys, the one customers spend at first."""
    return _hotkeys("GREENCOMPUTE_GAMMA_SERVICE_HOTKEYS", [SN110_OWNER_HOTKEY])


def service_hotkey() -> str:
    return service_hotkeys()[0]


def treasury_hotkeys() -> set[str]:
    return set(_hotkeys("GREENCOMPUTE_GAMMA_TREASURY_HOTKEYS", []))


def services_url() -> str:
    return (os.environ.get("GREENCOMPUTE_GAMMA_SERVICES_URL") or "").strip() or f"{API_BASE}/gamma/services.json"


def bonus_pct() -> float:
    """Extra credit per gamma (engy gives 5%); BONUS_RATES["gamma"], 0 now."""
    from greencompute_gateway.application.billing_service import BONUS_RATES

    return max(0.0, float(BONUS_RATES.get("gamma", 0.0)))


def base_cents(total_raw: int) -> int:
    """Whole cents a total of gamma is worth at $1 per gamma, rounded down: a
    fraction of a cent is credited when the next spend completes it."""
    return (max(0, int(total_raw)) * 100) // RAW_PER_GAMMA


def credit_cents(total_raw: int, bonus: float) -> int:
    """Every cent a total of gamma earns, bonus included."""
    cents = base_cents(total_raw)
    return cents + int(cents * bonus)


def gamma_text(raw: int) -> str:
    """"5", "0.1", "12.345678901": gamma without trailing zeros."""
    whole, frac = divmod(max(0, int(raw)), RAW_PER_GAMMA)
    return f"{whole}.{frac:09d}".rstrip("0").rstrip(".") if frac else str(whole)


def usd_text(cents: int) -> str:
    return f"{cents / 100:.2f}"


def how_to_pay(hotkey: str | None = None) -> dict:
    """The steps, with the exact commands, for an agent or a person."""
    n, svc = netuid(), service_hotkey()
    key = hotkey or "API_KEY"
    return {
        "netuid": n,
        "service_hotkey": svc,
        "credit_per_gamma_usd": f"{1 + bonus_pct():.2f}",
        "steps": [
            "btcli wallet new-hotkey -H API_KEY  (a fresh hotkey: it is your account here)",
            f"btcli gamma mint --netuid {n} --hotkey {key} --amount-tao 1",
            f"btcli gamma spend --netuid {n} --service-hotkey {svc} --amount-gamma 5 -H {key}",
            "Within about a minute of the spend finalizing, the hotkey's account is credited $1 per gamma. "
            f"Check it at {API_BASE}/gamma/balance?hotkey=<hotkey>.",
            "Then sign each request with the hotkey (btauth/1, receiver = the service hotkey), or sign in "
            "with it (POST /platform/auth/wallet/challenge) for an API key.",
        ],
        "docs": DOCS_URL,
    }


def services_document() -> dict:
    """services.json, version 1, as the standard describes it."""
    publishers = [{"netuid": netuid(), "hotkey": hk} for hk in service_hotkeys()]
    return {
        "version": 1,
        "name": "Green Compute",
        "description": (
            "GPU rentals (RTX 4090 and 5090, billed by the minute) and OpenAI-compatible inference on "
            "Bittensor subnet 110. Pay with gamma: spend SN110 gamma at our service hotkey and that hotkey's "
            "account is credited $1 per gamma; then call the API with btauth/1-signed requests."
        ),
        "publishers": publishers,
        "services": [
            {"type": "http", "endpoint": API_BASE, "spec": f"{API_BASE}/openapi.json"},
            {"type": "mcp", "endpoint": f"{API_BASE}/mcp"},
        ],
        "payments": [
            {"type": "gamma", "signature": "btauth/1", "balance": f"{API_BASE}/gamma/balance"},
        ],
        "pricing": f"{API_BASE}/platform/pricing",
        "terms": "https://www.green-compute.com/terms",
        "docs": DOCS_URL,
    }

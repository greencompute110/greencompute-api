"""What an API key may do.

A key with no scopes has full access: every key made before scopes existed,
and the dashboard's own key. A key with scopes may only make requests whose
required scope it holds, so an agent can be given, say, an inference-only or a
read-only key, and a leaked or prompt-injected key can't rent machines, fetch
SSH access or top up the account.

The required scope is worked out from the request's method and path in one
place, so a new route can't forget to check it. Anything not listed below
needs a full-access key: scoped keys fail closed.
"""
from __future__ import annotations

import re

SCOPES = {
    "read": "See deployments, workloads, balance, invoices and other account data",
    "rentals": "Create, change and delete workloads and deployments, and get SSH access",
    "inference": "Call models through /v1 (chat, completions, embeddings, model list)",
    "billing": "Add credit: crypto invoices, verified wallets, payment reviews",
    "keys": "Create, list and delete API keys (never broader than its own)",
}

_SSH = re.compile(r"^/platform/deployments/[^/]+/ssh/?$")
_RENTAL_PREFIXES = (
    "/platform/workloads", "/platform/deployments", "/platform/fleets", "/platform/builds", "/platform/images",
    "/platform/capacity-alerts",
)


def normalize_scopes(scopes: list[str] | None) -> list[str]:
    """Validated, de-duplicated, sorted scope names. Raises ValueError."""
    out = sorted({(s or "").strip().lower() for s in (scopes or [])})
    unknown = [s for s in out if s not in SCOPES]
    if unknown:
        raise ValueError(f"unknown scope(s) {unknown}; choose from {sorted(SCOPES)}")
    return out


#: required_scope's answer for a request any valid key may make: POST /mcp,
#: whose tools then each run as their REST request, with its scope.
ANY_SCOPE = "*"


def required_scope(method: str, path: str) -> str | None:
    """The scope a request needs, or None when only a full-access key may
    make it."""
    method = (method or "").upper()
    path = path or ""
    if path.rstrip("/") == "/mcp":
        return ANY_SCOPE
    if path.startswith("/v1/"):
        return "inference"
    if path.startswith("/platform/api-keys"):
        return "keys"
    if _SSH.match(path):
        return "rentals"  # SSH access is root on the machine, not a read
    if path.startswith("/platform/billing"):
        return "read" if method == "GET" else "billing"
    if path.startswith(_RENTAL_PREFIXES):
        return "read" if method == "GET" else "rentals"
    if method == "GET":
        return "read"
    return None

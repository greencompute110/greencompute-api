"""Public-registry cross-checks for provider applications.

The one *verification* (vs. model judgement) signal we have: match a claimed
company / certificate against an authoritative public registry. This is
inherently per-country, and for most of the world no queryable registry exists —
in that case we return ``available=False`` (neutral: the decision leans on
document review + the confidence gate, per the fail-closed policy).

Implemented today:
  * UK company existence + name match via the free Companies House API.
Not yet implemented (returns available=False, extend later):
  * Ofgem REGO / EU GO / US REC certificate-number lookups.

A registry outage degrades to ``available=False`` (neutral) rather than raising —
a third-party being down must not block onboarding.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from greencompute_validator.domain.application_review import RegistryResult

logger = logging.getLogger(__name__)

_UK_COUNTRIES = {
    "uk", "gb", "gbr", "united kingdom", "great britain", "england",
    "scotland", "wales", "northern ireland",
}


def _is_uk(country: str | None) -> bool:
    return (country or "").strip().lower() in _UK_COUNTRIES


def _norm(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower()
    for junk in (",", ".", "&"):
        s = s.replace(junk, " ")
    tokens = [t for t in s.split() if t not in {
        "ltd", "limited", "llc", "inc", "gmbh", "plc", "co", "company",
        "corp", "corporation", "the",
    }]
    return " ".join(tokens).strip()


class NullRegistryVerifier:
    """No registry available — every lookup is neutral. Safe global default."""

    def verify_certificate(self, **_: object) -> RegistryResult:
        return RegistryResult(available=False, detail="no certificate registry available")

    def verify_company(self, **_: object) -> RegistryResult:
        return RegistryResult(available=False, detail="no company registry available")


class DefaultRegistryVerifier:
    """Routes by country. UK companies are checked against Companies House;
    everything else (and all certificate-number lookups) is currently neutral."""

    def __init__(self, *, companies_house_api_key: str = "", timeout: float = 15.0) -> None:
        self._ch_key = companies_house_api_key
        self._timeout = timeout

    def verify_certificate(
        self,
        *,
        scheme: str | None,
        certificate_number: str | None,
        holder_name: str | None,
        country: str | None,
    ) -> RegistryResult:
        # No certificate-number registry is wired yet (Ofgem/AIB/M-RETS are the
        # future adds). Neutral until then.
        return RegistryResult(
            available=False,
            detail="certificate-registry lookup not implemented for this scheme/country",
        )

    def verify_company(
        self,
        *,
        name: str,
        registration_number: str | None,
        country: str | None,
    ) -> RegistryResult:
        if _is_uk(country) and self._ch_key and name.strip():
            return self._companies_house(name, registration_number)
        return RegistryResult(available=False, detail="no company registry for this country")

    def _companies_house(self, name: str, registration_number: str | None) -> RegistryResult:
        url = (
            "https://api.company-information.service.gov.uk/search/companies?q="
            + urllib.parse.quote(name)
            + "&items_per_page=20"
        )
        auth = base64.b64encode(f"{self._ch_key}:".encode()).decode()
        req = urllib.request.Request(  # noqa: S310 — fixed https host
            url, headers={"Authorization": f"Basic {auth}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                items = json.loads(resp.read().decode()).get("items", [])
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.warning("Companies House lookup failed for %r: %s", name, exc)
            return RegistryResult(available=False, detail=f"companies house unavailable: {exc}")

        want_name = _norm(name)
        want_num = (registration_number or "").strip().lstrip("0").lower()
        for item in items:
            title = _norm(item.get("title"))
            number = str(item.get("company_number", "")).strip().lstrip("0").lower()
            active = str(item.get("company_status", "")).lower() == "active"
            name_hit = title and (title == want_name or want_name in title or title in want_name)
            num_hit = bool(want_num) and number == want_num
            if num_hit or name_hit:
                return RegistryResult(
                    available=True,
                    matched=active and (num_hit or name_hit),
                    detail=(
                        f"Companies House: {item.get('title')} ({item.get('company_number')}), "
                        f"status={item.get('company_status')}"
                    ),
                    source="companies-house",
                )
        return RegistryResult(
            available=True,
            matched=False,
            detail=f"no active Companies House match for {name!r}",
            source="companies-house",
        )

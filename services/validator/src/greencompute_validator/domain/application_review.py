"""Automated review of a provider application (Phase-1 automated onboarding).

This replaces the manual admin review of a `/apply` submission: it reads the
structured form + the uploaded certificates and decides whether the applicant
is eligible to be auto-whitelisted, so an approved miner can plug nodes into
the pool without a human in the loop.

Design:
  * The *judgement* over free-form documents (is this certificate genuine? whose
    name is on it? is it an on-site generator or just a green-tariff purchase?)
    is delegated to a vision LLM behind the ``CertReviewer`` protocol.
  * A public-registry cross-check (Ofgem / Companies House / …) is delegated to
    the ``RegistryVerifier`` protocol — the one signal that is *verification*
    rather than a model's judgement. It is per-country and often unavailable.
  * The *policy* — the hard gates and the final approve/reject — is deterministic
    Python here, so it is explicit, auditable, and unit-testable with fakes.

Policy (all decided by the product owner, 2026-07-13):
  * 100% renewable, hard requirement.
  * Accepted proof: REGO / GoO / REC / I-TRACK only.
  * Certificate must be in the applicant's business name.
  * Issued within the last 12 months.
  * Direct/on-site generation — the certificate must identify the applicant as
    the generator/self-supplier, not merely a buyer of green-tariff grid power.
  * Global applicants.
  * ISO 27001 / SOC 2 accreditations are a plus, never a gate.
  * Leased/rented hardware → reject.
  * Fully automated: confident pass → approve + whitelist; otherwise → reject.
  * Anything borderline or unverifiable fails closed (reject with reasons); the
    applicant may fix the gap and re-apply.

Nothing here does I/O except through the injected protocols, and
``evaluate_application`` is pure — safe to call from a request handler.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, Field

from greencompute_validator.domain.spec_check import evaluate_provider_specs

# --- Policy constants --------------------------------------------------------

# Accepted renewable-certificate schemes, normalized (lowercased, punctuation
# stripped) so "I-TRACK", "i track", "itrack" all match.
ACCEPTED_SCHEMES = {"rego", "goo", "rec", "itrack"}
MAX_CERTIFICATE_AGE_DAYS = 365
MIN_RENEWABLE_PCT = 99.5  # ≈100% — small tolerance for rounding on the document
# Words in the free-text form that indicate the hardware is not owned.
_LEASING_MARKERS = (
    "leased",
    "leasing",
    "rented",
    "renting",
    "rental",
    "co-located",
    "colocated",
    "colocation",
    "on loan",
    "financed",
    "hire purchase",
)

PASS, FAIL = "pass", "fail"
APPROVE, REJECT = "approve", "reject"


# --- I/O-boundary protocols (concrete impls live in infrastructure/) ---------


class CertReviewer(Protocol):
    """Vision-LLM review of a single uploaded document."""

    def review_energy_certificate(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        declared_org: str,
        declared_sources: list[str],
    ) -> CertAssessment: ...

    def review_accreditation(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        declared_org: str,
    ) -> AccreditationAssessment: ...


class RegistryVerifier(Protocol):
    """Cross-check a claim against a public registry, where one exists."""

    def verify_certificate(
        self,
        *,
        scheme: str | None,
        certificate_number: str | None,
        holder_name: str | None,
        country: str | None,
    ) -> RegistryResult: ...

    def verify_company(
        self,
        *,
        name: str,
        registration_number: str | None,
        country: str | None,
    ) -> RegistryResult: ...


# --- Assessment / decision models -------------------------------------------


class CertAssessment(BaseModel):
    """What the vision model extracted from one energy certificate."""

    filename: str = ""
    is_genuine: bool = False  # reads as a real certificate, not fabricated/blank/irrelevant
    scheme: str | None = None  # REGO / GoO / REC / I-TRACK / other / unknown
    certificate_number: str | None = None
    holder_name: str | None = None  # named account holder / generating-station operator
    identifies_holder_as_generator: bool | None = None  # on-site/self-supply vs grid purchase
    renewable_percent: float | None = None
    issue_date: str | None = None  # ISO 8601
    valid_until: str | None = None  # ISO 8601, if the document states an expiry
    authenticity_concerns: list[str] = Field(default_factory=list)
    confidence: float = 0.0  # 0..1 — the model's confidence in this extraction
    notes: str = ""


class AccreditationAssessment(BaseModel):
    """What the vision model extracted from an ISO/SOC2 certificate (advisory)."""

    filename: str = ""
    is_genuine: bool = False
    kind: str | None = None  # ISO 27001 / SOC 2 Type II / other
    holder_name: str | None = None
    valid: bool | None = None
    confidence: float = 0.0
    notes: str = ""


class RegistryResult(BaseModel):
    available: bool = False  # a registry existed for this scheme/country
    matched: bool | None = None  # found and consistent with the claim
    detail: str = ""
    source: str = ""


class CheckResult(BaseModel):
    name: str
    status: str  # pass | fail
    reason: str = ""
    hard_gate: bool = True


class ReviewDecision(BaseModel):
    decision: str  # approve | reject
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    winning_certificate: str | None = None
    signature_verified: bool = False
    registry: RegistryResult | None = None
    spec_assessment: dict = Field(default_factory=dict)
    cert_assessments: list[CertAssessment] = Field(default_factory=list)
    accreditation_assessments: list[AccreditationAssessment] = Field(default_factory=list)
    model: str = ""
    reviewed_via: str = "automated"

    @property
    def approved(self) -> bool:
        return self.decision == APPROVE


@dataclass
class ReviewAttachment:
    """An uploaded file, ready for review (bytes already decoded)."""

    filename: str
    content_type: str
    data: bytes

    @property
    def kind(self) -> str:
        name = (self.filename or "").lower()
        if name.startswith("energy-"):
            return "energy"
        if name.startswith("accred-"):
            return "accreditation"
        return "other"


# --- Helpers -----------------------------------------------------------------


def _normalize_scheme(scheme: str | None) -> str | None:
    if not scheme:
        return None
    s = "".join(ch for ch in scheme.lower() if ch.isalnum())
    # Map a few common spellings onto the canonical tokens.
    if s in {"itrack", "itracke", "itrackeu"}:
        return "itrack"
    if s in {"guaranteeoforigin", "guaranteesoforigin"}:
        return "goo"
    if s.startswith("rego"):
        return "rego"
    if s.startswith("rec") or s in {"renewableenergycertificate", "renewableenergycredits"}:
        return "rec"
    if s in {"rego", "goo", "rec", "itrack"}:
        return s
    return s  # unknown token — caller checks membership in ACCEPTED_SCHEMES


def _norm_name(name: str | None) -> str:
    """Lowercase, drop common company suffixes and punctuation for comparison."""
    if not name:
        return ""
    s = name.lower()
    for junk in (",", ".", "&", "  "):
        s = s.replace(junk, " ")
    tokens = [t for t in s.split() if t not in {
        "ltd", "limited", "llc", "inc", "incorporated", "gmbh", "plc", "co",
        "company", "corp", "corporation", "the", "bv", "sa", "ag", "srl",
    }]
    return " ".join(tokens).strip()


def _names_match(a: str | None, b: str | None) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    # token-overlap fallback: majority of the shorter name's tokens appear
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap >= max(1, min(len(ta), len(tb)))


def _parse_date(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(raw[: len(fmt) + 4], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _cert_eligibility(cert: CertAssessment, now: datetime) -> list[str]:
    """Return the list of policy reasons this cert FAILS. Empty = eligible."""
    fails: list[str] = []
    if not cert.is_genuine:
        fails.append("certificate does not read as a genuine document")
    if cert.authenticity_concerns:
        fails.append("authenticity concerns: " + "; ".join(cert.authenticity_concerns[:3]))
    if _normalize_scheme(cert.scheme) not in ACCEPTED_SCHEMES:
        fails.append(f"scheme {cert.scheme!r} is not one of REGO/GoO/REC/I-TRACK")
    if cert.renewable_percent is None or cert.renewable_percent < MIN_RENEWABLE_PCT:
        fails.append(f"does not evidence 100% renewable (got {cert.renewable_percent})")
    if cert.identifies_holder_as_generator is not True:
        fails.append("does not identify the applicant as the on-site generator/self-supplier")
    issued = _parse_date(cert.issue_date)
    if issued is None:
        fails.append("issue date missing/unparseable")
    elif issued < now - timedelta(days=MAX_CERTIFICATE_AGE_DAYS):
        fails.append(f"issued more than 12 months ago ({cert.issue_date})")
    elif issued > now + timedelta(days=1):
        fails.append(f"issue date is in the future ({cert.issue_date})")
    valid_until = _parse_date(cert.valid_until)
    if valid_until is not None and valid_until < now:
        fails.append(f"certificate expired ({cert.valid_until})")
    return fails


def _looks_leased(details: dict, description: str) -> bool:
    blob = (description or "").lower()
    for key in ("notes",):
        blob += " " + str(details.get(key, "")).lower()
    infra = details.get("infrastructure") or {}
    blob += " " + " ".join(str(v).lower() for v in infra.values() if isinstance(v, (str, int, float)))
    return any(marker in blob for marker in _LEASING_MARKERS)


def _completeness_reasons(details: dict, organization: str, energy_count: int) -> list[str]:
    fails: list[str] = []
    business = details.get("business") if isinstance(details, dict) else None
    business = business if isinstance(business, dict) else {}
    if not (organization or "").strip() and not str(business.get("name", "")).strip():
        fails.append("business name missing")
    if not str(business.get("registration_number", "")).strip():
        fails.append("business registration number missing")
    if not business.get("address"):
        fails.append("business address missing")
    if not details.get("data_center_address"):
        fails.append("data-centre address missing")
    energy = details.get("energy") if isinstance(details.get("energy"), dict) else {}
    if not energy.get("sources"):
        fails.append("no renewable energy source declared")
    if energy_count < 1:
        fails.append("no renewable-energy certificate attached")
    nodes = details.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        fails.append("no node specifications declared")
    return fails


# --- Pure decision engine ----------------------------------------------------


def evaluate_application(
    *,
    details: dict,
    organization: str,
    description: str,
    signature_verified: bool,
    energy_assessments: list[CertAssessment],
    accreditation_assessments: list[AccreditationAssessment],
    registry: RegistryResult,
    winning_certificate: str | None,
    winning_cert_fails: list[str],
    confidence: float,
    confidence_threshold: float,
    now: datetime | None = None,
) -> ReviewDecision:
    """Deterministic approve/reject given already-gathered assessments.

    ``energy_assessments`` is the full list (for the audit trail);
    ``winning_certificate`` / ``winning_cert_fails`` describe the best candidate
    (chosen by ``run_review``); ``confidence`` is the overall eligibility
    confidence after folding in the registry result.
    """
    details = details if isinstance(details, dict) else {}
    checks: list[CheckResult] = []

    def add(name: str, ok: bool, reason: str = "", hard: bool = True) -> None:
        checks.append(CheckResult(name=name, status=PASS if ok else FAIL, reason=reason, hard_gate=hard))

    # 1. Identity — advisory. Applications come through the web UI, which can't
    #    sign with the hotkey, so a missing signature must NOT auto-reject a
    #    legitimate applicant. It's recorded (and becomes a strong positive
    #    signal once the UI adds wallet-extension signing). The whitelist stays
    #    revocable, and the green-energy/registry gates carry the fraud filter.
    add(
        "signature",
        signature_verified,
        "" if signature_verified else "hotkey ownership not proven (web UI cannot sign yet)",
        hard=False,
    )

    # 2. Completeness of the submission.
    completeness_fails = _completeness_reasons(details, organization, len(energy_assessments))
    add("completeness", not completeness_fails, "; ".join(completeness_fails))

    # 3. Hardware ownership — leased/rented is an outright reject.
    leased = _looks_leased(details, description)
    add("hardware_ownership", not leased, "hardware appears leased/rented/colocated" if leased else "")

    # 4. Node specs — reuse the deterministic conformance check. Under a fully
    #    automated decision, "unclear" (conformant is None) fails closed.
    spec = evaluate_provider_specs(details)
    spec_ok = spec.get("conformant") is True
    add(
        "node_specs",
        spec_ok,
        "" if spec_ok else "node specs not confirmed in-spec: " + "; ".join(spec.get("flags", [])[:4]),
    )

    # 5. Green-energy eligibility — a certificate that satisfies every rule.
    green_ok = winning_certificate is not None
    add(
        "green_energy",
        green_ok,
        "" if green_ok else ("no eligible renewable certificate: " + "; ".join(winning_cert_fails[:4])),
    )

    # 6. Registry cross-check — only a *contradiction* is a hard fail; an
    #    unavailable registry is neutral (we lean on document review + the
    #    confidence gate below).
    registry_ok = not (registry.available and registry.matched is False)
    add(
        "registry_cross_check",
        registry_ok,
        "" if registry_ok else f"registry contradicts the claim: {registry.detail}",
    )

    # 7. Confidence gate — borderline evidence fails closed.
    conf_ok = confidence >= confidence_threshold
    add(
        "confidence",
        conf_ok,
        "" if conf_ok else f"eligibility confidence {confidence:.2f} below threshold {confidence_threshold:.2f}",
    )

    hard_fails = [c for c in checks if c.hard_gate and c.status == FAIL]
    decision = APPROVE if not hard_fails else REJECT
    reasons = [f"{c.name}: {c.reason}" for c in hard_fails if c.reason]

    return ReviewDecision(
        decision=decision,
        confidence=round(confidence, 4),
        reasons=reasons,
        checks=checks,
        winning_certificate=winning_certificate,
        signature_verified=signature_verified,
        registry=registry,
        spec_assessment=spec,
        cert_assessments=energy_assessments,
        accreditation_assessments=accreditation_assessments,
    )


# --- Orchestrator (does I/O through the injected protocols) ------------------


def run_review(
    *,
    details: dict,
    organization: str,
    description: str,
    country: str | None,
    signature_verified: bool,
    attachments: list[ReviewAttachment],
    reviewer: CertReviewer,
    registry: RegistryVerifier,
    confidence_threshold: float,
    now: datetime | None = None,
) -> ReviewDecision:
    """Review one application end to end: read each certificate with the vision
    model, pick the best eligible one, cross-check it against a registry, and
    decide. Any exception from an injected protocol propagates to the caller,
    which should fall back to a human queue rather than a blind reject."""
    now = now or datetime.now(UTC)
    details = details if isinstance(details, dict) else {}
    declared_sources = []
    energy = details.get("energy")
    if isinstance(energy, dict) and isinstance(energy.get("sources"), list):
        declared_sources = [str(s) for s in energy["sources"]]

    energy_assessments: list[CertAssessment] = []
    accreditation_assessments: list[AccreditationAssessment] = []
    for att in attachments:
        if att.kind == "energy":
            a = reviewer.review_energy_certificate(
                data=att.data,
                content_type=att.content_type,
                filename=att.filename,
                declared_org=organization,
                declared_sources=declared_sources,
            )
            a.filename = a.filename or att.filename
            energy_assessments.append(a)
        elif att.kind == "accreditation":
            a = reviewer.review_accreditation(
                data=att.data,
                content_type=att.content_type,
                filename=att.filename,
                declared_org=organization,
            )
            a.filename = a.filename or att.filename
            accreditation_assessments.append(a)

    # Pick the best eligible certificate whose holder matches the applicant.
    candidates: list[tuple[CertAssessment, list[str]]] = []
    for cert in energy_assessments:
        fails = _cert_eligibility(cert, now)
        if not _names_match(cert.holder_name, organization):
            fails = [*fails, f"certificate holder {cert.holder_name!r} does not match applicant {organization!r}"]
        candidates.append((cert, fails))

    eligible = [(c, f) for c, f in candidates if not f]
    winning_cert: CertAssessment | None = None
    winning_fails: list[str] = []
    if eligible:
        winning_cert, _ = max(eligible, key=lambda cf: cf[0].confidence)
    elif candidates:
        # Report the reasons from the closest-to-passing certificate.
        winning_cert_none, winning_fails = min(candidates, key=lambda cf: len(cf[1]))
        del winning_cert_none

    registry_result = RegistryResult()
    confidence = 0.0
    if winning_cert is not None:
        confidence = winning_cert.confidence
        cert_reg = registry.verify_certificate(
            scheme=_normalize_scheme(winning_cert.scheme),
            certificate_number=winning_cert.certificate_number,
            holder_name=winning_cert.holder_name,
            country=country,
        )
        business = details.get("business") if isinstance(details.get("business"), dict) else {}
        company_reg = registry.verify_company(
            name=organization or str(business.get("name", "")),
            registration_number=str(business.get("registration_number", "")) or None,
            country=country,
        )
        registry_result = _merge_registry(cert_reg, company_reg)
        confidence = _fold_registry_confidence(winning_cert.confidence, registry_result)

    return evaluate_application(
        details=details,
        organization=organization,
        description=description,
        signature_verified=signature_verified,
        energy_assessments=energy_assessments,
        accreditation_assessments=accreditation_assessments,
        registry=registry_result,
        winning_certificate=winning_cert.filename if winning_cert else None,
        winning_cert_fails=winning_fails,
        confidence=confidence,
        confidence_threshold=confidence_threshold,
        now=now,
    )


def _merge_registry(cert: RegistryResult, company: RegistryResult) -> RegistryResult:
    available = cert.available or company.available
    # A contradiction on either lookup is a contradiction overall.
    matched: bool | None
    if cert.matched is False or company.matched is False:
        matched = False
    elif cert.matched is True or company.matched is True:
        matched = True
    else:
        matched = None
    detail = " | ".join(d for d in (cert.detail, company.detail) if d)
    source = ", ".join(s for s in {cert.source, company.source} if s)
    return RegistryResult(available=available, matched=matched, detail=detail, source=source)


def _fold_registry_confidence(base: float, registry: RegistryResult) -> float:
    """Combine the model's per-cert confidence with the registry outcome."""
    if registry.available and registry.matched is True:
        return max(base, 0.9)  # external corroboration
    if registry.available and registry.matched is False:
        return 0.0  # contradicted — hard fail downstream anyway
    # No registry available for this country/scheme — lean on document review,
    # with a small discount for the lack of external corroboration.
    return round(base * 0.95, 4)

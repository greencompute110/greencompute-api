"""Automated provider-application review: the deterministic approve/reject
policy over vision-model + registry assessments. The LLM and registry are faked
so these tests pin the *policy*, not any network behaviour.

Policy under test (product-owner decided): 100% renewable; REGO/GoO/REC/I-TRACK
only; certificate in the applicant's business name; issued within 12 months;
on-site generator (not grid purchase); leased hardware rejected; fully
automated with a fail-closed default.
"""
from datetime import UTC, datetime

import pytest

from greencompute_validator.domain.application_review import (
    AccreditationAssessment,
    CertAssessment,
    RegistryResult,
    ReviewAttachment,
    run_review,
)

NOW = datetime(2026, 7, 13, tzinfo=UTC)
ORG = "Green Data Ltd"


def good_cert(**over) -> CertAssessment:
    base = dict(
        is_genuine=True,
        scheme="REGO",
        certificate_number="R-123456",
        holder_name="Green Data Ltd",
        identifies_holder_as_generator=True,
        renewable_percent=100.0,
        issue_date="2026-03-01",
        valid_until=None,
        authenticity_concerns=[],
        confidence=0.9,
        notes="ok",
    )
    base.update(over)
    return CertAssessment(**base)


def details(**over) -> dict:
    d = {
        "business": {
            "name": ORG,
            "registration_number": "12345678",
            "address": {"country": "United Kingdom", "city": "London"},
        },
        "data_center_address": {"country": "United Kingdom", "city": "London"},
        "energy": {"sources": ["Solar"]},
        "infrastructure": {"upload_speed": "10 Gbps", "download_speed": "10 Gbps"},
        "nodes": [{"gpu": "8x RTX 4090", "cpu": "Dual Xeon", "ram": "512GB",
                   "storage": "8TB", "quantity": "8"}],
        "notes": "",
    }
    d.update(over)
    return d


class FakeReviewer:
    def __init__(self, energy=None, accred=None, raises=False):
        self._energy = list(energy or [])
        self._accred = list(accred or [])
        self.raises = raises

    def review_energy_certificate(self, **_):
        if self.raises:
            raise RuntimeError("openrouter down")
        return self._energy.pop(0)

    def review_accreditation(self, **_):
        return self._accred.pop(0) if self._accred else AccreditationAssessment()


class FakeRegistry:
    def __init__(self, cert=None, company=None):
        self._cert = cert or RegistryResult()
        self._company = company or RegistryResult()

    def verify_certificate(self, **_):
        return self._cert

    def verify_company(self, **_):
        return self._company


def review(*, certs=None, sig=True, det=None, desc="", registry=None, energy_files=None, threshold=0.75):
    certs = [good_cert()] if certs is None else certs
    if energy_files is None:
        energy_files = len(certs)
    atts = [ReviewAttachment(f"energy-{i}.pdf", "application/pdf", b"data") for i in range(energy_files)]
    return run_review(
        details=details(**(det or {})),
        organization=ORG,
        description=desc,
        country="United Kingdom",
        signature_verified=sig,
        attachments=atts,
        reviewer=FakeReviewer(energy=certs),
        registry=registry or FakeRegistry(),
        confidence_threshold=threshold,
        now=NOW,
    )


def _check(decision, name):
    return next(c for c in decision.checks if c.name == name)


# --- happy path --------------------------------------------------------------


def test_valid_application_is_approved():
    d = review()
    assert d.decision == "approve"
    assert d.approved is True
    assert d.winning_certificate == "energy-0.pdf"
    assert all(c.status == "pass" for c in d.checks)


def test_registry_match_boosts_low_model_confidence_to_approve():
    # Model only 0.6 sure, but Companies House corroborates → approve.
    d = review(
        certs=[good_cert(confidence=0.6)],
        registry=FakeRegistry(company=RegistryResult(available=True, matched=True, detail="found")),
    )
    assert d.decision == "approve"
    assert d.confidence >= 0.75


# --- green-energy gates (each an independent reject) --------------------------


def test_wrong_scheme_rejected():
    d = review(certs=[good_cert(scheme="Green Tariff")])
    assert d.decision == "reject"
    assert _check(d, "green_energy").status == "fail"


def test_not_100_percent_rejected():
    assert review(certs=[good_cert(renewable_percent=80.0)]).decision == "reject"


def test_grid_purchase_not_onsite_rejected():
    d = review(certs=[good_cert(identifies_holder_as_generator=False)])
    assert d.decision == "reject"


def test_holder_name_mismatch_rejected():
    assert review(certs=[good_cert(holder_name="Totally Different Co")]).decision == "reject"


def test_expired_certificate_rejected():
    assert review(certs=[good_cert(valid_until="2026-01-01")]).decision == "reject"


def test_older_than_12_months_rejected():
    assert review(certs=[good_cert(issue_date="2024-01-01")]).decision == "reject"


def test_fabricated_certificate_rejected():
    d = review(certs=[good_cert(is_genuine=False, authenticity_concerns=["fonts inconsistent"])])
    assert d.decision == "reject"


# --- other hard gates --------------------------------------------------------


def test_unverified_signature_is_advisory_not_a_hard_reject():
    # Web-UI applicants can't sign; a missing signature is recorded but must not
    # reject an otherwise-valid application.
    d = review(sig=False)
    assert d.decision == "approve"
    sig = _check(d, "signature")
    assert sig.status == "fail"
    assert sig.hard_gate is False


def test_leased_hardware_rejected():
    d = review(desc="All GPUs are leased from a hosting vendor on a 12-month rental.")
    assert d.decision == "reject"
    assert _check(d, "hardware_ownership").status == "fail"


def test_missing_energy_certificate_rejected():
    d = review(certs=[], energy_files=0)
    assert d.decision == "reject"
    assert _check(d, "green_energy").status == "fail"
    assert _check(d, "completeness").status == "fail"


def test_low_confidence_fails_closed():
    d = review(certs=[good_cert(confidence=0.4)])  # 0.4*0.95 < 0.75
    assert d.decision == "reject"
    assert _check(d, "confidence").status == "fail"


def test_registry_contradiction_rejected():
    d = review(
        registry=FakeRegistry(company=RegistryResult(available=True, matched=False, detail="not found")),
    )
    assert d.decision == "reject"
    assert _check(d, "registry_cross_check").status == "fail"


def test_out_of_spec_nodes_rejected():
    d = review(det={"nodes": [{"gpu": "8x RTX 3090", "cpu": "Dual Xeon", "ram": "512GB", "storage": "8TB"}]})
    assert d.decision == "reject"
    assert _check(d, "node_specs").status == "fail"


def test_incomplete_business_rejected():
    d = review(det={"business": {"name": ORG}})  # no reg number / address
    assert d.decision == "reject"
    assert _check(d, "completeness").status == "fail"


# --- infrastructure failure propagates (caller routes to human) --------------


def test_reviewer_error_propagates():
    atts = [ReviewAttachment("energy-0.pdf", "application/pdf", b"x")]
    with pytest.raises(RuntimeError):
        run_review(
            details=details(),
            organization=ORG,
            description="",
            country="United Kingdom",
            signature_verified=True,
            attachments=atts,
            reviewer=FakeReviewer(raises=True),
            registry=FakeRegistry(),
            confidence_threshold=0.75,
            now=NOW,
        )


# --- audit trail is preserved ------------------------------------------------


def test_decision_records_full_audit():
    d = review()
    dumped = d.model_dump(mode="json")
    assert dumped["cert_assessments"]
    assert dumped["spec_assessment"]["conformant"] is True
    assert "checks" in dumped and len(dumped["checks"]) == 7

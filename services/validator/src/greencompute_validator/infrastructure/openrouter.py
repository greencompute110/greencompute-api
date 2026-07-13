"""OpenRouter vision client for reading provider certificates.

OpenRouter exposes an OpenAI-compatible ``/chat/completions`` endpoint that
fronts many models (Claude, Gemini, GPT, …). We use a strong vision model to
read each uploaded certificate and return a structured assessment.

Implemented with the standard library only (urllib) to match the existing
validator canary and avoid adding a runtime dependency. On any transport/parse
failure this RAISES ``OpenRouterError`` — the caller treats an infrastructure
failure as "route to human", never as a blind reject.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from greencompute_validator.domain.application_review import (
    AccreditationAssessment,
    CertAssessment,
)

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    """Raised on any failure to obtain a well-formed model response."""


_ENERGY_SYSTEM = (
    "You are a meticulous compliance reviewer for a green-compute network. You "
    "inspect an uploaded renewable-energy certificate image/PDF and report ONLY "
    "what the document actually shows. Never invent details. If the document is "
    "blank, unrelated, illegible, or looks fabricated/edited, say so via "
    "is_genuine=false and authenticity_concerns. Respond with a single JSON "
    "object and nothing else."
)

_ENERGY_INSTRUCTIONS = (
    "Assess this renewable-energy certificate. The applicant organisation is "
    "{org!r} and they declared these energy sources: {sources}.\n\n"
    "Return JSON with EXACTLY these keys:\n"
    '  "is_genuine": bool  — does this read as a real, unedited certificate of a recognised scheme?\n'
    '  "scheme": string|null — the scheme: one of "REGO","GoO","REC","I-TRACK", or the actual name / "unknown".\n'
    '  "certificate_number": string|null — the certificate/accreditation number if printed.\n'
    '  "holder_name": string|null — the named account holder / generating-station operator on the certificate.\n'
    '  "identifies_holder_as_generator": bool|null — TRUE only if the document shows the holder is the GENERATOR / on-site self-supplier of the renewable energy, FALSE if it merely shows they purchase green-tariff grid electricity or hold tradable certificates without generating.\n'
    '  "renewable_percent": number|null — the percentage of renewable energy evidenced (100 if it certifies fully renewable generation).\n'
    '  "issue_date": string|null — ISO 8601 (YYYY-MM-DD) issue/accreditation date.\n'
    '  "valid_until": string|null — ISO 8601 expiry if stated.\n'
    '  "authenticity_concerns": string[] — specific tells of forgery/AI-generation/editing, else [].\n'
    '  "confidence": number — 0..1, your confidence in the above.\n'
    '  "notes": string — one short line of rationale.\n'
)

_ACCRED_SYSTEM = (
    "You inspect an uploaded compliance accreditation certificate (e.g. ISO "
    "27001, SOC 2 Type II). Report only what the document shows. Respond with a "
    "single JSON object and nothing else."
)

_ACCRED_INSTRUCTIONS = (
    "Assess this accreditation certificate for organisation {org!r}. Return JSON "
    "with keys: is_genuine (bool), kind (string|null e.g. 'ISO 27001'), "
    "holder_name (string|null), valid (bool|null — currently in-date and not "
    "obviously fake), confidence (0..1), notes (string)."
)


class OpenRouterCertReviewer:
    """Concrete ``CertReviewer`` backed by an OpenRouter vision model."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 90.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    # -- public API (CertReviewer protocol) --------------------------------

    def review_energy_certificate(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        declared_org: str,
        declared_sources: list[str],
    ) -> CertAssessment:
        instructions = _ENERGY_INSTRUCTIONS.format(
            org=declared_org or "(unknown)",
            sources=", ".join(declared_sources) or "(none stated)",
        )
        payload = self._chat(_ENERGY_SYSTEM, instructions, data, content_type, filename)
        try:
            return CertAssessment.model_validate({**payload, "filename": filename})
        except Exception as exc:  # noqa: BLE001 — malformed model output = infra failure
            raise OpenRouterError(f"could not parse energy assessment: {exc}") from exc

    def review_accreditation(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        declared_org: str,
    ) -> AccreditationAssessment:
        instructions = _ACCRED_INSTRUCTIONS.format(org=declared_org or "(unknown)")
        payload = self._chat(_ACCRED_SYSTEM, instructions, data, content_type, filename)
        try:
            return AccreditationAssessment.model_validate({**payload, "filename": filename})
        except Exception as exc:  # noqa: BLE001
            raise OpenRouterError(f"could not parse accreditation assessment: {exc}") from exc

    # -- internals ---------------------------------------------------------

    def _chat(
        self, system: str, instructions: str, data: bytes, content_type: str, filename: str
    ) -> dict:
        if not self._api_key:
            raise OpenRouterError("OpenRouter API key not configured")
        body = json.dumps(
            {
                "model": self._model,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instructions},
                            self._document_part(data, content_type, filename),
                        ],
                    },
                ],
            }
        ).encode()

        req = urllib.request.Request(  # noqa: S310 — fixed https host
            f"{self._base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers (optional but recommended).
                "HTTP-Referer": "https://green-compute.com",
                "X-Title": "GreenCompute Onboarding Review",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise OpenRouterError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        try:
            envelope = json.loads(raw)
            content = envelope["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"unexpected OpenRouter envelope: {exc}") from exc

        return _parse_json_content(content)

    def _document_part(self, data: bytes, content_type: str, filename: str) -> dict:
        ctype = (content_type or "").lower()
        if not ctype or ctype == "application/octet-stream":
            ctype = _sniff_content_type(filename)
        b64 = base64.b64encode(data).decode()
        data_uri = f"data:{ctype};base64,{b64}"
        if ctype == "application/pdf":
            # OpenRouter's file/PDF input format.
            return {
                "type": "file",
                "file": {"filename": filename or "document.pdf", "file_data": data_uri},
            }
        return {"type": "image_url", "image_url": {"url": data_uri}}


def _sniff_content_type(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith((".png",)):
        return "image/png"
    if name.endswith((".webp",)):
        return "image/webp"
    if name.endswith((".gif",)):
        return "image/gif"
    return "image/jpeg"


def _parse_json_content(content: object) -> dict:
    """Extract a JSON object from the model's message content, tolerating a
    string, a content-parts list, or stray prose/code-fences around the JSON."""
    if isinstance(content, list):  # some models return content parts
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str):
        raise OpenRouterError("model content is not text")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") : text.rfind("}") + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise OpenRouterError("no JSON object in model response") from None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OpenRouterError(f"invalid JSON in model response: {exc}") from exc
    if not isinstance(obj, dict):
        raise OpenRouterError("model response JSON is not an object")
    return obj

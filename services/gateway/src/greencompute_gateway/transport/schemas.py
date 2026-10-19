"""Response shapes for the OpenAPI spec.

Routes declare these with `responses=` (documentation only) rather than
`response_model`, so what the API returns is unchanged while the spec stops
describing every response as "any object". Agents generating clients from
/openapi.json get real field names and types.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, create_model

from greencompute_protocol import CryptoInvoice, DeploymentRecord, LedgerEntry, WorkloadSpec

# A deployment as the API returns it: the stored record without its secrets.
Deployment = create_model(
    "Deployment",
    __doc__="A running (or stopped) instance of a workload.",
    **{
        name: (field.annotation, field)
        for name, field in DeploymentRecord.model_fields.items()
        if name != "ssh_private_key"
    },
    storage_cents_per_day=(
        int,
        Field(default=0, description="While a saved rental is suspended: what it costs per day to keep, in cents. 0 otherwise."),
    ),
    deletes_at=(
        datetime | None,
        Field(default=None, description="While a rental is suspended: when it and its disk will be deleted unless resumed."),
    ),
)

Workload = WorkloadSpec


class SSHAccess(BaseModel):
    ssh_host: str
    ssh_port: int
    ssh_username: str
    ssh_command: str
    private_key: str | None = Field(
        default=None,
        description="Only for clients that supplied no ssh_public_keys; null when you brought your own key.",
    )


class Balance(BaseModel):
    balance_credits: int = Field(description="Balance in cents.")
    balance_usd: float


class LedgerItem(LedgerEntry):
    """One top-up or charge. amount_cents is negative for charges."""


class GpuPrice(BaseModel):
    gpu_model: str
    available_now: bool = Field(description="A machine with this GPU is online and rentable right now.")
    vram_gb: int | None = None
    cents_per_gpu_hour: int
    usd_per_gpu_hour: float


class GpuRentalPricing(BaseModel):
    billing: str
    deployment_fee_usd: float
    gpus: list[GpuPrice]


class ModelPrice(BaseModel):
    model: str
    usd_per_million_input_tokens: float
    usd_per_million_output_tokens: float


class InferencePricing(BaseModel):
    billing: str
    minimum_charge_usd: float
    models: list[ModelPrice]


class Pricing(BaseModel):
    currency: str
    gpu_rental: GpuRentalPricing
    inference: InferencePricing
    docs: str


class CryptoInvoiceCreated(BaseModel):
    invoice_id: str
    currency: str
    amount_crypto: float
    amount_to_send: str = Field(description="Send exactly this, as written: its last digits identify the invoice.")
    amount_usd: float
    bonus_pct: float
    total_credits: int
    deposit_address: str
    expires_at: str


class CryptoInvoiceListed(CryptoInvoice):
    amount_to_send: str
    review_status: str | None = None


def ok(model, description: str = "Success") -> dict:
    """`responses=` entry declaring a 200 body of `model` (a list via list[...])."""
    return {200: {"model": model, "description": description}}

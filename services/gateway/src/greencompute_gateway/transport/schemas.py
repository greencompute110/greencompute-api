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

# Added to each row of GET /platform/deployments: what the deployment runs.
_LIST_FIELDS = {
    "workload_kind": (str | None, Field(
        default=None, description="List only: `pod`/`vm` for a rental, `inference` for a model.",
    )),
    "workload_name": (str | None, Field(
        default=None, description="List only: the model's or workload's display name.",
    )),
    "gpu_model": (str | None, Field(
        default=None, description="List only: the GPU the workload asked for, e.g. `rtx-4090`.",
    )),
    "gpu_count": (int | None, Field(default=None, description="List only: GPUs per instance.")),
}

# A deployment as the API returns it: the stored record without its secrets.
Deployment = create_model(
    "Deployment",
    __doc__="A running (or stopped) instance of a workload.",
    **_LIST_FIELDS,
    **{
        name: (field.annotation, field)
        for name, field in DeploymentRecord.model_fields.items()
        if name not in ("ssh_private_key", "hourly_rate_cents")
    },
    hourly_rate_cents=(
        int,
        Field(
            default=10,
            ge=0,
            description=(
                "Cents per GPU per hour, quoted when the deployment is created and locked to the GPU it "
                "is placed on (never above the quote). Your deployments, GPU rentals and private models "
                "alike, are charged this × the GPU count, by the minute, while `ready`; requests to a "
                "private model are not charged per token. (Shared catalog models aren't deployments of "
                "yours: they are charged per token.)"
            ),
        ),
    ),
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


class RunningCost(BaseModel):
    deployment_id: str
    name: str
    label: str = Field(description="Name with its GPUs, e.g. `train-job (1× rtx4090)`.")
    kind: str = Field(description="`rental` (pod/VM) or `model` (your private model).")
    gpu_model: str | None
    gpu_count: int
    cents_per_hour: int = Field(description="What this deployment costs per hour while it runs.")
    resume_on_topup: bool


class Forecast(BaseModel):
    """How long your balance lasts at the current rate. Only deployments billed
    by the hour count (rentals and private models; shared models are per
    request). `hours_left` is null when nothing of yours is running."""

    balance_cents: int
    balance_usd: float
    burn_cents_per_hour: int = Field(description="What everything running costs per hour, together.")
    burn_usd_per_hour: float
    hours_left: float | None = Field(description="balance / burn; null when nothing is running.")
    runs_out_at: str | None = Field(description="When the balance reaches zero at this rate (ISO 8601), or null.")
    running: list[RunningCost]


class SpendingDay(BaseModel):
    date: str
    charged_usd: float = Field(description="Everything metered that day: rentals, private models, storage, shared models.")
    topped_up_usd: float
    adjustments_usd: float = Field(description="Refunds, adjustments and chargebacks, signed.")
    gpu_rentals_usd: float
    private_models_usd: float
    storage_usd: float
    shared_models_usd: float


class SpendingDeployment(BaseModel):
    deployment_id: str
    name: str
    kind: str = Field(description="`rental` or `model` (your private model).")
    state: str = Field(description="Now: `running`, `paused`, `ended` or `deleted`.")
    gpu_model: str | None = Field(description="The card it ran on.")
    gpu_count: int | None
    hours: float = Field(description="Hours it ran and was billed in the period (wall-clock; a 2-GPU pod for 1 hour is 1).")
    gpu_usd: float
    storage_usd: float
    total_usd: float
    total_cents: int
    first_charge_at: str | None
    last_charge_at: str | None


class SpendingSummary(BaseModel):
    from_: str = Field(alias="from")
    to: str
    opening_balance_usd: float
    closing_balance_usd: float
    charged_usd: float
    topped_up_usd: float
    adjustments_usd: float
    shared_models_usd: float
    shared_model_requests: int
    days: list[SpendingDay]
    deployments: list[SpendingDeployment] = Field(description="Most expensive first.")


class StatementLine(BaseModel):
    date: str
    type: str = Field(description="GPU rental, Private model, Saved-pod storage, Shared models, Top-up, Refund, Adjustment…")
    description: str
    quantity: str | None = Field(description="e.g. `23.4 h` of GPU time or `3 days` of storage, where it applies.")
    deployment_id: str | None
    amount_cents: int = Field(description="Positive adds to your balance, negative is a charge.")
    amount_usd: float


class Statement(BaseModel):
    month: str
    from_: str = Field(alias="from")
    to: str
    opening_balance_cents: int
    closing_balance_cents: int
    credits_cents: int
    charges_cents: int
    charges_by_type_cents: dict[str, int]
    lines: list[StatementLine] = Field(
        description="opening + credits - charges = closing; closing for the current month is your balance now.",
    )


from greencompute_gateway.application.fleets import MAX_TARGET as MAX_FLEET_TARGET  # noqa: E402


class FleetCreate(BaseModel):
    workload_id: str = Field(description="A pod workload of yours.")
    target: int = Field(ge=1, le=MAX_FLEET_TARGET, description="How many rentals the fleet keeps (or starts).")
    replace: bool = Field(default=True, description="true: replace any that stops (keep N running). "
                                                    "false: start `target` as GPUs free up, then stop.")
    name_prefix: str | None = Field(default=None, description="Members are named <prefix>-01, -02, … "
                                                              "(GC_DEPLOYMENT_NAME). Default: from the workload's name.")
    max_runtime_minutes: int | None = Field(default=None, ge=1, le=43200, description="Per member, as on a deployment.")
    save_on_exhaustion: bool = False
    resume_on_topup: bool = Field(default=False, description="Needs save_on_exhaustion.")


class FleetUpdate(BaseModel):
    target: int | None = Field(default=None, ge=1, le=MAX_FLEET_TARGET)
    paused: bool | None = Field(default=None, description="true: start and replace nothing; false: resume.")


class FleetMember(BaseModel):
    deployment_id: str
    name: str
    slot: int
    state: str
    ready_at: str | None
    last_error: str | None


class Fleet(BaseModel):
    fleet_id: str
    workload_id: str
    name_prefix: str
    target: int
    replace: bool
    state: str = Field(description="`active`, `paused`, `completed` (replace=false, all have run) or `stopped`.")
    status: str = Field(description="What the fleet is doing or waiting for, in plain English.")
    running: int
    starting: int
    paused: int = Field(description="Members paused (e.g. the balance ran out); they are not replaced.")
    max_runtime_minutes: int | None
    save_on_exhaustion: bool
    resume_on_topup: bool
    usd_per_hour_at_target: float | None
    members: list[FleetMember] = Field(description="Live members, and those that ended in the last day; newest first.")
    created_at: str | None
    updated_at: str | None


class LedgerItem(LedgerEntry):
    """One top-up or charge. amount_cents is negative for charges."""


class GpuPrice(BaseModel):
    gpu_model: str
    online: bool = Field(
        description="A machine with this GPU is online and taking rentals. false: a create asking for it returns 400 "
                    "(the same test as /platform/nodes/supported).",
    )
    available_now: bool = Field(description="At least one GPU of this model is free this instant (available_gpus > 0).")
    available_gpus: int = Field(
        description="GPUs of this model free this instant across all machines, net of everything already placed or "
                    "reserved: the scheduler's own count. 0: a create is refused at once with 503 no_capacity.",
    )
    max_gpus_per_deployment: int = Field(
        description="The most GPUs of this model free together on one machine: the largest gpu_count one deployment "
                    "can get right now.",
    )
    vram_gb: int | None = None
    cents_per_gpu_hour: int
    usd_per_gpu_hour: float
    usd_per_gpu_hour_paid_in_alpha: float = Field(
        description="What an hour costs you when you top up in subnet 110 alpha, which is credited "
                    "payment_bonuses.alpha extra: usd_per_gpu_hour / (1 + bonus). Fees your wallet or "
                    "exchange charges to move or swap alpha are not included.",
    )


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


class PrivateModelPricing(BaseModel):
    billing: str
    idle_pause_minutes: int = Field(
        description="A private model with no requests for this long is paused, and costs nothing while paused.",
    )


class Pricing(BaseModel):
    currency: str
    payment_bonuses: dict[str, float] = Field(
        description="Extra credit per top-up method, as a fraction: {\"alpha\": 0.1} means $10 of alpha "
                    "adds $11. Methods not listed (card, USDC, USDT, TAO) are credited at par.",
    )
    gpu_rental: GpuRentalPricing
    inference: InferencePricing
    private_model: PrivateModelPricing = Field(
        description="Your own deployed model: the GPU rental rate × its GPU count, by the minute while it runs.",
    )
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

# greenference-api

Validator-side services for the Green Compute subnet. Three production services share one Postgres:

| Service | Port | Role |
|---|---|---|
| [`services/gateway`](services/gateway) | 28000 | User-facing ingress: OpenAI-compatible `/v1/chat/completions`, API-key auth, rate limits, per-token billing, miner routing, usage streaming, admin credit/debit |
| [`services/control-plane`](services/control-plane) | 28001 | Miner registration, heartbeats, capacity tracking, lease placement, deployment lifecycle, per-GPU-hour metering |
| [`services/validator`](services/validator) | 28002 | Probes + scoring + Flux orchestration + weight publishing + signed audit reports |

Supporting services:

| Path | Role |
|---|---|
| [`services/builder`](services/builder) | Resumable container-image build jobs with stage progression + SSE log streaming |
| [`packages/persistence`](packages/persistence) | Shared SQLAlchemy ORM, bus, metrics, rate-limiter |
| [`alembic`](alembic) | Schema migrations (currently up to `20260424_0040`) |
| [`infra/production`](infra/production) | Docker Compose for a validator host (api-only, no GPU) |
| [`infra/local`](infra/local) | Docker Compose for a full local stack incl. mock miner |
| [`tests`](tests) | Unit + integration coverage |

Full architecture + setup docs: [../README.md](../README.md).

## what each service owns

### gateway
- **OpenAI-compat routing**: `/v1/chat/completions` resolves the requested model (catalog slug or private workload), picks a healthy miner deployment via round-robin with latency-EMA sorting, streams or buffers the response, debits per-token cost.
- **Catalog vs private**: the same endpoint serves both tiers. Catalog models are slug-resolved via `workload.name`; private endpoints resolve via `workload_id` UUIDs.
- **Billing**: pre-flight balance gate; on success, debits user + accrues miner reporting row + records demand-stats tick.
- **Streaming fix**: injects `stream_options.include_usage=true` so vLLM emits a final usage chunk (otherwise streaming calls would be un-billable).
- Auth: API-key-scoped user accounts, admin key bypasses balance gate.

### control-plane
- Miner registration (`POST /miner/v1/register`), heartbeats (`/miner/v1/heartbeat`), capacity reports (`/miner/v1/capacity`).
- Scheduler + placement policy: finds eligible miner nodes respecting health, staleness, cooldown, VRAM, GPU count.
- Lease assignment → `LeaseAssignmentORM`; miners poll `GET /miner/v1/leases/{hotkey}` to pick up.
- Hourly pod-rental metering with millicent fractional accumulator.
- Idle-kill on private-endpoint inference workloads (`managed_by != flux`) after 30 min of zero invocations.

### validator
- Probe mechanism: nonce-bearing canary prompts miners with a random token; miner must echo it back verbatim (guards against caching/proxying).
- `ScoreEngine`: multi-factor formula combining `capacity_weight × security^α × reliability^β × performance^γ × fraud_penalty × utilization^δ × (1 + rental_bonus)`.
- **Flux orchestrator**: fleet brain that allocates per-miner GPU slots between inference/rental, picks catalog models to host based on demand, writes `DeploymentORM` rows that miners consume via existing sync_leases.
- Demand-reactive scaling: per-minute `inference_demand_stats` table + blended 10-min/1-hour EMA → `target_replicas` per catalog model with 5-min scale-down hysteresis.
- **Audit publishing**: every Bittensor epoch (~360 blocks / 72 min, same tempo on netuid 110 mainnet + netuid 16 testnet), generates a signed audit report (probes + scorecards + weight snapshot + chain commit), SHA256-anchors the hash on-chain via `Commitments.set_commitment`, exposes publicly at `/validator/v1/audit/*`.

## audit endpoints (public, no auth)

For independent verifiers running [`greenference-audit`](../greenference-audit):

| Endpoint | Returns |
|---|---|
| `GET /validator/v1/audit/reports?limit=100&offset=0` | Paginated index of published reports with `{epoch_id, report_sha256, chain_commitment_tx}` |
| `GET /validator/v1/audit/reports/{epoch_id}` | Full signed report (canonical JSON + ed25519 signature + on-chain tx) |
| `GET /validator/v1/audit/commitment/{epoch_id}` | Just the anchor info (low-bandwidth path) |
| `GET /validator/v1/audit/hotkey.pub` | Validator's SS58 hotkey so auditors can verify signatures |

## catalog endpoints

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /validator/v1/catalog` | public | Approved catalog entries (filterable by visibility) |
| `GET /validator/v1/catalog-status` | public | Running replica counts + rpm per model (powers "hot/warm/cold" badges) |
| `GET /validator/v1/catalog/submissions/status/{hotkey}` | public | Miner's own submissions by hotkey |
| `POST /validator/v1/catalog/submissions` | public | Miner proposal |
| `GET /validator/v1/catalog/submissions` | admin | List pending/approved/rejected |
| `POST /validator/v1/catalog/submissions/{id}/approve` | admin | Approves → auto-creates canonical workload, Flux starts scheduling |
| `POST /validator/v1/catalog/submissions/{id}/reject` | admin | Rejects with notes |
| `POST /validator/v1/catalog` | admin | Direct upsert (skips review) |
| `DELETE /validator/v1/catalog/{model_id}` | admin | Removes entry — Flux drops replicas on next rebalance |

## flux dashboard endpoints (admin)

| Endpoint | Returns |
|---|---|
| `GET /validator/v1/flux/dashboard` | Fleet tiles, catalog pool, per-miner summary — one-shot snapshot, UI polls every 5s |
| `GET /validator/v1/flux/demand?model_id=...&window_minutes=60` | Per-minute invocation time series |
| `GET /validator/v1/flux/events?limit=50` | Recent Flux events feed |
| `GET /validator/v1/flux/{hotkey}` | Per-miner Flux state |
| `POST /validator/v1/flux/rebalance` | Force rebalance |
| `POST /validator/v1/probes/inference/{hotkey}/{model_id}` | Manually fire an attestation canary |

## migrations

```bash
docker compose -f infra/production/docker-compose.validator.yml exec control-plane \
  python -c "from alembic.config import Config; from alembic import command; \
             cfg = Config('/app/api/alembic.ini'); command.upgrade(cfg, 'head')"
```

Current head: `20260424_0040_audit_tables` (scorecard history + audit reports + probe digest columns).

## testing

```bash
cd greenference-api
pytest                         # full suite
pytest tests/gateway -k chat   # focused: chat completion routing
```

## config

All services read env vars; defaults live in [`services/{name}/src/greenference_{name}/config.py`](services/validator/src/greenference_validator/config.py). Highlights:

- `GREENFERENCE_ADMIN_API_KEY` — bootstraps the admin API key that gates admin routes (also falls back as the inference canary auth key).
- `GREENFERENCE_BITTENSOR_ENABLED` / `_NETWORK` / `_NETUID` / `_WALLET_PATH` — on-chain integration. Network + netuid pairs: `("test", 16)` for testnet, `("finney", 110)` for mainnet. When enabled, validator publishes weights + audit commitments.
- `GREENFERENCE_FLUX_TARGET_RPM_PER_REPLICA=30` — Flux demand threshold per replica.
- `GREENFERENCE_FLUX_COOLDOWN_SECONDS=300` — scale-down hysteresis.
- `GREENFERENCE_IDLE_PRIVATE_ENDPOINT_TIMEOUT_SECONDS=1800` — idle-kill window for private endpoints.
- `GREENFERENCE_INFERENCE_CANARY_INTERVAL_SECONDS=300` — periodic attestation cadence.

## hardening highlights (recent rounds)

- Build history, attempts, logs, retry, cleanup, cancellation for container image jobs (builder).
- Deployment retry, lease history, drift detection, placement history, failover recovery (control-plane).
- Fractional-millicent metering accumulator (control-plane) — per-GPU-hour rates priced exactly even on sub-minute intervals.
- Streaming usage-chunk injection so streaming inference is billable (gateway).
- Nonce-bearing canary probes + proxy detection (validator).
- Per-epoch signed audit reports with on-chain SHA256 commitment (validator).
- Failure cooldown prevents infinite respawn on a broken miner (validator Flux).
- Admin/operator debug endpoints across all services.

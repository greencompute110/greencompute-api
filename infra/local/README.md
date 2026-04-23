# Local Stack

This stack brings up the GreenCompute V1 control plane and two bootstrap miners:

- Postgres
- Redis
- NATS JetStream
- MinIO
- OCI registry
- Alembic migration job
- Gateway
- Control plane
- Builder
- Validator
- Primary miner agent
- Failover miner agent

## Bring Up

Run the stack:

```bash
docker compose -f greencompute-api/infra/local/docker-compose.yml up -d
```

The migration job runs first and the service containers only start after `alembic upgrade head` succeeds.

## Runtime Defaults

The local stack uses Postgres as the default development path through:

`GREENCOMPUTE_DATABASE_URL=postgresql+psycopg://greencompute:greencompute@postgres:5432/greencompute`

Runtime dependency URLs are injected for Redis, NATS, MinIO, and the local OCI registry. The `builder` container also runs with `GREENCOMPUTE_BUILD_EXECUTION_MODE=live`, so build workers stage context metadata and logs into MinIO and push OCI manifests to the local registry instead of using the simulated publish path. The `builder`, `control-plane`, and both miner containers run with background workers enabled. The miner containers bootstrap two default nodes and continuously reconcile assigned leases, so both the inference happy path and the reassignment path can complete without manual reconcile calls.

Host-exposed dependency ports are configurable so the stack can coexist with other local services. Defaults:

- Gateway: `28000`
- Control plane: `28001`
- Validator: `28002`
- Builder: `28003`
- Miner agent: `28004`
- Failover miner agent: `28005`
- Postgres: `15432`
- Redis: `16379`
- NATS client: `14222`
- NATS monitor: `18222`
- MinIO API: `19000`
- MinIO console: `19001`
- Registry: `15000`

Override them with:

```bash
GREENCOMPUTE_LOCAL_GATEWAY_PORT=38000 \
GREENCOMPUTE_LOCAL_CONTROL_PLANE_PORT=38001 \
GREENCOMPUTE_LOCAL_POSTGRES_PORT=25432 \
GREENCOMPUTE_LOCAL_REDIS_PORT=26379 \
docker compose -f greencompute-api/infra/local/docker-compose.yml up -d
```

## Health Checks

Every service exposes:

- `/healthz` for liveness
- `/readyz` for readiness

For `builder`, `control-plane`, and `miner-agent`, `/readyz` also includes worker state so you can confirm the background loop has started and recorded at least one iteration.

Service ports:

- `28000` gateway
- `28001` control-plane
- `28002` validator
- `28003` builder
- `28004` miner-agent
- `28005` miner-agent-b

## Smoke Test

After the stack is healthy, run:

```bash
python greencompute-api/infra/local/smoke_test.py
```

The smoke test waits for service readiness, verifies that `builder`, `control-plane`, and `validator` are running with `bus_transport=nats`, and also requires `builder` to report `build_execution_mode=live`. It then registers a user and admin API key, publishes a validator capability for the bootstrap miner, and validates:

- build publish plus `/platform/builds/{id}` and `/platform/images/{image}/history`
- workload creation with alias and ingress host
- deployment scheduling and ready-state routing
- host-based inference routing plus routing decision debug output
- invocation persistence and recent invocation export
- usage aggregation
- server, node, and placement debug views
- validator probe scoring and weight snapshot publication

To validate failover behavior against the running compose stack:

```bash
python greencompute-api/infra/local/smoke_test.py --check-failover
```

Failover mode marks the primary miner unhealthy through its public agent API, waits for `/platform/v1/debug/reassignments` to record the event, waits for the deployment to become ready again on the failover miner, and then verifies that the next routed inference request returns from the failover hotkey.

To validate restart and recovery behavior against the running compose stack:

```bash
python greencompute-api/infra/local/smoke_test.py --check-recovery
```

By default, recovery mode restarts `control-plane`, `builder`, and `miner-agent`, waits for them to become ready again, then verifies the same deployment is still routable and usage continues to aggregate. You can override the restart set with:

```bash
GREENCOMPUTE_STACK_RESTART_SERVICES=control-plane,validator python greencompute-api/infra/local/smoke_test.py --check-recovery
```

To validate operational surfaces exposed by the stack:

```bash
python greencompute-api/infra/local/smoke_test.py --check-ops
```

Ops mode verifies Prometheus-style `/_metrics` output from the API-side services and checks `/platform/v1/debug/workers` plus `/platform/v1/debug/event-deliveries` on the control plane.

To validate miner runtime ownership against the running compose stack:

```bash
python greencompute-api/infra/local/smoke_test.py --check-miner-runtime
```

Miner-runtime mode verifies:

- miner runtime records exist for the routed deployment
- staged artifact paths are created on the miner
- process-backed runtimes expose a local runtime URL and process id when the environment allows local socket binds
- runtime summaries expose mode and backend breakdowns
- runtime summary and runtime detail endpoints reflect the ready backend
- miner deployment health reflects backend health
- both one-shot and streamed inference go through a ready runtime

To validate failure-mode handling against the running compose stack:

```bash
python greencompute-api/infra/local/smoke_test.py --check-failures
```

Failure mode validation covers:

- transient builder failure followed by cleanup and retry
- build attempts and failed-build operator visibility
- deployment retry exhaustion when no compatible capacity exists
- lease history and miner-drift debug visibility
- consolidated operator status output

To validate operator actions against the running compose stack:

```bash
python greencompute-api/infra/local/smoke_test.py --check-operator-actions
```

Operator-action validation covers:

- build cancellation plus persisted build logs
- miner drain visibility through placement exclusions
- manual deployment requeue
- manual deployment failure
- miner undrain recovery

## Local Runbook

When debugging a local stack issue, the highest-signal checks are:

- `GET /readyz` on `gateway`, `control-plane`, `builder`, `validator`, and both miners
- `GET /agent/v1/runtimes`, `/runtimes/summary`, `/runtimes/failed`, and `/runtimes/{deployment_id}` on each miner
- `GET /platform/v1/debug/workers` on `control-plane`
- `GET /platform/v1/debug/event-deliveries` on `control-plane`
- `GET /platform/v1/debug/routing-decisions` on `gateway`
- `GET /platform/v1/debug/build-failures` on `gateway`
- `GET /platform/builds/{id}/jobs`, `/jobs/latest`, `/jobs/latest/timeline`, `/logs`, `/logs/stream`, and `/attempts/{attempt}` on `gateway`
  `jobs/latest` now includes `last_completed_stage` and `stage_state` for recovery inspection.
- `POST /platform/builds/{id}/jobs/latest/cancel` and `/restart` on `gateway`
- `GET /platform/builds/recovery/status` and `POST /platform/builds/recovery` on `gateway`
- `GET /platform/v1/debug/invocation-failures` on `gateway`
- `GET /platform/v1/debug/servers`, `/nodes`, and `/placements` on `control-plane`
- `GET /platform/v1/debug/lease-history`, `/deployment-retries`, `/miner-drift`, `/placement-exclusions`, `/deployment-failures`, and `/status` on `control-plane`
- `POST /platform/v1/debug/deployments/{id}/requeue` and `/fail` on `control-plane`
- `POST /platform/v1/debug/miners/{hotkey}/drain` and `/undrain` on `control-plane`
- `GET /platform/v1/invocations/exports/recent` on `gateway`
- `GET /_metrics` on each API-side service

## Config Matrix

Use these runtime profiles as the working defaults:

- Local: the compose stack in `infra/local` with Postgres, NATS, MinIO, registry, and two bootstrap miners
- Single-node staging: one API stack with one miner, live builder execution, and admin/operator endpoints enabled behind a private network
- Multi-miner staging: one API stack with at least two miners so reassignment, drain policy, and placement exclusions can be exercised before production

The compose stack expects these runtime secrets and defaults:

- Postgres: `greencompute` / `greencompute`
- MinIO: `greencompute` / `greencompute`
- Registry: local unauthenticated `registry:5000`
- Miner auth secrets:
  - `greencompute-miner-local-secret`
  - `greencompute-miner-failover-secret`

## Recovery Expectations

The stack validator is expected to prove these cases cleanly:

- pending workflow events survive service restarts because they are stored in Postgres
- builder restarts requeue stale job deliveries and republish in-flight build stages
- deployments remain queryable after `gateway` or `control-plane` restarts
- usage aggregation continues after a worker restart
- the bootstrap miners reconnect and resume reconcile loops on restart
- miner runtime recovery counters surface in `/readyz` and `/agent/v1/runtimes/summary`
- process-backed miner runtimes either resume with the same local runtime metadata or rebuild deterministically after restart
- a deployment can be reassigned from the primary miner to the failover miner through the control-plane worker loop

# Local Stack

This stack brings up the Greenference V1 control plane and two bootstrap miners:

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
docker compose -f greenference-api/infra/local/docker-compose.yml up -d
```

The migration job runs first and the service containers only start after `alembic upgrade head` succeeds.

## Runtime Defaults

The local stack uses Postgres as the default development path through:

`GREENFERENCE_DATABASE_URL=postgresql+psycopg://greenference:greenference@postgres:5432/greenference`

Runtime dependency URLs are injected for Redis, NATS, MinIO, and the local OCI registry. The `builder` container also runs with `GREENFERENCE_BUILD_EXECUTION_MODE=live`, so build workers stage context metadata and logs into MinIO and push OCI manifests to the local registry instead of using the simulated publish path. The `builder`, `control-plane`, and both miner containers run with background workers enabled. The miner containers bootstrap two default nodes and continuously reconcile assigned leases, so both the inference happy path and the reassignment path can complete without manual reconcile calls.

## Health Checks

Every service exposes:

- `/healthz` for liveness
- `/readyz` for readiness

For `builder`, `control-plane`, and `miner-agent`, `/readyz` also includes worker state so you can confirm the background loop has started and recorded at least one iteration.

Service ports:

- `8000` gateway
- `8001` control-plane
- `8002` validator
- `8003` builder
- `8004` miner-agent
- `8005` miner-agent-b

## Smoke Test

After the stack is healthy, run:

```bash
python greenference-api/infra/local/smoke_test.py
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
python greenference-api/infra/local/smoke_test.py --check-failover
```

Failover mode marks the primary miner unhealthy through its public agent API, waits for `/platform/v1/debug/reassignments` to record the event, waits for the deployment to become ready again on the failover miner, and then verifies that the next routed inference request returns from the failover hotkey.

To validate restart and recovery behavior against the running compose stack:

```bash
python greenference-api/infra/local/smoke_test.py --check-recovery
```

By default, recovery mode restarts `control-plane`, `builder`, and `miner-agent`, waits for them to become ready again, then verifies the same deployment is still routable and usage continues to aggregate. You can override the restart set with:

```bash
GREENFERENCE_STACK_RESTART_SERVICES=control-plane,validator python greenference-api/infra/local/smoke_test.py --check-recovery
```

To validate operational surfaces exposed by the stack:

```bash
python greenference-api/infra/local/smoke_test.py --check-ops
```

Ops mode verifies Prometheus-style `/_metrics` output from the API-side services and checks `/platform/v1/debug/workers` plus `/platform/v1/debug/event-deliveries` on the control plane.

To validate failure-mode handling against the running compose stack:

```bash
python greenference-api/infra/local/smoke_test.py --check-failures
```

Failure mode validation covers:

- transient builder failure followed by cleanup and retry
- build attempts and failed-build operator visibility
- deployment retry exhaustion when no compatible capacity exists
- lease history and miner-drift debug visibility
- consolidated operator status output

To validate operator actions against the running compose stack:

```bash
python greenference-api/infra/local/smoke_test.py --check-operator-actions
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
- `GET /platform/v1/debug/workers` on `control-plane`
- `GET /platform/v1/debug/event-deliveries` on `control-plane`
- `GET /platform/v1/debug/routing-decisions` on `gateway`
- `GET /platform/v1/debug/build-failures` on `gateway`
- `GET /platform/builds/{id}/logs` and `/attempts/{attempt}` on `gateway`
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

- Postgres: `greenference` / `greenference`
- MinIO: `greenference` / `greenference`
- Registry: local unauthenticated `registry:5000`
- Miner auth secrets:
  - `greenference-miner-local-secret`
  - `greenference-miner-failover-secret`

## Recovery Expectations

The stack validator is expected to prove these cases cleanly:

- pending workflow events survive service restarts because they are stored in Postgres
- deployments remain queryable after `gateway` or `control-plane` restarts
- usage aggregation continues after a worker restart
- the bootstrap miners reconnect and resume reconcile loops on restart
- a deployment can be reassigned from the primary miner to the failover miner through the control-plane worker loop

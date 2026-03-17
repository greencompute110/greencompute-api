# greenference-api

Greenference API-side services and infrastructure:

- `services/gateway`
- `services/control-plane`
- `services/validator`
- `services/builder`
- `tests`
- `docs/adr`
- `infra/local`
- `infra/helm`

Current hardening includes:

- build history, attempts, logs, retry, cleanup, and cancellation controls
- deployment retry, lease history, miner drift, placement history, and failover recovery
- admin/operator debug endpoints for status, failures, requeue, fail, drain, and undrain flows
- local stack smoke coverage for happy path, recovery, failover, ops, failures, and operator actions

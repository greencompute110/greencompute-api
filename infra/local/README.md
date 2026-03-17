# Local Stack

This stack brings up the base Greenference dependencies:

- Postgres
- Redis
- NATS JetStream
- MinIO
- OCI registry
- Gateway
- Control plane
- Validator
- Builder
- Miner agent
- Alembic migration job

Run:

```bash
docker compose -f greenference-api/infra/local/docker-compose.yml up -d
```

The local stack uses Postgres as the default development path through:

`GREENFERENCE_DATABASE_URL=postgresql+psycopg://greenference:greenference@postgres:5432/greenference`

Runtime dependency URLs are also injected for Redis, NATS, MinIO, and the local OCI registry. The builder and control-plane containers run with background workers enabled so accepted builds and lease timeout checks progress without direct in-process test calls.

Service health endpoints:

- `/healthz` for liveness
- `/readyz` for database-backed readiness

Service ports:

- `8000` gateway
- `8001` control-plane
- `8002` validator
- `8003` builder
- `8004` miner-agent

# Local Stack

This stack brings up the base Greenference dependencies:

- Postgres
- Redis
- NATS JetStream
- MinIO
- OCI registry

Run:

```bash
docker compose -f greenference-api/infra/local/docker-compose.yml up -d
```


Each service package can then be started with its own FastAPI entrypoint.

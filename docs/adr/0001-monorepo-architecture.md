# ADR 0001: GreenCompute Project Split Architecture

## Status

Accepted

## Context

Reference upstream repositories separate SDK, validator, and miner concerns, but they also mix
transport, orchestration, and policy logic heavily inside large modules. GreenCompute needs a
cleaner substrate for an inference-first subnet that later expands into pods and VMs without a
full rewrite.

## Decision

GreenCompute uses a Python-first split that mirrors the upstream references:

- `greencompute` for shared protocol and SDK
- `greencompute-api` for gateway, control-plane, validator, and builder
- `greencompute-miner` for miner-side services and infra

Within each top-level project, code follows the same structure:

- thin service transport layers
- application services that own use cases
- domain modules that own policy and state transitions
- infrastructure adapters for persistence or external systems

## Consequences

- Shared contracts remain stable in `greencompute/protocol`.
- Each top-level project can carry its own workspace config, tests, and tooling.
- Service packages stay independently deployable.
- Core scheduling, scoring, metering, and signing logic are testable without HTTP.
- Pods, VMs, and confidential-compute support can extend the existing enums and interfaces.

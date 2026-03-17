# ADR 0001: Greenference Project Split Architecture

## Status

Accepted

## Context

The reference Chutes repositories separate SDK, validator, and miner concerns, but they also mix
transport, orchestration, and policy logic heavily inside large modules. Greenference needs a
cleaner substrate for an inference-first subnet that later expands into pods and VMs without a
full rewrite.

## Decision

Greenference uses a Python-first split that mirrors the Chutes references:

- `greenference` for shared protocol and SDK
- `greenference-api` for gateway, control-plane, validator, and builder
- `greenference-miner` for miner-side services and infra

Within each top-level project, code follows the same structure:

- thin service transport layers
- application services that own use cases
- domain modules that own policy and state transitions
- infrastructure adapters for persistence or external systems

## Consequences

- Shared contracts remain stable in `greenference/protocol`.
- Each top-level project can carry its own workspace config, tests, and tooling.
- Service packages stay independently deployable.
- Core scheduling, scoring, metering, and signing logic are testable without HTTP.
- Pods, VMs, and confidential-compute support can extend the existing enums and interfaces.

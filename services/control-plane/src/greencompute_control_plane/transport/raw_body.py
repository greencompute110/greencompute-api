"""The exact bytes a miner sent, for checking its signature.

Every route that verifies a miner's signature over a request body takes
`raw_body: RawBody` and passes it to require_miner_request. Never verify over a
re-serialised model: on 2026-10-29 an optional field added to
DeploymentStatusUpdate made the control plane's re-serialisation differ from
what nodes on the previous protocol had signed, and every status report got
401 while heartbeats (unchanged model) kept passing.

Shared by the control plane and the validator. As a dependency, a route
stays a plain `def` and keeps running in the threadpool.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request


async def _raw_body(request: Request) -> bytes:
    return await request.body()


RawBody = Annotated[bytes, Depends(_raw_body)]

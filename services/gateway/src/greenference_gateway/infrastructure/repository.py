from __future__ import annotations

from dataclasses import dataclass, field

from greenference_protocol import APIKeyRecord


@dataclass
class GatewayRepository:
    api_keys: dict[str, APIKeyRecord] = field(default_factory=dict)


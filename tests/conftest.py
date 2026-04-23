from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("GREENCOMPUTE_BUILD_EXECUTION_MODE", "simulated")

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
SRC_PATHS = [
    WORKSPACE_ROOT / "greencompute/protocol/src",
    ROOT / "packages/persistence/src",
    ROOT / "services/gateway/src",
    ROOT / "services/control-plane/src",
    ROOT / "services/validator/src",
    ROOT / "services/builder/src",
    WORKSPACE_ROOT / "greencompute-miner/services/miner-agent/src",
]

for path in SRC_PATHS:
    sys.path.insert(0, str(path))

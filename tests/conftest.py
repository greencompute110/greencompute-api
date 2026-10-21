from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("GREENCOMPUTE_BUILD_EXECUTION_MODE", "simulated")
# Tests never email anyone: payment emails are off and point nowhere unless a
# test turns them on (tests/unit/test_payment_emails.py, with the POST mocked).
os.environ["GREENCOMPUTE_PAYMENT_EMAILS_ENABLED"] = "false"
os.environ["WEBSITE_NOTIFY_URL"] = "http://127.0.0.1:9/api/internal/notify"

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

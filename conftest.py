import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
for p in ["protocols", "vm-agent", "services/computer-runtime", "services/vm-runtime",
          "services/tool-router", "services/llm-router", "services/agent-runtime", "services/api"]:
    sys.path.insert(0, str(ROOT / p))

os.environ.setdefault("AIW_SETTLE_SCALE", "0.25")  # tests drive a mock desktop with nothing to wait for

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _shop_routes_file(tmp_path, monkeypatch):
    """Probe v10 remembers which route priced a sku (~/.config/aiw/shop/routes.json): never the real file in tests."""
    monkeypatch.setenv("AIW_SHOP_ROUTES", str(tmp_path / "shop-routes.json"))

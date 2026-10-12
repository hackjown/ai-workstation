import sys
from pathlib import Path

ROOT = Path(__file__).parent
for p in ["protocols", "vm-agent", "services/computer-runtime", "services/vm-runtime",
          "services/tool-router", "services/llm-router", "services/agent-runtime", "services/api"]:
    sys.path.insert(0, str(ROOT / p))

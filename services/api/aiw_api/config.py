"""Runtime configuration from environment (see .env.example)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class MachineConfig:
    id: str
    provider: str
    provider_vm_id: str
    agent_endpoint: str          # ws://VM_IP:8765
    desktop_http: str            # http://VM_IP:6080  (noVNC + websockify)
    name: str = ""


@dataclass
class Settings:
    api_token: str | None = os.environ.get("AIW_API_TOKEN")
    agent_token: str | None = os.environ.get("VM_AGENT_TOKEN")
    db_path: str = os.environ.get("AIW_DB", str(ROOT / "workspace" / "aiw.db"))
    artifacts_dir: str = os.environ.get("AIW_ARTIFACTS", str(ROOT / "workspace" / "artifacts"))
    web_dir: str = str(ROOT / "apps" / "web")
    pve_enabled: bool = bool(os.environ.get("PVE_HOST"))
    machines: list[MachineConfig] = field(default_factory=list)

    def __post_init__(self):
        raw = os.environ.get("AIW_MACHINES")
        if raw:
            self.machines = [MachineConfig(**m) for m in json.loads(raw)]
        else:
            ip = os.environ.get("AIW_VM_IP", "127.0.0.1")
            self.machines = [MachineConfig(
                id="ai-linux-001", name="ai-linux-001", provider="proxmox",
                provider_vm_id=os.environ.get("AIW_VM_ID", "100"),
                agent_endpoint=f"ws://{ip}:{os.environ.get('VM_AGENT_PORT', '8765')}",
                desktop_http=f"http://{ip}:{os.environ.get('AIW_NOVNC_PORT', '6080')}")]

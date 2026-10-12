"""VM Runtime (spec §15). The ONLY component allowed to talk to PVE (spec §30.5).

Agent runtime never imports this module; the API layer does, and exposes
start/stop/restart to the user, not to the LLM.
"""
from __future__ import annotations

import abc

from aiw_protocols import VMStatus


class VMProvider(abc.ABC):
    @abc.abstractmethod
    async def status(self, vm_id: str) -> VMStatus: ...
    @abc.abstractmethod
    async def start(self, vm_id: str) -> dict: ...
    @abc.abstractmethod
    async def stop(self, vm_id: str, force: bool = False) -> dict: ...
    @abc.abstractmethod
    async def restart(self, vm_id: str) -> dict: ...
    @abc.abstractmethod
    async def snapshot(self, vm_id: str, name: str, description: str = "") -> dict: ...
    @abc.abstractmethod
    async def restore(self, vm_id: str, snapshot: str) -> dict: ...
    async def list_snapshots(self, vm_id: str) -> list[dict]:
        raise NotImplementedError
    async def create(self, template: dict) -> dict:  # V1: not implemented (spec §15)
        raise NotImplementedError("create is planned after V1")
    async def destroy(self, vm_id: str) -> dict:
        raise NotImplementedError("destroy is planned after V1")


class VMRuntime:
    """Maps logical machine ids (ai-linux-001) to provider VM ids (100)."""

    def __init__(self, provider: VMProvider, machines: dict[str, str]):
        self.provider = provider
        self.machines = machines

    def _vmid(self, machine_id: str) -> str:
        if machine_id not in self.machines:
            raise KeyError(f"unknown machine {machine_id}")
        return self.machines[machine_id]

    async def status(self, machine_id: str):
        return await self.provider.status(self._vmid(machine_id))

    async def start(self, machine_id: str):
        return await self.provider.start(self._vmid(machine_id))

    async def stop(self, machine_id: str, force: bool = False):
        return await self.provider.stop(self._vmid(machine_id), force)

    async def restart(self, machine_id: str):
        return await self.provider.restart(self._vmid(machine_id))

    async def snapshot(self, machine_id: str, name: str):
        return await self.provider.snapshot(self._vmid(machine_id), name)

    async def restore(self, machine_id: str, snapshot: str):
        return await self.provider.restore(self._vmid(machine_id), snapshot)

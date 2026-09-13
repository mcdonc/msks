"""The driver seam every backend implements (#1).

Only the lifecycle surface a workspace needs to exist: launch, info,
shutdown (graceful, deadline-bounded), kill (immediate), cleanup
(artifact removal). Nothing about workspaces, users, or persistence
lives below or above this line on the driver's side.
"""

import abc

from .errors import MicrovmError
from .spec import VmInfo, VmSpec


class MicrovmDriver(abc.ABC):
    """Abstract per-backend VM lifecycle implementation."""

    @abc.abstractmethod
    async def launch(self, spec: VmSpec) -> None:
        """Create and start the VM described by ``spec``."""

    @abc.abstractmethod
    async def info(self, workspace_id: str) -> VmInfo:
        """Report the current lifecycle state of one workspace."""

    @abc.abstractmethod
    async def shutdown(self, workspace_id: str, timeout_s: float | None = None) -> None:
        """Ask the VM to power off. Backends bound to a local process
        wait for the exit within the deadline and raise on timeout;
        remote backends (k8s) request deletion with a grace period and
        do not wait for it to complete."""

    @abc.abstractmethod
    async def kill(self, workspace_id: str) -> None:
        """Terminate immediately; no graceful shutdown attempt."""

    @abc.abstractmethod
    async def cleanup(self, workspace_id: str) -> None:
        """Remove the workspace's artifacts (idempotent)."""

    async def console(self, workspace_id: str):
        """An interactive byte stream into a running workspace.

        Returns an ``(reader, writer)`` pair carrying raw bytes both
        ways. Backends without an interactive console raise
        MicrovmError; the API layer maps that to a close code, never
        a silent no-op.
        """
        raise MicrovmError(f"the {type(self).__name__} backend has no console support")

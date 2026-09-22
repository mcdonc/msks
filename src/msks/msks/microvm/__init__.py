"""The microvm seam: the klangkd ``Podman(app)`` analogue (#1).

``Microvm(app)`` is the single object workspace code talks to. It owns
no VM logic itself — it dispatches to the driver named by the live
setting ``settings.vmm.driver``, so swapping the backend that runs
workspaces changes no code above this seam.
"""

from .driver import MicrovmDriver
from .errors import MicrovmError, MicrovmTimeoutError
from .local import LocalCloudHypervisor
from .spec import VmInfo, VmSpec

__all__ = [
    "LocalCloudHypervisor",
    "MicrovmDriver",
    "MicrovmError",
    "MicrovmTimeoutError",
    "VmInfo",
    "VmSpec",
    "Microvm",
]


class Microvm:
    """The dispatching facade over the driver backends."""

    def __init__(self, app) -> None:
        self.app = app
        self.local = LocalCloudHypervisor(app)

    @property
    def driver(self) -> MicrovmDriver:
        """The active driver, resolved live from settings."""
        name = self.app.state.settings.vmm.driver
        if name == "local":
            return self.local
        raise MicrovmError(f"unknown vmm driver: {name!r}")

    async def prepare(self, spec: VmSpec) -> None:
        """Materialize one workspace's persistent artifacts (#14)."""
        await self.driver.prepare(spec)

    async def launch(self, spec: VmSpec) -> None:
        """Create and start one workspace VM."""
        await self.driver.launch(spec)

    async def info(self, workspace_id: str) -> VmInfo:
        """Report one workspace's lifecycle state."""
        return await self.driver.info(workspace_id)

    async def shutdown(
        self, workspace_id: str, timeout_s: float | None = None
    ) -> None:
        """Gracefully power off one workspace VM."""
        await self.driver.shutdown(workspace_id, timeout_s)

    async def kill(self, workspace_id: str) -> None:
        """Immediately terminate one workspace VM."""
        await self.driver.kill(workspace_id)

    async def cleanup(self, workspace_id: str) -> None:
        """Remove one workspace's artifacts."""
        await self.driver.cleanup(workspace_id)

    async def reset(self, workspace_id: str) -> None:
        """Factory-reset one workspace (drop the overlay, keep /home)."""
        await self.driver.reset(workspace_id)

    async def console(
        self,
        workspace_id: str,
        user: str | None = None,
        rows: int = 0,
        cols: int = 0,
        term: str = "xterm",
    ):
        """An interactive byte stream into a running workspace.

        Prelude images (#63) negotiate ``user`` and the terminal
        geometry and type in-band; legacy images serve the raw root
        shell.
        """
        return await self.driver.console(workspace_id, user, rows, cols, term)

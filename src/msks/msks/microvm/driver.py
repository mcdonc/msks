"""The driver seam every backend implements (#1).

Only the lifecycle surface a workspace needs to exist: prepare
(artifacts), launch, info, shutdown (graceful, deadline-bounded),
kill (immediate), cleanup (artifact removal), reset (factory reset).
Nothing about workspaces, users, or persistence lives below or above
this line on the driver's side.
"""

import abc

from .errors import MicrovmError
from .spec import VmInfo, VmSpec


class MicrovmDriver(abc.ABC):
    """Abstract per-backend VM lifecycle implementation."""

    @abc.abstractmethod
    async def prepare(self, spec: VmSpec) -> None:
        """Materialize the workspace's persistent artifacts (#14).

        Called at workspace create (and idempotently by launch on
        backends that boot from local files): the local backend
        creates the root overlay and home volume; the k8s backend
        creates the per-workspace PVC that holds both."""

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

    @abc.abstractmethod
    async def reset(self, workspace_id: str) -> None:
        """Factory reset: drop the root overlay, keep the home volume.

        The next boot presents the pristine base image again —
        provisioned state on the root is gone, ``/home`` data stays.
        Backends whose overlay lives where they cannot reach it raise
        MicrovmError naming the limitation."""

    async def console(
        self, workspace_id: str, user: str | None = None, rows: int = 0, cols: int = 0
    ):
        """An interactive byte stream into a running workspace.

        Returns an ``(reader, writer)`` pair carrying raw bytes both
        ways. Prelude images (#63) negotiate the identity in-band when
        ``user`` is given (with the client terminal's size in
        ``rows``/``cols``); legacy images ignore the parameters and
        serve the raw root shell. Backends without an interactive
        console raise MicrovmError; the API layer maps that to a close
        code, never a silent no-op.
        """
        raise MicrovmError(f"the {type(self).__name__} backend has no console support")

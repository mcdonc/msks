"""Local backend: one cloud-hypervisor process per workspace VM (#1).

Per-workspace layout under ``<state_dir>/vms/<workspace_id>/``:

- ``api.sock``  — the CH REST socket this process serves
- ``ch.pid``    — the CH process id (restart-surviving kill path)
- ``ch.log``    — the VMM's own stderr
- ``serial.log``— the guest serial console (file-backed serial device)
"""

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import Callable
from pathlib import Path

from ..settings import Settings
from .chapi import CloudHypervisorApi
from .driver import MicrovmDriver
from .errors import MicrovmError, MicrovmTimeoutError
from .spec import VmInfo, VmSpec, VmStatus

CH_STATE_TO_STATUS = {
    "Running": VmStatus.RUNNING,
    "Paused": VmStatus.PAUSED,
}


def vm_config(spec: VmSpec, serial_log: Path) -> dict:
    """The ``PUT /vm.create`` body for one spec (direct kernel boot)."""
    config: dict = {
        "cpus": {"boot_count": spec.cpus},
        "memory": {"size": spec.mem_mib},
        "kernel": {"path": str(spec.kernel)},
        "cmdline": {"args": spec.cmdline},
        "disks": [{"path": str(spec.rootfs), "is_root_device": True}],
        "serial": {"mode": "File", "file": {"path": str(serial_log)}},
    }
    if spec.initrd is not None:
        config["initramfs"] = {"path": str(spec.initrd)}
    return config


def map_ch_state(state: str | None) -> VmStatus:
    """Translate a CH ``vm.info`` state string."""
    if state is None:
        return VmStatus.UNKNOWN
    return CH_STATE_TO_STATUS.get(state, VmStatus.UNKNOWN)


class LocalCloudHypervisor(MicrovmDriver):
    """Drives per-VM cloud-hypervisor processes on this host."""

    def __init__(self, app) -> None:
        self.app = app
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def _settings(self) -> Settings:
        return self.app.state.settings

    def _dir(self, workspace_id: str) -> Path:
        return self._settings().vmm.state_dir / "vms" / workspace_id

    async def launch(self, spec: VmSpec) -> None:
        vmm = self._settings().vmm
        vm_dir = self._dir(spec.workspace_id)
        vm_dir.mkdir(parents=True, exist_ok=True)
        socket_path = vm_dir / "api.sock"
        serial_log = vm_dir / "serial.log"
        log_file = open(vm_dir / "ch.log", "wb")
        proc = await asyncio.create_subprocess_exec(
            vmm.cloud_hypervisor,
            "--api-socket",
            str(socket_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
        log_file.close()
        self._procs[spec.workspace_id] = proc
        (vm_dir / "ch.pid").write_text(str(proc.pid))
        await self._wait_for_socket(socket_path, vmm.socket_wait_timeout_s)
        api = CloudHypervisorApi(socket_path, vmm.request_timeout_s)
        try:
            await api.create(vm_config(spec, serial_log))
            await api.start()
        finally:
            await api.aclose()

    async def _wait_for_socket(self, socket_path: Path, timeout_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not socket_path.exists():
            if asyncio.get_running_loop().time() >= deadline:
                raise MicrovmTimeoutError(
                    f"cloud-hypervisor API socket never appeared: {socket_path}"
                )
            await asyncio.sleep(0.05)

    async def info(self, workspace_id: str) -> VmInfo:
        vm_dir = self._dir(workspace_id)
        if not vm_dir.is_dir():
            return VmInfo(workspace_id, VmStatus.ABSENT)
        socket_path = vm_dir / "api.sock"
        if not socket_path.exists():
            return VmInfo(workspace_id, VmStatus.STOPPED, self._pid(workspace_id))
        vmm = self._settings().vmm
        api = CloudHypervisorApi(socket_path, vmm.request_timeout_s)
        try:
            document = await api.info()
        except MicrovmError:
            return VmInfo(workspace_id, VmStatus.UNKNOWN, self._pid(workspace_id))
        finally:
            await api.aclose()
        return VmInfo(
            workspace_id, map_ch_state(document.get("state")), self._pid(workspace_id)
        )

    def _pid(self, workspace_id: str) -> int | None:
        pid_file = self._dir(workspace_id) / "ch.pid"
        with contextlib.suppress(OSError, ValueError):
            return int(pid_file.read_text().strip())
        return None

    async def shutdown(self, workspace_id: str, timeout_s: float | None = None) -> None:
        vmm = self._settings().vmm
        socket_path = self._dir(workspace_id) / "api.sock"
        if not socket_path.exists():
            self._procs.pop(workspace_id, None)
            return
        timeout = timeout_s if timeout_s is not None else vmm.shutdown_timeout_s
        api = CloudHypervisorApi(socket_path, vmm.request_timeout_s)
        await api.shutdown()
        await api.aclose()
        await self._await_exit(workspace_id, timeout)

    async def _await_exit(self, workspace_id: str, timeout_s: float) -> None:
        proc = self._procs.get(workspace_id)
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout_s)
        except TimeoutError:
            raise MicrovmTimeoutError(
                f"VM {workspace_id} did not exit within {timeout_s}s of shutdown"
            ) from None

    async def kill(self, workspace_id: str) -> None:
        proc = self._procs.pop(workspace_id, None)
        if proc is not None:
            proc.kill()
            await proc.wait()
            return
        pid = self._pid(workspace_id)
        if pid is None:
            raise MicrovmError(f"no such VM: {workspace_id}")
        os.kill(pid, signal.SIGKILL)

    async def cleanup(self, workspace_id: str) -> None:
        self._procs.pop(workspace_id, None)
        shutil.rmtree(self._dir(workspace_id), ignore_errors=True)


# Re-exported for tests that want to hook socket-appearance events.
SocketWaiter = Callable[[Path, float], object]

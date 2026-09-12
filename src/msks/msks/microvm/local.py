"""Local backend: one cloud-hypervisor process per workspace VM (#1).

Per-workspace layout under ``<state_dir>/vms/<workspace_id>/``:

- ``api.sock``  — the CH REST socket this process serves
- ``ch.pid``    — the CH process id (restart-surviving kill path)
- ``ch.log``    — the VMM's own stderr
- ``serial.log``— the guest serial console (file-backed serial device)

Shutdown model, matching how the VMM really behaves: a bare
``cloud-hypervisor --api-socket`` is a daemon that keeps running after
the guest powers off (``vm.info`` returns to a non-running state), so
graceful shutdown is PUT /vm.shutdown -> poll the guest down -> SIGTERM
the VMM -> wait for process exit, all under one deadline.
"""

import asyncio
import contextlib
import os
import shutil
import signal
from pathlib import Path

from .chapi import API_ROOT, CloudHypervisorApi
from .driver import MicrovmDriver
from .errors import MicrovmError, MicrovmTimeoutError
from .spec import VmInfo, VmSpec, VmStatus

CH_STATE_TO_STATUS = {
    "Created": VmStatus.STARTING,
    "Running": VmStatus.RUNNING,
    "Paused": VmStatus.PAUSED,
    "Shutdown": VmStatus.STOPPED,
}
GUEST_DOWN_STATES = ("Created", "Shutdown")
POLL_INTERVAL_S = 0.05


def vm_config(spec: VmSpec, serial_log: Path) -> dict:
    """The ``PUT /api/v1/vm.create`` body for one spec (v52 schema).

    Memory is bytes (``mem_mib`` is converted), the payload nests
    kernel/cmdline/initramfs, the serial file is a plain path string,
    and the first disk is the root device by position.
    """
    payload: dict = {
        "kernel": str(spec.kernel),
        "cmdline": spec.cmdline,
    }
    if spec.initrd is not None:
        payload["initramfs"] = str(spec.initrd)
    return {
        "cpus": {"boot_vcpus": spec.cpus, "max_vcpus": spec.cpus},
        "memory": {"size": spec.mem_mib * 1024 * 1024},
        "payload": payload,
        "disks": [{"path": str(spec.rootfs)}],
        "serial": {"mode": "File", "file": str(serial_log)},
    }


def _check_id(workspace_id: str) -> None:
    """Reject ids that would escape the vms/ directory.

    The API already enforces the charset; this is the driver-side
    backstop so no future caller can turn ``../..`` into an rmtree of
    the state directory or plant artifacts at absolute paths.
    """
    unsafe = (
        workspace_id in ("", ".", "..")
        or "/" in workspace_id
        or "\\" in workspace_id
        or workspace_id != workspace_id.strip()
    )
    if unsafe:
        raise MicrovmError(f"unsafe workspace id: {workspace_id!r}")


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

    def _settings(self):
        return self.app.state.settings

    def _dir(self, workspace_id: str) -> Path:
        _check_id(workspace_id)
        return self._settings().vmm.state_dir / "vms" / workspace_id

    async def launch(self, spec: VmSpec) -> None:
        vmm = self._settings().vmm
        vm_dir = self._dir(spec.workspace_id)
        self._ensure_launchable(spec.workspace_id, vm_dir)
        self._check_socket_path(vm_dir / "api.sock")
        vm_dir.mkdir(parents=True, exist_ok=True)
        socket_path = vm_dir / "api.sock"
        serial_log = vm_dir / "serial.log"
        proc = await self._spawn(vmm.cloud_hypervisor, socket_path, vm_dir / "ch.log")
        self._procs[spec.workspace_id] = proc
        (vm_dir / "ch.pid").write_text(str(proc.pid))
        try:
            await self._wait_ready(socket_path, proc, vmm.socket_wait_timeout_s)
            await self._configure_and_boot(
                spec, socket_path, serial_log, vmm.request_timeout_s
            )
        except BaseException:
            await self._reap(spec.workspace_id, proc)
            raise

    def _check_socket_path(self, socket_path: Path) -> None:
        """AF_UNIX sun_path caps at 108 bytes; fail with a named cause.

        cloud-hypervisor dies with an opaque "path must be shorter than
        SUN_LEN" when handed an over-long --api-socket, so the driver
        checks first and names the fix (shorter state_dir or id).
        """
        if len(str(socket_path).encode()) >= 108:
            raise MicrovmError(
                f"API socket path exceeds the AF_UNIX 108-byte limit: "
                f"{socket_path}; use a shorter state_dir or workspace id"
            )

    def _ensure_launchable(self, workspace_id: str, vm_dir: Path) -> None:
        if workspace_id in self._procs or self._pid_alive(self._pid(workspace_id)):
            raise MicrovmError(
                f"VM {workspace_id} already exists; shutdown or cleanup first"
            )

    async def _spawn(self, binary: str, socket_path: Path, log_path: Path):
        log_file = open(log_path, "wb")
        try:
            return await asyncio.create_subprocess_exec(
                binary,
                "--api-socket",
                str(socket_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise MicrovmError(f"cloud-hypervisor binary not found: {binary}") from exc
        finally:
            log_file.close()

    async def _configure_and_boot(
        self, spec, socket_path, serial_log, timeout_s
    ) -> None:
        api = CloudHypervisorApi(socket_path, timeout_s)
        try:
            await api.create(vm_config(spec, serial_log))
            await api.boot()
        finally:
            await api.aclose()

    async def _wait_ready(self, socket_path: Path, proc, timeout_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not socket_path.exists():
            if proc.returncode is not None:
                raise MicrovmError(
                    f"cloud-hypervisor exited with {proc.returncode} before serving "
                    f"{socket_path} (see its log)"
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise MicrovmTimeoutError(
                    f"cloud-hypervisor API socket never appeared: {socket_path}"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    async def info(self, workspace_id: str) -> VmInfo:
        vm_dir = self._dir(workspace_id)
        if not vm_dir.is_dir():
            return VmInfo(workspace_id, VmStatus.ABSENT)
        socket_path = vm_dir / "api.sock"
        pid = self._pid(workspace_id)
        if not socket_path.exists():
            return VmInfo(workspace_id, VmStatus.STOPPED, pid)
        api = CloudHypervisorApi(socket_path, self._settings().vmm.request_timeout_s)
        try:
            document = await api.info()
        except MicrovmError:
            status = VmStatus.UNKNOWN if self._pid_alive(pid) else VmStatus.STOPPED
            return VmInfo(workspace_id, status, pid)
        finally:
            await api.aclose()
        return VmInfo(workspace_id, map_ch_state(document.get("state")), pid)

    def _pid(self, workspace_id: str) -> int | None:
        pid_file = self._dir(workspace_id) / "ch.pid"
        with contextlib.suppress(OSError, ValueError):
            return int(pid_file.read_text().strip())
        return None

    def _pid_alive(self, pid: int | None) -> bool:
        if pid is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 0)
            return True
        return False

    async def shutdown(self, workspace_id: str, timeout_s: float | None = None) -> None:
        vmm = self._settings().vmm
        socket_path = self._dir(workspace_id) / "api.sock"
        if not socket_path.exists():
            self._procs.pop(workspace_id, None)
            return
        timeout = timeout_s if timeout_s is not None else vmm.shutdown_timeout_s
        deadline = asyncio.get_running_loop().time() + timeout
        api = CloudHypervisorApi(socket_path, vmm.request_timeout_s)
        try:
            await api.shutdown()
            await self._poll_guest_down(api, deadline)
        finally:
            await api.aclose()
        await self._terminate(workspace_id, deadline)

    async def _poll_guest_down(self, api: CloudHypervisorApi, deadline: float) -> None:
        while True:
            state = (await api.info()).get("state")
            if state in GUEST_DOWN_STATES:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise MicrovmTimeoutError(
                    f"guest did not power off within the shutdown "
                    f"deadline (state={state!r})"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _terminate(self, workspace_id: str, deadline: float) -> None:
        """SIGTERM the VMM daemon and wait for exit (guest already down)."""
        proc = self._procs.pop(workspace_id, None)
        if proc is None:
            pid = self._pid(workspace_id)
            if pid is not None and self._pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(proc.wait(), remaining)
        except TimeoutError:
            raise MicrovmTimeoutError(
                f"cloud-hypervisor for {workspace_id} did not exit after SIGTERM"
            ) from None

    async def _reap(self, workspace_id: str, proc) -> None:
        self._procs.pop(workspace_id, None)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

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


__all__ = ["API_ROOT", "LocalCloudHypervisor", "map_ch_state", "vm_config"]

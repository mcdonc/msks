"""Local backend: one cloud-hypervisor process per workspace VM (#1).

Per-workspace layout under ``<state_dir>/vms/<workspace_id>/``:

- ``api.sock``  — the CH REST socket this process serves
- ``ch.pid``    — the CH process id (restart-surviving kill path)
- ``ch.log``    — the VMM's own stderr
- ``serial.log``— the guest serial console (file-backed serial device)
- ``vsock.sock``— the vsock device's unix socket (the console proxy
                 dials it with the CONNECT handshake, #21)

Shutdown model, matching how the VMM really behaves: a bare
``cloud-hypervisor --api-socket`` is a daemon that keeps running after
the guest powers off (``vm.info`` returns to a non-running state), so
graceful shutdown is PUT /vm.power-button (the guest's handler runs
the clean poweroff) -> poll the guest down -> SIGTERM the VMM -> wait
for process exit, all under one deadline.
"""

import asyncio
import contextlib
import os
import shutil
import signal
from pathlib import Path

from .. import persist
from .chapi import API_ROOT, CloudHypervisorApi
from .driver import MicrovmDriver
from .errors import MicrovmError, MicrovmTimeoutError
from .spec import VmInfo, VmSpec, VmStatus

# Bound on the OK reply once the handshake bytes are sent.
VSOCK_REPLY_S = 5.0


class _VsockRetry(Exception):
    """A retryable console bring-up state, carrying its human cause."""


async def _vsock_handshake(socket_path: Path, port: int):
    """One connect+CONNECT attempt against the vsock unix socket.

    The socket carries a small handshake before raw bytes: the dialer
    sends ``CONNECT <port>\n``, cloud-hypervisor answers
    ``OK <local_port>\n`` once the guest accepts. Raises _VsockRetry
    for every state that a still-booting guest can present (missing
    socket, refused or silent handshake); returns the established
    stream otherwise.
    """
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except OSError as exc:
        # FileNotFoundError/ConnectionRefusedError while the guest
        # brings the device up, PermissionError on a hostile path,
        # and friends: all retryable-shaped, all carrying their errno.
        raise _VsockRetry(f"vsock socket unreachable: {exc}") from exc
    try:
        writer.write(f"CONNECT {port}\n".encode())
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), VSOCK_REPLY_S)
    except TimeoutError as exc:
        writer.close()
        raise _VsockRetry("handshake reply never arrived") from exc
    except OSError as exc:
        # A VMM dying mid-handshake resets the stream; that is the
        # same boot-window flakiness the retry exists to absorb.
        writer.close()
        raise _VsockRetry(f"handshake stream died: {exc}") from exc
    if not reply.startswith(b"OK"):
        writer.close()
        raise _VsockRetry(f"handshake refused: {reply.strip()!r}")
    return reader, writer


# The guest-side CID cloud-hypervisor reports for the vsock device.
# CIDs are per-VMM (each workspace has its own), so a constant is
# unambiguous.
VSOCK_CID = 3

CH_STATE_TO_STATUS = {
    "Created": VmStatus.STARTING,
    "Running": VmStatus.RUNNING,
    "Paused": VmStatus.PAUSED,
    "Shutdown": VmStatus.STOPPED,
}
GUEST_DOWN_STATES = ("Created", "Shutdown")
POLL_INTERVAL_S = 0.05
# How long to wait before pressing the ACPI button again: an early-boot
# press lands before the guest's logind listens and is silently dropped.
POWER_REPRESS_S = 5.0


def vm_config(
    spec: VmSpec, disks: list[dict], serial_log: Path, vsock_socket: Path | None = None
) -> dict:
    """The ``PUT /api/v1/vm.create`` body for one spec (v52 schema).

    Memory is bytes (``mem_mib`` is converted), the payload nests
    kernel/cmdline/initramfs, the serial file is a plain path string,
    and the disks arrive as their own entries (#14): the root overlay
    first, the home volume second — position makes the root device.

    ``vsock_socket`` adds the virtio-vsock device: cloud-hypervisor
    LISTENS on that unix path, and each host-side connection maps to
    one vsock connection into the guest after the ``CONNECT <port>``
    handshake (#21). The CID is per-VMM — every workspace runs its
    own cloud-hypervisor with its own socket, so a constant works.
    """
    payload: dict = {
        "kernel": str(spec.kernel),
        "cmdline": spec.cmdline,
    }
    if spec.initrd is not None:
        payload["initramfs"] = str(spec.initrd)
    vm: dict = {
        "cpus": {"boot_vcpus": spec.cpus, "max_vcpus": spec.cpus},
        "memory": {"size": spec.mem_mib * 1024 * 1024},
        "payload": payload,
        "disks": disks,
        "serial": {"mode": "File", "file": str(serial_log)},
        # No virtio-console device: the default leaves a second,
        # non-autologin getty (hvc0) writing into the VMM log.
        "console": {"mode": "Off"},
    }
    if vsock_socket is not None:
        vm["vsock"] = {"cid": VSOCK_CID, "socket": str(vsock_socket)}
    return vm


def disk_entries(state_dir: Path, workspace_id: str) -> list[dict]:
    """The VM's two persistent disks (#14): root overlay, home volume.

    Both are writable — the overlay absorbs root writes over the
    pristine base (copy-on-write protects it; ``readonly`` on the old
    single raw disk is obsolete) — and carry an explicit
    ``image_type``: v52's autodetection otherwise disables sector-0
    writes on untyped disks. The overlay additionally opts into
    ``backing_files``: v51 loads a qcow2 backing file only when the
    disk says so (landlock hardening, GHSA advisory follow-up).
    """
    return [
        {
            "path": str(persist.overlay_path(state_dir, workspace_id)),
            "readonly": False,
            "image_type": "Qcow2",
            "backing_files": True,
        },
        {
            "path": str(persist.home_volume_path(state_dir, workspace_id)),
            "readonly": False,
            "image_type": "Raw",
        },
    ]


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

    async def prepare(self, spec: VmSpec) -> None:
        """Create the workspace's overlay and home volume (#14).

        Strict on leftovers: artifacts present with no create in
        flight means a previous workspace of the same id (a failed
        create whose cleanup could not remove them). Launch heals
        crashed creations; create never reuses a predecessor's data —
        the operator clears the files and tries again.
        """
        vmm = self._settings().vmm
        self._dir(spec.workspace_id)
        for artifact in (
            persist.overlay_path(vmm.state_dir, spec.workspace_id),
            persist.home_volume_path(vmm.state_dir, spec.workspace_id),
        ):
            if artifact.exists():
                raise MicrovmError(
                    f"artifact for workspace {spec.workspace_id} already exists: "
                    f"{artifact}; remove it (or restore the workspace row) first"
                )
        await persist.ensure_artifacts(spec, vmm)

    async def launch(self, spec: VmSpec) -> None:
        vmm = self._settings().vmm
        vm_dir = self._dir(spec.workspace_id)
        self._ensure_launchable(spec.workspace_id, vm_dir)
        self._check_socket_path(vm_dir / "api.sock")
        vm_dir.mkdir(parents=True, exist_ok=True)
        # Boots heal their artifacts (#14): a workspace row whose
        # overlay or volume is missing (a crash mid-create, or a row
        # that predates #14) gets them back before the VM starts.
        await persist.ensure_artifacts(spec, vmm)
        socket_path = vm_dir / "api.sock"
        serial_log = vm_dir / "serial.log"
        proc = await self._spawn(vmm.cloud_hypervisor, socket_path, vm_dir / "ch.log")
        self._procs[spec.workspace_id] = proc
        (vm_dir / "ch.pid").write_text(str(proc.pid))
        try:
            await self._wait_ready(socket_path, proc, vmm.socket_wait_timeout_s)
            await self._configure_and_boot(
                spec,
                disk_entries(self._settings().vmm.state_dir, spec.workspace_id),
                socket_path,
                serial_log,
                vmm.request_timeout_s,
                vsock_socket=vm_dir / "vsock.sock",
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
        self, spec, disks, socket_path, serial_log, timeout_s, vsock_socket=None
    ) -> None:
        api = CloudHypervisorApi(socket_path, timeout_s)
        try:
            await api.create(vm_config(spec, disks, serial_log, vsock_socket))
            await api.boot()
        finally:
            await api.aclose()

    async def console(self, workspace_id: str):
        """(reader, writer): one interactive stream into the VM.

        A freshly booted workspace refuses the console twice over, in
        order: the unix socket appears only when the GUEST's driver
        activates the device (seconds after vm.boot reported success),
        and even then the first CONNECT can meet a guest kernel whose
        shell server has not called listen() yet — the kernel answers
        RST and cloud-hypervisor closes the unix stream. The whole
        connect+handshake is retried under one deadline; a dead VMM
        fails fast instead of waiting it out.
        """
        socket_path = self._dir(workspace_id) / "vsock.sock"
        settings = self._settings().vmm
        port = settings.vsock_shell_port
        deadline = asyncio.get_running_loop().time() + settings.vsock_wait_timeout_s
        while True:
            if not self._vmm_reachable(workspace_id):
                raise MicrovmError(
                    f"workspace {workspace_id} has no live VMM for a console"
                )
            try:
                return await _vsock_handshake(socket_path, port)
            except _VsockRetry as retry:
                if asyncio.get_running_loop().time() >= deadline:
                    raise MicrovmError(
                        f"console unavailable for {workspace_id} "
                        f"(is the VM running?): {retry}"
                    ) from retry
            await asyncio.sleep(POLL_INTERVAL_S)

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
        """Stop one VM gracefully; an already-stopped VM is success.

        The absent-VM contract every backend honors (see the k8s
        driver): stopping a workspace whose VMM died — or that was
        never started — must not wedge the caller. A stale api socket
        (the file outlives a dead VMM) reports ECONNREFUSED rather
        than ENOENT, so dead-behind-a-socket counts as stopped too.
        """
        vmm = self._settings().vmm
        socket_path = self._dir(workspace_id) / "api.sock"
        if not socket_path.exists() or not self._vmm_reachable(workspace_id):
            self._procs.pop(workspace_id, None)
            return
        timeout = timeout_s if timeout_s is not None else vmm.shutdown_timeout_s
        deadline = asyncio.get_running_loop().time() + timeout
        await self._graceful_guest_down(workspace_id, deadline)
        await self._terminate(workspace_id, deadline)

    async def _graceful_guest_down(self, workspace_id: str, deadline: float) -> None:
        """Request the ACPI poweroff and wait for the guest to land.

        The button is re-pressed every few seconds (_press_button_until_down):
        an event pressed during early boot lands before logind listens
        and is dropped, and one re-press closes that window without
        extending the deadline for a guest that truly refuses to go.
        """
        vmm = self._settings().vmm
        api = CloudHypervisorApi(
            self._dir(workspace_id) / "api.sock", vmm.request_timeout_s
        )
        try:
            await self._press_button_until_down(api, deadline)
        except MicrovmTimeoutError:
            # A live guest refusing to power off is a real result —
            # surface it, exactly as before.
            raise
        except MicrovmError:
            # The VMM died or hung between the liveness check and the
            # call (narrowed race, never zero) — including a press
            # racing the guest's own transition to down: either way
            # the right next step is SIGTERM, not a surfaced 500.
            pass
        finally:
            await api.aclose()

    async def _press_button_until_down(
        self, api: CloudHypervisorApi, deadline: float
    ) -> None:
        now = asyncio.get_running_loop().time
        next_press = 0.0
        while True:
            if now() >= next_press:
                await api.power_button()
                next_press = now() + POWER_REPRESS_S
            state = (await api.info()).get("state")
            if state in GUEST_DOWN_STATES:
                return
            if now() >= deadline:
                raise MicrovmTimeoutError(
                    f"guest did not power off within the shutdown "
                    f"deadline (state={state!r})"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    def _vmm_reachable(self, workspace_id: str) -> bool:
        """Whether a VMM process for this workspace looks alive.

        A pid-liveness heuristic, deliberately cheap: races it cannot
        close are handled by the MicrovmError fallthrough in
        ``shutdown`` escalating to SIGTERM.
        """
        pid = self._pid(workspace_id)
        proc = self._procs.get(workspace_id)
        if proc is not None:
            return proc.returncode is None
        return pid is not None and self._pid_alive(pid)

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
            await asyncio.wait_for(proc.wait(), max(remaining, POLL_INTERVAL_S))
        except TimeoutError:
            raise MicrovmTimeoutError(
                f"cloud-hypervisor for {workspace_id} did not exit after "
                f"SIGTERM (shutdown deadline exhausted)"
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
        if pid is None or not self._pid_alive(pid):
            # Already dead (or never started): killing an absent VM is
            # success — the absent-VM contract shutdown honors too.
            return
        os.kill(pid, signal.SIGKILL)

    async def reset(self, workspace_id: str) -> None:
        """Factory reset: drop the root overlay, keep the home volume.

        The overlay is the running VM's root device, so a live VMM
        must be stopped first — deleting the file under it would
        leave the guest writing into an unlinked inode.
        """
        self._dir(workspace_id)
        if self._vmm_reachable(workspace_id):
            raise MicrovmError(
                f"workspace {workspace_id} still runs; stop it before reset"
            )
        persist.remove_overlay(self._settings().vmm.state_dir, workspace_id)

    async def cleanup(self, workspace_id: str) -> None:
        proc = self._procs.pop(workspace_id, None)
        if proc is not None and proc.returncode is None:
            # A tracked-and-running VMM must not be orphaned by a
            # directory delete: kill and reap it, then remove the dir.
            proc.kill()
            await proc.wait()
        shutil.rmtree(self._dir(workspace_id), ignore_errors=True)
        # The home volume lives outside the vm dir so stop/start
        # cycles and resets cannot lose it; cleanup owns its removal.
        persist.remove_home_volume(self._settings().vmm.state_dir, workspace_id)


__all__ = [
    "API_ROOT",
    "LocalCloudHypervisor",
    "disk_entries",
    "map_ch_state",
    "vm_config",
]

"""The local cloud-hypervisor driver.

:class:`LocalCloudHypervisor` drives per-VM processes on this
host; the virtiofs daemon lifecycle lives in the mixin the
class composes (:class:`.fsproc.FsDaemonMixin`)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from pathlib import Path

from ...interceptor import ca
from ...persist import (
    ensure_artifacts,
    home_volume_path,
    overlay_path,
    remove_home_volume,
    remove_overlay,
    seed_bake_pending,
    seed_path,
)
from ...spec.egress import EgressPolicy
from ...spec.vm import VmInfo, VmSpec, VmStatus
from ..chapi import API_ROOT, CloudHypervisorApi
from ..driver import MicrovmDriver
from ..errors import MicrovmError, MicrovmTimeoutError
from .config import (
    GUEST_DOWN_STATES,
    POLL_INTERVAL_S,
    POWER_REPRESS_S,
    check_id,
    console_retry_error,
    disk_entries,
    map_ch_state,
    vm_config,
    vm_net,
)
from .console import _ConsoleRetry, console_attempt, socket_stale
from .fsproc import (
    FsDaemonMixin,
)
from .procs import cmdline_is_vmm, read_cmdline


class LocalCloudHypervisor(FsDaemonMixin, MicrovmDriver):
    """Drives per-VM cloud-hypervisor processes on this host."""

    def __init__(self, app) -> None:
        self.app = app
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # The virtiofs daemons (#620, part #622): one list per
        # workspace, the mounts its live VM attaches. Each daemon
        # runs in its own session, like the VMM — it serves as
        # long as the VM does, and a daemon crash leaves it
        # serving for the restarted daemon to find by pidfile
        # (the #56 pattern).
        self._fs_procs: dict[str, list[asyncio.subprocess.Process]] = {}
        # One lock per workspace (#70 review): a stop/kill racing a
        # start must not interleave — shutdown pops the VMM before it
        # detaches the net plumbing, and a launch squeezing into that
        # window would attach a tap the shutdown then deletes under
        # the booting VM.
        self._locks: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def _guard(self, workspace_id: str):
        """Serialize one workspace's lifecycle transitions."""
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            yield

    def _settings(self):
        return self.app.state.settings

    def _dir(self, workspace_id: str) -> Path:
        check_id(workspace_id)
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
            overlay_path(vmm.state_dir, spec.workspace_id),
            home_volume_path(vmm.state_dir, spec.workspace_id),
            seed_path(vmm.state_dir, spec.workspace_id),
        ):
            if artifact.exists():
                raise MicrovmError(
                    f"artifact for workspace {spec.workspace_id} "
                    f"already exists: {artifact}; "
                    "remove it (or restore the workspace row) first"
                )
        await ensure_artifacts(
            spec,
            vmm,
            self._settings().llm.port,
            await self.interceptor_ca_pem(),
        )

    async def interceptor_ca_pem(self) -> str:
        """The daemon's interceptor CA certificate (#424, #485): one
        CA for every workspace, minted on first need into the
        daemon state root.

        ``prepare`` lands here at create, so the seed disk carries
        the certificate and the guest trusts its interception path
        from first boot; ``_boot``'s artifact heal re-reads the
        same CA. The load rides the interceptor manager's
        single-flight ``authority()`` — the arm path and this seed
        path mint against the same pair of files, and two unguarded
        mints racing the absent pair would interleave two
        identities.
        """
        authority = await self.app.state.interceptor.authority()
        return ca.cert_pem(authority.cert).decode()

    async def launch(self, spec: VmSpec) -> None:
        async with self._guard(spec.workspace_id):
            await self._launch_locked(spec)

    async def _launch_locked(self, spec: VmSpec) -> None:
        vmm = self._settings().vmm
        vm_dir = self._dir(spec.workspace_id)
        await self._ensure_launchable(spec.workspace_id)
        self._check_socket_path(vm_dir / "api.sock")
        vm_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_sockets(vm_dir)
        # Egress (#52): the tap must exist before the VMM opens it,
        # so the attachment arms first — and unwinds on any failure
        # below, leaving no half-open plumbing behind.
        attachment = await self._net_attach(spec)
        try:
            await self._boot(spec, vmm, vm_dir, attachment)
        except BaseException:
            await self._net_detach(spec.workspace_id)
            await self._stop_fs_daemons(spec.workspace_id)
            raise

    def _sweep_stale_sockets(self, vm_dir: Path) -> None:
        """Remove residue a hard kill left behind (#151).

        A VMM killed without cleanup (host crash, a hard
        stop) leaves ``api.sock`` and ``console.sock`` in place; the
        next spawn then dies binding them -- the VMM at
        ``CreateApiServerSocket: AddrInUse`` before a byte of
        serial, vm.boot at ``Error binding to the host-side Unix
        socket`` -- and the operator sees a misleading
        unreachable-API/ENOENT 503. Only refused sockets are swept
        (socket_stale: a full backlog or a slow accept means LIVE
        and is never unlinked -- cutting off a serving VMM would be
        worse than the residue; the test fake pre-binds exactly such
        a socket and rides this same path).
        """
        for name in ("api.sock", "console.sock", "ch.pid"):
            self._sweep_one_residue(vm_dir, name)

    def _sweep_one_residue(self, vm_dir: Path, name: str) -> None:
        if name == "ch.pid":
            # Definitionally stale here: _ensure_launchable already
            # proved no live VMM owns this workspace, and a recycled
            # pid would otherwise block starts (or worse, aim kill
            # at an innocent process) after a host reboot.
            self._unlink_residue(vm_dir / name, "pidfile")
            return
        if socket_stale(vm_dir / name):
            self._unlink_residue(vm_dir / name, "socket")

    def _unlink_residue(self, path: Path, kind: str) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise MicrovmError(
                f"cannot remove stale {kind} {path} "
                f"({exc}); remove it by hand and retry"
            ) from exc

    async def _boot(self, spec: VmSpec, vmm, vm_dir: Path, attachment) -> None:
        """Spawn the VMM and boot the VM (artifacts healed first, #14).

        A healed *overlay* is a recorded event (#697): the fresh
        overlay boots a guest whose ssh host keys are regenerated
        (the image ships none — #110 — and the first boot writes
        them onto the overlay), so the row's disk epoch advances
        and the client rotates its host-key cache on the next
        identity fetch instead of refusing the new keys. The home
        volume carries no guest identity, so its heal bumps
        nothing. A heal under no row (the create flow prepares
        artifacts before the row lands; a fixture launch) records
        nothing — there is no cache to rotate.

        A baked or renewed seed is a recorded event too (#803):
        the new instance-id makes the guest re-provision, and the
        stock cloud-init ssh module regenerates its host keys
        (Debian, Fedora; NixOS omits the module). The row's seed
        epoch — a monotonic counter, one bump per bake — advances
        *before* the bake for the same reason the disk epoch
        precedes its heal: a daemon death between bump and bake
        over-counts harmlessly, while a bake the row never
        recorded is the silent refusal both counters exist to
        prevent. The counter (not the bake's content) is what the
        client rotates on, so a mount set leaving and returning
        — the same content hash twice — still lands a fresh
        cache. The same no-row carve-out applies, and a seed
        left alone records nothing."""
        overlay = overlay_path(vmm.state_dir, spec.workspace_id)
        healed_overlay = not overlay.is_file()
        if healed_overlay:
            # The bump precedes the heal (#697 review): a bump
            # failure aborts with no artifact touched, and a heal
            # that then rolls its install back leaves the bump
            # over-counted — harmless for a monotonic token —
            # instead of the reverse (a healed overlay the epoch
            # never recorded, the silent refusal this fix exists
            # for). No row (the fixture launch, a create in flight)
            # bumps nothing.
            await self.app.state.model.bump_disk_epoch(spec.workspace_id)
        # Boots heal their artifacts (#14): a workspace row whose
        # overlay or volume is missing (a crash mid-create, or a row
        # that predates #14) gets them back before the VM starts.
        # The seed's bump rides the same review lesson ahead of
        # the bake (#803).
        ca_pem = await self.interceptor_ca_pem()
        if seed_bake_pending(spec, vmm, self._settings().llm.port, ca_pem):
            await self.app.state.model.bump_seed_epoch(spec.workspace_id)
        await ensure_artifacts(
            spec,
            vmm,
            self._settings().llm.port,
            ca_pem,
        )
        # The share daemons spawn before the VMM (#620, part #622):
        # cloud-hypervisor opens each vhost-user socket at
        # vm.create, so the daemon behind it must already serve.
        fs_devices = await self._start_fs_daemons(spec, vm_dir)
        socket_path = vm_dir / "api.sock"
        proc = await self._spawn(
            vmm.cloud_hypervisor, socket_path, vm_dir / "ch.log"
        )
        self._procs[spec.workspace_id] = proc
        (vm_dir / "ch.pid").write_text(str(proc.pid))
        try:
            await self._wait_ready(
                socket_path,
                proc,
                vmm.socket_wait_timeout_s,
                vm_dir / "ch.log",
            )
            await self._configure_and_boot(
                spec,
                disk_entries(
                    self._settings().vmm.state_dir,
                    spec.workspace_id,
                ),
                socket_path,
                vmm.request_timeout_s,
                console_socket=vm_dir / "console.sock",
                net=vm_net(attachment),
                fs=fs_devices,
            )
        except BaseException:
            await self._reap(spec.workspace_id, proc)
            await self._stop_fs_daemons(spec.workspace_id)
            raise

    async def _net_attach(self, spec: VmSpec):
        """Arm the workspace's egress plumbing when it asked for it,
        under its consent policy (#69 — mode and specs from the
        spec, which the create path validated)."""
        policy = EgressPolicy(
            spec.workspace_id, spec.egress_mode, spec.egress_allowlist
        )
        return await self.app.state.net.attach(
            spec.workspace_id, want=spec.egress, policy=policy
        )

    async def _net_detach(self, workspace_id: str) -> None:
        """Tear the workspace's egress plumbing down (idempotent)."""
        await self.app.state.net.detach(workspace_id)

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

    async def _ensure_launchable(self, workspace_id: str) -> None:
        """Refuse a live VMM; reconcile the residue a dead one left.

        A tracked VMM answers by its exit status; one a restarted
        daemon never tracked answers by its pidfile (#56). A VMM
        that exits outside the stop flow (#825 — the guest's own
        poweroff, or a guest landing its shutdown just past a stop
        deadline) leaves its tracked entry, share daemons, and tap
        behind with no stop path to reap them: the launch clears
        the same residue the stop path's dead-VMM branch clears,
        so the next start is a start, not a 503.
        """
        if self._vmm_reachable(workspace_id):
            raise MicrovmError(
                f"VM {workspace_id} already exists; shutdown or cleanup first"
            )
        self._procs.pop(workspace_id, None)
        await self._stop_fs_daemons(workspace_id)
        await self._net_detach(workspace_id)

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
                # The VMM gets its own session (#141): the daemon's
                # death must not become the workspace's — a TERM sent
                # to msksd directly (or a crash, or a plain exit)
                # leaves the VMM running for the restarted daemon to
                # re-find (info() probes the socket; cleanup's
                # pidfile fallback is the #56 case), verified live.
                # A guardian's managed stop (devenv processes down /
                # restart) kills the whole process TREE and takes
                # workspaces with it — hard, without their
                # power-button shutdown — so stop workspaces first
                # when that matters; the session split at least
                # keeps every accidental and crash path safe.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise MicrovmError(
                f"cloud-hypervisor binary not found: {binary}"
            ) from exc
        finally:
            log_file.close()

    async def _configure_and_boot(
        self,
        spec,
        disks,
        socket_path,
        timeout_s,
        console_socket=None,
        net=None,
        fs=None,
    ) -> None:
        api = CloudHypervisorApi(socket_path, timeout_s)
        try:
            await api.create(vm_config(spec, disks, console_socket, net, fs))
            await api.boot()
        finally:
            await api.aclose()

    async def console(self, workspace_id: str):
        """(reader, writer): one interactive stream into the VM.

        The unix socket appears only when the VMM activates the
        console device — seconds after vm.boot reported success —
        so the connect is retried under one deadline; a dead VMM
        fails fast instead of waiting it out. The stream is raw
        bytes: the guest side is an autologin root getty on the
        arch's console device (#833; #481), and whatever it
        prints is the session's first output.
        """
        socket_path = self._dir(workspace_id) / "console.sock"
        settings = self._settings().vmm
        deadline = (
            asyncio.get_running_loop().time() + settings.console_wait_timeout_s
        )
        while True:
            if not self._vmm_reachable(workspace_id):
                raise MicrovmError(
                    f"workspace {workspace_id} has no live VMM for a console"
                )
            try:
                return await console_attempt(socket_path)
            except _ConsoleRetry as retry:
                if asyncio.get_running_loop().time() >= deadline:
                    raise console_retry_error(workspace_id, retry) from retry
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _wait_ready(
        self, socket_path: Path, proc, timeout_s: float, log_path: Path
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not socket_path.exists():
            if proc.returncode is not None:
                raise MicrovmError(
                    f"cloud-hypervisor exited with {proc.returncode} "
                    f"before serving "
                    f"{socket_path}{self._log_tail(log_path)}"
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise MicrovmTimeoutError(
                    f"cloud-hypervisor API socket never appeared: "
                    f"{socket_path}{self._log_tail(log_path)}"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    @staticmethod
    def _log_tail(log_path: Path, limit: int = 400) -> str:
        """The VMM's own last words for an operator error (#151).

        A VMM that dies at spawn has usually said why (a socket bind
        refusal, a bad flag); naming it in the 503 beats pointing at
        a log the operator must go dig up -- the ENOENT variant of
        #151 hid a CreateApiServerSocket AddrInUse behind
        "unreachable API".
        """
        try:
            text = log_path.read_text(errors="replace").strip()
        except OSError:
            return " (log unreadable)"
        if not text:
            return " (log empty)"
        return f"; last log line: {text[-limit:].splitlines()[-1]}"

    async def info(self, workspace_id: str) -> VmInfo:
        vm_dir = self._dir(workspace_id)
        if not vm_dir.is_dir():
            return VmInfo(workspace_id, VmStatus.ABSENT)
        socket_path = vm_dir / "api.sock"
        pid = self._pid(workspace_id)
        if not socket_path.exists():
            return VmInfo(workspace_id, VmStatus.STOPPED, pid)
        api = CloudHypervisorApi(
            socket_path, self._settings().vmm.request_timeout_s
        )
        try:
            document = await api.info()
        except MicrovmError:
            status = (
                VmStatus.UNKNOWN
                if self._pid_alive(pid, workspace_id)
                else VmStatus.STOPPED
            )
            return VmInfo(workspace_id, status, pid)
        finally:
            await api.aclose()
        return VmInfo(workspace_id, map_ch_state(document.get("state")), pid)

    def _pid(self, workspace_id: str) -> int | None:
        pid_file = self._dir(workspace_id) / "ch.pid"
        with contextlib.suppress(OSError, ValueError):
            return int(pid_file.read_text().strip())
        return None

    def _pid_alive(self, pid: int | None, workspace_id: str) -> bool:
        """Whether the pid is a live VMM of OUR binary (#151).

        Bare pid liveness is not identity: after a host reboot pids
        restart low, and a recycled pid used to refuse every start
        ("VM already exists") -- or aim SIGKILL at an innocent
        process. The identity is per-workspace: the command line
        must carry the configured binary and this workspace's
        --api-socket path.
        """
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return self._pid_alive_denied(pid, workspace_id)
        return self._pid_is_vmm(pid, workspace_id)

    def _pid_alive_denied(self, pid: int, workspace_id: str) -> bool:
        """kill(0) said EPERM: alive but not ours to signal.

        procfs is world-readable without hidepid: a
        owned-by-another-uid VMM of ours is named, anything foreign
        reads as dead (mixed-uid hosts recycle pids too).
        """
        if not self._pid_is_vmm(pid, workspace_id):
            return False
        raise MicrovmError(
            f"workspace {workspace_id}: VMM pid {pid} is "
            f"owned by another user and cannot be stopped"
        )

    def _pid_is_vmm(self, pid: int, workspace_id: str) -> bool:
        binary = self._settings().vmm.cloud_hypervisor
        sock = self._dir(workspace_id) / "api.sock"
        cmdline = read_cmdline(pid)
        if cmdline is None:  # pragma: no cover — a kill(0)-vs-read
            # race or a hidepid mount; liveness callers reach here
            # only with an alive pid, and both suite hosts read
            # those (procfs on Linux, ps on the Mac dev host).
            return False
        return cmdline_is_vmm(cmdline, binary, sock)

    async def shutdown(
        self, workspace_id: str, timeout_s: float | None = None
    ) -> None:
        """Stop one VM gracefully; an already-stopped VM is success.

        The absent-VM contract: stopping a workspace whose VMM died
        — or that was never started — must not wedge the caller. A
        stale api socket (the file outlives a dead VMM) reports
        ECONNREFUSED rather than ENOENT, so dead-behind-a-socket
        counts as stopped too.
        """
        async with self._guard(workspace_id):
            await self._shutdown_locked(workspace_id, timeout_s)

    async def _shutdown_locked(
        self, workspace_id: str, timeout_s: float | None
    ) -> None:
        vmm = self._settings().vmm
        socket_path = self._dir(workspace_id) / "api.sock"
        if not socket_path.exists() or not self._vmm_reachable(workspace_id):
            self._procs.pop(workspace_id, None)
            await self._stop_fs_daemons(workspace_id)
            await self._net_detach(workspace_id)
            return
        timeout = (
            timeout_s if timeout_s is not None else vmm.shutdown_timeout_s
        )
        deadline = asyncio.get_running_loop().time() + timeout
        await self._graceful_guest_down(workspace_id, deadline)
        await self._terminate(workspace_id, deadline)
        await self._stop_fs_daemons(workspace_id)
        await self._net_detach(workspace_id)

    async def _graceful_guest_down(
        self, workspace_id: str, deadline: float
    ) -> None:
        """Press the guest's power button and wait for it to land.

        The button is the ACPI event on x86_64 and the device-tree
        gpio key on arm64. It is re-pressed every few seconds
        (_press_button_until_down): an event pressed during early boot
        lands before logind listens and is dropped, and one re-press
        closes that window without extending the deadline for a guest
        that truly refuses to go. The re-press safety net is x86_64's
        alone while cloud-hypervisor's arm64 emulation stays one-way
        (#826): its PL061 pulse drives the gpio line 0->1 and never
        back, so the first press consumes the only edge the guest can
        ever see and every later press delivers nothing (the VMM logs
        "Failed to trigger GPIO input interrupt"). An arm64 stop that
        loses its first press is carried by the deadline and the
        fallback kill behind it.
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
        return self._pid_alive(pid, workspace_id)

    async def _terminate(self, workspace_id: str, deadline: float) -> None:
        """SIGTERM the VMM daemon and wait for exit (guest already down)."""
        proc = self._procs.pop(workspace_id, None)
        if proc is None:
            pid = self._pid(workspace_id)
            if pid is not None and self._pid_alive(pid, workspace_id):
                os.kill(pid, signal.SIGTERM)
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(
                proc.wait(), max(remaining, POLL_INTERVAL_S)
            )
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
        async with self._guard(workspace_id):
            await self._kill_locked(workspace_id)

    async def _kill_locked(self, workspace_id: str) -> None:
        proc = self._procs.pop(workspace_id, None)
        if proc is not None:
            proc.kill()
            await proc.wait()
            await self._stop_fs_daemons(workspace_id)
            await self._net_detach(workspace_id)
            return
        pid = self._pid(workspace_id)
        if pid is None or not self._pid_alive(pid, workspace_id):
            # Already dead (or never started): killing an absent VM is
            # success — the absent-VM contract shutdown honors too.
            await self._stop_fs_daemons(workspace_id)
            await self._net_detach(workspace_id)
            return
        os.kill(pid, signal.SIGKILL)
        await self._stop_fs_daemons(workspace_id)
        await self._net_detach(workspace_id)

    async def reset(self, workspace_id: str) -> None:
        """Factory reset: drop the root overlay, keep the home volume.

        The overlay is the running VM's root device, so a live VMM
        must be stopped first — deleting the file under it would
        leave the guest writing into an unlinked inode.
        """
        async with self._guard(workspace_id):
            await self._reset_locked(workspace_id)

    async def _reset_locked(self, workspace_id: str) -> None:
        self._dir(workspace_id)
        if self._vmm_reachable(workspace_id):
            raise MicrovmError(
                f"workspace {workspace_id} still runs; stop it before reset"
            )
        await self._net_detach(workspace_id)
        await self._stop_fs_daemons(workspace_id)
        # The dropped overlay takes the guest's ssh host keys with
        # it (#110): the next boot heals a fresh overlay and the
        # guest regenerates them, so the epoch advances the moment
        # an overlay actually drops — a reset with none to drop (a
        # never-started pre-#14 row, a second reset) bumps nothing,
        # keeping the epoch an honest count of recreations.
        if remove_overlay(self._settings().vmm.state_dir, workspace_id):
            await self.app.state.model.bump_disk_epoch(workspace_id)

    async def cleanup(self, workspace_id: str) -> None:
        # Deleting a workspace stops its VMM first: the unlocked kill
        # takes the tracked process (kill+wait) and falls back to the
        # pidfile for a VMM a restarted daemon no longer tracks (#56).
        async with self._guard(workspace_id):
            await self._kill_locked(workspace_id)
            shutil.rmtree(self._dir(workspace_id), ignore_errors=True)
        # The home volume lives outside the vm dir so stop/start
        # cycles and resets cannot lose it; cleanup owns its removal.
        remove_home_volume(self._settings().vmm.state_dir, workspace_id)


__all__ = [
    "API_ROOT",
    "LocalCloudHypervisor",
    "disk_entries",
    "map_ch_state",
    "vm_config",
    "vm_net",
]

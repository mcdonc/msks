"""The virtiofs daemon process helpers: pidfile and
socket facts, the sweep, and the stop grace."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from ...hostshares import check_host_share
from ...spec.mounts import MODE_RW, mount_tag
from ...spec.vm import VmSpec
from ..errors import MicrovmError
from ..fsdaemons import (
    fs_daemon_args,
    fs_device_entries,
    fs_pidfile_path,
    fs_socket_path,
    guest_user_ids,
    sandbox_mode,
    virtiofsd_floor_message,
    virtiofsd_meets_floor,
    virtiofsd_missing_message,
    virtiofsd_version,
)
from .config import POLL_INTERVAL_S
from .console import socket_stale
from .procs import kill_proc, kill_procs, pid_dead_by, read_cmdline

#: The fs daemons' one grace, three roles: how long a terminated
#: daemon gets to exit before SIGKILL, the budget a freshly
#: spawned daemon gets to serve its socket, and the post-KILL
#: death-poll window.
FS_TERM_GRACE_S = 5.0

#: The version probe's ceiling (#655): the doctor's own probe
#: answers inside 10s, and a mount start must not hang on an
#: MSKSD_VIRTIOFSD wrapper that wedges — the probe dies with the
#: error naming it instead.
VERSION_PROBE_TIMEOUT_S = 10.0


def host_mount_facts(mount, mounts) -> tuple[str, bool]:
    """One mount's spawn facts (#672): the shared directory
    through the #667 gates re-read at spawn (the flip that lands
    between the route's derivation and this spawn refuses here,
    naming its gate), and writability from the mount's mode
    alone — there is no operator share to narrow it (#685
    removed the named form)."""
    try:
        resolved = check_host_share(
            mounts.host_mode, mounts.host_roots, mount.host
        )
    except ValueError as exc:
        raise MicrovmError(str(exc)) from None
    return resolved, mount.mode != MODE_RW


async def sweep_fs_pidfile(vm_dir: Path, pidfile: Path) -> None:
    """One untracked daemon's pidfile: a pid nothing wrote is
    swept alone; a pid whose command line names this workspace's
    socket is TERM'd, waited, and KILL'd past the grace — a
    recycled pid never qualifies (#56's guard, the fs shape), and
    a TERM-ignoring daemon cannot wedge the workspace behind its
    own live socket."""
    pid = read_fs_pid(pidfile)
    if pid is not None and fs_pid_serves(
        pid, fs_socket_path(vm_dir, pidfile.stem[3:])
    ):
        if not await stop_fs_pid(pid):
            # A daemon that outlives even SIGKILL (D-state): keep
            # the pidfile so a later stop can find it — the sweep
            # never strands the only record of a live daemon.
            return
    pidfile.unlink(missing_ok=True)


async def stop_fs_pid(pid: int) -> bool:
    """TERM, a short death-wait, then KILL; True when the pid
    died. An untracked daemon has no process handle to wait on,
    so the death check polls the pid."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    if await pid_dead_by(pid, FS_TERM_GRACE_S):
        return True
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    return await pid_dead_by(pid, FS_TERM_GRACE_S)


def read_fs_pid(pidfile: Path) -> int | None:
    """The pid a pidfile names, or None when it holds nothing a
    daemon wrote (a torn write, a stray file — swept, never
    trusted enough to kill)."""
    try:
        return int(pidfile.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def fs_pid_serves(pid: int, socket_path: Path) -> bool:
    """Whether the pid's command line names this workspace's
    virtiofs socket — the guard that keeps a recycled pid alive
    (#56's rule, the fs shape): only a process that says it serves
    the socket dies by its pidfile."""
    cmdline = read_cmdline(pid)
    if cmdline is None:
        return False
    return os.fsencode(str(socket_path)) in cmdline.split(b"\0")


class FsDaemonMixin:
    """The virtiofs daemon lifecycle: spawn per share, stop per
    workspace, sweep the pidfiles. Composed into
    LocalCloudHypervisor; every method speaks the driver's own
    attributes (``_fs_procs``, ``_dir``)."""

    async def _start_fs_daemons(
        self, spec: VmSpec, vm_dir: Path
    ) -> list[dict]:
        """Spawn one virtiofsd per attached share (#620, part #622).

        The start path derived the attachable subset (#621) — the
        spec carries exactly the mounts that clear the config,
        mode, and exclusivity gates. The share facts re-read here:
        writability from the current config, the path resolved
        now against the load-time pin (the route's derivation
        caught the slow drift; this guard catches the flip that
        lands between derivation and spawn). A daemon that fails
        to spawn or serve unwinds the ones before it — no
        orphaned sockets, no half-mounted row, no untracked
        leak.
        """
        if not spec.mounts:
            return []
        settings = self._settings()
        binary = settings.vmm.virtiofsd
        await self._require_virtiofsd_floor(binary)
        uid, gid = os.getuid(), os.getgid()
        guest_uid, guest_gid = guest_user_ids(spec.login_user)
        sandbox = sandbox_mode(uid)
        procs: list[asyncio.subprocess.Process] = []
        sockets: dict[str, Path] = {}
        try:
            for mount in spec.mounts:
                tag = mount_tag(mount)
                proc = await self._spawn_one_fs(
                    binary,
                    mount,
                    tag,
                    vm_dir,
                    uid,
                    gid,
                    guest_uid,
                    guest_gid,
                    sandbox,
                )
                procs.append(proc)
                fs_pidfile_path(vm_dir, tag).write_text(str(proc.pid))
                sockets[tag] = fs_socket_path(vm_dir, tag)
        except BaseException:
            await kill_procs(procs)
            raise
        self._fs_procs[spec.workspace_id] = procs
        return fs_device_entries(sockets)

    async def _require_virtiofsd_floor(self, binary: str) -> None:
        """The version floor at mount start (#655): a below-floor
        virtiofsd rejects the spawn's flag set at argument
        parsing, so the start would unwind from "exited at start"
        with a log tail the operator must decode. The probe names
        the floor and the override instead, before any socket or
        pidfile lands — one short-lived exec per mount-carrying
        start."""
        version = await self._virtiofsd_version(binary)
        if not virtiofsd_meets_floor(version):
            raise MicrovmError(virtiofsd_floor_message(version))

    async def _virtiofsd_version(self, binary: str) -> tuple[int, ...] | None:
        """The resolved daemon's reported version (#655): exec
        ``<binary> --version`` and parse it, bounded by the
        doctor probe's own ceiling — an MSKSD_VIRTIOFSD wrapper
        that hangs must not hang a workspace start with it. A
        binary the daemon cannot exec refuses with the shared
        named error (the probe runs before the spawn seam's own
        copy); a nonzero exit refuses naming the exit and the
        stderr tail — a broken new build is not the floor's
        "older than" story."""
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise MicrovmError(virtiofsd_missing_message(binary)) from None
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), VERSION_PROBE_TIMEOUT_S
            )
        except TimeoutError:
            await kill_proc(proc)
            raise MicrovmError(
                f"virtiofsd {binary} --version did not answer within "
                f"{VERSION_PROBE_TIMEOUT_S:.0f}s (check the "
                "MSKSD_VIRTIOFSD wrapper; msksd doctor names the row)"
            ) from None
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip()[:200]
            raise MicrovmError(
                f"virtiofsd {binary} --version exited "
                f"{proc.returncode}{f': {tail}' if tail else ''}"
            )
        return virtiofsd_version(out.decode(errors="replace"))

    async def _spawn_one_fs(
        self,
        binary: str,
        mount,
        tag: str,
        vm_dir: Path,
        uid: int,
        gid: int,
        guest_uid: int,
        guest_gid: int,
        sandbox: str,
    ):
        """One mount's daemon: the shared directory through the
        #667 gates re-read at spawn (the flip that lands between
        the route's derivation and this spawn) — the socket swept
        of residue, and the spawn carrying the pinned flag set,
        held until it serves its socket, then returned for the
        registry."""
        resolved, readonly = host_mount_facts(mount, self._settings().mounts)
        socket = self._fs_socket_for(vm_dir, tag)
        log_path = vm_dir / f"fs-{tag}.log"
        proc = await self._spawn_fs(
            binary,
            fs_daemon_args(
                resolved,
                tag,
                socket,
                uid=uid,
                gid=gid,
                guest_uid=guest_uid,
                guest_gid=guest_gid,
                readonly=readonly,
                sandbox=sandbox,
            ),
            log_path,
        )
        # The daemon must serve before the VMM opens the socket:
        # a daemon that died at exec (a bad flag set, a missing
        # library) surfaces here as a named error pointing at its
        # log, not downstream as an opaque CH connect failure —
        # and a refused daemon dies here too, never left serving
        # (or half-serving) untracked behind a start that failed.
        try:
            await self._wait_fs_socket(socket, proc, tag, log_path)
        except BaseException:
            await kill_proc(proc)
            raise
        return proc

    async def _wait_fs_socket(
        self, socket: Path, proc, share: str, log_path: Path
    ) -> None:
        """Wait for the daemon's socket to appear, bounded: a
        dead proc (or a silent one past the grace) refuses naming
        the log tail."""
        deadline = asyncio.get_running_loop().time() + FS_TERM_GRACE_S
        while not socket.exists():
            if proc.returncode is not None:
                raise MicrovmError(
                    f"virtiofsd for share {share!r} exited at start"
                    f"{self._log_tail(log_path)}"
                )
            if asyncio.get_running_loop().time() > deadline:
                raise MicrovmError(
                    f"virtiofsd for share {share!r} never served "
                    f"{socket}{self._log_tail(log_path)}"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    def _fs_socket_for(self, vm_dir: Path, share: str) -> Path:
        """The share's socket, swept of residue — a socket still
        serving (a daemon from a prior life the stale sweep cannot
        prove dead) refuses the spawn by name, never a silent
        second daemon behind one tag."""
        socket = fs_socket_path(vm_dir, share)
        if socket.exists() and not socket_stale(socket):
            raise MicrovmError(
                f"a live virtiofs daemon already serves {share!r} "
                "for this workspace; stop the workspace before "
                "starting it again"
            )
        self._unlink_residue(socket, "socket")
        return socket

    async def _spawn_fs(self, binary: str, args: list[str], log_path: Path):
        """One virtiofsd under the VMM's session rules: the daemon's
        death must not kill a serving share (a restarted daemon
        finds it by pidfile, the #56 pattern), and a workspace's
        teardown takes it with the VMM."""
        log_file = open(log_path, "wb")
        try:
            return await asyncio.create_subprocess_exec(
                binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise MicrovmError(virtiofsd_missing_message(binary)) from exc
        finally:
            log_file.close()

    async def _stop_fs_daemons(self, workspace_id: str) -> None:
        """Tear the workspace's share daemons down (idempotent).

        Tracked daemons terminate and wait; a restarted daemon's
        untracked ones die by pidfile — the pid's command line
        must name this workspace's socket, so a recycled pid is
        never killed (#56's guard, the fs shape)."""
        procs = self._fs_procs.pop(workspace_id, None)
        if procs is None:
            await self._kill_untracked_fs_daemons(workspace_id)
            return
        for proc in procs:
            self._term_fs_proc(proc)
        await self._wait_fs_procs(procs)
        await self._unlink_fs_pidfiles(self._dir(workspace_id))

    def _term_fs_proc(self, proc) -> None:
        """One daemon's TERM: a daemon that already died on its
        own answers ProcessLookupError, suppressed — the stop
        completes either way."""
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def _wait_fs_procs(self, procs: list) -> None:
        """Wait out the terminated daemons, killing any that hang
        past the grace."""
        for proc in procs:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), FS_TERM_GRACE_S)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def _kill_untracked_fs_daemons(self, workspace_id: str) -> None:
        """The restarted-daemon path: pidfiles under the vm dir name
        daemons this process never spawned."""
        vm_dir = self._dir(workspace_id)
        for pidfile in sorted(vm_dir.glob("fs-*.pid")):
            await sweep_fs_pidfile(vm_dir, pidfile)

    async def _unlink_fs_pidfiles(self, vm_dir: Path) -> None:
        """Drop the tracked daemons' pidfiles beside their sockets."""
        for pidfile in sorted(vm_dir.glob("fs-*.pid")):
            pidfile.unlink(missing_ok=True)

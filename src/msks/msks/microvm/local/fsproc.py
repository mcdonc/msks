"""The virtiofs daemon process helpers: pidfile and
socket facts, the sweep, and the stop grace."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket as socket_module
from pathlib import Path

from ...hostshares import check_host_share
from ...spec.mounts import MODE_RW, mount_tag
from ...spec.owners import dead_owner_cause
from ...spec.vm import VmSpec
from ..errors import MicrovmError
from ..fsdaemons import (
    fs_daemon_args,
    fs_device_entries,
    fs_pidfile_path,
    fs_socket_path,
    guest_user_ids,
    helper_lane,
    helper_target,
    resolve_virtiofsd_as,
    sandbox_mode,
    virtiofsd_as_exec_message,
    virtiofsd_as_missing_message,
    virtiofsd_as_target_message,
    virtiofsd_floor_message,
    virtiofsd_meets_floor,
    virtiofsd_missing_message,
    virtiofsd_target_mismatch_message,
    virtiofsd_version,
)
from .config import POLL_INTERVAL_S
from .console import socket_stale
from .procs import (
    kill_proc,
    kill_procs,
    pid_dead_by,
    proc_start_time,
    read_cmdline,
)

#: The fs daemons' TERM grace, two roles: how long a terminated
#: daemon gets to exit before SIGKILL, and the post-KILL
#: death-poll window. (A freshly spawned daemon's serve budget
#: is FD_SERVE_GRACE_S below.)
FS_TERM_GRACE_S = 5.0

#: The serve grace a freshly exec'd virtiofsd gets (#1088): under
#: the --fd contract the socket file exists from the daemon's own
#: bind, so the file's appearance no longer signals readiness —
#: a daemon that dies at exec (a bad flag set, a missing binary,
#: a helper refusal) surfaces as a death inside this window,
#: named with its log tail. A daemon alive past it is serving;
#: the bound-before-accept approximation is the one the
#: --socket-path shape always carried (bind preceded accept
#: there too). The budget covers a loaded host's fork+exec of the
#: helper and the virtiofsd before its main() runs — fast
#: failures (a refusal, an argument-parse death) land inside the
#: first 100ms — without taxing every mount-carrying start with
#: the TERM grace's full span.
FD_SERVE_GRACE_S = 2.0

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


def bind_fs_socket(path: Path) -> int:
    """Bind and listen the share's vhost socket (#1088): the
    daemon owns the socket, so a virtiofsd running as the
    workspace's owner never needs write access to the daemon's
    tree — the child accepts the listening fd it was handed and
    creates no path. The returned descriptor is the child's by
    number (``pass_fds`` holds it across the shortcut lane's
    exec; the helper's own contract holds it across the helper
    lane's) and the daemon's own until the spawn closes it."""
    sock = socket_module.socket(
        socket_module.AF_UNIX,
        socket_module.SOCK_STREAM,
    )
    # Close-on-exec without the Linux-only SOCK_CLOEXEC flag
    # (#1105): a darwin CPython's socket module carries no such
    # attribute and the macOS verify suite died on it, while
    # set_inheritable(False) sets the same fcntl close-on-exec
    # bit on every platform. The spawn's pass_fds clears the
    # bit for the child that receives the descriptor.
    sock.set_inheritable(False)
    sock.bind(str(path))
    sock.listen(8)
    return sock.detach()


def write_fs_pidfile(pidfile: Path, proc) -> None:
    """The pid and its kernel start time (#1088): the identity
    pair that proves a pidfile's pid is the very process an
    earlier daemon spawned — a recycled pid never matches both.
    Under the --fd contract the cmdline names no socket path, so
    the start time is the guard; a start time that cannot be
    read falls back to the bare pid and the legacy cmdline
    guard beside it."""
    start = proc_start_time(proc.pid)
    text = str(proc.pid) if start is None else f"{proc.pid} {start}"
    pidfile.write_text(text, encoding="utf-8")


async def sweep_fs_pidfile(vm_dir: Path, pidfile: Path) -> None:
    """One untracked daemon's pidfile: a pid nothing writes is
    swept alone; a pid the identity pair names is TERM'd, waited,
    and KILL'd past the grace — a recycled pid never qualifies
    (#56's guard, the fs shape), and a TERM-ignoring daemon
    cannot wedge the workspace behind its own live socket. A
    swept daemon's socket file goes with it (under --fd the
    child never knew the path); an unidentified pid's socket
    stays for the spawn's own live check to judge — a live
    daemon behind it refuses the next start by name, never a
    silent second bind behind one tag."""
    pid, start = read_fs_identity(pidfile)
    socket = fs_socket_path(vm_dir, pidfile.stem[3:])
    if pid is not None and fs_pid_serves(pid, start, socket):
        if not await stop_fs_pid(pid, socket):
            # A daemon that outlives even the nudge (D-state):
            # keep the pidfile so a later stop can find it — the
            # sweep never strands the only record of a live
            # daemon.
            return
        socket.unlink(missing_ok=True)
    pidfile.unlink(missing_ok=True)


def fs_exit_nudge(socket_path: Path) -> None:
    """The stop lever on a child the daemon cannot signal (#1088):
    a helper lane's virtiofsd runs as the workspace's owner, and
    an unprivileged daemon's TERM and KILL both answer EPERM. But
    virtiofsd exits when its vhost client's connection closes —
    the same exit the VMM's own death fires — so the daemon
    connects to the share's socket and hangs up at once: the
    child accepts the connection, sees the disconnect, and
    exits. Nothing serving the path makes the connect refuse —
    the nudge is a no-op then, and a client still connected (a
    live VMM) holds the accept, leaving the nudge unread."""
    try:
        probe = socket_module.socket(
            socket_module.AF_UNIX, socket_module.SOCK_STREAM
        )
        try:
            # The stale probe's own bound (console.py's rule): a
            # full backlog means live, and a wedged pre-accept
            # daemon must not hold the stop path open on it.
            probe.settimeout(0.5)
            probe.connect(str(socket_path))
        finally:
            probe.close()
    except OSError:
        pass


async def stop_fs_pid(pid: int, socket_path: Path) -> bool:
    """TERM, a short death-wait, then KILL, then the socket's
    disconnect exit; True when the pid died. An untracked daemon
    has no process handle to wait on, so the death check polls
    the pid. The signals and the nudge all land best-effort
    (#1088: a helper-lane child runs as another uid — its
    daemon's signal answers EPERM, suppressed — and the nudge is
    the lever that reaches it)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    # The nudge lands beside the TERM, not only after the KILL:
    # a helper-lane child's signals both answer EPERM, and the
    # disconnect exit is the one lever that reaches it — firing
    # it up front spares the sweep a grace of polling a pid no
    # signal was ever going to move. Idempotent either way.
    fs_exit_nudge(socket_path)
    if await pid_dead_by(pid, FS_TERM_GRACE_S):
        return True
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)
    fs_exit_nudge(socket_path)
    return await pid_dead_by(pid, FS_TERM_GRACE_S)


def read_fs_identity(pidfile: Path) -> tuple[int | None, int | None]:
    """The (pid, start time) a pidfile names, (None, None) when
    it holds nothing a daemon wrote (a torn write, a stray file
    — swept, never trusted enough to kill). A pre-#1088
    pidfile's bare pid answers (pid, None) and takes the legacy
    cmdline guard."""
    try:
        parts = pidfile.read_text(encoding="utf-8").split()
    except OSError:
        return None, None
    return fs_identity_words(parts)


def fs_identity_words(
    parts: list[str],
) -> tuple[int | None, int | None]:
    """The pidfile's whitespace-split words as the identity
    pair: a decimal pid alone is the legacy arm, a second
    decimal word is the start time, and anything else names
    nothing (both None) — never trusted enough to kill."""
    try:
        pid = int(parts[0])
        start = int(parts[1]) if len(parts) > 1 else None
    except IndexError, ValueError:
        return None, None
    return pid, start


def fs_pid_serves(pid: int, start: int | None, socket_path: Path) -> bool:
    """Whether the pid is the daemon this workspace spawned
    (#56's rule, the fs shape): with the pidfile's start time
    (#1088), a pid whose kernel start time matches is the very
    process an earlier daemon exec'd, whatever uid it runs as
    now — the identity a recycled pid can never reproduce. A
    bare pid (a pre-#1088 pidfile) keeps the legacy guard: the
    command line names this workspace's socket, the arg the
    --socket-path shape always carried."""
    if start is not None:
        return proc_start_time(pid) == start
    cmdline = read_cmdline(pid)
    if cmdline is None:
        return False
    return os.fsencode(str(socket_path)) in cmdline.split(b"\0")


class FsDaemonMixin:
    """The virtiofs daemon lifecycle: spawn per share, stop per
    workspace, sweep the pidfiles. Composed into
    LocalCloudHypervisor; every method speaks the driver's own
    attributes (``_fs_procs``, ``_dir``)."""

    #: The serve grace a fresh spawn waits out (FD_SERVE_GRACE_S
    #: above); an attribute so the hermetic suite runs the wait
    #: at milliseconds, not the shipping budget.
    serve_grace_s: float = FD_SERVE_GRACE_S

    async def _start_fs_daemons(
        self, spec: VmSpec, vm_dir: Path
    ) -> list[dict]:
        """Spawn one virtiofsd per attached share (#620, part #622;
        #1088).

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

        #1088's exec lanes: the workspace's owner, resolved
        through the setuid helper, when it differs from the
        daemon's own uid; directly, exactly as before, when it
        does not. The helper execs a virtiofsd path compiled into
        itself, so a coherent install puts the same binary there
        the floor probe checked; an operator's MSKSD_VIRTIOFSD
        override reaches only the shortcut lane.
        """
        if not spec.mounts:
            return []
        settings = self._settings()
        binary = settings.vmm.virtiofsd
        await self._require_virtiofsd_floor(binary)
        daemon_uid = os.getuid()
        as_uid = helper_lane(spec.owner_uid, daemon_uid)
        helper = self._resolve_helper(settings, as_uid)
        guest_uid, guest_gid = guest_user_ids(spec.login_user)
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
                    helper,
                    as_uid,
                    daemon_uid,
                    guest_uid,
                    guest_gid,
                )
                procs.append(proc)
                write_fs_pidfile(fs_pidfile_path(vm_dir, tag), proc)
                sockets[tag] = fs_socket_path(vm_dir, tag)
        except BaseException:
            await self._unwind_fs_daemons(procs, sockets, vm_dir)
            raise
        self._fs_procs[spec.workspace_id] = procs
        return fs_device_entries(sockets)

    async def _unwind_fs_daemons(
        self, procs: list, sockets: dict[str, Path], vm_dir: Path
    ) -> None:
        """A failed start's cleanup (#1088): the nudge first — a
        launch that fails before the VMM ever connects leaves a
        helper-lane daemon in accept() with no client to
        disconnect it, and the daemon's own connect-and-hang-up
        fires that exit — then the signal sweep (a child the
        daemon cannot signal answers EPERM, suppressed; the
        pdeathsig the helper armed is the last resort) and the
        residue cleanup."""
        for tag in sockets:
            fs_exit_nudge(fs_socket_path(vm_dir, tag))
        await kill_procs(procs)
        for tag in sockets:
            fs_socket_path(vm_dir, tag).unlink(missing_ok=True)

    def _resolve_helper(self, settings, as_uid: int | None) -> str | None:
        """The helper lane's binary, resolved once per start
        (#1088): None — the shortcut lane, which needs no helper
        — when the owner is the daemon's own uid; a resolved
        helper path otherwise, refusing by name when the owner
        is dead, no helper is installed, or the helper will exec
        a virtiofsd other than the one the daemon resolved — the
        compile-time target is the privilege boundary, and this
        probe is what keeps the floor probe and the exec
        coherent when an operator's override would drift them
        apart. The dead-owner refusal here is the race arm: the
        derivation gate (#1088's mount_state) already skipped a
        dead owner's mounts, and this catches the deletion that
        lands between that gate and the spawn."""
        if as_uid is None:
            return None
        dead = dead_owner_cause(as_uid)
        if dead is not None:
            raise MicrovmError(f"cannot exec a share for a dead owner: {dead}")
        helper = resolve_virtiofsd_as(settings.vmm.virtiofsd_as)
        if helper is None:
            raise MicrovmError(
                virtiofsd_as_missing_message(settings.vmm.virtiofsd_as)
            )
        self._require_helper_coherence(settings, helper)
        return helper

    def _require_helper_coherence(self, settings, helper: str) -> None:
        """The compile-time target probe (#1088): the helper will
        exec a virtiofsd path compiled into itself, so a start
        whose daemon-resolved binary differs refuses by name —
        the floor probe and the exec stay coherent when an
        operator's override would drift them apart."""
        target = helper_target(helper)
        if target is None:
            raise MicrovmError(virtiofsd_as_target_message(helper))
        resolved = os.path.realpath(settings.vmm.virtiofsd)
        if os.path.realpath(target) != resolved:
            raise MicrovmError(
                virtiofsd_target_mismatch_message(target, resolved)
            )

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
        helper: str | None,
        as_uid: int | None,
        daemon_uid: int,
        guest_uid: int,
        guest_gid: int,
    ):
        """One mount's daemon (#1088): the shared directory
        through the #667 gates re-read at spawn (the flip that
        lands between the route's derivation and this spawn) —
        the socket swept of residue, bound and listened by the
        daemon itself, the spawn carrying the pinned flag set
        with the listening fd, held until it serves, then
        returned for the registry."""
        resolved, readonly = host_mount_facts(mount, self._settings().mounts)
        socket = self._fs_socket_for(vm_dir, tag)
        log_path = vm_dir / f"fs-{tag}.log"
        fd = bind_fs_socket(socket)
        try:
            proc = await self._exec_fs_daemon(
                binary,
                helper,
                as_uid,
                resolved,
                tag,
                fd,
                daemon_uid,
                guest_uid,
                guest_gid,
                readonly,
                log_path,
            )
        except BaseException:
            os.close(fd)
            socket.unlink(missing_ok=True)
            raise
        # The child holds its own copy of the listening fd; the
        # daemon's copy goes here, so a long-lived daemon leaks
        # one descriptor per live share, not per start.
        os.close(fd)
        # The daemon must serve before the VMM opens the socket:
        # a daemon that died at exec (a bad flag set, a missing
        # binary, a helper refusal) surfaces here as a named
        # error pointing at its log, not downstream as an opaque
        # CH connect failure — and a refused daemon dies here
        # too, never left serving (or half-serving) untracked
        # behind a start that failed.
        try:
            await self._wait_fs_serving(proc, tag, log_path)
        except BaseException:
            await kill_proc(proc)
            socket.unlink(missing_ok=True)
            raise
        return proc

    async def _exec_fs_daemon(
        self,
        binary: str,
        helper: str | None,
        as_uid: int | None,
        resolved: str,
        tag: str,
        fd: int,
        daemon_uid: int,
        guest_uid: int,
        guest_gid: int,
        readonly: bool,
        log_path: Path,
    ):
        """One mount's exec (#1088): the argv in the lane the
        owner picks — the resolved helper with the owner uid and
        the fd when the owner differs from the daemon's own uid,
        the virtiofsd binary directly (the daemon's own uid and
        gid exactly as before) when it does not."""
        uid = as_uid if as_uid is not None else daemon_uid
        # The helper's contract pins the gid to the uid (its
        # setresgid takes the uid's value); the shortcut lane
        # keeps the daemon's own pair, byte for byte today's.
        gid = as_uid if as_uid is not None else os.getgid()
        # The lane picks the sandbox (#1088): the helper has
        # dropped every capability and set no-new-privileges by
        # the exec, so its child can never take a namespace —
        # even an owner uid 0 — while the shortcut lane keeps
        # the privilege rule (root's namespace, none otherwise).
        sandbox = "none" if helper is not None else sandbox_mode(uid)
        args = fs_daemon_args(
            resolved,
            tag,
            fd,
            uid=uid,
            gid=gid,
            guest_uid=guest_uid,
            guest_gid=guest_gid,
            readonly=readonly,
            sandbox=sandbox,
        )
        if helper is None:
            return await self._spawn_fs(
                binary,
                args,
                log_path,
                fd,
                virtiofsd_missing_message(binary),
            )
        argv = [str(as_uid), "--fd", str(fd), "--", *args]
        return await self._spawn_fs(
            helper,
            argv,
            log_path,
            fd,
            virtiofsd_as_exec_message(helper),
        )

    async def _wait_fs_serving(self, proc, share: str, log_path: Path) -> None:
        """Hold the serve grace open for one freshly exec'd
        daemon (#1088): a death inside the window refuses naming
        the log tail; survival past it is serving. The
        socket-file appearance this replaces was the same
        approximation — bind always preceded accept."""
        deadline = asyncio.get_running_loop().time() + self.serve_grace_s
        while asyncio.get_running_loop().time() < deadline:
            if proc.returncode is not None:
                raise MicrovmError(
                    f"virtiofsd for share {share!r} exited at start"
                    f"{self._log_tail(log_path)}"
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

    async def _spawn_fs(
        self,
        binary: str,
        args: list[str],
        log_path: Path,
        fd: int,
        missing: str,
    ):
        """One virtiofsd under the VMM's session rules: the two
        lanes answer a daemon's death differently. The shortcut
        lane's child outlives the daemon (a restarted daemon
        finds it by pidfile, the #56 pattern); the helper lane's
        child dies with it, through the parent-death signal the
        helper arms. A workspace's teardown takes either with
        the VMM. The listening fd crosses the exec (pass_fds
        holds its number in the child; the helper lane's binary
        clears the close-on-exec bit itself on the way in).
        *missing* is the named refusal an exec the OS refuses
        maps to — the lane's own binary, not a generic one.

        This spawn stays on the event loop thread (#1176). The
        helper lane's parent-death signal is delivered when the
        parent *thread* exits, not the process: asyncio forks
        here on the loop thread, which lives as long as the
        daemon, so the signal means the daemon's death. A spawn
        moved to a worker thread (``to_thread``, an executor)
        would arm the share's death on that worker's exit
        instead. The helper resets the signal dispositions and
        the mask before its own exec, so the daemon's signal
        state (an ignored disposition, a mask some library
        blocked) never reaches the share."""
        log_file = open(log_path, "wb")
        try:
            return await asyncio.create_subprocess_exec(
                binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(fd,),
            )
        except OSError as exc:
            raise MicrovmError(missing) from exc
        finally:
            log_file.close()

    async def _stop_fs_daemons(self, workspace_id: str) -> None:
        """Tear the workspace's share daemons down (idempotent).

        Tracked daemons terminate and wait; a restarted daemon's
        untracked ones die by pidfile — the identity pair names
        the very process this daemon spawned, so a recycled pid
        is never killed (#56's guard, the fs shape). A helper
        lane's child runs as the workspace's owner (#1088): the
        signals answer EPERM and the socket's disconnect exit is
        the lever that reaches it — the stop nudges the sockets
        beside the TERM."""
        procs = self._fs_procs.pop(workspace_id, None)
        if procs is None:
            await self._kill_untracked_fs_daemons(workspace_id)
            return
        vm_dir = self._dir(workspace_id)
        for proc in procs:
            self._term_fs_proc(proc)
        for socket in sorted(vm_dir.glob("fs-*.sock")):
            fs_exit_nudge(socket)
        await self._wait_fs_procs(procs)
        await self._unlink_fs_residue(vm_dir)

    def _term_fs_proc(self, proc) -> None:
        """One daemon's TERM: a daemon that already died on its
        own answers ProcessLookupError, and a helper lane's
        owner-uid child answers PermissionError (#1088) — both
        suppressed, the nudge and the client's own disconnect are
        the exits that reach it — so the stop completes either
        way."""
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.terminate()

    async def _wait_fs_procs(self, procs: list) -> None:
        """Wait out the terminated daemons, killing any that hang
        past the grace; a kill the daemon cannot land (EPERM,
        #1088's helper lane) is suppressed — the wait ends at
        the child's self-exit, and the residue the stop unlinks
        after is what a later sweep judges."""
        for proc in procs:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), FS_TERM_GRACE_S)
                except TimeoutError:
                    with contextlib.suppress(
                        ProcessLookupError, PermissionError
                    ):
                        proc.kill()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), FS_TERM_GRACE_S)

    async def _kill_untracked_fs_daemons(self, workspace_id: str) -> None:
        """The restarted-daemon path: pidfiles under the vm dir name
        daemons this process never spawned."""
        vm_dir = self._dir(workspace_id)
        for pidfile in sorted(vm_dir.glob("fs-*.pid")):
            await sweep_fs_pidfile(vm_dir, pidfile)

    async def _unlink_fs_residue(self, vm_dir: Path) -> None:
        """Drop the tracked daemons' pidfiles and sockets beside
        them (#1088: the child never knew the socket's path, so
        the daemon that bound it unlinks it)."""
        for residue in sorted(vm_dir.glob("fs-*.pid")) + sorted(
            vm_dir.glob("fs-*.sock")
        ):
            residue.unlink(missing_ok=True)

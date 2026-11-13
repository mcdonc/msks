"""Host process facts: cmdline reads, VMM identity,
and the kill/wait tails a shutdown needs."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path

from .config import POLL_INTERVAL_S


def has_socket_arg(fields: list[bytes], sock: Path) -> bool:
    want_sock = os.fsencode(str(sock))
    return any(
        a == b"--api-socket" and b == want_sock
        for a, b in zip(fields, fields[1:])
    )


def read_cmdline(pid: int) -> bytes | None:
    """The pid's argv as /proc cmdline bytes — NUL-separated fields.

    Linux reads ``/proc/<pid>/cmdline`` directly (#151's identity
    source). A darwin dev host has no procfs, and without it the
    identity checks fell into their not-ours branch: kills never
    fired, and every suite run on a Mac left spawned VMM stubs
    alive while tests waited on their exit forever (#690). ps(1)
    carries the argv there — space-joined, re-joined with NULs —
    so the exact-field match keeps its shape with one
    approximation: a field containing a space splits, and a
    spaced path reads as foreign (a daemon of ours leaks — the
    #690 wedge again) while a foreign argument whose pieces
    happen to spell a workspace's socket path can read as ours
    (a recycled pid the sweep would signal). The deployment
    hosts are Linux and keep the /proc path byte-for-byte; the
    approximation is the darwin dev host's alone.
    """
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        pass
    if sys.platform == "darwin":
        # The darwin dev host's arm (#690): CI and the deployment
        # hosts run Linux and take the /proc path above. The
        # platform-steering test below runs this arm on every
        # host — ps(1) answers argv on Linux too — so it carries
        # no pragma.
        try:
            out = subprocess.run(
                ["ps", "-ww", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                check=True,
            ).stdout.strip()
        except OSError, subprocess.CalledProcessError:
            return None
        return b"\0".join(out.split())
    # A non-darwin host without a readable procfs entry: the
    # fs-daemon dead-pid tests reach this on the Linux CI host.
    return None


def cmdline_is_vmm(cmdline: bytes, binary: str, sock: Path) -> bool:
    """Whether a /proc cmdline is OUR VMM for the workspace (#151).

    Exact-field equality, not substring: `grep cloud-hypervisor` as
    a recycled pidfile pid must not read as our VMM. A bare argv
    field match is allowed because a shebang script's argv[0] is
    the interpreter (the test stub rides as argv[1]) while a real
    ELF VMM carries the binary as argv[0]; the per-workspace
    --api-socket requirement is what seals lookalikes either way.
    """
    fields = [f for f in cmdline.split(b"\0") if f]
    want_binary = os.fsencode(binary)
    return any(f == want_binary for f in fields) and has_socket_arg(
        fields, sock
    )


async def kill_procs(procs: list) -> None:
    """Kill and reap every process in the sweep — the
    spawn-failure unwind's shape."""
    for proc in procs:
        await kill_proc(proc)


async def kill_proc(proc) -> None:
    """One process's hard stop: kill (an already-exited one raises
    ProcessLookupError, suppressed) and reap."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def pid_dead_by(pid: int, timeout_s: float) -> bool:
    """Poll one pid until it dies or the deadline passes; True
    when it died."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if pid_gone(pid):
            return True
        await asyncio.sleep(POLL_INTERVAL_S)
    return pid_gone(pid)


def pid_gone(pid: int) -> bool:
    """Whether a pid no longer answers kill(0) — dead or never
    was."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False

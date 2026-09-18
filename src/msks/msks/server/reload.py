"""Development reload: watch the package tree, restart on change (#144).

The appliance dev-tree flow runs the daemon from a live-shared source
tree (the host repo over virtiofs). Watching that tree with inotify
does not work — FUSE delivers no host-side change events — so this
polls: every :data:`POLL_SECONDS` the tree's ``(mtime_ns, size)``
fingerprint is re-walked and compared, and a difference restarts the
process via ``os.execv`` (a fresh interpreter is the point: the
changed code must be imported anew). The guest mounts the dev-tree
share with server-side caching disabled, so each walk stats the real
host files.
"""

import os
import sys
import threading
import time

# The poll cadence: fast enough that an edit is picked up between
# keystrokes' consequences, slow enough that a walk of the package
# tree costs nothing measurable.
POLL_SECONDS = 0.5


def fingerprint(path: str) -> tuple[int, int] | None:
    """One file's change signature, or None if it vanished mid-walk.

    An editor's atomic rename can delete a listed file between
    listdir and stat; it appears (or its replacement does) in the
    next snapshot.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def file_paths(root: str):
    """Every file path under *root*, recursively."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def snapshot(roots: list[str]) -> dict[str, tuple[int, int]]:
    """The tree's change fingerprint: path -> (mtime_ns, size) of every .py."""
    snap: dict[str, tuple[int, int]] = {}
    for root in roots:
        for path in file_paths(root):
            if not path.endswith(".py"):
                continue
            fp = fingerprint(path)
            if fp is not None:
                snap[path] = fp
    return snap


def changed(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> bool:
    """Whether two snapshots differ — any path, mtime, or size."""
    return before != after


def package_roots() -> list[str]:
    """The watched tree: the msks package this module was imported from."""
    return [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]


def exec_restart(
    argv: list[str],
) -> None:  # pragma: no cover — exec never returns
    """Replace this process with a fresh interpreter over *argv*."""
    os.execv(sys.executable, argv)


def restart_argv() -> list[str]:
    """The execv argument vector for re-running the current command line."""
    return [sys.executable, "-m", "msks.server.main", *sys.argv[1:]]


def watch_loop(
    roots: list[str],
    *,
    sleep=time.sleep,
    restart=exec_restart,
) -> None:
    """Poll *roots* forever; on change, *restart* the process.

    Known limits, deliberate for a development tool: an edit landing
    between interpreter start and the first snapshot is absorbed
    into the baseline (the next edit restarts), and there is no
    debounce — a multi-file ``git checkout`` can restart on a
    half-updated tree (the unit's crash-restart self-heals a
    mid-write file). mtime+size fingerprints miss an mtime- and
    size-preserving rewrite, which editors do not do.

    *sleep* and *restart* are injection seams for the tests; in
    production the loop only ever leaves through :func:`exec_restart`
    (which never returns) or process teardown — a daemon thread that
    dies with the interpreter either way.
    """
    before = snapshot(roots)
    while True:
        sleep(POLL_SECONDS)
        now = snapshot(roots)
        if changed(before, now):
            print("msksd: source tree changed — restarting", file=sys.stderr)
            try:
                restart(restart_argv())
            except OSError as exc:
                # A failed exec (the interpreter path vanished, say)
                # must not kill the watcher: the current process keeps
                # serving, and the next change retries.
                print(
                    f"msksd: restart failed ({exc}); watching continues",
                    file=sys.stderr,
                )
            before = now


def arm_reload_watcher() -> None:
    """Start the reload poller as a daemon thread (development only)."""
    thread = threading.Thread(
        target=watch_loop,
        args=(package_roots(),),
        name="msksd-reload",
        daemon=True,
    )
    thread.start()

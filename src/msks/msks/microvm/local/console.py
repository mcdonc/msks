"""The console stream: stale-socket probes and the
half-close teardown both console consumers share."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import socket
import sys
from pathlib import Path


def socket_stale(path: Path) -> bool:
    """Whether a unix socket path exists with nothing listening (#151).

    Only the two refusal errors count as stale: a full accept
    backlog answers connect with EAGAIN/BlockingIOError and a
    slow-to-accept listener with a timeout -- both mean LIVE, and
    unlinking them would cut a serving VMM off at the name. The
    refused/no-such-file pair alone is dead residue: the file a
    hard-killed VMM left, or a non-socket path squatting on the name.
    """
    if not os.path.lexists(path):
        # lexists, not exists: a dangling symlink at the name is
        # residue too -- it refuses the bind as surely as a file.
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except ConnectionRefusedError, FileNotFoundError:
        return True
    except OSError as exc:
        # EAGAIN and timeouts mean live; the classification of
        # the remaining errnos (one darwin-only residue shape)
        # lives beside its taxonomy.
        return probe_refusal_is_stale(exc)
    finally:
        probe.close()
    return False


def probe_refusal_is_stale(exc: OSError) -> bool:
    """Whether a connect() failure beyond the refusal pair still
    names dead residue — the one case that differs by host
    (#690's taxonomy): darwin's connect() against a non-socket
    path answers ENOTSOCK where Linux answers ECONNREFUSED (a
    refusal, matched above), so a plain file squatting on the
    socket name is swept on both. Every other OSError — EAGAIN
    from a full backlog, a timeout against a slow acceptor —
    means live.
    """
    if sys.platform == "darwin" and exc.errno == errno.ENOTSOCK:
        # darwin's connect() produces ENOTSOCK against a
        # non-socket path; Linux's answers ECONNREFUSED (the
        # refusal pair above). The steering test below reaches
        # this arm on every host, so it carries no pragma.
        return True
    # The EAGAIN/timeout leg: pinned on the Linux CI host by the
    # saturation test (darwin's backlog refusal is ECONNREFUSED,
    # so the same leg cannot run there).
    return False


class _ConsoleRetry(Exception):
    """A retryable console bring-up state, carrying its human cause."""


async def console_attempt(socket_path: Path):
    """One connect against the console device's unix socket.

    cloud-hypervisor listens on the path; the guest's autologin
    getty on the arch's console device, ttyS0 on x86_64 and
    ttyAMA0 on aarch64 (#833), speaks through it (#481). Raises
    _ConsoleRetry
    for every state a still-booting guest can present (missing
    socket, refused connect, hostile path).
    """
    try:
        return await asyncio.open_unix_connection(str(socket_path))
    except OSError as exc:
        # FileNotFoundError/ConnectionRefusedError while the VMM
        # brings the device up, PermissionError on a hostile path,
        # and friends: all retryable-shaped, all carrying their errno.
        raise _ConsoleRetry(f"console socket unreachable: {exc}") from exc


def half_close_raw_socket(writer) -> None:
    """SHUT_WR on the stream's real socket, whatever event loop
    runs it.

    Stock asyncio's ``get_extra_info("socket")`` returns a real
    socket; uvloop's PseudoSocket answers ``shutdown`` with
    TypeError but still carries the fd — a socket object wrapped
    around that fd does the half-close. Fakes that model no
    socket at all are skipped.
    """
    get = getattr(writer, "get_extra_info", None)
    if get is None:
        return
    raw = get("socket")
    if raw is None:
        return
    try:
        raw.shutdown(socket.SHUT_WR)
        return
    except TypeError:
        pass
    wrapped = socket.socket(fileno=raw.fileno())
    try:
        wrapped.shutdown(socket.SHUT_WR)
    finally:
        wrapped.detach()


async def close_console_stream(reader, writer) -> None:
    """Tear down a console socket session so the VMM survives it.

    A plain close() on an AF_UNIX socket that still has unread
    bytes queued to this side aborts the connection: the VMM's
    serial-manager read() gets ECONNRESET, and v52's epoll loop
    returns the error out of the thread closure — whose wrapper
    only reports panics — so the thread dies silently, the console
    listener stops serving forever, and the guest's serial output
    path can wedge its tty transmitter (upstream #8998, fixed on
    cloud-hypervisor main by #8687, unreleased at v52). Half-closing
    first (SHUT_WR) hands the VMM a clean EOF — the path its
    graceful-disconnect branch takes — and draining afterwards
    empties this side's receive queue so the final close carries
    no unread data.
    """
    half_close_raw_socket(writer)
    # Read the tail in flight (prompt redraws, escape sequences)
    # until the stream reaches EOF; each slice is capped so a
    # chatty guest cannot hold the detach hostage. RuntimeError is
    # the detach racing the bridge's own unwind: an endpoint whose
    # task was cancelled runs this teardown while the pump's reader
    # coroutine is still mid-cancellation and holds the reader's
    # waiter — the half-close above already landed (the part the
    # VMM needs), so the tail read skips and the close proceeds.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while loop.time() < deadline:
        try:
            if not await asyncio.wait_for(reader.read(4096), 0.25):
                break
        except TimeoutError, OSError:
            break
        except RuntimeError:
            break
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

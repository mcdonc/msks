"""Opt-in smoke tests against real infrastructure.

- Local: boots a real cloud-hypervisor VM when MSKSD_TEST_VMLINUX and
  MSKSD_TEST_ROOTFS point at guest artifacts (and /dev/kvm is
  accessible); skipped otherwise. The devenv task `msks:build-guest`
  sets all of these from `.guest/` automatically (see conftest.py and
  msks.guestassets); the stock nixpkgs kernel also needs the initrd
  (MSKSD_TEST_INITRD) and the cmdline the manifest carries
  (MSKSD_TEST_CMDLINE) to reach userspace.
- k8s: boots the runner image's guest in a pod when MSKSD_TEST_KUBECONFIG
  points at a cluster kubeconfig (k3s in dev); skipped otherwise. The
  runner image owns the guest artifacts, so the pod needs a KVM-capable
  node and the imported image, nothing from this host.

These never count toward the coverage gate (the package is fully
covered by the faked-transport unit suites).

This module is the suite's shared harness: the env-tunable constants,
the skip markers, and the helpers every area module uses. The tests
live in one module per area beside this one — test_boot_lifecycle,
test_user_data, test_k8s, test_appliance, test_console, test_egress,
test_dev_workspace, test_ssh_forward, test_identity, and
test_l3_recursion — and import what they need from here. The harness
lives in the package `__init__` itself rather than a harness.py the
package would re-export because test_smoke_harness.py pins
run_in_console's retry logic by monkeypatching attributes on
``test_smoke``: the functions must read CONSOLE_TIMEOUT_S and
CONSOLE_ATTEMPTS from this module's namespace for those pins to bite.
"""

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient

VMLINUX = os.environ.get("MSKSD_TEST_VMLINUX")
INITRD = os.environ.get("MSKSD_TEST_INITRD")
ROOTFS = os.environ.get("MSKSD_TEST_ROOTFS")
CMDLINE = os.environ.get("MSKSD_TEST_CMDLINE")
KUBECONFIG = os.environ.get("MSKSD_TEST_KUBECONFIG")
# The package __init__ sits one level below the old flat module.
REPO_ROOT = Path(__file__).resolve().parents[4]
client = AsyncClient(verify=False, timeout=10.0)

needs_local = pytest.mark.skipif(
    not VMLINUX or not ROOTFS or not os.access("/dev/kvm", os.W_OK),
    reason="set MSKSD_TEST_VMLINUX/MSKSD_TEST_ROOTFS with /dev/kvm access",
)
needs_k8s = pytest.mark.skipif(not KUBECONFIG, reason="set MSKSD_TEST_KUBECONFIG")

#: The serial autologin's root-shell prompt: the last line the
#: Debian boot produces (#30) and the "guest is usable" marker —
#: the logind that answers host-side shutdowns is up by then too.
#: The prompt, not the getty's login banner above it (#75): the
#: banner only says the getty started, while the prompt proves a
#: whole shell started, ran its rc files, and answered — the
#: strongest guest-side signal the console probes can build on
#: (the vsock console's own shell can stall behind an echo-alive
#: pty on a slow nested-KVM boot, long after its service started).
GUEST_UP_MARKER = "root@msks-guest:~#"

#: Per-phase timeouts, env-tunable for slow hosts (#64): a runner's
#: nested-KVM guest runs the same boot several times slower than a
#: dev host's KVM guest, and CI sets all three explicitly. Defaults
#: keep the dev-host behavior unchanged.
GUEST_UP_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_GUEST_UP_TIMEOUT_S", "60"))
CONSOLE_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_CONSOLE_TIMEOUT_S", "30"))
SHUTDOWN_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_SHUTDOWN_TIMEOUT_S", "60"))

#: Fresh console sessions per command (#75): the vsock console can
#: accept a connection and echo — the pty's line discipline answers
#: while the shell behind it never reaches its first prompt on a
#: slow nested-KVM boot. One wedged session must not fail the test;
#: each retry opens a fresh shell on an already-further-along boot.
CONSOLE_ATTEMPTS = int(os.environ.get("MSKSD_TEST_CONSOLE_ATTEMPTS", "3"))


def serial_tail(serial_log: Path, limit: int = 2000) -> str:
    """The end of the guest's serial log, for failure messages."""
    if not serial_log.exists():
        return "(no serial log)"
    return serial_log.read_text(encoding="utf-8", errors="replace")[-limit:]


def collect_failure_evidence(state_dir: Path, wid: str, serial_log: Path) -> None:
    """On a smoke failure, print and keep the guest's own story.

    The console service's state is on the serial log (systemd names
    failed/restarting units there); a raw CONNECT probe tells whether
    the guest's vsock listener is answering at all; and the vm dir is
    copied out before the finally-clause cleanup deletes it, for the
    CI artifact upload (``/tmp/msks-smoke-failed/``).
    """
    print(
        f"smoke failure evidence — {wid} serial tail:\n{serial_tail(serial_log, 4000)}",
        flush=True,
    )
    vm_dir = state_dir / "vms" / wid
    vsock = vm_dir / "vsock.sock"
    if vsock.exists():
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(vsock))
            sock.sendall(b"CONNECT 1023\n")
            reply = sock.recv(100)
            sock.settimeout(15)
            sock.sendall(b"\n")
            try:
                data = sock.recv(4096)
            except TimeoutError:
                data = b"<no bytes within 15s>"
            print(
                f"smoke failure evidence — raw vsock probe: "
                f"handshake={reply!r} after-newline={data!r}",
                flush=True,
            )
        except OSError as exc:
            print(f"smoke failure evidence — raw vsock probe: {exc}", flush=True)
        finally:
            with contextlib.suppress(OSError):
                sock.close()
    keep = Path("/tmp/msks-smoke-failed") / wid
    keep.mkdir(parents=True, exist_ok=True)
    # The root-run smokes write these as root; the CI artifact upload
    # runs as the unprivileged runner user and needs read access.
    with contextlib.suppress(OSError):
        keep.chmod(0o755)
    # The driver's own filenames (local.py: ch.log, ch.pid): the
    # vsock socket is a socket, never a file, so it stays out — the
    # copy list once carried names nothing writes, and the VMM's own
    # log (the one line that names device and config errors) never
    # reached the CI artifact.
    for name in ("serial.log", "ch.log", "ch.pid"):
        source = vm_dir / name
        if source.is_file():
            with contextlib.suppress(OSError):
                shutil.copy2(source, keep / name)
                (keep / name).chmod(0o644)
    # The ssh and git-out smokes' scratch logs (forward clients,
    # the scratch sshd): the ssh-path evidence the artifact upload
    # exists for. The git-out workdir is plain "gitout" (no -work
    # suffix), so both shapes are globbed.
    for pattern in ("*-work/*.log", "gitout/*.log"):
        for source in state_dir.glob(pattern):
            with contextlib.suppress(OSError):
                shutil.copy2(source, keep / source.name)
                (keep / source.name).chmod(0o644)


async def await_guest_up(serial_log: Path, timeout_s: float | None = None) -> None:
    """Block until the guest announces itself on the serial console."""
    timeout_s = timeout_s if timeout_s is not None else GUEST_UP_TIMEOUT_S
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if GUEST_UP_MARKER in serial_tail(serial_log):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"guest serial never showed {GUEST_UP_MARKER!r} within {timeout_s}s; "
        f"serial log tail:\n{serial_tail(serial_log)}"
    )


async def read_until(reader, needle: bytes, timeout_s: float | None = None) -> bytes:
    """Read the stream until it carries ``needle``; return the bytes.

    The vsock console is an echoing pty: the sent command comes
    back too, so ``needle`` must be guest-computed output — never a
    substring of the sent bytes, which the pty echoes verbatim
    (see run_in_console).
    """
    timeout_s = timeout_s if timeout_s is not None else CONSOLE_TIMEOUT_S
    data = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s

    def stalled() -> AssertionError:
        return AssertionError(
            f"never saw {needle!r} within {timeout_s}s; got: {data[-400:]!r}"
        )

    while needle not in data:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise stalled()
        try:
            chunk = await asyncio.wait_for(reader.read(4096), remaining)
        except TimeoutError:
            # A stall, not a bare TimeoutError: name what was awaited,
            # for how long, and what arrived (#75) — this message is
            # the evidence the retry loop prints and CI reads.
            raise stalled() from None
        if not chunk:
            raise AssertionError(
                f"stream closed before {needle!r}; got: {data[-400:]!r}"
            )
        data += chunk
    return data


#: The root shell's PS1 tail (``root@msks-guest:/# ``): the guest's
#: bash, with #62's real TERM, runs readline — and readline discards
#: typeahead that arrived before it started. A client that writes
#: the instant the vsock connects loses its first line to that
#: flush; an interactive user never notices (the prompt is on screen
#: before fingers move). The tests wait for the prompt first.
PROMPT_NEEDLE = b"root@msks-guest:/# "

#: The vsock console's first prompt (#63): the prelude helper execs a
#: login shell with cwd=$HOME, so a fresh session's prompt reads ~,
#: not / — and bash's interactive rc files may emit terminal control
#: sequences around it, which read_until's contains-scan tolerates.
CONSOLE_PROMPT_NEEDLE = b"root@msks-guest:~# "

#: The same prompt for the image's workspace user (#63): a login
#: shell as uid 1000 whose HOME is /home/msks.
USER_CONSOLE_PROMPT_NEEDLE = b"msks@msks-guest:~$ "


async def answer_console_auth(reader, writer, workspace_id, app, signer=None) -> None:
    """Answer a #123 console challenge on a raw vsock stream.

    A seeded guest challenges before any prompt; a guest without the
    trust store speaks the shell's own first bytes — the bracketed-
    paste escape and a prompt that carries no newline, so a line
    read would stall forever. The detection is byte-wise against the
    challenge prefix; bytes that are not the challenge go back for
    the marker wait. The key record comes straight from the daemon's
    model — the harness runs in-process with it.
    """
    from msks.client import consoleauth  # allow-deferred-import

    prefix = b"AUTH CHALLENGE "
    seen = b""
    while True:
        chunk = await asyncio.wait_for(reader.read(4096), 30)
        seen += chunk
        if not chunk:
            # EOF: nothing to answer, and nothing more will come.
            if seen:
                reader.feed_data(seen)
            return
        if seen.startswith(prefix):
            if b"\n" in seen:
                break
            continue  # the challenge line is still arriving
        if prefix.startswith(seen):
            continue  # a prefix-sized first chunk: not decidable yet
        # The shell's own bytes: put everything back and let the
        # prompt wait read them.
        if seen:
            reader.feed_data(seen)
        return
    line, _, rest = seen.partition(b"\n")
    if rest and rest != b"":
        reader.feed_data(rest)
    nonce = bytes.fromhex(line[len(prefix) :].strip().decode())
    if signer is None:
        key = await app.state.model.get_ssh_key(workspace_id)
        assert key is not None and key["public_key"] is not None, (
            f"guest challenged but {workspace_id} has no identity key"
        )
        signer, _public = consoleauth.signer_for_key(key, workspace_id)
    writer.write(b"AUTH SIG " + signer(nonce).encode() + b"\n")
    await writer.drain()
    reply = await asyncio.wait_for(reader.readline(), 30)
    assert reply.startswith(b"AUTH OK"), f"console auth refused: {reply!r}"


async def run_in_console(
    microvm,
    workspace_id: str,
    command: str,
    marker: str,
    user: str = "root",
    app=None,
    signer=None,
) -> None:
    """Run one shell command over the vsock console and wait for its
    marker, in a fresh guest shell session per attempt (#75).

    The prompt wait is where a slow boot bites: the console service
    accepts the connection and the pty echoes, but the shell behind
    it has not reached its first prompt. A stalled session is closed
    and replaced instead of failing the test — and every command this
    harness sends is idempotent, so re-running it in a new session
    is safe.

    Markers are guest-computed sentinels (``echo X-$((6*7))`` /
    ``X-42``): the pty echoes the sent bytes verbatim, so a marker
    that appears in the command text would match the echo and pass
    without the command's output ever arriving.

    The fresh-session-per-attempt shape is also the workaround for
    #103's mid-session console stalls (input echoed, never
    executed): a stalled session times out, and its replacement is
    a new connection — exactly what a human reconnecting does.
    """
    for attempt in range(1, CONSOLE_ATTEMPTS + 1):
        try:
            reader, writer = await microvm.console(workspace_id, user=user)
            try:
                if app is not None or signer is not None:
                    # The console challenge (#123): answer it with the
                    # workspace key before any prompt appears.
                    await answer_console_auth(reader, writer, workspace_id, app, signer)
                needle = (
                    CONSOLE_PROMPT_NEEDLE
                    if user == "root"
                    else USER_CONSOLE_PROMPT_NEEDLE
                )
                await read_until(reader, needle)
                writer.write(command.encode() + b"\n")
                await writer.drain()
                await read_until(reader, marker.encode())
                return
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        # TimeoutError is an OSError subclass, so the stalled-session
        # paths (read_until's AssertionError, a dead stream's OSError)
        # all land here as retryable.
        except (AssertionError, OSError) as exc:
            if attempt == CONSOLE_ATTEMPTS:
                raise AssertionError(
                    f"{marker!r} never arrived within {CONSOLE_ATTEMPTS} "
                    f"console sessions (last session: {exc})"
                ) from exc
            print(
                f"console session {attempt}/{CONSOLE_ATTEMPTS} for {marker!r} "
                f"stalled ({exc}); retrying in a fresh session",
                flush=True,
            )


async def await_pod_running(
    microvm, workspace_id: str, timeout_s: float = 120.0
) -> None:
    """Block until the runner pod reports the VM as running.

    A crash-looping or never-scheduled pod (bad image, no /dev/kvm on
    the node) never reaches ``running``; fail with the observed status
    instead of letting a ``starting`` snapshot pass.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        status = (await microvm.info(workspace_id)).status.value
        if status == "running":
            return
        if status in ("stopped", "absent"):
            raise AssertionError(
                f"runner pod for {workspace_id!r} went {status!r} before "
                "running — check the image import and the node's /dev/kvm"
            )
        await asyncio.sleep(1.0)
    raise AssertionError(
        f"runner pod for {workspace_id!r} stayed {status!r} for {timeout_s}s "
        "— check the image import and the node's /dev/kvm"
    )


APPLIANCE = os.environ.get("MSKSD_TEST_APPLIANCE")


def read_appliance_journal(state_disk: Path) -> list[str] | None:
    """The appliance's persistent journal, read from the state disk.

    The trixie appliance (#92) persists journald to the state disk
    (/var is a bind mount from it); after teardown, the journal is
    host-readable evidence. A hard-stopped VM (a crash, or the
    supervisor's grace expiring) leaves the ext4 mid-transaction —
    debugfs refuses such a filesystem — so the read runs on a SPARSE
    copy (`cp --sparse=always`: the state disk is an 8 GiB file with
    large holes, and a dense copy would ENOSPC a tmpfs-backed
    TMPDIR) repaired by e2fsck (the journal replays; unprivileged,
    no loop mount), then debugfs rdump + journalctl --directory.
    Returns None when the host lacks the tools (the assertions that
    need it then soften to a printed note instead of failing) and
    [] when no intact journal file survived — the caller's "no
    journal files" assertion. Archived-and-corrupted files
    (``*.journal~``) are deliberately not counted: journalctl cannot
    read them, and an empty intact set must fail, not pass vacuously.
    A failed copy/extract/read raises instead: an extraction problem
    must not masquerade as "no journal on the disk".
    """
    tools = ("journalctl", "debugfs", "e2fsck", "cp")
    if any(shutil.which(t) is None for t in tools):
        return None
    with tempfile.TemporaryDirectory(prefix="msks-appliance-journal") as tmp:
        repair = Path(tmp) / "state.ext4"
        copy = subprocess.run(
            ["cp", "--sparse=always", str(state_disk), str(repair)],
            capture_output=True,
            timeout=300,
        )
        assert copy.returncode == 0, (
            f"sparse copy of the state disk failed:\n{copy.stderr}"
        )
        fsck = subprocess.run(
            ["e2fsck", "-fy", str(repair)],
            capture_output=True,
            timeout=300,
        )
        # e2fsck's exit code is a bitmask (1 = errors corrected);
        # anything above 2 means the copy could not be repaired.
        assert fsck.returncode <= 2, (
            f"e2fsck could not repair the state disk copy:\n{fsck.stdout}"
        )
        extract = Path(tmp) / "extract"
        extract.mkdir()
        dump = subprocess.run(
            [
                "debugfs",
                "-R",
                f"rdump /var/log/journal {extract}",
                str(repair),
            ],
            capture_output=True,
            timeout=120,
        )
        # rdump's stderr mixes benign unprivileged-ownership noise
        # with real errors; the exit code separates them.
        assert dump.returncode == 0, (
            f"debugfs rdump of the journal directory failed:\n{dump.stderr}"
        )
        journal_dir = extract / "journal"
        if not any(journal_dir.rglob("*.journal")):
            return []
        text = subprocess.run(
            [
                "journalctl",
                "--directory",
                str(journal_dir),
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert text.returncode == 0, (
            f"journalctl could not read the extracted journal:\n{text.stderr}"
        )
        return text.stdout.splitlines()


def host_net_installed() -> bool:
    """The one-time installer's footprint: the appliance's bridge.

    The host network (bridge, tap, forwarding, NAT) is installed
    once as root by scripts/appliance-host-setup.sh (#101); the
    appliance itself starts unprivileged, so the gate is the
    install's presence, not sudo.
    """
    try:
        return (
            subprocess.run(
                ["ip", "link", "show", "dev", "msksbr0"],
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    except OSError, subprocess.TimeoutExpired:
        return False


needs_appliance = pytest.mark.skipif(
    not APPLIANCE
    or not os.access("/dev/kvm", os.W_OK)
    or not (REPO_ROOT / ".appliance" / "vmlinux").is_file()
    or not host_net_installed(),
    reason=(
        "set MSKSD_TEST_APPLIANCE=1 with /dev/kvm, the one-time host "
        "network (sudo bash scripts/appliance-host-setup.sh), and "
        "devenv tasks run msks:appliance-build + msks:build-guest"
    ),
)


def seed_legacy_state_disk(state_disk: Path, marker_text: str) -> bool:
    """Seed a pre-#101 (root-daemon) state disk shape (#101 review).

    A legacy disk carries the daemon's entries at the /state TOP
    level; first boot with the service-user daemon must converge
    them into /state/msksd with service-user ownership — a missed
    move silently rotates the TLS CA, so the convergence gets an
    end-to-end check. debugfs writes root-owned inodes, exactly the
    legacy ownership. Returns False when the host lacks debugfs
    (the assertions then soften to a printed note, like the journal
    read). The seeded entries are ones the running daemon tolerates:
    volumes/ it never scans, msks-cert.host it rewrites.
    """
    if shutil.which("debugfs") is None:
        return False
    marker = state_disk.parent / "legacy-marker"
    marker.write_text(marker_text)
    for op in (
        "mkdir /volumes",
        f"write {marker} /volumes/legacy-marker",
        f"write {marker} /msks-cert.host",
    ):
        seed = subprocess.run(
            ["debugfs", "-w", "-R", op, str(state_disk)],
            capture_output=True,
            timeout=120,
        )
        assert seed.returncode == 0, f"seeding {op!r} failed: {seed.stderr}"
    return True


def devenv_processes(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """Drive the devenv process manager from inside the shell."""
    return subprocess.run(
        ["bash", "-c", f"devenv processes {' '.join(args)}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        env={**os.environ, "DEVENV_TUI": "false"},
    )


def devenv_task(task: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run one devenv task — the opt-in appliance lifecycle (#141).

    The appliance no longer lives under the process manager (that is
    the bare-host dev daemon's home now): msks:appliance-up boots it
    detached behind a pidfile, msks:appliance-down TERMs that pid
    through the ACPI-first trap. Environment surgery (state disk,
    cmdline extras, memory) reaches the VM exactly as before — the
    task and its detached child inherit this process's environment.
    """
    return subprocess.run(
        ["bash", "-c", f"devenv tasks run {task}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        env={**os.environ, "DEVENV_TUI": "false"},
    )


EGRESS = os.environ.get("MSKSD_TEST_EGRESS")


def default_route_iface() -> str:
    """The uplink NAT hides guests behind (the default route's dev)."""
    route = subprocess.run(
        ["ip", "route", "show", "default"], capture_output=True, text=True
    ).stdout
    parts = route.split()
    for i, part in enumerate(parts):
        if part == "dev":
            return parts[i + 1]
    raise AssertionError(f"no default route to NAT behind: {route!r}")


def uplink_address() -> str:
    """The host's own address on the default-route interface.

    A connect() on an unconnected-protocol socket only picks the
    route's source address — nothing is sent — so this names the
    address NAT'd guest traffic wears reaching the host itself:
    where the git-out smoke's scratch sshd listens (#81).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("9.9.9.9", 53))
        return sock.getsockname()[0]


needs_egress = pytest.mark.skipif(
    not EGRESS
    or not VMLINUX
    or not ROOTFS
    or not os.access("/dev/kvm", os.W_OK)
    or os.geteuid() != 0,
    reason=(
        "set MSKSD_TEST_EGRESS=1 as root with /dev/kvm, built guest "
        "assets, and MSKSD_TEST_VMLINUX/MSKSD_TEST_ROOTFS"
    ),
)


#: The dev-workspace bootstrap (#77) is minutes of downloads on a
#: slow path, not seconds — far past CONSOLE_TIMEOUT_S. The polls
#: below drive their own deadline; this bounds the whole sequence
#: (bootstrap downloads + the in-guest suite).
DEV_BOOTSTRAP_TIMEOUT_S = float(
    os.environ.get("MSKSD_TEST_DEV_BOOTSTRAP_TIMEOUT_S", "3600")
)


def dev_workspace_seed() -> str:
    """The bootstrap payload from the repo's scripts/ tree (#77)."""
    path = REPO_ROOT / "scripts" / "dev-workspace.sh"
    return path.read_text()


async def await_dev_state(microvm, app, workspace_id: str, needle: bytes) -> bytes:
    """Poll the guest's bootstrap state trail until it says ``needle``.

    Each probe is a fresh console session well inside
    CONSOLE_TIMEOUT_S: the file's contents (the running step name)
    arrive ahead of the E-42 sentinel, so a stall fails with the last
    observed step named in the assertion — the bootstrap's own
    /root/.msks-bootstrap/ trail, no log scraping. The fresh
    session per probe is also the #103 workaround (a stalled
    session's replacement is a new connection).

    The probe reads both the bootstrap state file and the
    unit-tests rc file (whichever exists — the sentinel is its own
    echo's computation, not ``cat``'s exit status, so one missing
    file still answers): both live under /root/.msks-bootstrap/ and
    carry short sentinel values. A trail already showing the suite
    finished nonzero (``done-N``, N≠0) fails immediately instead of
    spinning to the deadline.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEV_BOOTSTRAP_TIMEOUT_S
    last = b""
    while loop.time() < deadline:
        try:
            # user="root": the prelude-v1 helper refuses prelude-less
            # connections (MSKS ERR timeout after its read deadline),
            # so the raw console() default cannot speak to it.
            reader, writer = await microvm.console(workspace_id, user="root")
            try:
                await answer_console_auth(reader, writer, workspace_id, app)
                await read_until(reader, CONSOLE_PROMPT_NEEDLE)
                writer.write(
                    b"cat /root/.msks-bootstrap/state "
                    b"/root/.msks-bootstrap/unit-tests.rc 2>/dev/null; "
                    b"echo E-$((21*2))\n"
                )
                await writer.drain()
                data = await read_until(reader, b"E-42", timeout_s=CONSOLE_TIMEOUT_S)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        except (AssertionError, OSError) as exc:
            last = f"<console probe failed: {exc}>".encode()
        else:
            # Strip the echoed command (it carries E-$((21*2)), never
            # the computed E-42) and the sentinel line; what is left
            # is the state trail (plus prompt noise).
            body = data.split(b"E-$((21*2))", 1)[-1]
            body = body.split(b"E-42", 1)[0]
            last = body.strip()
            if needle in body:
                return data
            for line in body.splitlines():
                if line.startswith(b"done-") and line != b"done-0":
                    raise AssertionError(
                        "the in-guest suite exited nonzero "
                        f"({line!r}); see /root/.msks-bootstrap/unit-tests.log"
                    )
        await asyncio.sleep(15)
    raise AssertionError(
        f"bootstrap state never reached {needle!r} within "
        f"{DEV_BOOTSTRAP_TIMEOUT_S}s; last observed state: {last[-200:]!r}"
    )


#: Host-side ssh/rsync ceilings (#110): the forward is up before the
#: client runs, so this bounds connection setup + command round-trip
#: on a slow nested-KVM guest (CI raises it alongside the others).
SSH_CMD_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_SSH_TIMEOUT_S", "90"))

#: The host-side tools the ssh smoke needs (#110): the devenv shell
#: ships openssh + rsync; a bare environment without them skips
#: rather than fails.
SSH_BIN = shutil.which("ssh")
SSH_KEYGEN_BIN = shutil.which("ssh-keygen")
RSYNC_BIN = shutil.which("rsync")
needs_ssh_tools = pytest.mark.skipif(
    not (SSH_BIN and SSH_KEYGEN_BIN and RSYNC_BIN),
    reason="ssh, ssh-keygen, and rsync must be on PATH (the devenv shell ships them)",
)

#: The git-out smoke's host tools (#81): a scratch sshd serves the
#: push target and a scratch agent carries the credential — the
#: devenv shell ships all four; a bare environment skips.
GIT_BIN = shutil.which("git")
SSHD_BIN = shutil.which("sshd")
SSH_AGENT_BIN = shutil.which("ssh-agent")
SSH_ADD_BIN = shutil.which("ssh-add")
needs_git_tools = pytest.mark.skipif(
    not (GIT_BIN and SSHD_BIN and SSH_AGENT_BIN and SSH_ADD_BIN),
    reason="git, sshd, ssh-agent, and ssh-add must be on PATH",
)

#: The git-out legs' ceiling (#81): apt + an HTTPS fetch inside the
#: guest and the push itself. Download-bound, like the bootstrap
#: smoke's budget — CI raises it on slow paths.
GIT_OUT_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_GIT_OUT_TIMEOUT_S", "600"))


def free_port() -> int:
    """One loopback port the kernel has not handed out (bind/close)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def await_guest_trail(
    microvm, app, workspace_id: str, probe: str, needle: bytes, timeout_s: float
) -> None:
    """Poll a guest-side probe command until its output carries
    ``needle``.

    The git-out smoke's long legs (apt, an HTTPS ``git ls-remote``)
    run detached inside the guest and append step names to a trail
    file; each probe here is a fresh console session well inside
    CONSOLE_TIMEOUT_S — the same fresh-session-per-probe shape the
    bootstrap poll uses (#103 workaround). The probe fragment cats
    the trail and tails the run log, so a wait that times out or
    fast-fails names the step that died with its own stderr in the
    assertion — no rerun needed to see why. A ``fail-*`` trail line
    fails the wait immediately instead of spinning to the deadline.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last = b""
    while loop.time() < deadline:
        try:
            reader, writer = await microvm.console(workspace_id, user="root")
            try:
                await answer_console_auth(reader, writer, workspace_id, app)
                await read_until(reader, CONSOLE_PROMPT_NEEDLE)
                writer.write(f"{probe}; echo E-$((21*2))\n".encode())
                await writer.drain()
                data = await read_until(reader, b"E-42", timeout_s=CONSOLE_TIMEOUT_S)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        except (AssertionError, OSError) as exc:
            last = f"<console probe failed: {exc}>".encode()
        else:
            body = data.split(b"E-$((21*2))", 1)[-1].split(b"E-42", 1)[0]
            last = body.strip()
            if needle in body:
                return
            for line in body.splitlines():
                if line.startswith(b"fail-"):
                    raise AssertionError(
                        f"guest trail reported {line!r} while awaiting "
                        f"{needle!r}: {last[-400:]!r}"
                    )
        await asyncio.sleep(5)
    raise AssertionError(
        f"the guest never showed {needle!r} within {timeout_s}s; "
        f"last observed: {last[-400:]!r}"
    )

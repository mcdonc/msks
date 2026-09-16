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
"""

import asyncio
import contextlib
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
import websockets
from httpx import AsyncClient
from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import (
    K8sSettings,
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)

from msks import persist

VMLINUX = os.environ.get("MSKSD_TEST_VMLINUX")
INITRD = os.environ.get("MSKSD_TEST_INITRD")
ROOTFS = os.environ.get("MSKSD_TEST_ROOTFS")
CMDLINE = os.environ.get("MSKSD_TEST_CMDLINE")
KUBECONFIG = os.environ.get("MSKSD_TEST_KUBECONFIG")
REPO_ROOT = Path(__file__).resolve().parents[3]
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


async def run_in_console(
    microvm,
    workspace_id: str,
    command: str,
    marker: str,
    user: str = "root",
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
    """
    for attempt in range(1, CONSOLE_ATTEMPTS + 1):
        try:
            reader, writer = await microvm.console(workspace_id, user=user)
            try:
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


@needs_local
async def test_local_vm_boot_and_shutdown() -> None:
    # A shallow base: deep pytest tmp dirs can push the API socket path
    # past the AF_UNIX 108-byte limit under xdist workers. Shutdown is
    # the power-button press — the guest's logind runs the clean
    # poweroff, which matters with persistent disks (#14: a hard stop
    # would drop page-cache writes).
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        egress=False,
    )
    try:
        await microvm.launch(spec)
        info = await microvm.info(wid)
        assert info.status.value == "running"
        # Wait for userspace before shutting down: the graceful shutdown
        # is an ACPI power-button press, and the guest only answers it
        # once its logind is running — pressing earlier would drop the
        # event and time out against a VM that is running but not yet
        # listening.
        await await_guest_up(serial_log)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        # Never leak a live VMM (and its /dev/kvm handle) on failure;
        # print and keep the guest's evidence for the artifact upload.
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_local
async def test_local_persistence_across_restart_and_reset() -> None:
    """The two persistent artifacts (#14), end to end on real KVM:

    - a root write (what an ``apt install`` does) survives a full
      stop/start cycle — the overlay, not the base, carries it;
    - a /home write survives the same cycle — the home volume;
    - factory reset drops the root write and keeps the /home write.
    """
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
    )
    root_marker = f"ROOT-{uuid.uuid4().hex[:6]}"
    home_marker = f"HOME-{uuid.uuid4().hex[:6]}"

    async def boot_and_probe(probe_commands: list[tuple[str, str]]) -> None:
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        for command, marker in probe_commands:
            await run_in_console(microvm, wid, command, marker)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)

    try:
        await microvm.prepare(spec)
        assert persist.overlay_path(state_dir, wid).is_file()
        assert persist.home_volume_path(state_dir, wid).is_file()
        await boot_and_probe(
            [
                # The write sentinels are guest-computed ($((6*7)) → 42):
                # the pty echoes the sent bytes, so a marker that
                # appears in the command text would match the echo —
                # the restart boot's `cat` (below) carries the actual
                # content claim, with a per-run marker the sent bytes
                # never contain.
                (
                    f"echo {root_marker} > /root/probe && echo WROTE-$((6*7))",
                    "WROTE-42",
                ),
                (
                    f"echo {home_marker} > /home/probe && echo WROTE-$((6*7))",
                    "WROTE-42",
                ),
            ]
        )
        serial_log.unlink(missing_ok=True)
        await boot_and_probe(
            [
                ("cat /root/probe", root_marker),
                ("cat /home/probe", home_marker),
            ]
        )
        # Factory reset: pristine root, same /home.
        serial_log.unlink(missing_ok=True)
        await microvm.reset(wid)
        assert not persist.overlay_path(state_dir, wid).exists()
        assert persist.home_volume_path(state_dir, wid).is_file()
        await boot_and_probe(
            [
                ("cat /home/probe", home_marker),
                ("test ! -e /root/probe && echo GONE-$((6*7))", "GONE-42"),
            ]
        )
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_local
async def test_local_user_data_provisioning() -> None:
    """The #41 seed end to end on real KVM, through cloud-init: a
    user_data workspace gets a cidata seed built at prepare; the
    guest's cloud-init runs a script payload on the first boot, a
    stop/start cycle does not re-run it, and a factory reset (the
    overlay's death takes /var/lib/cloud with it) re-provisions from
    the same seed. A cloud-config document lands too (write_files),
    and the seed reaches the guest as a labeled disk.
    """
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    script = (
        "#!/bin/sh\n"
        "count=$(cat /root/firstboot-count 2>/dev/null || echo 0)\n"
        "echo $((count + 1)) > /root/firstboot-count\n"
    )
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
        user_data=script,
    )

    async def boot_and_probe(expected_count: int) -> None:
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        # Payloads run in cloud-final, which can lag the login getty;
        # wait for cloud-init to be done before asserting on files it
        # was supposed to write.
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(
            microvm, wid, "cat /root/firstboot-count", str(expected_count)
        )
        # The seed reaches the guest as a labeled, read-only disk.
        await run_in_console(microvm, wid, "blkid -o value -s LABEL /dev/vdc", "cidata")
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)

    try:
        await microvm.prepare(spec)
        seed = persist.seed_path(state_dir, wid)
        assert seed.is_file()
        await boot_and_probe(1)
        # stop/start: cloud-init's state survives on the overlay, the
        # script does not run again.
        serial_log.unlink(missing_ok=True)
        await boot_and_probe(1)
        # Factory reset: the overlay (cloud-init state included) dies;
        # the seed stays and provisions the pristine root again.
        serial_log.unlink(missing_ok=True)
        await microvm.reset(wid)
        assert not persist.overlay_path(state_dir, wid).exists()
        assert seed.is_file()
        await boot_and_probe(1)
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_local
async def test_local_user_data_cloud_config() -> None:
    """A cloud-config document (the payload form scripts cannot
    cover) lands through cloud-init: write_files puts the file where
    the document says, on the first boot and only there."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    marker = f"CLOUDCONFIG-{uuid.uuid4().hex[:6]}"
    cloud_config = (
        "#cloud-config\n"
        "write_files:\n"
        "  - path: /root/provisioned.txt\n"
        f"    content: {marker}\n"
    )
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
        user_data=cloud_config,
    )
    try:
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(microvm, wid, "cat /root/provisioned.txt", marker)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_k8s
async def test_k8s_pod_lifecycle() -> None:
    settings = Settings(
        vmm=VmmSettings(driver="k8s"),
        k8s=K8sSettings(
            kubeconfig=KUBECONFIG,
            namespace=os.environ.get("MSKSD_TEST_NAMESPACE", "default"),
            # The image devenv task `msks:build-runner-image` builds and
            # `k3s ctr images import` loads; conftest.py exposes it via
            # MSKSD_TEST_RUNNER_IMAGE once the archive exists. The image
            # owns the guest it boots, so the spec's host-side artifact
            # paths do not reach the pod.
            runner_image=os.environ.get(
                "MSKSD_TEST_RUNNER_IMAGE", K8sSettings.runner_image
            ),
        ),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    # The runner image owns the guest it boots; these spec fields are
    # host-side bookkeeping and do not reach the pod (see spec_env).
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path("/opt/msks/vmlinux"),
        rootfs=Path("/opt/msks/rootfs.ext4"),
        egress=False,
    )
    try:
        await microvm.launch(spec)
        await await_pod_running(microvm, wid)
    finally:
        # k8s cleanup deletes the pod and tolerates it already being
        # gone, on both the success and failure paths.
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)


# --- appliance smoke (#10) -------------------------------------------------
#
# Boots the real appliance through the devenv supervisor scripts — the
# same path an operator uses — then drives one workspace VM through
# the API served from inside it. Opt-in: it needs /dev/kvm (nested
# virt: the workspace boots inside the appliance VM), the one-time
# host network install (scripts/appliance-host-setup.sh, run once
# as root), and built appliance + guest assets.
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


def _host_net_installed() -> bool:
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
    or not _host_net_installed(),
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


def _devenv_processes(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """Drive the devenv process manager from inside the shell."""
    return subprocess.run(
        ["bash", "-c", f"devenv processes {' '.join(args)}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        env={**os.environ, "DEVENV_TUI": "false"},
    )


@needs_appliance
async def test_appliance_boot_and_workspace() -> None:
    app_dir = REPO_ROOT / ".appliance"
    base = "https://192.168.77.2:8660/api/v1"
    wid = f"appliance-{uuid.uuid4().hex[:8]}"

    # Refuse to stomp a *running* appliance — including the README's
    # documented orphan case (manager dead, VMM still answering on
    # api.sock, unreachable by `devenv processes down`).
    if (app_dir / "api.sock").is_socket():
        probe = subprocess.run(
            [
                "curl",
                "-sS",
                "--unix-socket",
                str(app_dir / "api.sock"),
                "-X",
                "PUT",
                "http://localhost/api/v1/vm.info",
            ],
            capture_output=True,
            timeout=30,
        )
        if probe.returncode == 0:
            pytest.skip("an appliance VMM is already answering on this host")

    # The upgrade-path pin (#101 review): this run boots a FRESH
    # state disk seeded with the pre-#101 legacy layout, so the
    # migration runs for real every time — and the dev host's own
    # state disk stays untouched.
    marker_text = f"pre-101 daemon state {uuid.uuid4().hex[:8]}\n"
    legacy_dir = tempfile.TemporaryDirectory(prefix="msks-legacy-state")
    state_disk = Path(legacy_dir.name) / "state.ext4"
    # Sparse copy: the template is an 8 GiB image with large holes,
    # and a dense copy would ENOSPC a tmpfs-backed TMPDIR (the same
    # care read_appliance_journal takes).
    copy = subprocess.run(
        [
            "cp",
            "--sparse=always",
            str(app_dir / "image" / "state.ext4"),
            str(state_disk),
        ],
        capture_output=True,
        timeout=300,
    )
    assert copy.returncode == 0, (
        f"sparse copy of the state template failed: {copy.stderr}"
    )
    # The template is 0444 in the store; the writable disk the VMM
    # opens O_RDWR needs the write bit (appliance-setup.sh chmods its
    # own copy — this pre-made one must match).
    state_disk.chmod(0o644)
    seeded = seed_legacy_state_disk(state_disk, marker_text)
    if not seeded:
        print("debugfs not on PATH; skipping the legacy-state assertions")
    prior_state_env = os.environ.get("MSKSD_APPLIANCE_STATE")
    os.environ["MSKSD_APPLIANCE_STATE"] = str(state_disk)
    # The console bring-up knob's documented slow-host use: the
    # workspace guest boots under nested KVM, and the default 15s
    # vsock window is short there (the run script's own comment).
    # An operator's value wins.
    prior_cmdline_extra = os.environ.get("MSKS_APPLIANCE_CMDLINE_EXTRA")
    if not prior_cmdline_extra:
        os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = "msksd.vsock_wait_timeout_s=120"

    async def await_token(timeout_s: float = 120.0) -> str:
        # `up -d` returns when the MANAGER starts; setup (state-disk
        # copy, token generation) still runs asynchronously — poll for
        # the token instead of assuming it exists.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            token_file = app_dir / "bootstrap-token"
            if token_file.is_file() and token_file.read_text().strip():
                return token_file.read_text().strip()
            await asyncio.sleep(0.5)
        raise AssertionError("appliance bootstrap token never appeared")

    async def await_api(timeout_s: float = 120.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last = ""
        while loop.time() < deadline:
            try:
                response = await client.get(f"{base}/health")
                last = f"{response.status_code} {response.text[:100]}"
                if response.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last = repr(exc)
            await asyncio.sleep(1.0)
        serial = (app_dir / "serial.log").read_text(errors="replace")[-2000:]
        raise AssertionError(
            f"appliance API never became healthy ({last}); serial tail:\n{serial}"
        )

    status = None
    token = headers = None
    up = None
    try:
        # Inside the guarded region: a failed start still tears the
        # detached manager down below instead of leaving it running
        # against the temp state disk with mutated env.
        up = _devenv_processes("up", "-d")
        assert up.returncode == 0, (
            f"devenv processes up failed:\n{up.stdout}\n{up.stderr}"
        )
        token = await await_token()
        headers = {"authorization": f"Bearer {token}"}
        await await_api()
        # A bare create (#40): the appliance imported its built-in
        # default image at first boot; the catalog resolves the boot
        # artifacts with nothing else specified.
        response = await client.post(
            f"{base}/workspaces",
            json={"id": wid},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        row = response.json()
        assert row["kernel"].endswith("/kernel"), row
        images = await client.get(f"{base}/images", headers=headers)
        assert images.status_code == 200
        defaults = [i for i in images.json() if i["default"]]
        assert [i["name"] for i in defaults] == ["debian"], images.text
        response = await client.post(f"{base}/workspaces/{wid}/start", headers=headers)
        assert response.status_code in (200, 202), response.text

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 120.0
        while loop.time() < deadline:
            response = await client.get(f"{base}/workspaces/{wid}", headers=headers)
            status = response.json().get("status")
            if status == "running":
                break
            await asyncio.sleep(1.0)
        else:
            raise AssertionError(f"workspace never reached running: {status}")

        # The workspace shell (#21): an authenticated byte stream into
        # the VM over the daemon's websocket. Drive one command, read
        # its output back, detach, and require the workspace to keep
        # running afterwards.
        ws_ctx = ssl.create_default_context()
        ws_ctx.check_hostname = False
        ws_ctx.verify_mode = ssl.CERT_NONE
        ws_url = (
            base.replace("https://", "wss://")
            + f"/workspaces/{wid}/console?token={token}"
        )
        async with websockets.connect(ws_url, ssl=ws_ctx, open_timeout=30) as shell_ws:
            # The marker's rendering differs from the sent bytes, so
            # the step proves OUTPUT flowed — not merely the pty echo.
            await shell_ws.send(b"echo MSKS-$((6*7))-SHELL-SMOKE\n")
            console_got = b""
            console_deadline = loop.time() + 180.0
            while b"MSKS-42-SHELL-SMOKE" not in console_got:
                if loop.time() >= console_deadline:
                    raise AssertionError(
                        f"console never echoed the marker; got: {console_got!r}"
                    )
                # A silent gap is normal, not failure: a nested-virt
                # guest can take a minute or more past "running" to
                # arm its vsock console, and the per-recv wait must
                # not cut the marker's own deadline short.
                try:
                    message = await asyncio.wait_for(shell_ws.recv(), 30.0)
                except TimeoutError:
                    continue
                console_got += (
                    message if isinstance(message, bytes) else message.encode()
                )

            # Guest networking, end to end through the appliance
            # (#52, #70 review): the default (egress) workspace took a
            # DHCP lease from the daemon's resolver path — the address
            # on the NIC and a resolution through the forwarder. The
            # host side must be wired (appliance-setup.sh: forwarding,
            # NAT, and the appliance's upstream resolver) for these to
            # pass, which is exactly the posture being pinned.
            #
            # A one-shot probe the SENDER retries: the guest may not
            # have its lease yet (the console session can open while
            # networkd is still configuring), and the console
            # transport can corrupt a sent line (#103: mangled pty
            # echo artifacts) — a fresh resend supersedes the corrupted
            # round, so each probe measures the network path, not the
            # console's byte fidelity. The markers render differently
            # from the sent bytes, so the pty echo of the command
            # cannot satisfy the wait.
            probe_buf = b""

            async def probe_collect(marker: bytes) -> bytes:
                nonlocal probe_buf
                end = loop.time() + 7.0
                while marker not in probe_buf and loop.time() < end:
                    try:
                        message = await asyncio.wait_for(shell_ws.recv(), 1.0)
                    except TimeoutError:
                        continue
                    probe_buf += (
                        message if isinstance(message, bytes) else message.encode()
                    )
                return probe_buf

            async def probe(marker: bytes, command: bytes) -> None:
                # Send first, then collect: the first collect window is
                # not free time to skip.
                end = loop.time() + 180.0
                await shell_ws.send(command)
                while marker not in (await probe_collect(marker)):
                    if loop.time() >= end:
                        raise AssertionError(f"console never showed {marker!r}")
                    await shell_ws.send(command)
                    await asyncio.sleep(7.0)

            await probe(
                b"NET-42-UP",
                b"ip -4 addr | grep -q 172.31. && echo NET-$((6*7))-UP\n",
            )
            await probe(
                b"DNS-42-UP",
                b"getent hosts deb.debian.org >/dev/null && echo DNS-$((6*7))-UP\n",
            )
            # Forwarded egress through the NAT'd uplink (#101): a TCP
            # connection the guest initiates must traverse the forward
            # chain and the masquerade — the DHCP and DNS markers above
            # both work without them (DNS relays through the daemon's
            # own socket), so this is the probe that proves the path.
            await probe(
                b"TCP-42-UP",
                b"timeout 5 bash -c '</dev/tcp/deb.debian.org/80' "
                b"&& echo TCP-$((6*7))-UP\n",
            )
        response = await client.get(f"{base}/workspaces/{wid}", headers=headers)
        assert response.json().get("status") == "running", response.text

        # #36: a killed vsock socat recovers without a workspace
        # restart. The guest's msks-console.service respawns the
        # listener (Restart=always); prove a fresh console connect
        # works after the listener is SIGKILLed. The marker renders
        # differently from the sent bytes, so it proves OUTPUT flowed
        # — not merely the pty echo of the input.
        async with websockets.connect(ws_url, ssl=ws_ctx, open_timeout=30) as kill_ws:
            await kill_ws.send(
                b"systemctl kill --kill-who=main -s SIGKILL msks-console.service\n"
            )
        recovered_at = None
        for attempt in range(30):
            await asyncio.sleep(1.0)
            # Only the connect phase is retriable: a console that
            # connects but never serves the marker fails fast below —
            # TimeoutError is an OSError subclass, so a blanket
            # except here would swallow the marker wait for ~15 min.
            try:
                recovery_ws = await websockets.connect(
                    ws_url, ssl=ws_ctx, open_timeout=10
                )
            except OSError, websockets.WebSocketException:
                continue
            try:
                await recovery_ws.send(b"echo MSKS-$((23*2))-RECOVERED\n")
                recovered = b""
                while b"MSKS-46-RECOVERED" not in recovered:
                    message = await asyncio.wait_for(recovery_ws.recv(), 30.0)
                    recovered += (
                        message if isinstance(message, bytes) else message.encode()
                    )
            finally:
                await recovery_ws.close()
            recovered_at = attempt
            break
        assert recovered_at is not None, (
            "console never recovered after the guest socat was killed"
        )

        # Lifecycle calls may legally take the daemon's full graceful
        # window (MSKSD_SHUTDOWN_TIMEOUT_S, 20s default, plus the
        # terminate path): the module client's 10s default would cut a
        # healthy-but-slow stop off mid-flight on a busy host.
        response = await client.post(
            f"{base}/workspaces/{wid}/stop", headers=headers, timeout=60.0
        )
        assert response.status_code == 200, response.text
        response = await client.delete(
            f"{base}/workspaces/{wid}", headers=headers, timeout=60.0
        )
        assert response.status_code == 200, response.text
        response = await client.get(f"{base}/workspaces/{wid}", headers=headers)
        assert response.status_code == 404
    finally:
        if headers is not None:
            with contextlib.suppress(Exception):
                await client.delete(f"{base}/workspaces/{wid}", headers=headers)
        with contextlib.suppress(Exception):
            await client.post(f"{base}/workspaces/{wid}/stop", headers=headers)
        # The env restore comes first: a failed teardown assert below
        # must not leak process-global env into other tests.
        if prior_state_env is None:
            del os.environ["MSKSD_APPLIANCE_STATE"]
        else:
            os.environ["MSKSD_APPLIANCE_STATE"] = prior_state_env
        if prior_cmdline_extra is None:
            del os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"]
        elif prior_cmdline_extra != os.environ.get("MSKS_APPLIANCE_CMDLINE_EXTRA"):
            os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = prior_cmdline_extra
        down = _devenv_processes("down", timeout=300)
        assert down.returncode == 0, (
            f"devenv processes down failed:\n{down.stdout}\n{down.stderr}"
        )
        # The state disk itself lives until the post-teardown reads
        # below are done: the journal and the migration asserts read
        # it after the appliance is down.
    assert not (app_dir / "api.sock").exists()
    # The supervisor is gone too: teardown is its view of "stopped",
    # not just pidfile/socket absence.
    listing = _devenv_processes("list", timeout=120)
    assert "No process manager is running" in listing.stdout + listing.stderr, (
        f"process manager still alive after down:\n{listing.stdout}"
    )
    # The journal is the appliance's own story, persisted to the state
    # disk by journald (#92): read it back from the host and require
    # the boot's records to have survived the teardown. Softens to a
    # printed note on hosts without journalctl/debugfs.
    journal = read_appliance_journal(state_disk)
    if journal is None:
        print("journalctl/debugfs not on PATH; skipping journal assertions")
    else:
        assert journal, "state disk carries no journal files"
        joined = "\n".join(journal)
        assert "Started msksd.service" in joined, (
            "journal never recorded the daemon start; "
            f"last 20 lines:\n{chr(10).join(journal[-20:])}"
        )
        assert "msks appliance: cmdline env:" in joined, (
            "journal never recorded the cmdline bridge"
        )
        # The privilege contract (#101): the daemon — and, through
        # ambient inheritance, every tool and VMM it execs — runs as
        # the service user, in the kvm group, holding exactly
        # CAP_NET_BIND_SERVICE (10) + CAP_NET_ADMIN (12) = 0x1400.
        # The boot script prints `id` and the /proc capability sets
        # before execing msksd; the journal carries them.
        identity = [ln for ln in journal if "daemon identity:" in ln]
        assert identity, "journal never recorded the daemon identity"
        match = re.search(r"uid=(\d+)\(msksd\)", identity[-1])
        assert match, f"daemon identity is not the msksd user: {identity[-1]!r}"
        service_uid = int(match.group(1))
        assert service_uid != 0, identity[-1]
        # uid and gid are allocated independently at build time; the
        # ownership assert below must use each, not assume they match.
        gid_match = re.search(r"gid=(\d+)\(msksd\)", identity[-1])
        assert gid_match, f"daemon identity carries no gid: {identity[-1]!r}"
        service_gid = int(gid_match.group(1))
        assert "(kvm)" in identity[-1], f"daemon missed the kvm group: {identity[-1]!r}"
        for field in ("CapEff", "CapAmb"):
            lines = [ln for ln in journal if f"daemon {field}:" in ln]
            assert lines, f"journal never recorded the daemon {field}"
            assert "0000000000001400" in lines[-1], (
                f"daemon {field} is not the two-capability set: {lines[-1]!r}"
            )
        # The legacy state converged into the service user's home
        # (#101 review): the seeded pre-#101 entries moved (not were
        # recreated — content survives) and carry the service user's
        # ownership, and nothing daemon-shaped stays at the top level
        # (the host-seeded debug-shell/diag.sh markers are not the
        # daemon's and are not touched).
        if seeded and journal is not None:
            # The teardown can leave the ext4 mid-transaction (a
            # hard-stopped guest — the same case read_appliance_journal
            # repairs its own copy for); repair the throwaway disk in
            # place so debugfs opens it.
            fsck = subprocess.run(
                ["e2fsck", "-fy", str(state_disk)],
                capture_output=True,
                timeout=300,
            )
            assert fsck.returncode <= 2, (
                f"e2fsck could not repair the state disk:\n{fsck.stdout}"
            )

            def debugfs_read(op: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["debugfs", "-R", op, str(state_disk)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

            stat = debugfs_read("stat /msksd/volumes/legacy-marker")
            assert stat.returncode == 0, stat.stderr
            assert re.search(
                rf"User:\s+{service_uid}\s+Group:\s+{service_gid}", stat.stdout
            ), f"legacy marker not service-user-owned:\n{stat.stdout}"
            readback = debugfs_read("cat /msksd/volumes/legacy-marker")
            assert readback.stdout == marker_text, (
                "the legacy marker was recreated, not moved"
            )
            top = debugfs_read("ls -l /")
            assert top.returncode == 0, top.stderr
            # debugfs pads with blank lines; only real rows carry a name.
            names = {
                line.split()[-1] for line in top.stdout.splitlines() if line.split()
            }
            assert "msksd" in names, f"no service-user home on the disk:\n{top.stdout}"
            assert "volumes" not in names and "msks-cert.host" not in names, (
                f"daemon entries still at the state-disk top level:\n{top.stdout}"
            )
    legacy_dir.cleanup()


# --- egress smoke (#52) ----------------------------------------------------
#
# Boots a workspace with egress on this host: real tap + nftables +
# DHCP + DNS forwarder, then proves the guest took its address over
# DHCP, resolves through the daemon's resolver, and reaches the
# outside over the NAT'd uplink. Opt-in: it needs root (tap/nft/ports
# 67+53), /dev/kvm, the built guest image (with the #52 DHCP overlay),
# and an egress-capable default route. Root is the TEST's constraint,
# not the server's (#101): ambient capabilities cannot be handed to
# an arbitrary shell, so the harness runs as full root — and owns
# ip_forward itself, since the daemon only verifies it.

EGRESS = os.environ.get("MSKSD_TEST_EGRESS")


def _default_route_iface() -> str:
    """The uplink NAT hides guests behind (the default route's dev)."""
    route = subprocess.run(
        ["ip", "route", "show", "default"], capture_output=True, text=True
    ).stdout
    parts = route.split()
    for i, part in enumerate(parts):
        if part == "dev":
            return parts[i + 1]
    raise AssertionError(f"no default route to NAT behind: {route!r}")


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


@needs_local
async def test_local_console_identity_drop() -> None:
    """A shell as the image's workspace user (#63): the helper drops
    from root to uid 1000, creates the home on the persistent volume,
    and execs a login shell whose identity the command output proves
    (id -u is guest-computed, so the marker cannot come from the
    echo). Root sessions keep working alongside it."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    try:
        await microvm.launch(
            VmSpec(
                workspace_id=wid,
                kernel=Path(VMLINUX),
                rootfs=Path(ROOTFS),
                initrd=Path(INITRD) if INITRD else None,
                cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
                root_mib=2048,
                home_mib=256,
                egress=False,
            )
        )
        await await_guest_up(serial_log)
        # Seed the workspace user's dotfiles from skel as root: the
        # helper creates a bare home, and bash without rc files
        # prints no recognizable prompt.
        await run_in_console(
            microvm, wid, "cp -r /etc/skel/. /home/msks/ && echo S-$((6*7))", "S-42"
        )
        await run_in_console(
            microvm, wid, "chown -R msks:msks /home/msks && echo O-$((6*7))", "O-42"
        )
        # The real drop: uid 1000, the persistent home, and root
        # alongside.
        await run_in_console(microvm, wid, "echo I-$(id -u)", "I-1000", user="msks")
        await run_in_console(microvm, wid, "echo H-$(pwd)", "H-/home/msks", user="msks")
        await run_in_console(microvm, wid, "echo R-$(id -u)", "R-0")
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_egress
@needs_local
async def test_local_egress_boot() -> None:
    """DHCP address, daemon resolver, NAT'd TCP — end to end (#52)."""
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=_default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(db_path=state_dir / "smoke.db"),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    # The daemon's lifespan migrates and the API creates the row
    # before any launch; this smoke drives the driver directly, so it
    # performs the same setup (claim_slice records the pool slice on
    # the workspace row, #70 review).
    app.state.model.migrate()
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        egress=True,
    )
    # The daemon verifies, never writes, ip_forward (#101 — the
    # appliance ships it as a sysctl); the root harness owns the dev
    # host's setting for the run and restores what it found.
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")
    try:
        await app.state.net.start()
        await app.state.model.create_workspace(spec)
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        # DHCP: the /30's guest address and the tap as the gateway.
        # Every marker is guest-computed ($((6*7)) → 42, gated on the
        # probe's exit status by &&): the pty echoes the sent bytes,
        # so a marker inside the command text would match the echo
        # and pass even when the probe found nothing.
        await run_in_console(
            microvm, wid, "ip -4 addr | grep 172.31 && echo ADDR-$((6*7))", "ADDR-42"
        )
        await run_in_console(
            microvm, wid, "ip route | grep default", "default via 172.31"
        )
        # DNS: through the daemon's forwarder (the offered resolver).
        await run_in_console(
            microvm,
            wid,
            "getent hosts deb.debian.org && echo DNS-$((6*7))",
            "DNS-42",
        )
        # Egress: a TCP connection out through the NAT'd uplink.
        await run_in_console(
            microvm,
            wid,
            "timeout 5 bash -c '</dev/tcp/deb.debian.org/80' && echo TCP-$((6*7))",
            "TCP-42",
        )
        # Containment: the tap's input chain lets DHCP and DNS through
        # and nothing else — the appliance's API (on the tap gateway)
        # must refuse the guest root's connection attempt.
        await run_in_console(
            microvm,
            wid,
            "G=$(ip route | awk '/default/ {print $3}'); "
            'timeout 3 bash -c "</dev/tcp/$G/8660" 2>/dev/null '
            "&& echo API-$((2+2)) || echo API-$((6*7))",
            "API-42",
        )
        await microvm.shutdown(wid, timeout_s=60)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)

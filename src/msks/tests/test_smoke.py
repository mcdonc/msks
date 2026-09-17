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
import pwd
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from httpx import AsyncClient
from msks.app import build_app
from msks.microvm import VmSpec
from msks.net.alloc import table_name, tap_name
from msks.server.api import build_api
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

    The fresh-session-per-attempt shape is also the workaround for
    #103's mid-session console stalls (input echoed, never
    executed): a stalled session times out, and its replacement is
    a new connection — exactly what a human reconnecting does.
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
        os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = (
            "msksd.vsock_wait_timeout_s=120 msksd.console_stall_timeout_s=15"
        )

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
    dev_wid = None
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

        # The workspace console (#21): an authenticated byte stream into
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
        #
        # A probe session that wedges mid-stream now fails loudly
        # (#103): once sent input draws no guest bytes for the stall
        # window (shortened through the cmdline bridge below), the
        # daemon closes the websocket with 4502 and the probe opens
        # a fresh session — the same recovery a human client gets.
        probe_buf = b""
        probe_ws: websockets.ClientConnection | None = None

        async def probe_connect() -> websockets.ClientConnection:
            nonlocal probe_ws
            probe_ws = await websockets.connect(ws_url, ssl=ws_ctx, open_timeout=30)
            return probe_ws

        async def probe_collect(marker: bytes) -> bytes:
            nonlocal probe_buf, probe_ws
            end = loop.time() + 7.0
            while marker not in probe_buf and loop.time() < end:
                try:
                    message = await asyncio.wait_for(probe_ws.recv(), 1.0)
                except TimeoutError:
                    continue
                except websockets.ConnectionClosed as closed:
                    # The named stall close (4502) or any teardown: the
                    # send loop reconnects for a fresh session.
                    print(
                        f"probe session closed ({closed.rcvd}); reconnecting",
                        flush=True,
                    )
                    probe_ws = None
                    return probe_buf
                probe_buf += message if isinstance(message, bytes) else message.encode()
            return probe_buf

        async def probe_send(command: bytes) -> None:
            # A close can land between collect rounds too (not just
            # inside recv): the send must reconnect like the collect
            # does, not raise the close into a test failure.
            nonlocal probe_ws
            if probe_ws is None:
                await probe_connect()
            try:
                await probe_ws.send(command)
            except websockets.ConnectionClosed as closed:
                print(
                    f"probe session closed mid-send ({closed.rcvd}); reconnecting",
                    flush=True,
                )
                probe_ws = None

        async def probe(marker: bytes, command: bytes) -> None:
            # Send first, then collect: the first collect window is
            # not free time to skip.
            end = loop.time() + 180.0
            await probe_send(command)
            while marker not in (await probe_collect(marker)):
                if loop.time() >= end:
                    raise AssertionError(f"console never showed {marker!r}")
                await probe_send(command)
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
        if probe_ws is not None:
            await probe_ws.close()
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

        # The dev-workspace bootstrap seed (#77) in the product shape:
        # a second workspace created with egress and the seed
        # provisions the dev toolchain over its own NIC — the
        # TCP-42-UP probe above proved the forwarded path its
        # downloads ride. The seed's state trail (the running step
        # name, then done) is polled to completion; nested KVM makes
        # the downloads minutes-slow, and the resend loop rides out
        # #103's mid-session console corruption (a fresh send
        # supersedes a corrupted round).
        dev_wid = f"appliance-dev-{uuid.uuid4().hex[:8]}"
        response = await client.post(
            f"{base}/workspaces",
            json={"id": dev_wid, "user_data": dev_workspace_seed()},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        response = await client.post(
            f"{base}/workspaces/{dev_wid}/start", headers=headers
        )
        assert response.status_code in (200, 202), response.text
        deadline = loop.time() + 120.0
        while loop.time() < deadline:
            response = await client.get(f"{base}/workspaces/{dev_wid}", headers=headers)
            if response.json().get("status") == "running":
                break
            await asyncio.sleep(1.0)
        else:
            raise AssertionError(
                f"dev workspace never reached running: {response.text}"
            )
        dev_ws_url = (
            base.replace("https://", "wss://")
            + f"/workspaces/{dev_wid}/console?token={token}"
        )
        dev_cmd = b"cat /root/.msks-bootstrap/state 2>/dev/null; echo E-$?\n"
        async with websockets.connect(
            dev_ws_url, ssl=ws_ctx, open_timeout=30
        ) as dev_ws:
            dev_buf = b""

            async def dev_collect(marker: bytes, window: float) -> None:
                nonlocal dev_buf
                end = loop.time() + window
                while marker not in dev_buf and loop.time() < end:
                    try:
                        message = await asyncio.wait_for(dev_ws.recv(), window)
                    except TimeoutError:
                        return
                    dev_buf += (
                        message if isinstance(message, bytes) else message.encode()
                    )

            # The sent line carries no "done" literal, so the marker
            # can only come from the state file's contents. A gap in
            # the trail (console corruption, #103) heals on resend.
            dev_end = loop.time() + DEV_BOOTSTRAP_TIMEOUT_S
            await dev_ws.send(dev_cmd)
            while b"done" not in dev_buf:
                if loop.time() >= dev_end:
                    raise AssertionError(
                        "seed state never reached done inside the "
                        f"appliance; last console bytes: {dev_buf[-300:]!r}"
                    )
                await dev_collect(b"done", 15.0)
                if b"done" not in dev_buf:
                    await dev_ws.send(dev_cmd)
            # Send-then-collect with resend — the same #103 ride-out
            # as the state loop: a corrupted round is superseded by a
            # fresh send, so the probe measures the venv, not the
            # console's byte fidelity.
            venv_cmd = b"test -x /root/msks/.venv/bin/pytest && echo VENV-$((6*7))\n"
            await dev_ws.send(venv_cmd)
            venv_end = loop.time() + 180.0
            while b"VENV-42" not in dev_buf:
                if loop.time() >= venv_end:
                    raise AssertionError(
                        f"dev venv never materialized; got: {dev_buf[-300:]!r}"
                    )
                await dev_collect(b"VENV-42", 15.0)
                if b"VENV-42" not in dev_buf:
                    await dev_ws.send(venv_cmd)
        response = await client.post(
            f"{base}/workspaces/{dev_wid}/stop", headers=headers, timeout=60.0
        )
        assert response.status_code == 200, response.text
        response = await client.delete(
            f"{base}/workspaces/{dev_wid}", headers=headers, timeout=60.0
        )
        assert response.status_code == 200, response.text

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
            if dev_wid is not None:
                with contextlib.suppress(Exception):
                    await client.post(
                        f"{base}/workspaces/{dev_wid}/stop",
                        headers=headers,
                        timeout=60.0,
                    )
                    await client.delete(
                        f"{base}/workspaces/{dev_wid}",
                        headers=headers,
                        timeout=60.0,
                    )
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


def _uplink_address() -> str:
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


#: The dev-workspace bootstrap (#77) is minutes of downloads on a
#: slow path, not seconds — far past CONSOLE_TIMEOUT_S. The polls
#: below drive their own deadline; this bounds the whole sequence
#: (bootstrap downloads + the in-guest suite).
DEV_BOOTSTRAP_TIMEOUT_S = float(
    os.environ.get("MSKSD_TEST_DEV_BOOTSTRAP_TIMEOUT_S", "3600")
)


def dev_workspace_seed() -> str:
    """The bootstrap payload from the repo's scripts/ tree (#77)."""
    path = Path(__file__).resolve().parents[3] / "scripts" / "dev-workspace.sh"
    return path.read_text()


async def await_dev_state(microvm, workspace_id: str, needle: bytes) -> bytes:
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


@needs_egress
@needs_local
async def test_local_dev_workspace_bootstrap() -> None:
    """The dev-workspace loop end to end (#77): a workspace created
    with egress and the scripts/dev-workspace.sh seed bootstraps the
    toolchain over its own NIC at first boot (uv with its own
    Python, the checkout, uv sync), the result survives stop/start
    without re-provisioning, and the `unit-tests` invocation runs
    to completion inside the guest.
    """
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
    app.state.model.migrate()
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        # 8 GiB covers the in-guest suite (pytest -n auto across the
        # guest's cores); the bootstrap itself is downloads, not
        # builds. The overlay holds the venv and uv's Python.
        root_mib=20480,
        mem_mib=8192,
        egress=True,
        user_data=dev_workspace_seed(),
    )
    # The daemon verifies, never writes, ip_forward (#101 — the
    # appliance ships it as a sysctl); the root harness owns the dev
    # host's setting for the run and restores what it found.
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    async def probe(marker_prefix: str, probe_cmd: str) -> None:
        # Each marker is gated on the probe's exit status so the
        # echoed command text cannot satisfy it (see run_in_console).
        await run_in_console(
            microvm,
            wid,
            f"{probe_cmd} && echo {marker_prefix}-$((6*7))",
            f"{marker_prefix}-42",
        )

    try:
        await app.state.net.start()
        await app.state.model.create_workspace(spec)
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        # First boot: the seed runs in cloud-final; poll its state
        # trail to "done" (each tool's marker gated on its presence).
        await await_dev_state(microvm, wid, b"done")
        await probe("UV", "command -v uv")
        await probe("CLONE", "git -C /root/msks rev-parse --is-inside-work-tree")
        await probe("SYNC", "test -x /root/msks/.venv/bin/pytest")

        # Persistence: stop/start keeps the toolchain and the
        # checkout on the overlay; cloud-init does not re-run the
        # seed (the state trail still says the one first-boot run).
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        serial_log.unlink(missing_ok=True)
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        await await_dev_state(microvm, wid, b"done")
        # Console-readiness after the reboot plus the persistence
        # proof: the venv survives, and the rerun log does not exist
        # — the seed executed exactly once (cloud-init state rode the
        # overlay), which the vacuous state poll alone cannot show.
        await probe("AGAIN", "test -x /root/msks/.venv/bin/pytest")
        await probe("NORERUN", "test ! -e /root/.msks-bootstrap/rerun.log")

        # Idempotence, the direct way: re-execute the seed verbatim
        # off the read-only cidata disk — every step's guard holds,
        # uv sync re-runs as a fast no-op, and the state trail ends
        # at done again with a zero exit status.
        await run_in_console(
            microvm,
            wid,
            "mkdir -p /mnt/cidata && mount -r /dev/vdc /mnt/cidata 2>/dev/null; "
            "sh /mnt/cidata/user-data >/root/.msks-bootstrap/rerun.log 2>&1; "
            "echo R-$?",
            "R-0",
        )
        await await_dev_state(microvm, wid, b"done")

        # The suite, inside the guest, the way the `unit-tests` task
        # runs it (the task's exec line, from the venv uv built) —
        # note this is the coverage-gated CI invocation itself
        # (addopts), so a future coverage edge on main reddens this
        # smoke for a reason unrelated to the bootstrap: recognizable,
        # not a bootstrap bug. Launched in the background, then the
        # rc trail polled to done-0. The launch is guarded — a
        # retried round (#103 corruption ate the BG marker while the
        # input still executed) finds rc at running-or-done and
        # reuses the live/finished run instead of relaunching pytest
        # beside it.
        await run_in_console(
            microvm,
            wid,
            "if grep -qE '^(done-|running)' "
            "/root/.msks-bootstrap/unit-tests.rc 2>/dev/null; "
            "then echo BG-$((6*7)); "
            "else echo running >/root/.msks-bootstrap/unit-tests.rc; "
            "nohup sh -c 'cd /root/msks && uv run python -m pytest "
            "src/msks/tests -v -n auto "
            ">/root/.msks-bootstrap/unit-tests.log 2>&1; "
            "echo done-$? >/root/.msks-bootstrap/unit-tests.rc' "
            ">/dev/null 2>&1 & echo BG-$((6*7)); fi",
            "BG-42",
        )
        await await_dev_state(microvm, wid, b"done-0")
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)


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


@needs_egress
@needs_local
@needs_ssh_tools
async def test_local_sshd_and_rsync() -> None:
    """sshd answers through the forward, keys persist, rsync syncs
    (#110, end to end over #109's transport).

    The guest's sshd is the image's own (enabled by Debian, pinned by
    the msks dropins): a console-planted key logs in through
    ``msks forward --local``, the host key ssh recorded on the first
    login still matches after a stop/start cycle (the key lives on
    the persistent root overlay, so the reconnect must not see a
    changed key), and ``rsync -e ssh`` lands a directory in the
    guest. A workspace without egress keeps its vsock console
    untouched by all of this — the no-NIC smokes above run that
    posture on the same image.
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    token = f"smoke-token-{uuid.uuid4().hex}"
    api_port = free_port()
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=_default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(
            host="127.0.0.1",
            port=api_port,
            db_path=state_dir / "smoke.db",
            bootstrap_token=token,
        ),
    )
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
        egress=True,
    )
    workdir = state_dir / "ssh-work"
    workdir.mkdir(parents=True)
    key = workdir / "id_ecdsa"
    known_hosts = workdir / "known_hosts"
    sync_marker = f"SYNCED-{uuid.uuid4().hex[:8]}"

    # The daemon verifies, never writes, ip_forward (#101); the root
    # harness owns the host's setting for the run and restores it.
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    api_server = None
    api_task = None
    forwards: list[subprocess.Popen] = []

    forward_logs: list[Path] = []

    def start_forward(port: int) -> None:
        """One ``msks forward --local`` client against the test API,
        its stderr kept in a file — a failed guest dial is exactly
        the evidence a hung login needs."""
        env = dict(
            os.environ,
            MSKSC_URL=f"http://127.0.0.1:{api_port}",
            MSKSC_TOKEN=token,
        )
        log = workdir / f"forward-{len(forward_logs)}.log"
        forward_logs.append(log)
        forwards.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "msks.client.cli",
                    "forward",
                    wid,
                    "22",
                    "--local",
                    str(port),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=open(log, "ab"),
            )
        )

    def forward_evidence() -> str:
        """The forward clients' collected stderr, for failure messages."""
        return "\n".join(
            f"--- {log.name} ---\n{log.read_text(errors='replace')[-800:]}"
            for log in forward_logs
            if log.exists()
        )

    async def await_forward_listener(port: int, timeout_s: float = 30.0) -> None:
        """Until the forward client says its loopback listener is up.

        The client prints its bind line after ``start_server``
        returns — no guest contact, so no sshd per-source penalty for
        an unauthenticated connection the login would then pay for.
        The login itself proves the whole chain (client websocket,
        daemon dial, guest sshd)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        needle = f"msks: 127.0.0.1:{port} -> "
        while loop.time() < deadline:
            log = forward_logs[-1]
            if log.exists() and needle in log.read_text(errors="replace"):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"msks forward never listened on 127.0.0.1:{port} within "
            f"{timeout_s}s; forward logs:\n{forward_evidence()}"
        )

    def ssh_opts(port: int) -> list[str]:
        """The client flags every invocation shares (#110's posture:
        this key only, this known_hosts only, no prompting)."""
        return [
            # -F /dev/null: hermetic — the host machine's ssh_config can
            # carry options this ssh build rejects (CI's runner config
            # ships GSSAPIAuthentication; nixpkgs builds without GSSAPI).
            "-F",
            os.devnull,
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            # accept-new: the first login records the host key, a
            # later login that presents a different one fails — the
            # persistence criterion with its teeth kept.
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "BatchMode=yes",
            "-p",
            str(port),
        ]

    async def run_ssh(port: int, command: str) -> subprocess.CompletedProcess:
        # to_thread, never a bare subprocess.run: the API server rides
        # this test's event loop, and a blocking call froze that loop
        # through the ssh attempt's whole ConnectTimeout — the daemon
        # could not finish the forward client's websocket handshake,
        # and the login died as a handshake timeout (the CI failure
        # this harness lesson comes from).
        result = await asyncio.to_thread(
            subprocess.run,
            [SSH_BIN, *ssh_opts(port), "root@127.0.0.1", command],
            capture_output=True,
            text=True,
            timeout=SSH_CMD_TIMEOUT_S,
        )
        if result.returncode != 0:
            # The failure rerun at full verbosity: a bare rc says
            # nothing (the first CI failure carried an EMPTY stderr),
            # while DEBUG3 names the phase that died.
            verbose = await asyncio.to_thread(
                subprocess.run,
                [
                    SSH_BIN,
                    *ssh_opts(port),
                    "-o",
                    "LogLevel=DEBUG3",
                    "root@127.0.0.1",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=SSH_CMD_TIMEOUT_S,
            )
            result.stderr += (
                f"\n--- verbose rerun (rc={verbose.returncode}) ---\n"
                f"{verbose.stderr[-3000:]}"
            )
        return result

    async def boot_and_wait_sshd() -> None:
        await microvm.launch(spec)
        await app.state.model.set_status(wid, "running")
        await await_guest_up(serial_log)
        # sshd listens once its interface has the address (#110's
        # ordering). Each unit is probed by itself — multi-unit
        # is-active is ANY-active semantics, under which an absent
        # wait unit hides behind ssh being up. The guest-side loop
        # rides out the unit's own DHCP wait (single-unit is-active
        # answers "activating" while it polls).
        await run_in_console(
            microvm,
            wid,
            "i=0; while [ $i -lt 30 ] "
            "&& ! { systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1; }; "
            "do sleep 1; i=$((i+1)); done; "
            "systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1 && echo U-$((6*7))",
            "U-42",
        )

    async def assert_sshd_posture() -> None:
        """The image's login contract, read from the running sshd:
        key-only, root by key only. The probe prints the effective
        values into the session before gating on them — a drift
        fails with the values it found, not a bare missing marker.
        sshd -T spells root-by-key-only "without-password" (the
        pre-7.x alias); "prohibit-password" is the same setting."""
        await run_in_console(
            microvm,
            wid,
            "sshd -T > /root/sshd-T 2>&1; "
            "grep -E '^(passwordauthentication|kbdinteractiveauthentication"
            "|permitrootlogin) ' /root/sshd-T; "
            "test \"$(awk '/^passwordauthentication/{print $2}' /root/sshd-T)\" = no "
            "&& test \"$(awk '/^kbdinteractiveauthentication/{print $2}' "
            '/root/sshd-T)" = no '
            "&& case \"$(awk '/^permitrootlogin/{print $2}' /root/sshd-T)\" "
            "in prohibit-password|without-password) true;; *) false;; esac "
            "&& echo P-$((6*7))",
            "P-42",
        )

    try:
        # The API server's lifespan owns migrate/token/net start; it
        # must be up before the forward client dials it.
        api_server = uvicorn.Server(
            uvicorn.Config(
                build_api(app),
                host="127.0.0.1",
                port=api_port,
                log_level="warning",
            )
        )
        api_task = asyncio.create_task(api_server.serve())
        deadline = asyncio.get_running_loop().time() + 30
        while not api_server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test API server never started (30s)")
            await asyncio.sleep(0.05)

        await app.state.model.create_workspace(spec)
        # ECDSA P-256 — FIPS-approvable from day one (#115); nothing
        # in the image or daemon depends on the key type, and a future
        # default change is this line plus #111's setting.
        keygen = await asyncio.to_thread(
            subprocess.run,
            [
                SSH_KEYGEN_BIN,
                "-t",
                "ecdsa",
                "-b",
                "256",
                "-N",
                "",
                "-C",
                "msks-smoke",
                "-f",
                str(key),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert keygen.returncode == 0, keygen.stderr

        # First boot: plant the login key through the console (the
        # identity path #111 automates; here the operator does it by
        # hand) and note the host key before any client records it.
        await boot_and_wait_sshd()
        public = (workdir / "id_ecdsa.pub").read_text().strip()
        await run_in_console(
            microvm,
            wid,
            f"mkdir -p /root/.ssh && chmod 700 /root/.ssh "
            f"&& printf '%s\\n' '{public}' > /root/.ssh/authorized_keys "
            f"&& chmod 600 /root/.ssh/authorized_keys && echo K-$((6*7))",
            "K-42",
        )
        # The guest names its host key on its own disk: the file rides
        # the overlay, so the second boot compares against the first
        # boot's fingerprint without the test ferrying bytes.
        await run_in_console(
            microvm,
            wid,
            "ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub > /root/host-fp "
            "&& echo N-$((6*7))",
            "N-42",
        )

        # Login through the forward (#109 transport, #110 listener):
        # accept-new records the host key on this first login and
        # demands the same one ever after.
        forward_port = free_port()
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        login = await run_ssh(forward_port, "echo SSH-OK-$((6*7))")
        assert login.returncode == 0, (
            f"{login.stdout}\n{login.stderr}\nforward logs:\n{forward_evidence()}"
        )
        assert "SSH-OK-42" in login.stdout, login.stdout

        # rsync over the same forward: a directory lands whole.
        source = workdir / "src"
        source.mkdir()
        (source / "sentinel.txt").write_text(f"{sync_marker}\n", encoding="utf-8")
        # to_thread for the same reason as run_ssh: the API server
        # shares this loop.
        sync = await asyncio.to_thread(
            subprocess.run,
            [
                RSYNC_BIN,
                "-e",
                " ".join([SSH_BIN, *ssh_opts(forward_port)]),
                "-a",
                f"{source}/",
                "root@127.0.0.1:/root/synced/",
            ],
            capture_output=True,
            text=True,
            timeout=SSH_CMD_TIMEOUT_S,
        )
        assert sync.returncode == 0, f"{sync.stdout}\n{sync.stderr}"
        await run_in_console(microvm, wid, "cat /root/synced/sentinel.txt", sync_marker)
        await assert_sshd_posture()

        # Stop/start: the overlay keeps the host key (its sshd-keygen
        # wrote it there on first boot), so the recorded known_hosts
        # entry still matches — and the guest names the same
        # fingerprint it had before.
        for proc in forwards:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(proc.wait, 10)
        forwards.clear()
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        serial_log.unlink(missing_ok=True)
        await boot_and_wait_sshd()
        await run_in_console(
            microvm,
            wid,
            'test "$(cat /root/host-fp)" '
            '= "$(ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub)" '
            "&& echo SAME-$((6*7))",
            "SAME-42",
        )
        # The SAME local port: the recorded known_hosts entry is
        # per [host]:port, so the reconnect meets the first boot's key.
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        relogin = await run_ssh(forward_port, "echo AGAIN-$((6*7))")
        assert relogin.returncode == 0, (
            f"{relogin.stdout}\n{relogin.stderr}\nforward logs:\n{forward_evidence()}"
        )
        assert "AGAIN-42" in relogin.stdout, relogin.stdout

        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        # to_thread even for the SIGTERM waits: the API server shares
        # this loop (the run_ssh lesson applies to any blocking call).
        for proc in forwards:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 10)
        if api_task is not None:
            api_server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)


async def await_guest_trail(
    microvm, workspace_id: str, probe: str, needle: bytes, timeout_s: float
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


@needs_egress
@needs_local
@needs_ssh_tools
@needs_git_tools
async def test_local_egress_git_out() -> None:
    """git-out through egress with a forwarded agent (#81).

    The dogfood loop's outbound half, end to end over the real
    paths: a workspace with egress reaches destinations the seed
    never needs through the NAT'd uplink (Debian's mirrors via
    apt, an HTTPS fetch of an unrelated host), and the credential
    the push authenticates with rides the forward as the
    operator's forwarded agent — nothing about it is baked into
    the image or the seed. Wide open, per #81's charter: every
    probe below runs with no grant or consent anywhere; #69 is the
    later narrowing of guest-initiated egress, not this loop.

    Legs, in order: the DHCP lease's resolver is the daemon's own
    (the per-link DNS the lease hands out sits inside the /30 pool
    — no public resolver); apt installs git from Debian's mirrors
    (no recommends) and an HTTPS `git ls-remote` reaches the
    project's public remote — both through the NAT'd egress path
    an off-host git remote rides; then the guest
    commits and pushes to a bare repo behind a scratch sshd on the
    host, authenticating only with the agent key that arrived
    through ``msks forward --local`` — the alias workflow's ``-A``
    path (#112) — never a key on the guest's disk. That push leg
    crosses a test-widened input pin: the daemon's own posture
    drops guest traffic aimed at the appliance by design (#52 —
    test_local_egress_boot pins the drop), and a hermetic runner
    has no off-host remote to receive the push, so the test pins
    exactly one widening (this workspace's git port) into its own
    ingress chain and removes it after.
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    uplink_iface = _default_route_iface()
    uplink_ip = _uplink_address()
    # The listen address and the NAT'd uplink iface must agree:
    # _uplink_address() picks the default route's source, which on
    # a multihomed host can diverge from the iface settings name —
    # fail naming both instead of binding a destination the daemon
    # never NATs for.
    iface = subprocess.run(
        [ip_tool, "-4", "addr", "show", "dev", uplink_iface],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert iface.returncode == 0, iface.stderr
    assert re.search(rf"inet {re.escape(uplink_ip)}[/ ]", iface.stdout), (
        f"{uplink_ip!r} is not an address of the uplink iface "
        f"{uplink_iface!r}: {iface.stdout}"
        " (the inet/<prefixlen> anchor matters: a bare substring "
        "match lets 10.1.0.19 pass against 10.1.0.198)"
    )
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    token = f"smoke-token-{uuid.uuid4().hex}"
    api_port = free_port()
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=uplink_iface,
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(
            host="127.0.0.1",
            port=api_port,
            db_path=state_dir / "smoke.db",
            bootstrap_token=token,
        ),
    )
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
        egress=True,
    )
    workdir = state_dir / "gitout"
    workdir.mkdir(parents=True)
    login_key = workdir / "login_key"  # console-planted; logs in through the forward
    agent_key = workdir / "agent_key"  # the git-out credential: host agent only
    git_host_key = workdir / "git_host_key"  # the scratch sshd's host key
    known_hosts = workdir / "known_hosts"
    authorized = workdir / "authorized_keys"
    sshd_config = workdir / "git-sshd.conf"
    gitd_log = workdir / "git-sshd.log"
    bare = workdir / "bare.git"
    push_marker = f"PUSHED-{uuid.uuid4().hex[:8]}"
    git_port = free_port()

    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    api_server = None
    api_task = None
    forwards: list[subprocess.Popen] = []
    forward_logs: list[Path] = []
    agent_env: dict[str, str] | None = None
    agent_pid: int | None = None
    gitd: subprocess.Popen | None = None

    def start_forward(port: int) -> None:
        """One ``msks forward --local`` client against the test API
        (the #109 transport the whole smoke rides)."""
        env = dict(
            os.environ,
            MSKSC_URL=f"http://127.0.0.1:{api_port}",
            MSKSC_TOKEN=token,
        )
        log = workdir / f"forward-{len(forward_logs)}.log"
        forward_logs.append(log)
        forwards.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "msks.client.cli",
                    "forward",
                    wid,
                    "22",
                    "--local",
                    str(port),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=open(log, "ab"),
            )
        )

    def forward_evidence() -> str:
        """The forward clients' collected stderr, for failure messages."""
        return "\n".join(
            f"--- {log.name} ---\n{log.read_text(errors='replace')[-800:]}"
            for log in forward_logs
            if log.exists()
        )

    async def await_forward_listener(port: int, timeout_s: float = 30.0) -> None:
        """Until the forward client says its loopback listener is up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        needle = f"msks: 127.0.0.1:{port} -> "
        while loop.time() < deadline:
            log = forward_logs[-1]
            if log.exists() and needle in log.read_text(errors="replace"):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"msks forward never listened on 127.0.0.1:{port} within "
            f"{timeout_s}s; forward logs:\n{forward_evidence()}"
        )

    def ssh_opts(port: int) -> list[str]:
        """The client flags every host-side login shares (hermetic,
        this key only, no prompting)."""
        return [
            "-F",
            os.devnull,
            "-i",
            str(login_key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "BatchMode=yes",
            "-p",
            str(port),
        ]

    async def wait_sshd() -> None:
        """Until the guest's address and ssh services are up."""
        await await_guest_up(serial_log)
        await run_in_console(
            microvm,
            wid,
            "i=0; while [ $i -lt 30 ] "
            "&& ! { systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1; }; "
            "do sleep 1; i=$((i+1)); done; "
            "systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1 && echo U-$((6*7))",
            "U-42",
        )

    async def widen_input(port: int) -> None:
        """One accept at the top of this workspace's ingress chain.

        The daemon's chain drops host-directed guest traffic by
        design (#52); the push leg pins a single widening — this
        workspace's tap, the git port — inserted FIRST in the chain
        so it wins ahead of the drop. narrow_input removes it; a
        leak dies with the table at teardown regardless.
        """
        rule = await asyncio.to_thread(
            subprocess.run,
            [
                nft_tool,
                "insert",
                "rule",
                "inet",
                table_name(wid),
                "ingress",
                "iifname",
                tap_name(wid),
                "tcp",
                "dport",
                str(port),
                "accept",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert rule.returncode == 0, rule.stderr

    async def narrow_input() -> None:
        """Drop every handle this test pinned into the chain."""
        listing = await asyncio.to_thread(
            subprocess.run,
            [nft_tool, "-a", "list", "chain", "inet", table_name(wid), "ingress"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if listing.returncode != 0:
            return  # the table is gone; teardown already won
        for line in listing.stdout.splitlines():
            if f"tcp dport {git_port} accept" not in line:
                continue
            handle = re.search(r"handle (\d+)", line)
            if handle:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        nft_tool,
                        "delete",
                        "rule",
                        "inet",
                        table_name(wid),
                        "ingress",
                        "handle",
                        handle.group(1),
                    ],
                    capture_output=True,
                    timeout=30,
                )

    async def await_gitd(timeout_s: float = 15.0) -> None:
        """Until the scratch sshd accepts a TCP connection."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.open_connection(uplink_ip, git_port)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.2)
        raise AssertionError(
            f"the scratch sshd never listened on {uplink_ip}:{git_port}; "
            f"its log:\n{gitd_log.read_text(errors='replace')[-800:]}"
        )

    try:
        api_server = uvicorn.Server(
            uvicorn.Config(
                build_api(app),
                host="127.0.0.1",
                port=api_port,
                log_level="warning",
            )
        )
        api_task = asyncio.create_task(api_server.serve())
        deadline = asyncio.get_running_loop().time() + 30
        while not api_server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test API server never started (30s)")
            await asyncio.sleep(0.05)

        await app.state.model.create_workspace(spec)

        # Launch first: the boot runs while the host side builds its
        # scratch pieces (the whole prep is seconds of subprocess
        # time, but on nested KVM every second of serial boot wall
        # counts).
        await microvm.launch(spec)
        await app.state.model.set_status(wid, "running")

        # The login key (this smoke's stand-in for the operator's
        # alias identity) and the credential the agent carries.
        for path, comment in (
            (login_key, "msks-smoke-login"),
            (agent_key, "msks-git-cred"),
        ):
            keygen = await asyncio.to_thread(
                subprocess.run,
                [
                    SSH_KEYGEN_BIN,
                    "-t",
                    "ecdsa",
                    "-b",
                    "256",
                    "-N",
                    "",
                    "-C",
                    comment,
                    "-f",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert keygen.returncode == 0, keygen.stderr

        # The push target: a bare repo behind a scratch sshd that
        # authorizes only the agent key. StrictModes off — the workdir
        # is a fresh tmp tree, not a home. SetEnv PATH: sshd's default
        # PATH has no nix store entries, and git-receive-pack must
        # resolve for the push's ssh to find it.
        init = await asyncio.to_thread(
            subprocess.run,
            [GIT_BIN, "init", "--bare", "-b", "main", str(bare)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert init.returncode == 0, init.stderr
        authorized.write_text((agent_key.with_suffix(".pub")).read_text())
        hostgen = await asyncio.to_thread(
            subprocess.run,
            [
                SSH_KEYGEN_BIN,
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "msks-smoke-githost",
                "-f",
                str(git_host_key),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert hostgen.returncode == 0, hostgen.stderr
        sshd_config.write_text(
            f"Port {git_port}\n"
            f"ListenAddress {uplink_ip}\n"
            f"HostKey {git_host_key}\n"
            "PermitRootLogin prohibit-password\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            f"AuthorizedKeysFile {authorized}\n"
            "StrictModes no\n"
            f"PidFile {workdir / 'git-sshd.pid'}\n"
            f"SetEnv PATH={Path(GIT_BIN).parent}:{Path(SSH_BIN).parent}"
            ":/usr/sbin:/usr/bin:/sbin:/bin\n"
        )
        # OpenSSH's privilege-separation directory: sshd refuses to
        # start without it. The compiled-in path differs by build —
        # Debian's is /run/sshd, nix openssh's is /var/empty (the
        # dev host ships both; the CI runner ships neither) — so
        # every candidate gets created, root-owned and 0755 (the
        # perms sshd demands).
        for privsep in ("/run/sshd", "/var/empty", "/var/empty/sshd"):
            os.makedirs(privsep, exist_ok=True)
            os.chmod(privsep, 0o755)
        # The privilege-separation USER is the same story: a
        # compile-time name (nix's is "sshd") the distro's packaging
        # normally creates. Best-effort — 9.8+ builds tolerate its
        # absence in some shapes, and a missing binary must not
        # mask the real failure — but where useradd exists and the
        # user does not, create it rather than discover the
        # hard-coded name one CI round at a time.
        try:
            pwd.getpwnam("sshd")
        except KeyError:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        "useradd",
                        "--system",
                        "--no-create-home",
                        "--shell",
                        "/usr/sbin/nologin",
                        "sshd",
                    ],
                    timeout=30,
                )
        gitd = subprocess.Popen(
            [SSHD_BIN, "-D", "-e", "-f", str(sshd_config)],
            stdout=subprocess.DEVNULL,
            stderr=open(gitd_log, "ab"),
        )
        await await_gitd()

        # The operator's scratch agent: holds the credential, runs on
        # the host, and only ever enters the guest as a forwarded
        # socket. ssh-agent forks; its stdout names the socket + pid.
        # Both are captured before any assertion so a failed parse
        # still cleans the fork up (finally keys off the pid).
        boot = await asyncio.to_thread(
            subprocess.run,
            [SSH_AGENT_BIN, "-s"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert boot.returncode == 0, boot.stderr
        sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", boot.stdout)
        pidm = re.search(r"SSH_AGENT_PID=(\d+);", boot.stdout) or re.search(
            r"Agent pid (\d+)", boot.stdout
        )
        agent_pid = int(pidm.group(1)) if pidm else None
        assert sock and pidm, boot.stdout
        agent_env = dict(os.environ, SSH_AUTH_SOCK=sock.group(1))
        add = await asyncio.to_thread(
            subprocess.run,
            [SSH_ADD_BIN, str(agent_key)],
            env=agent_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert add.returncode == 0, add.stderr

        # Guest up; plant the login key through the console.
        await wait_sshd()
        public = login_key.with_suffix(".pub").read_text().strip()
        await run_in_console(
            microvm,
            wid,
            f"mkdir -p /root/.ssh && chmod 700 /root/.ssh "
            f"&& printf '%s\\n' '{public}' > /root/.ssh/authorized_keys "
            f"&& chmod 600 /root/.ssh/authorized_keys && echo K-$((6*7))",
            "K-42",
        )

        # The DHCP lease's resolver is the daemon's forwarder: the
        # /30 pool (default 172.31.0.0/16), never a public resolver.
        # The image runs systemd-resolved, so the offered server is
        # resolved's per-link upstream (resolvectl) while the stub
        # owns resolv.conf — the cat fallback covers a resolver-less
        # image writing the lease straight to resolv.conf.
        await run_in_console(
            microvm,
            wid,
            "( resolvectl dns 2>/dev/null || cat /etc/resolv.conf ) "
            "| grep -q '172\\.31\\.' && echo R-$((6*7))",
            "R-42",
        )

        # Substitutes in, over egress, destinations the seed never
        # touches: Debian's mirrors, then an HTTPS ``git ls-remote``
        # of the project's own public remote — the host a real
        # dogfood push targets.
        #
        # The setup (mkdir, rm, the trail's first line) runs in the
        # FOREGROUND, gated on the BG marker: the marker proves the
        # trail file exists and is writable before anything detaches.
        # The long legs run as one ``nohup setsid sh -c`` — and the
        # job is DISOWNED before the marker is echoed. The CI failure
        # this shape replaces: on the session close the login bash
        # resends SIGHUP to everything in its jobs table, and on slow
        # nested KVM the background child's exec chain (nohup, then
        # setsid, then sh — three cold binaries) is still mid-flight
        # with a default HUP disposition, so it died before writing a
        # byte (run.log was never even created; only the disowned
        # table survives that resend deterministically — the marker
        # reaches the client strictly after the disown). Post-exec,
        # nohup (HUP ignored) and setsid (fresh session, no ctty)
        # carry the rest; stdin comes off the pty and every output
        # byte — including the inner sh's own parse errors — lands
        # in run.log, which the trail probe tails. The lockdir guard
        # (atomic mkdir) plus the guarded rm make a #103 retry of
        # this session harmless: a second detached instance exits
        # silently at the lock and the foreground leaves the running
        # instance's trail alone — without it, two apt-gets would
        # fight over the dpkg lock and the loser would write a
        # bogus fail marker. --no-install-recommends keeps the
        # download to what the legs use (git-man alone is tens of
        # MB of recommends the proof gains nothing from).
        await run_in_console(
            microvm,
            wid,
            "mkdir -p /root/.gitout "
            "&& { [ ! -d /root/.gitout/lock ] "
            "&& rm -f /root/.gitout/trail /root/.gitout/run.log || true; } "
            "&& echo start >>/root/.gitout/trail "
            "&& { nohup setsid sh -c '"
            "mkdir /root/.gitout/lock 2>/dev/null || exit 0; "
            "echo apt >>/root/.gitout/trail; "
            "apt-get update -qq >>/root/.gitout/run.log 2>&1 "
            "|| { echo fail-apt-update >>/root/.gitout/trail; exit 1; }; "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "--no-install-recommends git openssh-client "
            ">>/root/.gitout/run.log 2>&1 "
            "|| { echo fail-apt-install >>/root/.gitout/trail; exit 1; }; "
            "echo ls-remote >>/root/.gitout/trail; "
            "git ls-remote https://github.com/mcdonc/msks HEAD "
            ">/root/.gitout/remote 2>>/root/.gitout/run.log "
            "|| { echo fail-ls-remote >>/root/.gitout/trail; exit 1; }; "
            "echo done >>/root/.gitout/trail"
            "' >>/root/.gitout/run.log 2>&1 </dev/null & } "
            "&& disown && echo BG-$((6*7))",
            "BG-42",
        )
        trail_probe = (
            "cat /root/.gitout/trail 2>/dev/null; "
            "tail -c 400 /root/.gitout/run.log 2>/dev/null"
        )
        # A short grace first: if the detached script died instantly,
        # fail within 90s naming run.log's tail, not after the whole
        # apt budget. "apt" in the trail is the detached script's
        # first act.
        await await_guest_trail(
            microvm, wid, trail_probe, b"apt", min(90.0, GIT_OUT_TIMEOUT_S)
        )
        await await_guest_trail(microvm, wid, trail_probe, b"done", GIT_OUT_TIMEOUT_S)
        await run_in_console(
            microvm,
            wid,
            "test -s /root/.gitout/remote && echo Z-$((6*7))",
            "Z-42",
        )

        # The commit the guest pushes: made inside, identity local
        # to the guest, content the landing assertion knows.
        await run_in_console(
            microvm,
            wid,
            "git config --global user.email dev@msks.invalid "
            "&& git config --global user.name msks-dev "
            "&& git init -q -b main /root/push-src "
            f"&& printf '%s\\n' '{push_marker}' > /root/push-src/pushed.txt "
            "&& git -C /root/push-src add pushed.txt "
            "&& git -C /root/push-src commit -qm 'git-out probe' "
            "&& echo C-$((6*7))",
            "C-42",
        )

        # git-out: log in through the forward with -A (the agent
        # rides in), then push from inside the guest to the scratch
        # sshd — guest-initiated TCP from the tap, ssh auth with the
        # forwarded agent only: no IdentityFile anywhere, and
        # agent_key never touched the guest's disk. The daemon's
        # own input chain drops host-directed guest traffic by
        # design (#52's containment — test_local_egress_boot pins
        # it), so this leg rides a test-widened pin: one accept for
        # the git port, inserted at the top of this workspace's
        # ingress chain and removed after. The NAT egress path the
        # dogfood loop really rides (pushes to off-host remotes)
        # is proven by this test's apt and HTTPS legs — a hermetic
        # runner has no off-host remote to receive a push.
        await widen_input(git_port)
        forward_port = free_port()
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        remote = (
            "export GIT_SSH_COMMAND="
            f'"ssh -F /dev/null -o StrictHostKeyChecking=no '
            f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 "
            f'-p {git_port}"; '
            "ssh-add -l > /root/.gitout/agent-list 2>&1; "
            f"git -C /root/push-src push -q "
            f"ssh://root@{uplink_ip}:{git_port}{bare} main "
            "&& echo P-$((6*7))"
        )
        try:
            push = await asyncio.to_thread(
                subprocess.run,
                [
                    SSH_BIN,
                    *ssh_opts(forward_port),
                    "-o",
                    "ForwardAgent=yes",
                    "root@127.0.0.1",
                    remote,
                ],
                env=agent_env,
                capture_output=True,
                text=True,
                timeout=SSH_CMD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"the push session timed out after {SSH_CMD_TIMEOUT_S}s "
                "(a dropped or unroutable destination black-holes "
                "exactly like this); git sshd log:\n"
                f"{gitd_log.read_text(errors='replace')[-800:]}\n"
                f"forward logs:\n{forward_evidence()}"
            ) from exc
        if push.returncode != 0:
            # The failure rerun at full verbosity, off the loop: a
            # bare rc says nothing about which leg died (console,
            # forward, agent, guest-side push). The rerun asks only
            # for the agent listing — re-running the push itself
            # could report "up-to-date" and mask a transport flake —
            # and a rerun that itself times out degrades to the
            # original failure instead of replacing it.
            try:
                verbose = await asyncio.to_thread(
                    subprocess.run,
                    [
                        SSH_BIN,
                        *ssh_opts(forward_port),
                        "-o",
                        "ForwardAgent=yes",
                        "-o",
                        "LogLevel=DEBUG3",
                        "root@127.0.0.1",
                        "ssh-add -l",
                    ],
                    env=agent_env,
                    capture_output=True,
                    text=True,
                    timeout=SSH_CMD_TIMEOUT_S,
                )
                push.stderr += (
                    f"\n--- verbose rerun (rc={verbose.returncode}) ---\n"
                    f"{verbose.stderr[-3000:]}"
                )
            except subprocess.TimeoutExpired:
                push.stderr += "\n--- verbose rerun timed out ---\n"
        assert push.returncode == 0, (
            f"{push.stdout}\n{push.stderr}\nforward logs:\n{forward_evidence()}\n"
            f"git sshd log:\n{gitd_log.read_text(errors='replace')[-800:]}"
        )
        assert "P-42" in push.stdout, push.stdout
        # The forwarded agent carried the scratch key: the guest's
        # ssh-add lists it (comment and all).
        await run_in_console(
            microvm,
            wid,
            "grep -q msks-git-cred /root/.gitout/agent-list && echo A-$((6*7))",
            "A-42",
        )

        # The landing: the bare repo's HEAD is the guest's commit,
        # content and all.
        landed = await asyncio.to_thread(
            subprocess.run,
            [GIT_BIN, "-C", str(bare), "show", "HEAD:pushed.txt"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert landed.returncode == 0, landed.stderr
        assert landed.stdout.strip() == push_marker, landed.stdout

        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        for proc in forwards:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 10)
        if gitd is not None:
            with contextlib.suppress(Exception):
                gitd.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(gitd.wait, 10)
        # The input pin goes before the agent and API teardown so a
        # slow guest cannot hold a widened chain past the workspace's
        # own cleanup; a failure here leaves the rule to die with the
        # per-VM table at microvm cleanup.
        with contextlib.suppress(Exception):
            await narrow_input()
        if agent_env is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    subprocess.run,
                    [SSH_AGENT_BIN, "-k"],
                    env=agent_env,
                    timeout=15,
                )
        elif agent_pid is not None:
            # A parse that failed after the fork still gets cleaned.
            with contextlib.suppress(Exception):
                os.kill(agent_pid, signal.SIGTERM)
        if api_task is not None:
            api_server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_egress
@needs_local
@needs_ssh_tools
async def test_local_minted_identity() -> None:
    """The minted identity end to end (#111): create mints and seeds,
    the key fetch serves the halves, and a fresh egress workspace
    accepts ssh as root and as the msks workspace user with no manual
    key steps anywhere.

    The whole create path runs through the real API (POST /workspaces
    → mint → seed build at prepare → row), the private half arrives
    via ``msks key --out`` (the CLI over the same API the forward
    uses), and a stop/start cycle serves the same identity again —
    the halves live on the workspace's row, not in any process.
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    token = f"smoke-token-{uuid.uuid4().hex}"
    api_port = free_port()
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=_default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(
            host="127.0.0.1",
            port=api_port,
            db_path=state_dir / "smoke.db",
            bootstrap_token=token,
        ),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"ident-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    workdir = state_dir / "ident-work"
    workdir.mkdir(parents=True)
    key = workdir / "id"
    known_hosts = workdir / "known_hosts"
    # The operator payload rides the same seed as the identity (the
    # MIME-composed default path every --user-data create now takes)
    # and lands in the guest beside the planted keys.
    payload_marker = f"PAYLOAD-{uuid.uuid4().hex[:8]}"

    # The daemon verifies, never writes, ip_forward (#101); the root
    # harness owns the host's setting for the run and restores it.
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    api_server = None
    api_task = None
    forwards: list[subprocess.Popen] = []
    forward_logs: list[Path] = []

    cli_env = dict(
        os.environ,
        MSKSC_URL=f"http://127.0.0.1:{api_port}",
        MSKSC_TOKEN=token,
    )

    def start_forward(port: int) -> None:
        log = workdir / f"forward-{len(forward_logs)}.log"
        forward_logs.append(log)
        forwards.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "msks.client.cli",
                    "forward",
                    wid,
                    "22",
                    "--local",
                    str(port),
                ],
                env=cli_env,
                stdout=subprocess.DEVNULL,
                stderr=open(log, "ab"),
            )
        )

    def forward_evidence() -> str:
        return "\n".join(
            f"--- {log.name} ---\n{log.read_text(errors='replace')[-800:]}"
            for log in forward_logs
            if log.exists()
        )

    async def await_forward_listener(port: int, timeout_s: float = 30.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        needle = f"msks: 127.0.0.1:{port} -> "
        while loop.time() < deadline:
            log = forward_logs[-1]
            if log.exists() and needle in log.read_text(errors="replace"):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"msks forward never listened on 127.0.0.1:{port} within "
            f"{timeout_s}s; forward logs:\n{forward_evidence()}"
        )

    def ssh_opts(port: int) -> list[str]:
        return [
            "-F",
            os.devnull,
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "BatchMode=yes",
            "-p",
            str(port),
        ]

    async def run_ssh(
        port: int, user: str, command: str
    ) -> subprocess.CompletedProcess:
        # to_thread, never a bare subprocess.run: the API server rides
        # this loop (the #110 harness lesson).
        result = await asyncio.to_thread(
            subprocess.run,
            [SSH_BIN, *ssh_opts(port), f"{user}@127.0.0.1", command],
            capture_output=True,
            text=True,
            timeout=SSH_CMD_TIMEOUT_S,
        )
        if result.returncode != 0:
            verbose = await asyncio.to_thread(
                subprocess.run,
                [
                    SSH_BIN,
                    *ssh_opts(port),
                    "-o",
                    "LogLevel=DEBUG3",
                    f"{user}@127.0.0.1",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=SSH_CMD_TIMEOUT_S,
            )
            result.stderr += (
                f"\n--- verbose rerun (rc={verbose.returncode}) ---\n"
                f"{verbose.stderr[-3000:]}"
            )
        return result

    async def cli(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        """One msks CLI call against the test daemon (to_thread: the
        API server shares this loop)."""
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", *args],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    async def start_via_api() -> None:
        started = await cli("start", wid)
        assert started.returncode == 0, started.stderr

    async def stop_via_api() -> None:
        stopped = await cli("stop", wid)
        assert stopped.returncode == 0, stopped.stderr

    async def boot_and_wait() -> None:
        await await_guest_up(serial_log)
        # The seed's script runs in cloud-init's user-scripts stage
        # (cloud_final); wait for cloud-init to be done before any
        # login or authorized_keys assertion, so the stage's ordering
        # relative to sshd never matters.
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(
            microvm,
            wid,
            "i=0; while [ $i -lt 30 ] "
            "&& ! { systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1; }; "
            "do sleep 1; i=$((i+1)); done; "
            "systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1 && echo U-$((6*7))",
            "U-42",
        )

    async def fetch_key(out: Path) -> str:
        # The CLI fetch (#111): same API, same token, mode 0600 —
        # no manual key steps for the operator.
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "msks.client.cli",
                "key",
                wid,
                "--out",
                str(out),
            ],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert out.stat().st_mode & 0o777 == 0o600
        return out.read_text()

    try:
        api_server = uvicorn.Server(
            uvicorn.Config(
                build_api(app),
                host="127.0.0.1",
                port=api_port,
                log_level="warning",
            )
        )
        api_task = asyncio.create_task(api_server.serve())
        deadline = asyncio.get_running_loop().time() + 30
        while not api_server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test API server never started (30s)")
            await asyncio.sleep(0.05)

        # The real create path: mint at create, seed at prepare.
        payload_path = workdir / "payload.sh"
        payload_path.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {payload_marker} > /root/payload\n",
            encoding="utf-8",
        )
        # --daemon-mint keeps this smoke on the daemon-mint path
        # it pins (#111): the client mint is now the create default
        # (#121) and has its own smoke below.
        created = await cli(
            "create",
            wid,
            "--kernel",
            VMLINUX,
            *(["--initrd", INITRD] if INITRD else []),
            "--rootfs",
            ROOTFS,
            *(["--cmdline", CMDLINE] if CMDLINE else []),
            "--egress",
            "--user-data",
            str(payload_path),
            "--daemon-mint",
        )
        assert created.returncode == 0, created.stderr

        private_pem = await fetch_key(key)
        assert private_pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")

        # Start via the API, boot, and let the guest say who its keys
        # are for: both authorized_keys files carry the minted line.
        await start_via_api()
        await boot_and_wait()
        pub = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", "key", wid],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        minted = pub.stdout.strip()
        assert minted.startswith("ecdsa-sha2-nistp256 ") and minted.endswith(
            f"msksd:{wid}"
        )
        await run_in_console(
            microvm,
            wid,
            f"grep -qxF '{minted}' /root/.ssh/authorized_keys "
            f"&& grep -qxF '{minted}' /home/msks/.ssh/authorized_keys "
            f"&& stat -c %a /home/msks/.ssh/authorized_keys "
            f"&& echo AK-$((6*7))",
            "AK-42",
        )
        # The operator payload landed beside the identity — the
        # composed document ran whole through cloud-init.
        await run_in_console(microvm, wid, "cat /root/payload", payload_marker)

        # Logins: root and the workspace user, the minted key alone.
        forward_port = free_port()
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        root_login = await run_ssh(forward_port, "root", "echo ROOT-$((6*7))")
        assert root_login.returncode == 0, (
            f"{root_login.stdout}\n{root_login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "ROOT-42" in root_login.stdout, root_login.stdout
        user_login = await run_ssh(forward_port, "msks", 'echo "$(whoami)-$((6*7))"')
        assert user_login.returncode == 0, (
            f"{user_login.stdout}\n{user_login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "msks-42" in user_login.stdout, user_login.stdout

        # msks ssh (#112): the same login as one command — identity
        # fetched and served from the transient agent, the forward as
        # ProxyCommand,
        # the msks user by default and root via -l. -F /dev/null in
        # the passthrough keeps the harness hermetic (the #110
        # lesson: a host ssh_config can carry options this build
        # rejects); XDG_CACHE_HOME keeps the per-workspace known_hosts
        # inside the workdir.
        ssh_cache = workdir / "ssh-cache"
        ssh_env = dict(cli_env, XDG_CACHE_HOME=str(ssh_cache))

        async def run_msks_ssh(
            *options: str, command: str
        ) -> subprocess.CompletedProcess:
            return await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "msks.client.cli",
                    "ssh",
                    wid,
                    "--",
                    "-F",
                    os.devnull,
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=15",
                    *options,
                    "--",
                    command,
                ],
                env=ssh_env,
                capture_output=True,
                text=True,
                timeout=SSH_CMD_TIMEOUT_S,
            )

        sugar_login = await run_msks_ssh(command="echo SSHU-$(whoami)-$((6*7))")
        assert sugar_login.returncode == 0, (
            f"{sugar_login.stdout}\n{sugar_login.stderr}"
        )
        assert "SSHU-msks-42" in sugar_login.stdout, sugar_login.stdout
        root_login = await run_msks_ssh(
            "-l", "root", command="echo SSHR-$(id -u)-$((6*7))"
        )
        assert root_login.returncode == 0, f"{root_login.stdout}\n{root_login.stderr}"
        assert "SSHR-0-42" in root_login.stdout, root_login.stdout
        # The logins recorded the guest's host key in the msks cache.
        assert (ssh_cache / "msks" / wid / "known_hosts").exists()

        # stop/start: the row serves the same identity again — the
        # halves persist on the workspace, not in any process — and
        # the overlay keeps the planted keys, so the same private half
        # still opens the same guest.
        for proc in forwards:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(proc.wait, 10)
        forwards.clear()
        await stop_via_api()
        again_key = workdir / "id-again"
        again_pem = await fetch_key(again_key)
        assert again_pem == private_pem
        serial_log.unlink(missing_ok=True)
        await start_via_api()
        await boot_and_wait()
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        relogin = await run_ssh(forward_port, "msks", 'echo "BACK-$(whoami)-$((6*7))"')
        assert relogin.returncode == 0, (
            f"{relogin.stdout}\n{relogin.stderr}\nforward logs:\n{forward_evidence()}"
        )
        assert "BACK-msks-42" in relogin.stdout, relogin.stdout

        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        for proc in forwards:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 10)
        if api_task is not None:
            api_server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_egress
@needs_local
@needs_ssh_tools
async def test_local_client_minted_identity() -> None:
    """The client-minted identity end to end (#121): the client mints
    the keypair and sends the public half only, the daemon's row holds
    no private half (the no-escrow contract, checked in the database
    itself), and ``msks ssh`` opens the fresh workspace from the local
    cache alone — the identity the client kept is the identity the
    guest planted.
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    token = f"smoke-token-{uuid.uuid4().hex}"
    api_port = free_port()
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=_default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(
            host="127.0.0.1",
            port=api_port,
            db_path=state_dir / "smoke.db",
            bootstrap_token=token,
        ),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"cmint-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    workdir = state_dir / "cmint-work"
    workdir.mkdir(parents=True)
    data = workdir / "data"
    identity = data / "msks" / wid / "identity"

    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    api_server = None
    api_task = None

    # XDG_DATA_HOME holds the client-minted identity (#121);
    # XDG_CACHE_HOME keeps the msks ssh known_hosts inside the
    # workdir (the #110 hermeticity lesson).
    cli_env = dict(
        os.environ,
        MSKSC_URL=f"http://127.0.0.1:{api_port}",
        MSKSC_TOKEN=token,
        XDG_DATA_HOME=str(data),
        XDG_CACHE_HOME=str(workdir / "cache"),
    )

    async def cli(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", *args],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def row_halves() -> tuple[str | None, str | None]:
        """(ssh_privkey, ssh_pubkey) straight from the daemon's own
        database — the no-escrow contract, not the API's word for it."""
        import sqlite3

        with sqlite3.connect(settings.server.db_path) as conn:
            return conn.execute(
                "select ssh_privkey, ssh_pubkey from workspaces where id = ?", (wid,)
            ).fetchone()

    try:
        api_server = uvicorn.Server(
            uvicorn.Config(
                build_api(app),
                host="127.0.0.1",
                port=api_port,
                log_level="warning",
            )
        )
        api_task = asyncio.create_task(api_server.serve())
        deadline = asyncio.get_running_loop().time() + 30
        while not api_server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test API server never started (30s)")
            await asyncio.sleep(0.05)

        # The client mint is the create default (#121): the POST
        # carries the public half only, and the private half lands
        # mode 0600 under the client data root after the create.
        created = await cli(
            "create",
            wid,
            "--kernel",
            VMLINUX,
            *(["--initrd", INITRD] if INITRD else []),
            "--rootfs",
            ROOTFS,
            *(["--cmdline", CMDLINE] if CMDLINE else []),
            "--egress",
        )
        assert created.returncode == 0, created.stderr
        assert identity.exists()
        assert identity.stat().st_mode & 0o777 == 0o600
        assert identity.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")

        # The daemon's row: public half annotated with its own
        # provenance marker, private half NULL — no escrow.
        priv, pub = row_halves()
        assert priv is None
        assert pub is not None and pub.endswith(f"msks-client:{wid}")

        # The key endpoint serves the public half; the private forms
        # name where that half lives instead.
        served = await cli("key", wid)
        assert served.returncode == 0, served.stderr
        assert served.stdout.strip() == pub
        refused = await cli("key", wid, "--private")
        assert refused.returncode != 0
        assert "client-minted" in refused.stderr

        # Boot, let cloud-init plant the key, and confirm the guest's
        # authorized_keys carry the client's line.
        started = await cli("start", wid)
        assert started.returncode == 0, started.stderr
        await await_guest_up(serial_log)
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(
            microvm,
            wid,
            "i=0; while [ $i -lt 30 ] "
            "&& ! { systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1; }; "
            "do sleep 1; i=$((i+1)); done; "
            "systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1 && echo U-$((6*7))",
            "U-42",
        )
        await run_in_console(
            microvm,
            wid,
            f"grep -qxF '{pub}' /root/.ssh/authorized_keys "
            f"&& grep -qxF '{pub}' /home/msks/.ssh/authorized_keys "
            f"&& echo AK-$((6*7))",
            "AK-42",
        )

        # ``msks ssh`` from the local cache alone: the API serves the
        # public half, the private half comes from the file the create
        # wrote, and the login runs as the workspace user.
        login = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "msks.client.cli",
                "ssh",
                wid,
                "--",
                "-F",
                os.devnull,
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "--",
                "echo CMINT-$(whoami)-$((6*7))",
            ],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=SSH_CMD_TIMEOUT_S,
        )
        assert login.returncode == 0, f"{login.stdout}\n{login.stderr}"
        assert "CMINT-msks-42" in login.stdout, login.stdout

        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        if api_task is not None:
            api_server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_egress
@needs_local
@needs_ssh_tools
async def test_local_operator_pubkey() -> None:
    """An operator-supplied key end to end (#132): the workspace is
    created around a public key the operator already owns — here a
    real ssh-keygen pair — at any well-formed type, the daemon's row
    holds no private half, nothing is written client-side, and the
    guest accepts ssh with that key alone. ``msks ssh`` cannot serve
    a half it never had: its recovery names the operator's key.
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    token = f"smoke-token-{uuid.uuid4().hex}"
    api_port = free_port()
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=_default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(
            host="127.0.0.1",
            port=api_port,
            db_path=state_dir / "smoke.db",
            bootstrap_token=token,
        ),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"opkey-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    workdir = state_dir / "opkey-work"
    workdir.mkdir(parents=True)
    data = workdir / "data"

    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    api_server = None
    api_task = None
    forwards: list[subprocess.Popen] = []
    forward_logs: list[Path] = []

    cli_env = dict(
        os.environ,
        MSKSC_URL=f"http://127.0.0.1:{api_port}",
        MSKSC_TOKEN=token,
        XDG_DATA_HOME=str(data),
        XDG_CACHE_HOME=str(workdir / "cache"),
    )

    # The operator's own key, the way operators make them.
    key_path = workdir / "operator_key"
    keygen = await asyncio.to_thread(
        subprocess.run,
        [SSH_KEYGEN_BIN, "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert keygen.returncode == 0, keygen.stderr
    pub_file = Path(f"{key_path}.pub")
    supplied = pub_file.read_text().strip()

    async def cli(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", *args],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def row_halves() -> tuple[str | None, str | None]:
        import sqlite3

        with sqlite3.connect(settings.server.db_path) as conn:
            return conn.execute(
                "select ssh_privkey, ssh_pubkey from workspaces where id = ?", (wid,)
            ).fetchone()

    def forward_evidence() -> str:
        return "\n".join(
            f"--- {log.name} ---\n{log.read_text(errors='replace')[-800:]}"
            for log in forward_logs
            if log.exists()
        )

    ssh_opts = [
        "-F",
        os.devnull,
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={workdir / 'known_hosts'}",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
    ]

    try:
        api_server = uvicorn.Server(
            uvicorn.Config(
                build_api(app),
                host="127.0.0.1",
                port=api_port,
                log_level="warning",
            )
        )
        api_task = asyncio.create_task(api_server.serve())
        deadline = asyncio.get_running_loop().time() + 30
        while not api_server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test API server never started (30s)")
            await asyncio.sleep(0.05)

        created = await cli(
            "create",
            wid,
            "--kernel",
            VMLINUX,
            *(["--initrd", INITRD] if INITRD else []),
            "--rootfs",
            ROOTFS,
            *(["--cmdline", CMDLINE] if CMDLINE else []),
            "--egress",
            "--pubkey",
            str(pub_file),
        )
        assert created.returncode == 0, created.stderr
        # Nothing was written client-side: the private half stays
        # wherever the operator keeps it (here, the workdir).
        assert not (data / "msks" / wid).exists()

        priv, pub = row_halves()
        assert priv is None
        assert pub is not None and pub.endswith(f"msks-client:{wid}")
        assert pub.split()[:2] == supplied.split()[:2]

        served = await cli("key", wid)
        assert served.returncode == 0, served.stderr
        assert served.stdout.strip() == pub
        refused = await cli("key", wid, "--private")
        assert refused.returncode != 0
        assert "never held its private half" in refused.stderr

        started = await cli("start", wid)
        assert started.returncode == 0, started.stderr
        await await_guest_up(serial_log)
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(
            microvm,
            wid,
            "i=0; while [ $i -lt 30 ] "
            "&& ! { systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1; }; "
            "do sleep 1; i=$((i+1)); done; "
            "systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1 && echo U-$((6*7))",
            "U-42",
        )
        await run_in_console(
            microvm,
            wid,
            f"grep -qxF '{pub}' /root/.ssh/authorized_keys "
            f"&& grep -qxF '{pub}' /home/msks/.ssh/authorized_keys "
            f"&& echo AK-$((6*7))",
            "AK-42",
        )

        forward_port = free_port()
        log = workdir / "forward.log"
        forward_logs.append(log)
        forwards.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "msks.client.cli",
                    "forward",
                    wid,
                    "22",
                    "--local",
                    str(forward_port),
                ],
                env=cli_env,
                stdout=subprocess.DEVNULL,
                stderr=open(log, "ab"),
            )
        )
        needle = f"msks: 127.0.0.1:{forward_port} -> "
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            if log.exists() and needle in log.read_text(errors="replace"):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(
                f"msks forward never listened within 30s; logs:\n{forward_evidence()}"
            )

        login = await asyncio.to_thread(
            subprocess.run,
            [
                SSH_BIN,
                *ssh_opts,
                "-p",
                str(forward_port),
                "msks@127.0.0.1",
                'echo "OPKEY-$(whoami)-$((6*7))"',
            ],
            capture_output=True,
            text=True,
            timeout=SSH_CMD_TIMEOUT_S,
        )
        assert login.returncode == 0, (
            f"{login.stdout}\n{login.stderr}\nforward logs:\n{forward_evidence()}"
        )
        assert "OPKEY-msks-42" in login.stdout, login.stdout

        # msks ssh has no half to serve for an operator key: the
        # recovery names both places the half can be.
        sugar = await cli("ssh", wid, "--", "-F", os.devnull, "--", "true")
        assert sugar.returncode != 0
        assert "not on this client" in sugar.stderr
        assert "supplied" in sugar.stderr

        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        for proc in forwards:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 10)
        if api_task is not None:
            api_server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)

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
import shutil
import ssl
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
import websockets
from httpx import AsyncClient
from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import K8sSettings, Settings, VmmSettings

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

#: The guest init prints this on the serial console once userspace
#: (and the acpid that answers host-side shutdowns) is up.
# Debian's systemd boot (#30): the serial autologin getty is the last
# thing to come up, so it is the "guest is usable" marker.
GUEST_UP_MARKER = "msks-guest login:"

#: Per-phase timeouts, env-tunable for slow hosts (#64): a runner's
#: nested-KVM guest runs the same boot several times slower than a
#: dev host's KVM guest, and CI sets all three explicitly. Defaults
#: keep the dev-host behavior unchanged.
GUEST_UP_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_GUEST_UP_TIMEOUT_S", "60"))
CONSOLE_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_CONSOLE_TIMEOUT_S", "30"))
SHUTDOWN_TIMEOUT_S = float(os.environ.get("MSKSD_TEST_SHUTDOWN_TIMEOUT_S", "60"))


def serial_tail(serial_log: Path, limit: int = 2000) -> str:
    """The end of the guest's serial log, for failure messages."""
    if not serial_log.exists():
        return "(no serial log)"
    return serial_log.read_text(encoding="utf-8", errors="replace")[-limit:]


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

    The vsock console is an echoing pty: the sent command and its
    output both flow back, so the marker proves the guest ran it.
    """
    timeout_s = timeout_s if timeout_s is not None else CONSOLE_TIMEOUT_S
    data = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while needle not in data:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"never saw {needle!r}; got: {data[-400:]!r}")
        chunk = await asyncio.wait_for(reader.read(4096), remaining)
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


async def run_in_console(microvm, workspace_id: str, command: str, marker: str) -> None:
    """Run one shell command over the vsock console and wait for its
    marker — one fresh guest shell session per call."""
    reader, writer = await microvm.console(workspace_id)
    try:
        await read_until(reader, PROMPT_NEEDLE)
        writer.write(command.encode() + b"\n")
        await writer.drain()
        await read_until(reader, marker.encode())
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


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
    )
    try:
        await microvm.launch(spec)
        info = await microvm.info(wid)
        assert info.status.value == "running"
        # Wait for userspace before shutting down: the graceful shutdown
        # is an ACPI power-button press, and the guest only answers it
        # once its acpid is running — pressing earlier would drop the
        # event and time out against a VM that is running but not yet
        # listening.
        await await_guest_up(serial_log)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        # Never leak a live VMM (and its /dev/kvm handle) on failure.
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
    from msks import persist

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
                (f"echo {root_marker} > /root/probe && cat /root/probe", root_marker),
                (f"echo {home_marker} > /home/probe && cat /home/probe", home_marker),
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
                ("test ! -e /root/probe && echo GONE-OK", "GONE-OK"),
            ]
        )
    except BaseException:
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
# virt: the workspace boots inside the appliance VM), sudo -n for the
# one-time bridge/tap, and built appliance + guest assets.
APPLIANCE = os.environ.get("MSKSD_TEST_APPLIANCE")


def _sudo_available() -> bool:
    try:
        return (
            subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except OSError, subprocess.TimeoutExpired:
        return False


needs_appliance = pytest.mark.skipif(
    not APPLIANCE
    or not os.access("/dev/kvm", os.W_OK)
    or not (REPO_ROOT / ".appliance" / "vmlinux").is_file()
    or not _sudo_available(),
    reason=(
        "set MSKSD_TEST_APPLIANCE=1 with /dev/kvm, sudo -n (bridge/tap), "
        "and devenv tasks run msks:appliance-build + msks:build-guest"
    ),
)


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

    up = _devenv_processes("up", "-d")
    assert up.returncode == 0, f"devenv processes up failed:\n{up.stdout}\n{up.stderr}"

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
    try:
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
                message = await asyncio.wait_for(shell_ws.recv(), 30.0)
                console_got += (
                    message if isinstance(message, bytes) else message.encode()
                )
        response = await client.get(f"{base}/workspaces/{wid}", headers=headers)
        assert response.json().get("status") == "running", response.text

        response = await client.post(f"{base}/workspaces/{wid}/stop", headers=headers)
        assert response.status_code == 200, response.text
        response = await client.delete(f"{base}/workspaces/{wid}", headers=headers)
        assert response.status_code == 200, response.text
        response = await client.get(f"{base}/workspaces/{wid}", headers=headers)
        assert response.status_code == 404
    finally:
        if headers is not None:
            with contextlib.suppress(Exception):
                await client.delete(f"{base}/workspaces/{wid}", headers=headers)
        with contextlib.suppress(Exception):
            await client.post(f"{base}/workspaces/{wid}/stop", headers=headers)
        down = _devenv_processes("down", timeout=300)
        assert down.returncode == 0, (
            f"devenv processes down failed:\n{down.stdout}\n{down.stderr}"
        )
    assert not (app_dir / "api.sock").exists()
    # The supervisor is gone too: teardown is its view of "stopped",
    # not just pidfile/socket absence.
    listing = _devenv_processes("list", timeout=120)
    assert "No process manager is running" in listing.stdout + listing.stderr, (
        f"process manager still alive after down:\n{listing.stdout}"
    )

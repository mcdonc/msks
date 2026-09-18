"""L3 recursion smoke (#82): msksd inside a workspace boots an inner one."""

import asyncio
import base64
import contextlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
import websockets

from test_smoke import (
    APPLIANCE,
    REPO_ROOT,
    RSYNC_BIN,
    SSH_BIN,
    client,
    devenv_task,
    free_port,
    host_net_installed,
)

# --- L3 recursion smoke (#82) ----------------------------------------------
#
# The full recursion on real KVM: the appliance (L1) boots a dev
# workspace (L2) seeded with scripts/l3-recursion.sh, and msksd
# running INSIDE that workspace boots an inner workspace (L3)
# reachable through its console. Opt-in beyond the appliance smoke
# (MSKSD_TEST_L3=1): the sequence is the slowest thing this suite
# runs — the L2 bootstrap downloads the toolchain over nested-virt
# egress (minutes), and the L3 guest boots on nested-in-nested KVM
# (minutes more) — so it carries its own wall-clock budget
# (MSKSD_TEST_L3_TIMEOUT_S).
L3 = os.environ.get("MSKSD_TEST_L3")


def guest_boot_artifacts() -> dict[str, Path] | None:
    """The built guest's boot artifacts (.guest/, msks:build-guest)
    — kernel, initrd, and the sparse rootfs the rsync leg pushes
    into the L2 workspace. The inner daemon boots them directly
    (create with explicit paths): the catalog import would copy,
    hash, and densely extract the ~1.5 GiB archive for a rootfs
    that is mostly mke2fs zero-seek slack — several GiB of nested
    I/O the recursion does not need to re-prove (the appliance's
    own first-boot import covers the catalog path at L1)."""
    names = ("vmlinux", "initrd", "rootfs.ext4")
    paths = {name: REPO_ROOT / ".guest" / name for name in names}
    if not all(path.is_file() for path in paths.values()):
        return None
    return paths


needs_l3 = pytest.mark.skipif(
    not L3
    or not APPLIANCE
    or not os.access("/dev/kvm", os.W_OK)
    or not (REPO_ROOT / ".appliance" / "vmlinux").is_file()
    or not host_net_installed()
    or not (SSH_BIN and RSYNC_BIN)
    or guest_boot_artifacts() is None,
    reason=(
        "set MSKSD_TEST_L3=1 with /dev/kvm, the one-time host network, "
        "ssh+rsync on PATH, and devenv tasks run msks:appliance-build "
        "+ msks:build-guest"
    ),
)

#: The recursion's whole-sequence budget: the L2 bootstrap (apt, uv,
#: the checkout, uv sync — download-bound under nested KVM), the
#: archive rsync, the inner image import, and the L3 boot to console.
L3_RECURSION_TIMEOUT_S = float(
    os.environ.get("MSKSD_TEST_L3_TIMEOUT_S", "5400")
)


class L3Timeline:
    """Per-phase wall-clock tracking for one recursion run (#82).

    ``mark`` names each host-side phase as it completes; ``step``
    records the first-seen time of every guest-side step marker (the
    seed's state trail, the inner bring-up's log steps), so the
    summary printed at the end — success or failure — names where the
    minutes went. That table is the speed record the issue's evidence
    cites and the baseline any follow-up speedup compares against.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.marks: list[tuple[str, float]] = []
        self.steps: dict[str, float] = {}

    def mark(self, name: str) -> None:
        at = time.monotonic() - self.started
        self.marks.append((name, at))
        print(f"L3 phase: {name} at +{at:.0f}s", flush=True)

    def step(self, name: str) -> None:
        if not name or "$" in name or name in self.steps:
            return
        self.steps[name] = time.monotonic() - self.started
        print(f"L3 guest step: {name} at +{self.steps[name]:.0f}s", flush=True)

    def summary(self) -> None:
        print("L3 phase timeline (mark, at, since previous):", flush=True)
        previous = 0.0
        for name, at in self.marks:
            print(
                f"  {name:<26} +{at:6.0f}s  ({at - previous:5.0f}s)",
                flush=True,
            )
            previous = at
        if self.steps:
            print("L3 guest steps (first seen):", flush=True)
            for name, at in sorted(
                self.steps.items(), key=lambda pair: pair[1]
            ):
                print(f"  {name:<26} +{at:6.0f}s", flush=True)


def l3_seed() -> str:
    """The L3 recursion bootstrap from the repo's scripts/ tree."""
    path = REPO_ROOT / "scripts" / "l3-recursion.sh"
    return path.read_text()


#: The inner workspace's console probe, run INSIDE the L2 guest
#: against the inner daemon's own websocket: base64-staged as one
#: console line, because the vsock pty can corrupt long sent lines
#: (#103) and a base64 blob has nothing to corrupt — any mangled
#: round is superseded by the resend. The probe waits for the inner
#: guest's prompt, echoes a guest-computed marker, and prints the
#: recursion's own success line — the bytes the host asserts on.
L3_INNER_PROBE = """
import asyncio
import time

import websockets

TOKEN = open("/root/.msks-inner/token").read().strip()
URL = "ws://127.0.0.1:8660/api/v1/workspaces/inner1/console?token=" + TOKEN
PROMPT = b"root@msks-guest:~# "


async def main() -> None:
    started = time.monotonic()

    async def collect(ws, needle: bytes) -> None:
        buf = b""
        while needle not in buf:
            try:
                chunk = await asyncio.wait_for(ws.recv(), 10)
            except TimeoutError:
                # A heartbeat every silent window: the outer console's
                # stall detector (input answered by nothing) would
                # otherwise close the session under this wait.
                print(f"waiting {time.monotonic() - started:.0f}s", flush=True)
                continue
            buf += chunk if isinstance(chunk, bytes) else chunk.encode()

    async with websockets.connect(URL, open_timeout=30, max_size=2**22) as ws:
        await collect(ws, PROMPT)
        await ws.send(b"echo L3-$((6*7))-CONSOLE\\n")
        await collect(ws, b"L3-42-CONSOLE")
    print(f"INNER-CONSOLE-42-OK after {time.monotonic() - started:.1f}s")


asyncio.run(main())
"""


def l3_inner_setup_script(cmdline: str) -> str:
    """The idempotent inner-bring-up script staged into L2: create
    the inner workspace over the rsynced boot artifacts (explicit
    kernel/initrd/rootfs paths, the manifest's cmdline), then start
    it. Step markers under /root/.msks-l3-inner/ make every step a
    no-op on re-run — the console probe resends the WHOLE script when
    a session stalls (#103), so a half-finished first round must
    converge, never restart. flock keeps concurrent rounds from
    interleaving (the second waits, then re-runs as no-ops)."""
    assert "'" not in cmdline, cmdline
    return """#!/bin/sh
# Staged by the L3 recursion smoke (#82): idempotent inner bring-up.
# The EXIT trap records done-<exit> on the log the host polls — only
# a round that held the lock and ran the steps to completion writes
# done-0; a failed step leaves done-N with the error text above it.
set -eu
export MSKSC_URL=http://127.0.0.1:8660
export MSKSC_TOKEN=$(cat /root/.msks-inner/token)
MSKS=/root/msks/.venv/bin/msks
MARK=/root/.msks-l3-inner
mkdir -p "$MARK"
trap 'echo done-$?' EXIT
# BLOCKING flock: a duplicate round (a resent launch line) waits for
# the running round, then re-runs the steps as no-ops — its own
# done-0 is true by then.
exec 9>"$MARK/lock"
flock 9
if [ ! -f "$MARK/created" ]; then
  echo create
  # --cpus 1 is the measured depth-2 boundary: a 1-vCPU inner guest
  # boots to its login prompt at two removes, while a 2-vCPU one
  # hangs in early SMP bringup (vCPU executing, zero serial bytes —
  # the apicv-era pathology; see the issue evidence).
  "$MSKS" create inner1 --cpus 1 \
    --kernel /root/inner-artifacts/vmlinux \
    --initrd /root/inner-artifacts/initrd \
    --rootfs /root/inner-artifacts/rootfs.ext4 \
    --cmdline 'CMDLINE'
  touch "$MARK/created"
fi
if [ ! -f "$MARK/started" ]; then
  echo start
  t0=$(date +%s)
  "$MSKS" start inner1
  echo $(( $(date +%s) - t0 )) > "$MARK/boot-s"
  touch "$MARK/started"
fi
"""


async def l3_console_command(
    connect, command: bytes, marker: bytes, timeout_s: float
) -> bytes:
    """One command over the L2 workspace's console websocket, driven
    to its marker with the #103 ride-out (resend in the same session
    on silence; a fresh session after a stall close), under one
    deadline. Idempotent commands only — every silence re-sends."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    buf = b""
    ws = None

    def remaining() -> float:
        return max(deadline - loop.time(), 0.001)

    async def fresh_session() -> None:
        """Connect and send once: a new session's first line can be
        lost to readline's typeahead flush (#75), so the send is
        repeated per silence window below, never per chunk — a
        send-per-chunk loop would re-execute the command for every
        websocket message the pty splits the output into."""
        nonlocal ws
        ws = await connect()
        await ws.send(command)

    try:
        while marker not in buf:
            if loop.time() >= deadline:
                raise AssertionError(
                    f"console never showed {marker!r} within {timeout_s}s; "
                    f"got: {buf[-500:]!r}"
                )
            try:
                if ws is None:
                    await fresh_session()
                chunk = await asyncio.wait_for(ws.recv(), 20.0)
                buf += chunk if isinstance(chunk, bytes) else chunk.encode()
            except TimeoutError:
                # One silent window: the shell may have dropped the
                # line to the typeahead flush — resend it, same
                # session. (Idempotent commands only, by contract.)
                with contextlib.suppress(Exception):
                    await ws.send(command)
                continue
            except OSError, websockets.WebSocketException:
                # A stall close (4502), a teardown, or a boot-window
                # refusal: a fresh session retries under the deadline,
                # with the exception named on the final failure.
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                    ws = None
                await asyncio.sleep(min(2.0, remaining()))
                continue
    finally:
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
    return buf


@needs_l3
async def test_appliance_l3_recursion() -> None:
    """msksd inside a workspace boots an inner workspace (#82).

    The two feasibility questions this run answers with evidence:

    - the Debian generic kernel ships kvm/kvm-intel/kvm-amd as
      modules (CONFIG_KVM*=m in the pinned deb) and the workspace
      image's module closure carries them (#82's closure growth);
      the L2 guest proves it live — vmx in /proc/cpuinfo, the kvm
      module loaded by the image's own msks-kvm.service, /dev/kvm
      present;
    - vmx survives two removes of cloud-hypervisor's default CPU
      config (host -> appliance -> workspace -> inner workspace):
      proven by the L3 boot itself — cloud-hypervisor has no TCG
      fallback, so an inner VM that reaches its console is running
      on nested-in-nested KVM.

    The inner daemon is the dev image's own (#77's bootstrap + the
    L3 seed's additions), its state on the persistent /home volume,
    and the inner workspace's console is driven through the inner
    daemon's websocket from inside L2 — console in console.
    """
    app_dir = REPO_ROOT / ".appliance"
    base = "https://192.168.77.2:8660/api/v1"
    wid = f"l3-{uuid.uuid4().hex[:8]}"
    artifacts = guest_boot_artifacts()

    # Refuse to stomp a running appliance (the appliance smoke's own
    # guard; both tests boot THE appliance on this host).
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

    # A fresh state disk (the appliance smoke's shape): this run's
    # workspaces must not touch the dev host's own appliance state.
    scratch = tempfile.TemporaryDirectory(prefix="msks-l3-state")
    state_disk = Path(scratch.name) / "state.ext4"
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
    assert copy.returncode == 0, f"sparse state copy failed: {copy.stderr}"
    state_disk.chmod(0o644)
    prior_state_env = os.environ.get("MSKSD_APPLIANCE_STATE")
    os.environ["MSKSD_APPLIANCE_STATE"] = str(state_disk)
    # Three levels of guest memory ride the appliance's own: the L2
    # (4 GiB) plus the inner VMs' RAM faulting inside it, beside the
    # appliance's OS and daemon. The default 6 GiB OOM-kills the L2's
    # VMM once the inner guests touch their pages (seen live: the
    # appliance kernel's oom-kill of the workspace VMM, anon-rss
    # ~2.9 GiB, mid-probe) — 12 GiB carries it with headroom.
    prior_mem_mib = os.environ.get("MSKS_APPLIANCE_MEM_MIB")
    if not prior_mem_mib:
        os.environ["MSKS_APPLIANCE_MEM_MIB"] = "12288"
    prior_cmdline_extra = os.environ.get("MSKS_APPLIANCE_CMDLINE_EXTRA")
    if not prior_cmdline_extra:
        # console_stall_timeout_s=0: this test's probe steps wait
        # minutes for the inner guest between console bytes — the
        # stall window exists to free wedged sessions for humans, and
        # this run's own resend loop already recovers the wedged case.
        os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = (
            "msksd.vsock_wait_timeout_s=120 msksd.console_stall_timeout_s=0"
        )

    ws_ctx = ssl.create_default_context()
    ws_ctx.check_hostname = False
    ws_ctx.verify_mode = ssl.CERT_NONE

    async def connect_l2_console():
        url = (
            base.replace("https://", "wss://")
            + f"/workspaces/{wid}/console?token={token}"
        )
        return await websockets.connect(url, ssl=ws_ctx, open_timeout=30)

    token = None
    up = None
    forward_proc = None
    timeline = L3Timeline()
    try:
        up = devenv_task("msks:appliance-up")
        assert up.returncode == 0, (
            f"msks:appliance-up failed:\n{up.stdout}\n{up.stderr}"
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 120.0
        while loop.time() < deadline:
            token_file = app_dir / "bootstrap-token"
            if token_file.is_file() and token_file.read_text().strip():
                token = token_file.read_text().strip()
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("appliance bootstrap token never appeared")
        headers = {"authorization": f"Bearer {token}"}
        deadline = loop.time() + 120.0
        while loop.time() < deadline:
            try:
                response = await client.get(f"{base}/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0)
        else:
            serial = (app_dir / "serial.log").read_text(errors="replace")[
                -2000:
            ]
            raise AssertionError(
                f"appliance API never healthy; serial tail:\n{serial}"
            )
        timeline.mark("appliance-up")

        # The L2 workspace: egress (the bootstrap downloads over it),
        # the recursion seed, memory for a daemon beside an inner
        # guest, and a /home volume sized for the inner state.
        response = await client.post(
            f"{base}/workspaces",
            json={
                "id": wid,
                "mem_mib": 4096,
                "home_mib": 30720,
                "user_data": l3_seed(),
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        response = await client.post(
            f"{base}/workspaces/{wid}/start", headers=headers, timeout=120.0
        )
        assert response.status_code in (200, 202), response.text
        deadline = loop.time() + 120.0
        while loop.time() < deadline:
            response = await client.get(
                f"{base}/workspaces/{wid}", headers=headers
            )
            if response.json().get("status") == "running":
                break
            await asyncio.sleep(1.0)
        else:
            raise AssertionError(
                f"L2 workspace never reached running: {response.text}"
            )
        timeline.mark("l2-running")

        # Q1 evidence, live in the L2 guest: the closure's kvm trio
        # is in the shipped tree, the image's unit loaded it, and the
        # CPU carries vmx through one remove already (the appliance's
        # vm.create sets no CPU options).
        tree = await l3_console_command(
            connect_l2_console,
            b"ls /usr/lib/modules/*/kernel/arch/x86/kvm/ "
            b"&& echo TREE-$((6*7))\n",
            b"TREE-42",
            300.0,
        )
        assert "kvm-intel.ko.xz" in tree.decode(errors="replace"), tree[-400:]
        await l3_console_command(
            connect_l2_console,
            b"grep -qc vmx /proc/cpuinfo && systemctl is-active -q msks-kvm "
            b"&& test -c /dev/kvm && echo Q1-$((6*7))\n",
            b"Q1-42",
            300.0,
        )
        timeline.mark("l2-console-evidence")

        # The bootstrap: minutes of downloads over nested-virt egress,
        # watched through the seed's own state trail (the appliance
        # smoke's dev-workspace shape). "done" gates on the inner
        # daemon's unit being live; a failed step fails by name.
        async def bootstrap_state() -> bytes:
            return await l3_console_command(
                connect_l2_console,
                b"s=$(cat /root/.msks-bootstrap/state 2>/dev/null); "
                b'echo "STATE:$s"; '
                b'[ "$s" = done ] && echo BOOT-$((6*7)); '
                b"echo E-$((21*2))\n",
                b"E-42",
                60.0,
            )

        deadline = loop.time() + L3_RECURSION_TIMEOUT_S
        last = b""
        while loop.time() < deadline:
            try:
                data = await bootstrap_state()
            except AssertionError as exc:
                # A wedged console round under the bootstrap's own load
                # is the boot's slowness, not a failure — retry under
                # the phase deadline (the bring-up poll rides the same).
                print(
                    f"L3 bootstrap round stalled ({exc}); retrying", flush=True
                )
                await asyncio.sleep(10.0)
                continue
            body = data.split(b"E-$((21*2))", 1)[-1].split(b"E-42", 1)[0]
            last = body.strip()
            print(f"L3 bootstrap state round: {last[-120:]!r}", flush=True)
            # The break gate is a COMPUTED marker the shell emits only
            # when the state file's whole content is exactly "done" —
            # pty noise (bracketed-paste bytes, banners) cannot
            # synthesize it, and the echoed command carries only the
            # unevaluated $((6*7)) form.
            for step in re.findall(rb"STATE:(\S+)", body):
                timeline.step(step.decode(errors="replace"))
            if b"BOOT-42" in body:
                timeline.mark("l2-bootstrap-done")
                break
            if b"no-route" in body:
                raise AssertionError(
                    f"the L3 seed could not find an uplink: {last!r}"
                )
            await asyncio.sleep(15.0)
        else:
            raise AssertionError(
                f"L2 bootstrap never reached done within "
                f"{L3_RECURSION_TIMEOUT_S}s; last state: {last!r}"
            )

        # The inner daemon is up and answering inside L2 (its own
        # health, through the same console).
        await l3_console_command(
            connect_l2_console,
            b"curl -sf http://127.0.0.1:8660/api/v1/health "
            b"&& echo DAEMON-$((6*7))\n",
            b"DAEMON-42",
            120.0,
        )
        timeline.mark("inner-daemon-up")

        # The archive ride: the minted key + the TCP forward + rsync
        # — the documented operator path (docs/networking.md), driven
        # by the host against the appliance's API.
        workdir = Path(scratch.name) / "l3-work"
        workdir.mkdir()
        key = workdir / "l3.key"
        known_hosts = workdir / "known_hosts"
        # Scheme://host:port only: the client builds its own /api/v1
        # paths (rest.py), so a base carrying the suffix doubles it —
        # every CLI call then answers FastAPI's bare "Not Found".
        daemon_url = base.rsplit("/api/v1", 1)[0]
        cli_env = dict(
            os.environ,
            MSKSC_URL=daemon_url,
            MSKSC_TOKEN=token,
        )
        keygen = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "msks.client.cli",
                "key",
                wid,
                "--out",
                str(key),
            ],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert keygen.returncode == 0, keygen.stderr
        forward_port = free_port()
        forward_log = workdir / "forward.log"
        forward_err = open(forward_log, "ab")
        forward_proc = subprocess.Popen(
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
            stderr=forward_err,
        )
        deadline = loop.time() + 30.0
        while loop.time() < deadline:
            log = (
                forward_log.read_text(errors="replace")
                if forward_log.exists()
                else ""
            )
            if f"msks: 127.0.0.1:{forward_port} -> " in log:
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError(
                f"forward never listened on {forward_port}; "
                f"log: {forward_log.read_text(errors='replace')[-800:]}"
            )
        sync = await asyncio.to_thread(
            subprocess.run,
            [
                RSYNC_BIN,
                "-e",
                f"{SSH_BIN} -i {key} -p {forward_port} -F {os.devnull} "
                f"-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                f"-o UserKnownHostsFile={known_hosts} -o BatchMode=yes "
                f"-o ConnectTimeout=20",
                # -S lands the receiver's copy sparse and skips the
                # rootfs's mke2fs zero-seek slack; -z collapses what
                # does cross — the same content moved 26s compressed
                # against 181s sparse-only, live.
                "-aPSz",
                *sorted(str(path) for path in artifacts.values()),
                "root@127.0.0.1:/root/inner-artifacts/",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        assert sync.returncode == 0, (
            f"artifact rsync failed:\n{sync.stdout[-1500:]}\n"
            f"{sync.stderr[-1500:]}"
        )
        timeline.mark("artifacts-rsynced")

        # Inner bring-up, staged as base64 (one console line; #103's
        # corruption can only cost a resend) and LAUNCHED detached:
        # the import is minutes of silent guest work, and a console
        # session would stall-close around it (see l3_setup_launch).
        # The host polls the run.log trail to completion.
        cmdline = json.loads(
            (REPO_ROOT / ".guest" / "guest-manifest.json").read_text()
        )["cmdline"]
        setup_b64 = base64.b64encode(
            l3_inner_setup_script(cmdline).encode()
        ).decode()
        await l3_console_command(
            connect_l2_console,
            f"mkdir -p /root/.msks-l3-inner /root/inner-artifacts "
            f"&& echo {setup_b64} "
            f"| base64 -d > /root/l3-inner-up.sh "
            f"&& echo SETUP-$((6*7))\n".encode(),
            b"SETUP-42",
            300.0,
        )
        # No separate launch step: the poll's own round command starts
        # the idempotent script when its log is missing or empty, so a
        # corrupted launch line — the #103 pty mangling — costs one
        # round, never the phase.
        # The create/start steps build the overlay and home volume
        # inside the L2 (nested I/O), and a console session can wedge
        # silently through them — the daemon's own vsock bring-up
        # window is 120s, so a round's budget must exceed it, and a
        # wedged round is retried under the phase deadline instead of
        # failing the run (the same recovery the review's probe
        # machinery rides on).
        deadline = loop.time() + 1800.0
        last = b""
        while loop.time() < deadline:
            try:
                data = await l3_console_command(
                    connect_l2_console,
                    b"[ -s /root/.msks-l3-inner/run.log ] || "
                    b"nohup sh /root/l3-inner-up.sh "
                    b"> /root/.msks-l3-inner/run.log 2>&1 & sleep 1; "
                    b"tail -n +1 /root/.msks-l3-inner/run.log 2>/dev/null; "
                    b"grep -q ^done-0$ /root/.msks-l3-inner/run.log "
                    b"2>/dev/null "
                    b"&& echo INNER-UP-$((6*7)); "
                    b"echo E-$((21*2))\n",
                    b"E-42",
                    150.0,
                )
            except AssertionError as exc:
                print(
                    f"L3 inner bring-up round stalled ({exc}); retrying",
                    flush=True,
                )
                await asyncio.sleep(10.0)
                continue
            last = (
                data.split(b"E-$((21*2))", 1)[-1].split(b"E-42", 1)[0].strip()
            )
            print(f"L3 inner bring-up round: {last[-160:]!r}", flush=True)
            for step in re.findall(rb"^(create|start|done-\d+)$", last, re.M):
                timeline.step("inner:" + step.decode())
            if b"INNER-UP-42" in last:
                timeline.mark("inner-bringup-done")
                break
            for line in last.splitlines():
                if line.startswith(b"done-") and line != b"done-0":
                    raise AssertionError(
                        f"inner bring-up exited nonzero ({line!r}); "
                        f"log:\n{last.decode(errors='replace')}"
                    )
            await asyncio.sleep(15.0)
        else:
            raise AssertionError(
                f"inner bring-up never finished within 1800s; log:\n"
                f"{last.decode(errors='replace')}"
            )

        # Q2's answer: the inner console, driven from inside L2
        # through the inner daemon's own websocket. An inner guest
        # reaching its prompt is running on nested-in-nested KVM —
        # there is no fallback path. The probe is base64-staged and
        # re-run until it prints its OK line (each run is a fresh
        # inner console session; the boot is slow and the first runs
        # simply time out inside the probe).
        probe_b64 = base64.b64encode(L3_INNER_PROBE.encode()).decode()
        await l3_console_command(
            connect_l2_console,
            f"echo {probe_b64} | base64 -d > /root/inner-probe.py "
            f"&& echo STAGED-$((6*7))\n".encode(),
            b"STAGED-42",
            120.0,
        )
        started = loop.time()
        deadline = started + L3_RECURSION_TIMEOUT_S
        evidence = b""
        while loop.time() < deadline:
            try:
                evidence = await l3_console_command(
                    connect_l2_console,
                    b"/root/msks/.venv/bin/python /root/inner-probe.py; "
                    b"echo PROBE-$((6*7))\n",
                    b"PROBE-42",
                    330.0,
                )
            except AssertionError as exc:
                # A probe round can fail two ways while the inner
                # guest is still booting: its inner console wait
                # times out inside the guest (a traceback lands on
                # the console before the marker), or the outer
                # session stalls first. Both are the boot's own
                # slowness — retry under the recursion's budget.
                print(f"inner probe round: {exc}", flush=True)
                await asyncio.sleep(10.0)
                continue
            if b"INNER-CONSOLE-42-OK" in evidence:
                timeline.mark("inner-console-ok")
                break
            print(
                f"inner probe round without marker: {evidence[-300:]!r}",
                flush=True,
            )
            await asyncio.sleep(10.0)
        else:
            raise AssertionError(
                "the inner workspace console never answered within "
                f"{L3_RECURSION_TIMEOUT_S}s (Q2: vmx did not compose, or "
                "the boot is slower than the budget)"
            )
        elapsed = loop.time() - started
        print(
            f"L3 recursion: inner console answered after {elapsed:.0f}s "
            f"of probing; transcript bytes: {evidence[-400:]!r}",
            flush=True,
        )

        # The tuning record (#82): the inner boot's wall seconds, for
        # the issue's evidence and anyone sizing the timeouts after.
        tuning = await l3_console_command(
            connect_l2_console,
            b"cat /root/.msks-l3-inner/boot-s 2>/dev/null; echo T-$((6*7))\n",
            b"T-42",
            120.0,
        )
        body = tuning.split(b"T-$((6*7))", 1)[-1].split(b"T-42", 1)[0]
        print(
            f"L3 tuning: msks start inner1 -> {body.strip()!r} seconds",
            flush=True,
        )

        # Orderly teardown, inner first: the inner workspace stops
        # through its own daemon, then the L2 workspace and the
        # appliance.
        with contextlib.suppress(AssertionError):
            await l3_console_command(
                connect_l2_console,
                b"MSKSC_URL=http://127.0.0.1:8660 "
                b"MSKSC_TOKEN=$(cat /root/.msks-inner/token) "
                b"/root/msks/.venv/bin/msks stop inner1; "
                b"echo STOP-$((6*7))\n",
                b"STOP-42",
                300.0,
            )
        response = await client.post(
            f"{base}/workspaces/{wid}/stop", headers=headers, timeout=120.0
        )
        assert response.status_code == 200, response.text
        response = await client.delete(
            f"{base}/workspaces/{wid}", headers=headers, timeout=120.0
        )
        assert response.status_code == 200, response.text
    finally:
        if forward_proc is not None:
            forward_proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(forward_proc.wait, 10)
            with contextlib.suppress(Exception):
                forward_proc.kill()
                await asyncio.to_thread(forward_proc.wait, 5)
            forward_err.close()
        with contextlib.suppress(Exception):
            auth = {"authorization": f"Bearer {token}"} if token else {}
            await client.delete(f"{base}/workspaces/{wid}", headers=auth)
        if prior_state_env is None:
            del os.environ["MSKSD_APPLIANCE_STATE"]
        elif prior_state_env != os.environ.get("MSKSD_APPLIANCE_STATE"):
            os.environ["MSKSD_APPLIANCE_STATE"] = prior_state_env
        if prior_cmdline_extra is None:
            del os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"]
        elif prior_cmdline_extra != os.environ.get(
            "MSKS_APPLIANCE_CMDLINE_EXTRA"
        ):
            os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = prior_cmdline_extra
        if prior_mem_mib is None:
            del os.environ["MSKS_APPLIANCE_MEM_MIB"]
        elif prior_mem_mib != os.environ.get("MSKS_APPLIANCE_MEM_MIB"):
            os.environ["MSKS_APPLIANCE_MEM_MIB"] = prior_mem_mib
        down = devenv_task("msks:appliance-down", timeout=300)
        assert down.returncode == 0, (
            f"msks:appliance-down failed:\n{down.stdout}\n{down.stderr}"
        )
        timeline.summary()
        scratch.cleanup()

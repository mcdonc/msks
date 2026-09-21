"""Appliance smoke: the real appliance boots and serves a workspace."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
import websockets
from msks.client.consoleauth import auth_exchange

from test_smoke import (
    APPLIANCE_DIR,
    DEV_BOOTSTRAP_TIMEOUT_S,
    client,
    dev_workspace_seed,
    msks_script,
    needs_appliance,
    read_appliance_journal,
    seed_legacy_state_disk,
)

# --- shared appliance harness ----------------------------------------------


class StepTimer:
    """Live per-phase timings (run with ``-s``): one boundary after
    another names where the suite spends its time — and where it
    died, without re-running to find out."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last = self.started

    def mark(self, label: str) -> None:
        now = time.monotonic()
        print(
            f"[e2e-step] +{now - self.started:8.1f}s "
            f"(+{now - self.last:7.1f}s) {label}",
            flush=True,
        )
        self.last = now


def refuse_if_appliance_running(app_dir) -> None:
    """Refuse to stomp a *running* appliance — including the README's
    documented orphan case (run script dead, VMM still answering on
    api.sock, unreachable by msks-appliance-down)."""
    if not (app_dir / "api.sock").is_socket():
        return
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


@contextlib.contextmanager
def appliance_env(state_disk=None):
    """The process-global knobs a boot needs, restored after: the
    state-disk override (None leaves the appliance dir's own) and the
    nested-KVM console bring-up windows."""
    prior_state = os.environ.get("MSKSD_APPLIANCE_STATE")
    if state_disk is not None:
        os.environ["MSKSD_APPLIANCE_STATE"] = str(state_disk)
    prior_cmdline = os.environ.get("MSKS_APPLIANCE_CMDLINE_EXTRA")
    if not prior_cmdline:
        os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = (
            "msksd.vsock_wait_timeout_s=120 msksd.console_stall_timeout_s=15"
        )
    try:
        yield
    finally:
        if state_disk is not None:
            if prior_state is None:
                del os.environ["MSKSD_APPLIANCE_STATE"]
            else:
                os.environ["MSKSD_APPLIANCE_STATE"] = prior_state
        if prior_cmdline is None:
            del os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"]
        elif prior_cmdline != os.environ.get("MSKS_APPLIANCE_CMDLINE_EXTRA"):
            os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = prior_cmdline


async def await_token(app_dir, timeout_s: float = 120.0) -> str:
    # The up TASK returns when the run script is detached; setup
    # (state-disk copy, token generation) still runs asynchronously
    # — poll for the token instead of assuming it exists.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        token_file = app_dir / "bootstrap-token"
        if token_file.is_file() and token_file.read_text().strip():
            return token_file.read_text().strip()
        await asyncio.sleep(0.5)
    raise AssertionError("appliance bootstrap token never appeared")


async def await_api(app_dir, base, timeout_s: float = 120.0) -> None:
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


async def console_connect(
    base: str, token: str, ws_ctx, workspace_id: str
) -> tuple[websockets.ClientConnection, bytes]:
    """One console session that speaks the #123 challenge.

    A workspace whose identity seed has planted the guest's trust
    store challenges every fresh connection (a respawned console
    helper always does); a raw socket must answer as `msksc` does or
    the session dies on the refusal (#217). `auth_exchange` also
    passes a pre-challenge guest straight through, so this holds
    whichever state the workspace is in. Its return value — the
    session's first relayed bytes, before any caller input — is the
    caller's to fold into its buffer.
    """
    url = (
        base.replace("https://", "wss://")
        + f"/workspaces/{workspace_id}/console?token={token}"
    )
    ws = await websockets.connect(url, ssl=ws_ctx, open_timeout=30)
    origin = base.rsplit("/api/v1", 1)[0]
    lead = await auth_exchange(ws, workspace_id, origin, token, ws_ctx)
    return ws, lead if isinstance(lead, bytes) else b""


# --- appliance smoke (#10) -------------------------------------------------
#
# Boots the real appliance through the devenv supervisor scripts — the
# same path an operator uses — then drives one workspace VM through
# the API served from inside it. Opt-in: it needs /dev/kvm (nested
# virt: the workspace boots inside the appliance VM), the one-time
# host network install (scripts/appliance-host-setup.sh, run once
# as root), and built appliance + guest assets.


@needs_appliance
async def test_appliance_boot_and_workspace() -> None:
    app_dir = APPLIANCE_DIR
    base = "https://192.168.77.2:8660/api/v1"
    wid = f"appliance-{uuid.uuid4().hex[:8]}"

    refuse_if_appliance_running(app_dir)

    # The upgrade-path pin (#101 review, #180): this run boots a
    # FRESH state disk seeded with the pre-#101 legacy layout, so the
    # migration runs for real every time — and the disk itself is
    # built at the pre-#180 8G template size instead of copied from
    # the current one, so the #180 in-place grow (the host's
    # truncate to the template's size, the guest's resize2fs) runs
    # for real too. The dev host's own state disk stays untouched.
    marker_text = f"pre-101 daemon state {uuid.uuid4().hex[:8]}\n"
    legacy_dir = tempfile.TemporaryDirectory(prefix="msks-legacy-state")
    state_disk = Path(legacy_dir.name) / "state.ext4"
    # Sparse 8G file, formatted in place: the same shape the
    # pre-#180 template produced (labeled msks-state, empty staging
    # tree — the guest's state preparation converges the missing
    # var/ directories on the mounted disk).
    grow_test = subprocess.run(
        ["truncate", "-s", "8G", str(state_disk)],
        capture_output=True,
        timeout=120,
    )
    assert grow_test.returncode == 0, (
        f"truncate of the 8G test state disk failed: {grow_test.stderr}"
    )
    grow_test = subprocess.run(
        [
            "mkfs.ext4",
            "-q",
            "-F",
            "-L",
            "msks-state",
            str(state_disk),
        ],
        capture_output=True,
        timeout=300,
    )
    assert grow_test.returncode == 0, (
        f"mkfs of the 8G test state disk failed: {grow_test.stderr}"
    )
    # The pre-made disk the VMM opens O_RDWR needs the write bit
    # (appliance-setup.sh chmods its own copy — this one must
    # match).
    state_disk.chmod(0o644)
    # The EIO aftermath of the filled disk (#180): the superblock
    # carries the error flag, which resize2fs refuses until e2fsck
    # clears it — seed it so the grow path's repair step runs for
    # real. (The kernel preserves the seeded error bit across the
    # boot's own rw mount/umount, so the grown-filesystem assertion
    # below genuinely proves the repair ran.)
    if shutil.which("debugfs"):
        flagged = subprocess.run(
            [
                "debugfs",
                "-w",
                "-R",
                "set_super_value state 2",
                str(state_disk),
            ],
            capture_output=True,
            timeout=120,
        )
        assert flagged.returncode == 0, (
            f"seeding the superblock error state failed: {flagged.stderr}"
        )
    seeded = seed_legacy_state_disk(state_disk, marker_text)
    if not seeded:
        print("debugfs not on PATH; skipping the legacy-state assertions")

    steps = StepTimer()

    def step(label: str) -> None:
        steps.mark(label)

    status = None
    token = headers = None
    up = None
    # The env context sets the state-disk override and the
    # nested-KVM console windows before the up, and restores both on
    # its way out (after the down inside the finally).
    with appliance_env(state_disk):
        try:
            # Inside the guarded region: a failed start still tears the
            # detached appliance down below instead of leaving it running
            # against the temp state disk with mutated env.
            step("launching msks-appliance-up")
            up = msks_script("msks-appliance-up")
            assert up.returncode == 0, (
                f"msks-appliance-up failed:\n{up.stdout}\n{up.stderr}"
            )
            step("appliance-up returned")
            token = await await_token(app_dir)
            headers = {"authorization": f"Bearer {token}"}
            step("bootstrap token ready")
            await await_api(app_dir, base)
            step("api healthy")
            # A bare create (#40): the appliance imported its built-in
            # default image at first boot; the catalog resolves the boot
            # artifacts with nothing else specified.
            response = await client.post(
                f"{base}/workspaces",
                json={"id": wid},
                headers=headers,
            )
            assert response.status_code == 201, response.text
            step("workspace created")
            row = response.json()
            assert row["kernel"].endswith("/kernel"), row
            images = await client.get(f"{base}/images", headers=headers)
            assert images.status_code == 200
            defaults = [i for i in images.json() if i["default"]]
            assert [i["name"] for i in defaults] == ["debian"], images.text
            response = await client.post(
                f"{base}/workspaces/{wid}/start", headers=headers
            )
            assert response.status_code in (200, 202), response.text
            step("workspace start accepted")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 120.0
            while loop.time() < deadline:
                response = await client.get(
                    f"{base}/workspaces/{wid}", headers=headers
                )
                status = response.json().get("status")
                if status == "running":
                    break
                await asyncio.sleep(1.0)
            else:
                raise AssertionError(
                    f"workspace never reached running: {status}"
                )
            step(f"workspace running ({status})")

            # The workspace console (#21): an authenticated byte stream into
            # the VM over the daemon's websocket. Drive one command, read
            # its output back, detach, and require the workspace to keep
            # running afterwards.
            ws_ctx = ssl.create_default_context()
            ws_ctx.check_hostname = False
            ws_ctx.verify_mode = ssl.CERT_NONE
            shell_ws, lead = await console_connect(base, token, ws_ctx, wid)
            async with shell_ws:
                # The marker's rendering differs from the sent bytes, so
                # the step proves OUTPUT flowed — not merely the pty echo.
                await shell_ws.send(b"echo MSKS-$((6*7))-SHELL-SMOKE\n")
                console_got = lead
                console_deadline = loop.time() + 180.0
                while b"MSKS-42-SHELL-SMOKE" not in console_got:
                    if loop.time() >= console_deadline:
                        raise AssertionError(
                            f"console never echoed the marker; "
                            f"got: {console_got!r}"
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
                        message
                        if isinstance(message, bytes)
                        else message.encode()
                    )

            step("console marker echoed; workspace still running")

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
                nonlocal probe_ws, probe_buf
                # The probe path speaks the #123 challenge too (#217): a
                # mid-probe respawn (or the plant landing mid-suite)
                # would otherwise arm a challenge the raw socket cannot
                # answer. The session's first bytes fold into the probe
                # buffer like any other guest output.
                probe_ws, lead = await console_connect(
                    base, token, ws_ctx, wid
                )
                probe_buf += lead
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
                            f"probe session closed ({closed.rcvd}); "
                            "reconnecting",
                            flush=True,
                        )
                        probe_ws = None
                        return probe_buf
                    probe_buf += (
                        message
                        if isinstance(message, bytes)
                        else message.encode()
                    )
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
                        f"probe session closed mid-send ({closed.rcvd}); "
                        "reconnecting",
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
                        raise AssertionError(
                            f"console never showed {marker!r}"
                        )
                    await probe_send(command)
                    await asyncio.sleep(7.0)

            await probe(
                b"NET-42-UP",
                b"ip -4 addr | grep -q 172.31. && echo NET-$((6*7))-UP\n",
            )
            step("NET probe done")
            await probe(
                b"DNS-42-UP",
                b"getent hosts deb.debian.org >/dev/null "
                b"&& echo DNS-$((6*7))-UP\n",
            )
            # Forwarded egress through the NAT'd uplink (#101): a TCP
            # connection the guest initiates must traverse the forward
            # chain and the masquerade — the DHCP and DNS markers above
            # both work without them (DNS relays through the daemon's
            # own socket), so this is the probe that proves the path.
            step("DNS probe done")
            await probe(
                b"TCP-42-UP",
                b"timeout 5 bash -c '</dev/tcp/deb.debian.org/80' "
                b"&& echo TCP-$((6*7))-UP\n",
            )
            if probe_ws is not None:
                await probe_ws.close()
            response = await client.get(
                f"{base}/workspaces/{wid}", headers=headers
            )
            assert response.json().get("status") == "running", response.text

            step("TCP probe done")

            # #36: a killed vsock socat recovers without a workspace
            # restart. The guest's msks-console.service respawns the
            # listener (Restart=always); prove a fresh console connect
            # works after the listener is SIGKILLed. The marker renders
            # differently from the sent bytes, so it proves OUTPUT flowed
            # — not merely the pty echo of the input.
            kill_ws, _ = await console_connect(base, token, ws_ctx, wid)
            async with kill_ws:
                await kill_ws.send(
                    b"systemctl kill --kill-who=main -s SIGKILL "
                    b"msks-console.service\n"
                )
            recovered_at = None
            for attempt in range(30):
                await asyncio.sleep(1.0)
                # Only the connect phase is retriable: a console that
                # connects but never serves the marker fails fast below —
                # TimeoutError is an OSError subclass, so a blanket
                # except here would swallow the marker wait for ~15 min.
                try:
                    # The respawned helper (#36) reads the workspace's
                    # identity state on its way up, so every reconnect
                    # meets the #123 challenge a fresh helper serves —
                    # answer it as msksc does, then demand the marker
                    # (#217: the step measures listener recovery, not
                    # the challenge).
                    recovery_ws, lead = await console_connect(
                        base, token, ws_ctx, wid
                    )
                except OSError, websockets.WebSocketException:
                    continue
                recovered = lead
                try:
                    await recovery_ws.send(b"echo MSKS-$((23*2))-RECOVERED\n")
                    while b"MSKS-46-RECOVERED" not in recovered:
                        message = await asyncio.wait_for(
                            recovery_ws.recv(), 30.0
                        )
                        recovered += (
                            message
                            if isinstance(message, bytes)
                            else message.encode()
                        )
                finally:
                    await recovery_ws.close()
                recovered_at = attempt
                step(f"console recovered (attempt {attempt})")
                break
            assert recovered_at is not None, (
                "console never recovered after the guest socat was killed"
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
            response = await client.get(
                f"{base}/workspaces/{wid}", headers=headers
            )
            assert response.status_code == 404
        finally:
            if headers is not None:
                with contextlib.suppress(Exception):
                    await client.delete(
                        f"{base}/workspaces/{wid}", headers=headers
                    )
            with contextlib.suppress(Exception):
                await client.post(
                    f"{base}/workspaces/{wid}/stop", headers=headers
                )
            # The env context manager owns the restore: a failed
            # teardown assert below cannot leak process-global env
            # into other tests.
            down = msks_script("msks-appliance-down", timeout=300)
            assert down.returncode == 0, (
                f"msks-appliance-down failed:\n{down.stdout}\n{down.stderr}"
            )
        # The state disk itself lives until the post-teardown reads
        # below are done: the journal and the migration asserts read
        # it after the appliance is down.
    assert not (app_dir / "api.sock").exists()
    # The run script's own teardown view of "stopped": the pidfile is
    # gone with the socket, not just the VM beneath it.
    assert not (app_dir / "run.pid").exists()
    # The #180 in-place grow: the disk was built at the pre-#180 8G
    # size, and the run's setup plus the guest's state preparation
    # must have grown BOTH layers — the file to the template's size
    # (the host's truncate) and the ext4 to the device (the guest's
    # resize2fs). dumpe2fs reads the superblock on a mid-transaction
    # filesystem too (the hard-stop case); the block count is the
    # grown fact.
    # The template the grow must match: the Debian build's copy under
    # image/, or — under MSKS_APPLIANCE_BUILD=nixos — the store path
    # the manifest's stateDisk field names (setup grows the run disk
    # to whichever template this appliance dir was built with).
    template = app_dir / "image" / "state.ext4"
    if not template.is_file():
        manifest = app_dir / "image" / "appliance-manifest.json"
        template = Path(json.loads(manifest.read_text())["stateDisk"])
    assert template.is_file(), f"state-disk template missing: {template}"
    assert state_disk.stat().st_size == template.stat().st_size, (
        "the state-disk file was not grown to the template's size"
    )
    grown = subprocess.run(
        ["dumpe2fs", "-h", str(state_disk)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert grown.returncode == 0, (
        f"dumpe2fs could not read the state disk:\n{grown.stderr}"
    )
    blocks = re.search(r"^Block count:\s+(\d+)", grown.stdout, re.MULTILINE)
    assert blocks, f"no block count in dumpe2fs output:\n{grown.stdout}"
    # 40G at 4k blocks — the template's size, not the seeded 8G.
    assert int(blocks.group(1)) == 10485760, (
        "the state-disk filesystem stayed at its seeded size: "
        f"{blocks.group(1)} blocks"
    )
    step("appliance stopped; teardown checked")
    # The journal is the appliance's own story, persisted to the state
    # disk by journald (#92): read it back from the host and require
    # the boot's records to have survived the teardown. Softens to a
    # printed note on hosts without journalctl/debugfs.
    journal = read_appliance_journal(state_disk)
    step("journal read from state disk")
    if journal is None:
        print("journalctl/debugfs not on PATH; skipping journal assertions")
    else:
        assert journal, "state disk carries no journal files"
        joined = "\n".join(journal)
        assert "Started msksd appliance daemon" in joined, (
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
        assert match, (
            f"daemon identity is not the msksd user: {identity[-1]!r}"
        )
        service_uid = int(match.group(1))
        assert service_uid != 0, identity[-1]
        # uid and gid are allocated independently at build time; the
        # ownership assert below must use each, not assume they match.
        gid_match = re.search(r"gid=(\d+)\(msksd\)", identity[-1])
        assert gid_match, f"daemon identity carries no gid: {identity[-1]!r}"
        service_gid = int(gid_match.group(1))
        assert "(kvm)" in identity[-1], (
            f"daemon missed the kvm group: {identity[-1]!r}"
        )
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
                line.split()[-1]
                for line in top.stdout.splitlines()
                if line.split()
            }
            assert "msksd" in names, (
                f"no service-user home on the disk:\n{top.stdout}"
            )
            assert "volumes" not in names and "msks-cert.host" not in names, (
                f"daemon entries still at the state-disk top level:\n"
                f"{top.stdout}"
            )
    legacy_dir.cleanup()


# --- dev-workspace bootstrap (#77), opt-in ---------------------------------


@pytest.mark.timeout(int(DEV_BOOTSTRAP_TIMEOUT_S + 660))
@needs_appliance
@pytest.mark.skipif(
    not os.environ.get("MSKSD_TEST_DEV_BOOTSTRAP"),
    reason=(
        "set MSKSD_TEST_DEV_BOOTSTRAP=1: the #77 seed proves out over "
        "minutes of downloads under nested KVM; the main appliance "
        "e2e covers everything else in ~90s"
    ),
)
async def test_appliance_dev_workspace_bootstrap() -> None:
    """The dev-workspace bootstrap seed (#77) in the product shape.

    A workspace created with egress and the seed provisions the dev
    toolchain over its own NIC — downloads the main e2e's TCP probe
    proved the path for. The seed's state trail (the running step
    name, then done) is polled to completion; nested KVM makes the
    downloads minutes-slow, and the resend loop rides out #103's
    mid-session console corruption (a fresh send supersedes a
    corrupted round). The test carries its own timeout budget —
    boot plus the downloads' own deadline — above the suite's
    30-minute wedged-boot ceiling.
    """
    app_dir = APPLIANCE_DIR
    base = "https://192.168.77.2:8660/api/v1"
    dev_wid = f"appliance-dev-{uuid.uuid4().hex[:8]}"
    refuse_if_appliance_running(app_dir)

    ws_ctx = ssl.create_default_context()
    ws_ctx.check_hostname = False
    ws_ctx.verify_mode = ssl.CERT_NONE
    loop = asyncio.get_running_loop()

    steps = StepTimer()

    def step(label: str) -> None:
        steps.mark(label)

    headers = None
    try:
        with appliance_env():
            step("launching msks-appliance-up")
            up = msks_script("msks-appliance-up")
            assert up.returncode == 0, (
                f"msks-appliance-up failed:\n{up.stdout}\n{up.stderr}"
            )
            token = await await_token(app_dir)
            headers = {"authorization": f"Bearer {token}"}
            await await_api(app_dir, base)
            step("api healthy")
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
                response = await client.get(
                    f"{base}/workspaces/{dev_wid}", headers=headers
                )
                if response.json().get("status") == "running":
                    break
                await asyncio.sleep(1.0)
            else:
                raise AssertionError(
                    f"dev workspace never reached running: {response.text}"
                )
            step("dev workspace running")
            dev_ws, dev_lead = await console_connect(
                base, token, ws_ctx, dev_wid
            )
            async with dev_ws:
                dev_buf = dev_lead

                async def dev_collect(marker: bytes, window: float) -> None:
                    nonlocal dev_buf
                    end = loop.time() + window
                    while marker not in dev_buf and loop.time() < end:
                        try:
                            message = await asyncio.wait_for(
                                dev_ws.recv(), window
                            )
                        except TimeoutError:
                            return
                        dev_buf += (
                            message
                            if isinstance(message, bytes)
                            else message.encode()
                        )

                # The sent line carries no "done" literal, so the
                # marker can only come from the state file's
                # contents. A gap in the trail (console corruption,
                # #103) heals on resend.
                dev_end = loop.time() + DEV_BOOTSTRAP_TIMEOUT_S
                await dev_ws.send(
                    b"cat /root/.msks-bootstrap/state 2>/dev/null; echo E-$?\n"
                )
                while b"done" not in dev_buf:
                    if loop.time() >= dev_end:
                        raise AssertionError(
                            "seed state never reached done inside the "
                            "appliance; last console bytes: "
                            f"{dev_buf[-300:]!r}"
                        )
                    await dev_collect(b"done", 15.0)
                    if b"done" not in dev_buf:
                        await dev_ws.send(
                            b"cat /root/.msks-bootstrap/state "
                            b"2>/dev/null; echo E-$?\n"
                        )
                # Send-then-collect with resend — the same #103
                # ride-out as the state loop: a corrupted round is
                # superseded by a fresh send, so the probe measures
                # the venv, not the console's byte fidelity.
                venv_cmd = (
                    b"test -x /root/msks/.venv/bin/pytest "
                    b"&& echo VENV-$((6*7))\n"
                )
                await dev_ws.send(venv_cmd)
                venv_end = loop.time() + 180.0
                while b"VENV-42" not in dev_buf:
                    if loop.time() >= venv_end:
                        raise AssertionError(
                            "dev venv never materialized; got: "
                            f"{dev_buf[-300:]!r}"
                        )
                    await dev_collect(b"VENV-42", 15.0)
                    if b"VENV-42" not in dev_buf:
                        await dev_ws.send(venv_cmd)
            step("dev seed done; venv present")
            response = await client.post(
                f"{base}/workspaces/{dev_wid}/stop",
                headers=headers,
                timeout=60.0,
            )
            assert response.status_code == 200, response.text
            response = await client.delete(
                f"{base}/workspaces/{dev_wid}", headers=headers, timeout=60.0
            )
            assert response.status_code == 200, response.text
            down = msks_script("msks-appliance-down", timeout=300)
            assert down.returncode == 0, (
                f"msks-appliance-down failed:\n{down.stdout}\n{down.stderr}"
            )
        assert not (app_dir / "api.sock").exists()
    finally:
        if headers is not None:
            with contextlib.suppress(Exception):
                await client.delete(
                    f"{base}/workspaces/{dev_wid}",
                    headers=headers,
                    timeout=60.0,
                )

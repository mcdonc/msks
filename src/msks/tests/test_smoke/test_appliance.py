"""Appliance smoke: the real appliance boots and serves a workspace."""

import asyncio
import contextlib
import os
import re
import ssl
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
import websockets

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

    # Refuse to stomp a *running* appliance — including the README's
    # documented orphan case (run script dead, VMM still answering on
    # api.sock, unreachable by msks-appliance-down).
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
            f"appliance API never became healthy ({last}); "
            f"serial tail:\n{serial}"
        )

    status = None
    token = headers = None
    up = None
    dev_wid = None
    try:
        # Inside the guarded region: a failed start still tears the
        # detached appliance down below instead of leaving it running
        # against the temp state disk with mutated env.
        up = msks_script("msks-appliance-up")
        assert up.returncode == 0, (
            f"msks-appliance-up failed:\n{up.stdout}\n{up.stderr}"
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
        response = await client.post(
            f"{base}/workspaces/{wid}/start", headers=headers
        )
        assert response.status_code in (200, 202), response.text

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
        async with websockets.connect(
            ws_url, ssl=ws_ctx, open_timeout=30
        ) as shell_ws:
            # The marker's rendering differs from the sent bytes, so
            # the step proves OUTPUT flowed — not merely the pty echo.
            await shell_ws.send(b"echo MSKS-$((6*7))-SHELL-SMOKE\n")
            console_got = b""
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
            probe_ws = await websockets.connect(
                ws_url, ssl=ws_ctx, open_timeout=30
            )
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
                probe_buf += (
                    message if isinstance(message, bytes) else message.encode()
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
                    raise AssertionError(f"console never showed {marker!r}")
                await probe_send(command)
                await asyncio.sleep(7.0)

        await probe(
            b"NET-42-UP",
            b"ip -4 addr | grep -q 172.31. && echo NET-$((6*7))-UP\n",
        )
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

        # #36: a killed vsock socat recovers without a workspace
        # restart. The guest's msks-console.service respawns the
        # listener (Restart=always); prove a fresh console connect
        # works after the listener is SIGKILLed. The marker renders
        # differently from the sent bytes, so it proves OUTPUT flowed
        # — not merely the pty echo of the input.
        async with websockets.connect(
            ws_url, ssl=ws_ctx, open_timeout=30
        ) as kill_ws:
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
                        message
                        if isinstance(message, bytes)
                        else message.encode()
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
                        message
                        if isinstance(message, bytes)
                        else message.encode()
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
            venv_cmd = (
                b"test -x /root/msks/.venv/bin/pytest && echo VENV-$((6*7))\n"
            )
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
        elif prior_cmdline_extra != os.environ.get(
            "MSKS_APPLIANCE_CMDLINE_EXTRA"
        ):
            os.environ["MSKS_APPLIANCE_CMDLINE_EXTRA"] = prior_cmdline_extra
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
    template = app_dir / "image" / "state.ext4"
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

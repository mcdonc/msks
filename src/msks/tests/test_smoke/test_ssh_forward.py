"""sshd + rsync through the forward (#110, over #109's transport)."""

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import uvicorn
from msks.app import build_app
from msks.microvm import VmSpec
from msks.server.api import build_api
from msks.settings import (
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)

from test_smoke import (
    CMDLINE,
    INITRD,
    ROOTFS,
    RSYNC_BIN,
    SHUTDOWN_TIMEOUT_S,
    SSH_BIN,
    SSH_CMD_TIMEOUT_S,
    SSH_KEYGEN_BIN,
    VMLINUX,
    await_guest_up,
    collect_failure_evidence,
    default_route_iface,
    free_port,
    needs_egress,
    needs_local,
    needs_ssh_tools,
    run_in_console,
)


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
            uplink=default_route_iface(),
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

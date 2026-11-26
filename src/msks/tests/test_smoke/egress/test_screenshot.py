"""The screenshot pipe end to end (#1194): a real holder, a real
ssh reverse forward, a real guest dial.

The capture program is a stub: a shell script that prints the
``MSKS_SCREENSHOT_WORKSPACE`` and ``MSKS_SCREENSHOT_MONITOR``
variables the pipe hands it, so the same probe that proves the
bytes crossed the guest boundary proves the command's contract
(argv from the config alone, the selector as a variable). No
display server, ImageMagick, or spectacle takes part — the test
runs wherever the smoke suite runs.

The failure paths ride stubs through the same wire: a selector
the helper refuses (``400``), a command that exits nonzero
(``500`` with its stderr), and no holder at all (the guest's
refused connection, before the first holder and after the last).
"""

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
    SHUTDOWN_TIMEOUT_S,
    SSH_BIN,
    SSH_CMD_TIMEOUT_S,
    SSH_KEYGEN_BIN,
    VMLINUX,
    await_guest_up,
    collect_failure_evidence,
    created_id,
    default_route_iface,
    free_port,
    needs_egress,
    needs_local,
    needs_ssh_tools,
    run_in_console,
)

#: The ports the guest dials — the pipe's documented default,
#: and the override the second holder serves on: a fresh port
#: avoids the first holder's lingering bind while proving the
#: ``screenshot_port`` override reaches the guest.
PIPE_PORT = 8662
PIPE_PORT_OVERRIDE = 8663


@needs_egress
@needs_local
@needs_ssh_tools
async def test_local_screenshot_pipe() -> None:
    """The pipe's whole path: create with the operator's key, boot,
    hold the pipe from a real client process, and read the stub
    capture's bytes from inside the guest."""
    nft_tool = os.environ.get("TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("TEST_IP") or shutil.which("ip") or "ip"
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
    wid = f"shot-{uuid.uuid4().hex[:8]}"
    workdir = state_dir / "shot-work"
    workdir.mkdir(parents=True)
    key = workdir / "operator-key"
    known_hosts = workdir / "known_hosts"
    client_state = workdir / "client-state"
    # The stub capture command: the two variables the pipe hands
    # the real command, printed as the payload the guest reads.
    marker = f"PNG-{wid}-M%s"
    stub = workdir / "capture.sh"
    stub.write_text(
        f"#!/bin/sh\nprintf '{marker}' \"$MSKS_SCREENSHOT_MONITOR\"\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    failing = workdir / "capture-fail.sh"
    failing.write_text(
        "#!/bin/sh\necho 'stub capture failure' >&2\nexit 3\n",
        encoding="utf-8",
    )
    failing.chmod(0o755)
    serial_log = state_dir / "vms" / wid / "serial.log"

    # The daemon verifies, never writes, ip_forward (#101); the root
    # harness owns the host's setting for the run and restores it.
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    api_server = None
    api_task = None
    forwards: list[subprocess.Popen] = []
    holders: list[subprocess.Popen] = []
    holder_logs: list[Path] = []

    cli_env = dict(
        os.environ,
        MSKSC_URL=f"http://127.0.0.1:{api_port}",
        MSKSC_TOKEN=token,
        MSKSC_IDENTITY_FILE=str(key),
        MSKSC_CACHE_DIR=str(workdir / "client-cache"),
        MSKSC_STATE_DIR=str(client_state),
    )
    # The holder's listener sockets live under the runtime
    # directory: a test-local one keeps them hermetic and lets the
    # final check assert the SIGTERM path swept them.
    holder_runtime = workdir / "holder-runtime"
    holder_runtime.mkdir()

    def start_forward(port: int) -> None:
        log = workdir / f"forward-{len(forwards)}.log"
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
        return (
            "\n".join(
                f"--- {log.name} ---\n{log.read_text(errors='replace')[-800:]}"
                for log in sorted(workdir.glob("forward-*.log"))
                if log.exists()
            )
            or "(no forward logs)"
        )

    def holder_evidence() -> str:
        return (
            "\n".join(
                f"--- {log.name} ---\n"
                f"{log.read_text(errors='replace')[-1200:]}"
                for log in holder_logs
                if log.exists()
            )
            or "(no holder logs)"
        )

    def start_holder(command: Path, port: int) -> None:
        """One holder subprocess against the test daemon, its stderr
        kept in a file — the pipe's own lines and ssh's diagnostics
        are exactly the evidence a failed probe needs."""
        log = workdir / f"holder-{len(holder_logs)}.log"
        holder_logs.append(log)
        env = dict(
            cli_env,
            MSKSC_SCREENSHOT_COMMAND=str(command),
            MSKSC_SCREENSHOT_PORT=str(port),
            XDG_RUNTIME_DIR=str(holder_runtime),
        )
        holders.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "msks.client.cli",
                    "screenshot",
                    "hold",
                    wid,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=open(log, "ab"),
            )
        )

    async def stop_holders() -> None:
        for proc in holders:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait, 10)
        holders.clear()

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

    async def run_ssh(port: int, command: str) -> subprocess.CompletedProcess:
        # to_thread, never a bare subprocess.run: the API server rides
        # this test's event loop (the #110 harness lesson).
        return await asyncio.to_thread(
            subprocess.run,
            [SSH_BIN, *ssh_opts(port), "root@127.0.0.1", command],
            capture_output=True,
            text=True,
            timeout=SSH_CMD_TIMEOUT_S,
        )

    def guest_probe(pipe_port: int, selector: str) -> str:
        """One probe command for the guest's python: the body on
        success, ``HTTP <code> <body>`` on a refusal, ``FAIL
        <kind>`` on a transport failure."""
        return (
            "python3 -c '"
            "import urllib.request, urllib.error\n"
            "try:\n"
            "    body = urllib.request.urlopen("
            f'"http://127.0.0.1:{pipe_port}/screenshot?{selector}", '
            "timeout=10).read()\n"
            '    print("OK", body.decode())\n'
            "except urllib.error.HTTPError as exc:\n"
            '    print("HTTP", exc.code, exc.read().decode().strip())\n'
            "except Exception as exc:\n"
            '    print("FAIL", type(exc).__name__)\''
        )

    async def await_probe(
        pipe_port: int,
        selector: str,
        want: str,
        timeout_s: float = 180.0,
    ) -> str:
        """Run the guest probe (over the host's forward into the
        guest) until its first line carries ``want`` or the deadline
        passes; the last probe's output is the failure evidence."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last = ""
        while loop.time() < deadline:
            probe = await run_ssh(
                forward_port, guest_probe(pipe_port, selector)
            )
            last = probe.stdout.strip()
            lines = last.splitlines()
            if lines and want in lines[0]:
                return last
            await asyncio.sleep(2.0)
        raise AssertionError(
            f"the guest probe on port {pipe_port} never answered "
            f"{want!r} within {timeout_s}s; last probe:\n{last}\n"
            f"holder logs:\n{holder_evidence()}\n"
            f"forward logs:\n{forward_evidence()}"
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

        keygen = await asyncio.to_thread(
            subprocess.run,
            [
                SSH_KEYGEN_BIN,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(key),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert keygen.returncode == 0, keygen.stderr

        # The real create path: the operator's key plants the
        # identity, egress gives the guest its network, and the
        # named user is the login the holder's ssh dials as.
        created = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "msks.client.cli",
                "create",
                wid,
                "--kernel",
                VMLINUX,
                *(["--initrd", INITRD] if INITRD else []),
                "--rootfs",
                ROOTFS,
                *(["--cmdline", CMDLINE] if CMDLINE else []),
                "--egress",
                "--user",
                "alice",
            ],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert created.returncode == 0, created.stderr
        vm_id = created_id(created)
        serial_log = state_dir / "vms" / vm_id / "serial.log"

        started = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", "start", wid],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert started.returncode == 0, started.stderr
        await await_guest_up(microvm, vm_id, hostname=wid)
        await run_in_console(
            microvm,
            vm_id,
            "cloud-init status --wait",
            "done",
            hostname=wid,
        )
        await run_in_console(
            microvm,
            vm_id,
            "i=0; while [ $i -lt 30 ] "
            "&& ! { systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1; }; "
            "do sleep 1; i=$((i+1)); done; "
            "systemctl is-active msks-wait-address >/dev/null 2>&1 "
            "&& systemctl is-active ssh >/dev/null 2>&1 && echo U-$((6*7))",
            "U-42",
            hostname=wid,
        )

        forward_port = free_port()
        start_forward(forward_port)
        loop = asyncio.get_running_loop()
        needle = f"msks: 127.0.0.1:{forward_port} -> "
        deadline = loop.time() + 30
        while loop.time() < deadline:
            log = sorted(workdir.glob("forward-0.log"))[0]
            if log.exists() and needle in log.read_text(errors="replace"):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(
                f"msks forward never listened on {forward_port}; "
                f"forward logs:\n{forward_evidence()}"
            )
        login = await run_ssh(forward_port, "echo SSH-OK-$((6*7))")
        assert login.returncode == 0, (
            f"{login.stdout}\n{login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )

        # No holder yet: the guest's port is refused, the same
        # refusal an unconfigured client leaves.
        refused = await await_probe(PIPE_PORT, "monitor=0", "FAIL", 30.0)
        assert "URLError" in refused, refused

        # The holder with the stub command: the pipe comes up, and
        # the guest reads the stub's bytes — the workspace name and
        # the monitor number the command received as variables.
        start_holder(stub, PIPE_PORT)
        ok = await await_probe(PIPE_PORT, "monitor=1", "OK")
        assert f"OK PNG-{wid}-M1" in ok, ok

        # A selector the helper refuses: the 400 with the rule.
        bad = await await_probe(PIPE_PORT, "monitor=x", "HTTP 400")
        assert "whole number" in bad, bad

        # A command that fails: the 500 carries its stderr verbatim.
        # A fresh port — the override — sidesteps the first holder's
        # lingering bind and proves the port setting end to end.
        await stop_holders()
        start_holder(failing, PIPE_PORT_OVERRIDE)
        failed = await await_probe(PIPE_PORT_OVERRIDE, "monitor=0", "HTTP 500")
        assert "exited 3" in failed, failed
        assert "stub capture failure" in failed, failed

        # The audit trail: one JSONL line per command run, under
        # the client state root the holder carried.
        history = (client_state / "screenshots.jsonl").read_text().strip()
        statuses = [
            line.split('"status": "')[1].split('"')[0]
            for line in history.splitlines()
        ]
        assert "ok" in statuses, history
        assert "error" in statuses, history
        assert f'"workspace": "{wid}"' in history, history

        # The holder gone: the guest's port no longer serves. The
        # refusal's shape depends on the teardown window — a reset
        # while sshd drops the dying forward's listener, a plain
        # connection error once it is gone — so the probe asserts
        # the answer is a failure, and never which failure.
        await stop_holders()
        gone = await await_probe(PIPE_PORT_OVERRIDE, "monitor=0", "FAIL", 30.0)
        assert gone.splitlines()[0].startswith("FAIL"), gone
        # The SIGTERM answered through the holder's own teardown
        # (#1194): the loop's cancellation ran the pipe's stop, and
        # the runtime directory carries no socket left behind.
        assert list(holder_runtime.glob("shot-*.sock")) == []

        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        await stop_holders()
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

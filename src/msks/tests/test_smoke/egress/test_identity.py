"""Identity smokes: minted, client-minted, and operator-supplied keys."""

import asyncio
import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import uvicorn
from msks.app import build_app
from msks.client import consoleauth
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
    SSH_ADD_BIN,
    SSH_AGENT_BIN,
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
    wid = f"ident-{uuid.uuid4().hex[:8]}"
    # The minted id lands here after the create (#246); the typed
    # name is the pre-create fallback (nothing exists to clean).
    vm_id = wid
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

    async def await_forward_listener(
        port: int, timeout_s: float = 30.0
    ) -> None:
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

    async def cli(
        *args: str, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
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

    async def boot_and_wait(app=None) -> None:
        await await_guest_up(serial_log)
        # The seed's script runs in cloud-init's user-scripts stage
        # (cloud_final); wait for cloud-init to be done before any
        # login or authorized_keys assertion, so the stage's ordering
        # relative to sshd never matters.
        await run_in_console(
            microvm,
            vm_id,
            "cloud-init status --wait",
            "done",
            app=app,
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
            app=app,
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
            "--user",
            "alice",
        )
        assert created.returncode == 0, created.stderr
        # The daemon minted the workspace's id (#246); the artifacts
        # — the serial log among them — key on it, while the name the
        # test typed keeps addressing every API surface.
        vm_id = created_id(created)
        serial_log = state_dir / "vms" / vm_id / "serial.log"

        private_pem = await fetch_key(key)
        assert private_pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")

        # Start via the API, boot, and let the guest say who its keys
        # are for: both authorized_keys files carry the minted line.
        await start_via_api()
        await boot_and_wait(app=app)
        pub = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", "key", wid],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        minted = pub.stdout.strip()
        assert minted.startswith("ssh-ed25519 ") and minted.endswith(
            f"msksd:{vm_id}"
        )
        await run_in_console(
            microvm,
            vm_id,
            f"grep -qxF '{minted}' /root/.ssh/authorized_keys "
            f"&& grep -qxF '{minted}' /home/msks/.ssh/authorized_keys "
            "&& stat -c %a /home/msks/.ssh/authorized_keys "
            f"&& grep -qxF '{minted}' /home/alice/.ssh/authorized_keys "
            '&& [ "$(stat -c %a /home/alice/.ssh/authorized_keys)" = 600 ] '
            "&& id -nG alice | grep -qw wheel "
            '&& [ "$(id -u alice)" -ge 1000 ] '
            "&& echo AK-$((6*7))",
            "AK-42",
            app=app,
        )
        # The operator payload landed beside the identity — the
        # composed document ran whole through cloud-init.
        await run_in_console(
            microvm,
            vm_id,
            "cat /root/payload",
            payload_marker,
            app=app,
        )

        # Logins: root, the image's workspace user, and the
        # workspace's recorded login user (#248 — seeded at first
        # boot by the create's --user), the minted key alone.
        forward_port = free_port()
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        root_login = await run_ssh(forward_port, "root", "echo ROOT-$((6*7))")
        assert root_login.returncode == 0, (
            f"{root_login.stdout}\n{root_login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "ROOT-42" in root_login.stdout, root_login.stdout
        user_login = await run_ssh(
            forward_port, "msks", 'echo "$(whoami)-$((6*7))"'
        )
        assert user_login.returncode == 0, (
            f"{user_login.stdout}\n{user_login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "msks-42" in user_login.stdout, user_login.stdout
        named_login = await run_ssh(
            forward_port, "alice", 'echo "NL-$(whoami)-$((6*7))"'
        )
        assert named_login.returncode == 0, (
            f"{named_login.stdout}\n{named_login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "NL-alice-42" in named_login.stdout, named_login.stdout

        # msks ssh (#112): the same login as one command — identity
        # fetched and served from the transient agent, the forward as
        # ProxyCommand, the workspace's recorded login user (#248:
        # alice, the create's --user) by default and root via -l.
        # -F /dev/null in the passthrough keeps the harness hermetic
        # (the #110 lesson: a host ssh_config can carry options this
        # build rejects); XDG_CACHE_HOME keeps the per-workspace
        # known_hosts inside the workdir.
        ssh_cache = workdir / "ssh-cache"
        ssh_env = dict(cli_env, XDG_CACHE_HOME=str(ssh_cache))

        async def run_msks_ssh(
            *options: str, command: str, env: dict | None = None
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
                env=env or ssh_env,
                capture_output=True,
                text=True,
                timeout=SSH_CMD_TIMEOUT_S,
            )

        sugar_login = await run_msks_ssh(
            command="echo SSHU-$(whoami)-$((6*7))"
        )
        assert sugar_login.returncode == 0, (
            f"{sugar_login.stdout}\n{sugar_login.stderr}"
        )
        assert "SSHU-alice-42" in sugar_login.stdout, sugar_login.stdout
        root_login = await run_msks_ssh(
            "-l", "root", command="echo SSHR-$(id -u)-$((6*7))"
        )
        assert root_login.returncode == 0, (
            f"{root_login.stdout}\n{root_login.stderr}"
        )
        assert "SSHR-0-42" in root_login.stdout, root_login.stdout

        # -A names the operator's agent (#174): a throwaway agent
        # holding a generated key rides the session in, and the
        # guest lists that key and nothing else — stock ssh with an
        # IdentityAgent set would forward the session agent (the
        # workspace identity) instead, so the client rewrites the
        # request onto the operator's socket.
        agent_key = workdir / "agent-key"
        keygen_agent = await asyncio.to_thread(
            subprocess.run,
            [
                SSH_KEYGEN_BIN,
                "-t",
                "ed25519",
                "-N",
                "",
                "-q",
                "-f",
                str(agent_key),
            ],
            capture_output=True,
            timeout=30,
        )
        assert keygen_agent.returncode == 0, keygen_agent.stderr
        agent_up = await asyncio.to_thread(
            subprocess.run,
            [SSH_AGENT_BIN, "-s"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert agent_up.returncode == 0, agent_up.stderr
        agent_vars = {}
        for line in agent_up.stdout.splitlines():
            if line.startswith("SSH_") and "=" in line:
                name, _, value = line.partition("=")
                agent_vars[name] = value.split(";")[0].strip()
        agent_env = dict(ssh_env, **agent_vars)
        try:
            added = await asyncio.to_thread(
                subprocess.run,
                [SSH_ADD_BIN, str(agent_key)],
                env=agent_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert added.returncode == 0, added.stderr
            host_list = await asyncio.to_thread(
                subprocess.run,
                [SSH_ADD_BIN, "-l"],
                env=agent_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            forwarded = await run_msks_ssh(
                "-A",
                command="ssh-add -l",
                env=agent_env,
            )
            assert forwarded.returncode == 0, (
                f"{forwarded.stdout}\n{forwarded.stderr}"
            )
            # The guest's agent serves the operator's key — the one
            # entry, with the host's fingerprint.
            assert forwarded.stdout.strip() == host_list.stdout.strip(), (
                f"guest:\n{forwarded.stdout}\nhost:\n{host_list.stdout}"
            )
        finally:
            if "SSH_AGENT_PID" in agent_vars:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(agent_vars["SSH_AGENT_PID"]), 15)
        # The logins recorded the guest's host key in the msks
        # cache, keyed by the workspace INSTANCE (#246: the minted
        # id — with the #245 stamp as the daemon-side fallback shape
        # only) — pinning the shape that keeps a recreated workspace
        # from refusing its own first-boot keys.
        entries = list((ssh_cache / "msks").glob(f"{vm_id}*/known_hosts"))
        assert entries, (
            f"no instance-keyed known_hosts under {ssh_cache / 'msks'} "
            f"for {vm_id}"
        )
        assert not (ssh_cache / "msks" / wid).exists()

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
        await boot_and_wait(app=app)
        start_forward(forward_port)
        await await_forward_listener(forward_port)
        relogin = await run_ssh(
            forward_port, "msks", 'echo "BACK-$(whoami)-$((6*7))"'
        )
        assert relogin.returncode == 0, (
            f"{relogin.stdout}\n{relogin.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "BACK-msks-42" in relogin.stdout, relogin.stdout

        await microvm.shutdown(vm_id, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(vm_id)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, vm_id, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(vm_id)
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
            await microvm.cleanup(vm_id)
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
    wid = f"cmint-{uuid.uuid4().hex[:8]}"
    # The minted id lands here after the create (#246); the typed
    # name is the pre-create fallback (nothing exists to clean).
    vm_id = wid
    serial_log = state_dir / "vms" / wid / "serial.log"
    workdir = state_dir / "cmint-work"
    workdir.mkdir(parents=True)
    data = workdir / "data"

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
    os.environ["XDG_DATA_HOME"] = str(data)

    async def cli(
        *args: str, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
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
        database — the no-escrow contract, not the API's word for it.
        The row is found by name (#246): the id is minted."""
        with sqlite3.connect(settings.server.db_path) as conn:
            return conn.execute(
                "select ssh_privkey, ssh_pubkey from workspaces "
                "where name = ?",
                (wid,),
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
            "--user",
            "alice",
        )
        assert created.returncode == 0, created.stderr
        # The client mint lands under the minted id (#246).
        vm_id = created_id(created)
        serial_log = state_dir / "vms" / vm_id / "serial.log"
        identity = data / "msks" / vm_id / "identity"
        assert identity.exists()
        assert identity.stat().st_mode & 0o777 == 0o600
        assert identity.read_text().startswith(
            "-----BEGIN OPENSSH PRIVATE KEY-----"
        )

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
        assert "holds no private half" in refused.stderr
        assert "minted on a client" in refused.stderr

        # Boot, let cloud-init plant the key, and confirm the guest's
        # authorized_keys carry the client's line.
        started = await cli("start", wid)
        assert started.returncode == 0, started.stderr
        await await_guest_up(serial_log)
        await run_in_console(
            microvm,
            vm_id,
            "cloud-init status --wait",
            "done",
            app=app,
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
            app=app,
        )
        await run_in_console(
            microvm,
            vm_id,
            f"grep -qxF '{pub}' /root/.ssh/authorized_keys "
            f"&& grep -qxF '{pub}' /home/msks/.ssh/authorized_keys "
            f"&& echo AK-$((6*7))",
            "AK-42",
            app=app,
        )
        # The console challenge (#123): the seed planted the
        # allowed_signers trust store beside authorized_keys, so a
        # console session is challenged — the daemon relays a nonce
        # it cannot answer. Without the client's signature the
        # session is refused: a relayed attacker gets the refusal,
        # not a shell. The signers entry is the principal plus the
        # key's own two fields — an authorized_keys comment is not
        # signers syntax, so the store's line drops it.
        signers_key = " ".join(pub.split()[:2])
        await run_in_console(
            microvm,
            vm_id,
            f"grep -qxF '{vm_id} {signers_key}' "
            "/etc/msks/console.allowed_signers "
            f"&& echo AS-$((6*7))",
            "AS-42",
            app=app,
        )
        attacker_reader, attacker_writer = await microvm.console(
            vm_id, user="root"
        )
        try:
            challenge = await asyncio.wait_for(attacker_reader.readline(), 30)
            assert challenge.startswith(b"AUTH CHALLENGE "), challenge
            # The refusal lands when the guest's own 30s auth clock
            # expires — a clock that started before this one, on a
            # slower guest than this host — so this window outruns
            # it with room for the lag.
            refusal = await asyncio.wait_for(attacker_reader.readline(), 60)
            assert refusal.startswith(b"MSKS ERR auth"), refusal
        finally:
            attacker_writer.close()
            with contextlib.suppress(Exception):
                await attacker_writer.wait_closed()

        # ``msks ssh`` from the local cache alone: the API serves the
        # public half, the private half comes from the file the create
        # wrote, and the login runs as the workspace's recorded login
        # user (#248 — alice, the create's --user).
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
        assert "CMINT-alice-42" in login.stdout, login.stdout

        await microvm.shutdown(vm_id, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(vm_id)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, vm_id, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(vm_id)
        raise
    finally:
        if api_task is not None:
            api_server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(api_task), timeout=10)
        with contextlib.suppress(Exception):
            await microvm.cleanup(vm_id)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        os.environ.pop("XDG_DATA_HOME", None)
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
    wid = f"opkey-{uuid.uuid4().hex[:8]}"
    # The minted id lands here after the create (#246); the typed
    # name is the pre-create fallback (nothing exists to clean).
    vm_id = wid
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
    # The console challenge's answer for an operator-key workspace:
    # the harness signs with the operator's own half (as the
    # operator's agent would).
    operator_signer = consoleauth.console_signer(key_path.read_text())

    async def cli(
        *args: str, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "msks.client.cli", *args],
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def row_halves() -> tuple[str | None, str | None]:
        with sqlite3.connect(settings.server.db_path) as conn:
            return conn.execute(
                "select ssh_privkey, ssh_pubkey from workspaces "
                "where name = ?",
                (wid,),
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
        vm_id = created_id(created)
        serial_log = state_dir / "vms" / vm_id / "serial.log"
        # Nothing was written client-side: the private half stays
        # wherever the operator keeps it (here, the workdir).
        assert not (data / "msks").exists()

        priv, pub = row_halves()
        assert priv is None
        assert pub is not None and pub.endswith(f"msks-client:{wid}")
        assert pub.split()[:2] == supplied.split()[:2]

        served = await cli("key", wid)
        assert served.returncode == 0, served.stderr
        assert served.stdout.strip() == pub
        refused = await cli("key", wid, "--private")
        assert refused.returncode != 0
        assert "holds no private half" in refused.stderr
        assert "supplied from a key you already own" in refused.stderr

        started = await cli("start", wid)
        assert started.returncode == 0, started.stderr
        await await_guest_up(serial_log)
        await run_in_console(
            microvm,
            vm_id,
            "cloud-init status --wait",
            "done",
            app=app,
            signer=operator_signer,
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
            app=app,
            signer=operator_signer,
        )
        await run_in_console(
            microvm,
            vm_id,
            f"grep -qxF '{pub}' /root/.ssh/authorized_keys "
            f"&& grep -qxF '{pub}' /home/msks/.ssh/authorized_keys "
            f"&& echo AK-$((6*7))",
            "AK-42",
            app=app,
            signer=operator_signer,
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
                f"msks forward never listened within 30s; "
                f"logs:\n{forward_evidence()}"
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
            f"{login.stdout}\n{login.stderr}\n"
            f"forward logs:\n{forward_evidence()}"
        )
        assert "OPKEY-msks-42" in login.stdout, login.stdout

        # msks ssh has no half to serve for an operator key: the
        # recovery names both places the half can be.
        sugar = await cli("ssh", wid, "--", "-F", os.devnull, "--", "true")
        assert sugar.returncode != 0
        assert "not on this client" in sugar.stderr
        assert "supplied" in sugar.stderr

        await microvm.shutdown(vm_id, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(vm_id)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, vm_id, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(vm_id)
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
            await microvm.cleanup(vm_id)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)

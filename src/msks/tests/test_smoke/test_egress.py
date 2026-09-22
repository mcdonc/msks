"""Egress smokes: the daemon's own net stack end to end."""

import asyncio
import contextlib
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import uvicorn
from msks.app import build_app
from msks.microvm import VmSpec
from msks.net.alloc import table_name, tap_name
from msks.server.api import build_api
from msks.settings import (
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)

from test_smoke import (
    CMDLINE,
    GIT_BIN,
    GIT_OUT_TIMEOUT_S,
    INITRD,
    ROOTFS,
    SHUTDOWN_TIMEOUT_S,
    SSH_ADD_BIN,
    SSH_AGENT_BIN,
    SSH_BIN,
    SSH_CMD_TIMEOUT_S,
    SSH_KEYGEN_BIN,
    SSHD_BIN,
    VMLINUX,
    await_guest_trail,
    await_guest_up,
    collect_failure_evidence,
    default_route_iface,
    free_port,
    needs_egress,
    needs_git_tools,
    needs_local,
    needs_ssh_tools,
    run_in_console,
    uplink_address,
)

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
            uplink=default_route_iface(),
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
    # deployment ships it as a sysctl); the root harness owns the dev
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
            microvm,
            wid,
            "ip -4 addr | grep 172.31 && echo ADDR-$((6*7))",
            "ADDR-42",
            app=app,
        )
        await run_in_console(
            microvm,
            wid,
            "ip route | grep default",
            "default via 172.31",
            app=app,
        )
        # DNS: through the daemon's forwarder (the offered resolver).
        await run_in_console(
            microvm,
            wid,
            "getent hosts deb.debian.org && echo DNS-$((6*7))",
            "DNS-42",
            app=app,
        )
        # Egress: a TCP connection out through the NAT'd uplink.
        await run_in_console(
            microvm,
            wid,
            "timeout 5 bash -c '</dev/tcp/deb.debian.org/80' "
            "&& echo TCP-$((6*7))",
            "TCP-42",
            app=app,
        )
        # Containment: the tap's input chain lets DHCP and DNS
        # through and nothing else — every host-side service must
        # refuse the guest root's connection attempt.
        await run_in_console(
            microvm,
            wid,
            "G=$(ip route | awk '/default/ {print $3}'); "
            'timeout 3 bash -c "</dev/tcp/$G/8660" 2>/dev/null '
            "&& echo API-$((2+2)) || echo API-$((6*7))",
            "API-42",
            app=app,
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
    drops guest traffic aimed at the host by design (#52 —
    test_local_egress_boot pins the drop), and a hermetic runner
    has no off-host remote to receive the push, so the test pins
    exactly one widening (this workspace's git port) into its own
    ingress chain and removes it after.
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    uplink_iface = default_route_iface()
    uplink_ip = uplink_address()
    # The listen address and the NAT'd uplink iface must agree:
    # uplink_address() picks the default route's source, which on
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
    login_key = (
        workdir / "login_key"
    )  # console-planted; logs in through the forward
    agent_key = (
        workdir / "agent_key"
    )  # the git-out credential: host agent only
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

    async def await_forward_listener(
        port: int, timeout_s: float = 30.0
    ) -> None:
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

    async def wait_sshd(app=None) -> None:
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
            app=app,
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
            [
                nft_tool,
                "-a",
                "list",
                "chain",
                "inet",
                table_name(wid),
                "ingress",
            ],
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
                reader, writer = await asyncio.open_connection(
                    uplink_ip, git_port
                )
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
        await wait_sshd(app=app)
        public = login_key.with_suffix(".pub").read_text().strip()
        await run_in_console(
            microvm,
            wid,
            f"mkdir -p /root/.ssh && chmod 700 /root/.ssh "
            f"&& printf '%s\\n' '{public}' > /root/.ssh/authorized_keys "
            f"&& chmod 600 /root/.ssh/authorized_keys && echo K-$((6*7))",
            "K-42",
            app=app,
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
            app=app,
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
            app=app,
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
            microvm,
            app,
            wid,
            trail_probe,
            b"apt",
            min(90.0, GIT_OUT_TIMEOUT_S),
        )
        await await_guest_trail(
            microvm, app, wid, trail_probe, b"done", GIT_OUT_TIMEOUT_S
        )
        await run_in_console(
            microvm,
            wid,
            "test -s /root/.gitout/remote && echo Z-$((6*7))",
            "Z-42",
            app=app,
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
            app=app,
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
            f"{push.stdout}\n{push.stderr}\n"
            f"forward logs:\n{forward_evidence()}\n"
            f"git sshd log:\n{gitd_log.read_text(errors='replace')[-800:]}"
        )
        assert "P-42" in push.stdout, push.stdout
        # The forwarded agent carried the scratch key: the guest's
        # ssh-add lists it (comment and all).
        await run_in_console(
            microvm,
            wid,
            "grep -q msks-git-cred /root/.gitout/agent-list "
            "&& echo A-$((6*7))",
            "A-42",
            app=app,
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


# --- egress consent (#69) --------------------------------------------------
#
# The consent halves over the real kernel path: the per-VM chain's
# queue gate, the DNS naming layer, and verdict application. The
# decider itself is driven in-process (the WSS/REST decider legs have
# their own unit suites) — what only this smoke can prove is that a
# held SYN really holds, that an allow really releases it, and that
# the naming layer really names.


async def boot_consent_workspace(
    settings: Settings, state_dir: Path, mode: str, allowlist: tuple[str, ...]
):
    """Create + boot one consent workspace; returns the app, spec."""
    app = build_app(settings)
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
        egress_mode=mode,
        egress_allowlist=allowlist,
    )
    await app.state.net.start()
    await app.state.model.create_workspace(spec)
    await app.state.microvm.launch(spec)
    await await_guest_up(serial_log)
    return app, wid, serial_log


async def shutdown_workspace(app, wid: str) -> None:
    microvm = app.state.microvm
    try:
        await microvm.shutdown(wid, timeout_s=60)
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)


async def pending_request(app, wid: str, host: str) -> dict:
    """Poll until a pending hold for one destination lands."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15.0
    while loop.time() < deadline:
        rows = await app.state.model.egress_consent.list_requests(
            wid, decision="pending"
        )
        for row in rows:
            if row["dest_host"] == host:
                return row
        await asyncio.sleep(0.2)
    raise AssertionError(f"no pending consent request for {host!r}")


@needs_egress
@needs_local
async def test_local_egress_consent_interactive() -> None:
    """Hold, prompt, verdict, release — and the fail-closed denials.

    Legs, in order: an allowlisted name connects with no prompt (the
    chain accepts its learned address); an off-list HTTPS connect
    holds until a decider allows it (the prompt names the DNS name,
    not the IP — the naming layer); a denied destination fails fast
    (the RST element); a raw-IP connect prompts with the IP itself;
    and a foreign resolver on :53 drops (the naming-layer lockout).
    """
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
            # A hold that nothing answers expires inside the test's
            # budget, not the kernel's two-minute retransmit.
            consent_timeout_s=20.0,
        ),
        server=ServerSettings(db_path=state_dir / "smoke.db"),
    )
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")
    app = None
    wid = None
    try:
        app, wid, _serial = await boot_consent_workspace(
            settings, state_dir, "interactive", (".deb.debian.org",)
        )
        microvm = app.state.microvm
        engine = app.state.consent
        engine.app.state.deciders.register(1, wid)

        # Allowlisted: no prompt — the resolver learns the address
        # and the chain accepts it.
        await run_in_console(
            microvm,
            wid,
            "timeout 5 bash -c '</dev/tcp/deb.debian.org/80' "
            "&& echo ALLOW-$((6*7))",
            "ALLOW-42",
            app=app,
        )
        rows = await app.state.model.egress_consent.list_requests(wid)
        assert rows == []  # nothing prompted

        # Off-list HTTPS: the SYN holds, the prompt names the name,
        # and an allow releases it.
        console_task = asyncio.create_task(
            run_in_console(
                microvm,
                wid,
                "timeout 25 bash -c '</dev/tcp/example.com/443' "
                "&& echo HOLD-$((6*7))",
                "HOLD-42",
                app=app,
            )
        )
        request = await pending_request(app, wid, "example.com")
        assert request["dest_port"] == 443
        verdict = await engine.resolve(request["id"], "allowed", "smoke", "5m")
        assert verdict["decision"] == "allow"
        await console_task

        # Denied: the RST element answers the retransmit — the
        # connect refuses fast instead of hanging on the timer.
        deny_task = asyncio.create_task(
            run_in_console(
                microvm,
                wid,
                "timeout 15 bash -c '</dev/tcp/example.org/443' "
                "&& echo DENY-$((2+2)) || echo DENY-$((6*7))",
                "DENY-42",
                app=app,
            )
        )
        request = await pending_request(app, wid, "example.org")
        await engine.resolve(request["id"], "denied", "smoke", "once")
        await deny_task

        # A raw-IP connect prompts with the address itself (a
        # Postgres-style destination, no DNS involved).
        raw_task = asyncio.create_task(
            run_in_console(
                microvm,
                wid,
                "timeout 25 bash -c '</dev/tcp/1.1.1.1/443' "
                "&& echo RAW-$((6*7))",
                "RAW-42",
                app=app,
            )
        )
        request = await pending_request(app, wid, "1.1.1.1")
        await engine.resolve(request["id"], "allowed", "smoke", "once")
        await raw_task

        # The naming-layer lockout: a foreign resolver's :53 drops.
        await run_in_console(
            microvm,
            wid,
            "timeout 5 bash -c '</dev/tcp/8.8.8.8/53' "
            "&& echo LOCK-$((2+2)) || echo LOCK-$((6*7))",
            "LOCK-42",
            app=app,
        )
        await shutdown_workspace(app, wid)
    except BaseException:
        if app is not None and wid is not None:
            with contextlib.suppress(Exception):
                await app.state.microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_egress
@needs_local
async def test_local_egress_consent_static() -> None:
    """Static mode: the allowlist resolves and connects; an off-list
    name never resolves (NXDOMAIN — no resolution oracle); the
    denial is recorded for the audit trail."""
    nft_tool = os.environ.get("MSKSD_TEST_NFT") or shutil.which("nft") or "nft"
    ip_tool = os.environ.get("MSKSD_TEST_IP") or shutil.which("ip") or "ip"
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        net=NetSettings(
            enabled=True,
            uplink=default_route_iface(),
            ip_tool=ip_tool,
            nft_tool=nft_tool,
        ),
        server=ServerSettings(db_path=state_dir / "smoke.db"),
    )
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")
    app = None
    wid = None
    try:
        app, wid, _serial = await boot_consent_workspace(
            settings, state_dir, "static", (".deb.debian.org",)
        )
        microvm = app.state.microvm
        await run_in_console(
            microvm,
            wid,
            "timeout 5 bash -c '</dev/tcp/deb.debian.org/80' "
            "&& echo STATIC-$((6*7))",
            "STATIC-42",
            app=app,
        )
        # Off-list: NXDOMAIN (getent finds nothing), and the row
        # records the policy denial.
        await run_in_console(
            microvm,
            wid,
            "getent hosts off-list.example && echo OFF-$((2+2)) "
            "|| echo OFF-$((6*7))",
            "OFF-42",
            app=app,
        )
        rows = await app.state.model.egress_consent.list_requests(wid)
        assert [row["dest_host"] for row in rows] == ["off-list.example"]
        assert rows[0]["decision"] == "denied"
        assert rows[0]["decided_by"] is None  # policy, not a human
        await shutdown_workspace(app, wid)
    except BaseException:
        if app is not None and wid is not None:
            with contextlib.suppress(Exception):
                await app.state.microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)

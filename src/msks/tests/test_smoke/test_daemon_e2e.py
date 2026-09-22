"""Daemon e2e smoke (#234): the full path against the real msksd.

The appliance e2e (deleted with the appliance layer, #232) was the
one suite that booted the daemon as a real process and drove it
through its own wire surfaces. The in-process smokes prove the
subsystems — boot, console, egress — against ``build_app``, but the
daemon's own listener, TLS, startup bootstrap (token, default
image), and process lifecycle sat unproven since. This smoke
restores that coverage against the deployment host's daemon shape:
``msksd --config=none`` raised on env alone, serving TLS it
generated itself, driven by the client's own REST and console
machinery — create, start, console probes, egress probes, stop,
delete, graceful SIGTERM.
"""

import asyncio
import base64
import contextlib
import os
import shutil
import signal
import ssl
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import websockets
from msks.client import consoleauth
from msks.client.console import ws_url

from test_smoke import (
    CONSOLE_ATTEMPTS,
    CONSOLE_TIMEOUT_S,
    GUEST_DIR,
    default_route_iface,
    free_port,
    needs_egress,
)

#: The daemon's startup owns the listener budget: a fresh state dir
#: imports (hashes) the default image archive before uvicorn binds,
#: and a slow disk makes that seconds-to-tens-of-seconds.
HEALTH_TIMEOUT_S = float(
    os.environ.get("MSKSD_TEST_DAEMON_HEALTH_TIMEOUT_S", "180")
)

#: SIGTERM → exit: the graceful path covers the console bridge and
#: the sqlite close; a daemon that hangs on shutdown fails the test.
DAEMON_EXIT_TIMEOUT_S = float(
    os.environ.get("MSKSD_TEST_DAEMON_EXIT_TIMEOUT_S", "90")
)


def daemon_log_tail(state_dir: Path, limit: int = 40) -> str:
    """The daemon's captured output — the failure evidence this
    smoke owns (the in-process harnesses read the app objects
    directly; here the daemon's stderr is all the operator has)."""
    parts = []
    for name in ("daemon.out", "daemon.err"):
        path = state_dir / name
        if path.exists():
            text = path.read_text(errors="replace").splitlines()
            parts.append(f"--- {name} (last {limit}) ---")
            parts.extend(text[-limit:])
    return "\n".join(parts)


async def await_ca(proc: subprocess.Popen, state_dir: Path) -> None:
    """Wait out the daemon's CA birth (TLS material is generated
    into the state dir during startup, before the listener binds)."""
    ca = state_dir / "msks-ca.pem"
    deadline = asyncio.get_running_loop().time() + HEALTH_TIMEOUT_S
    while not ca.exists():
        if proc.poll() is not None:
            raise AssertionError(
                f"msksd exited ({proc.returncode}) before writing its "
                f"CA\n" + daemon_log_tail(state_dir)
            )
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"msksd wrote no CA within {HEALTH_TIMEOUT_S}s\n"
                + daemon_log_tail(state_dir)
            )
        await asyncio.sleep(0.25)


async def await_health(
    client: httpx.AsyncClient, proc: subprocess.Popen, state_dir: Path
) -> None:
    """Poll /health until the daemon answers — or fail with the
    daemon's own words when the process died trying."""

    async def request() -> int | None:
        try:
            response = await client.get("/api/v1/health")
            return response.status_code
        except httpx.HTTPError, ssl.SSLError:
            return None

    deadline = asyncio.get_running_loop().time() + HEALTH_TIMEOUT_S
    while True:
        code = await request()
        if code == 200:
            return
        if proc.poll() is not None:
            raise AssertionError(
                f"msksd exited ({proc.returncode}) before serving\n"
                + daemon_log_tail(state_dir)
            )
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"msksd did not serve /health within {HEALTH_TIMEOUT_S}s\n"
                + daemon_log_tail(state_dir)
            )
        await asyncio.sleep(0.5)


async def console_exec(
    url: str,
    token: str,
    ssl_ctx: ssl.SSLContext,
    workspace_id: str,
    command: str,
    marker: bytes,
) -> None:
    """One console probe through the daemon's websocket, fresh
    session per attempt (#75 — the same rule run_in_console holds).

    The auth is the client's own #123 flow: the challenge's signer
    is fetched over the API, so this exercises REST and websocket
    surfaces together, exactly as ``msks console`` does. Markers are
    guest-computed (``$((6*7))``): the pty echoes the sent command,
    so a marker inside the command text would match the echo and
    pass even when the probe itself found nothing.
    """
    address = ws_url(url, workspace_id, token)
    for _ in range(CONSOLE_ATTEMPTS):
        try:
            async with websockets.connect(
                address, ssl=ssl_ctx, max_size=2**22
            ) as ws:
                lead = await consoleauth.auth_exchange(
                    ws, workspace_id, url, token, ssl_ctx
                )
                if isinstance(lead, str):
                    lead = lead.encode()
                buf = bytes(lead)
                if marker in buf:
                    return
                await ws.send(command.encode() + b"\n")
                deadline = (
                    asyncio.get_running_loop().time() + CONSOLE_TIMEOUT_S
                )
                while asyncio.get_running_loop().time() < deadline:
                    chunk = await asyncio.wait_for(ws.recv(), 10)
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    buf += chunk
                    if marker in buf:
                        return
                raise AssertionError(
                    f"marker {marker!r} not seen within {CONSOLE_TIMEOUT_S}s"
                    f" for {command!r}; console tail: {buf[-2000:]!r}"
                )
        except websockets.ConnectionClosed, TimeoutError, OSError:
            # A wedged session must not fail the probe: the next
            # attempt opens a fresh shell on a further-along boot.
            continue
    raise AssertionError(
        f"console probe failed after {CONSOLE_ATTEMPTS} attempts: {command!r}"
    )


@needs_egress
async def test_daemon_e2e_full_path() -> None:
    """The whole workspace path against a real msksd process.

    The daemon the deployment host runs — env-configured, TLS on its
    own generated CA, bootstrap token, default image imported at
    startup — serves one workspace through every wire surface: the
    REST create/start/stop/delete, the #123-challenge console
    websocket, and the egress stack (DHCP, resolver, NAT, tap
    containment) behind the API. The daemon then exits cleanly on
    SIGTERM: the process lifecycle the NixOS module and the dev
    daemon both depend on.
    """
    archives = sorted(GUEST_DIR.glob("workspace-*.tar"))
    assert archives, (
        f"no workspace-*.tar under {GUEST_DIR} — run msks-build-guest "
        "before this smoke"
    )
    archive = archives[-1]

    state_dir = Path(f"/tmp/msks-daemon-e2e-{uuid.uuid4().hex[:8]}")
    state_dir.mkdir(parents=True)
    # The bootstrap token, minted the way dev-daemon.sh mints one:
    # urlsafe bytes under a private umask.
    token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=\n")
    (state_dir / "bootstrap-token").write_text(token + "\n")
    port = free_port()
    url = f"https://127.0.0.1:{port}"

    # The daemon's environment: env-only config (--config=none), the
    # same variables the dev daemon and the NixOS module's
    # EnvironmentFile carry. PATH passes through so the daemon
    # resolves cloud-hypervisor and the net tools the way the
    # deployment does.
    env = dict(os.environ)
    env.update(
        MSKSD_STATE_DIR=str(state_dir),
        MSKSD_BOOTSTRAP_TOKEN=token,
        MSKSD_HOST="127.0.0.1",
        MSKSD_PORT=str(port),
        MSKSD_EGRESS_ENABLED="true",
        MSKSD_EGRESS_UPLINK=default_route_iface(),
        MSKSD_DEFAULT_IMAGE=str(archive),
    )
    msksd = shutil.which("msksd")
    command = (
        [msksd, "--config=none"]
        if msksd
        else [sys.executable, "-m", "msks.server.main", "--config=none"]
    )
    out_file = open(state_dir / "daemon.out", "wb")  # noqa: SIM115
    err_file = open(state_dir / "daemon.err", "wb")  # noqa: SIM115
    proc = subprocess.Popen(
        command,
        env=env,
        stdout=out_file,
        stderr=err_file,
    )

    # The root harness owns the host's forwarding, exactly as the
    # in-process egress smoke does (#101: the daemon verifies,
    # never writes, ip_forward).
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    client = None
    wid = f"e2e-{uuid.uuid4().hex[:8]}"
    try:
        # TLS material first (the httpx context pins the CA file at
        # client construction, so the file must exist by then), then
        # the listener, then the console's own TLS context.
        await await_ca(proc, state_dir)
        client = httpx.AsyncClient(
            base_url=url,
            verify=ssl.create_default_context(
                cafile=str(state_dir / "msks-ca.pem")
            ),
            timeout=httpx.Timeout(
                connect=10.0, read=180.0, write=10.0, pool=10.0
            ),
            headers={"Authorization": f"Bearer {token}"},
        )
        await await_health(client, proc, state_dir)
        ssl_ctx = ssl.create_default_context(
            cafile=str(state_dir / "msks-ca.pem")
        )

        # Create: the default image (imported at startup) sizes the
        # boot artifacts; egress is the create default.
        response = await client.post("/api/v1/workspaces", json={"id": wid})
        assert response.status_code == 201, response.text
        row = (await client.get(f"/api/v1/workspaces/{wid}")).json()
        assert row["status"] == "created", row

        # Start: the call returns when the boot completes.
        response = await client.post(f"/api/v1/workspaces/{wid}/start")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "running"

        # The #52 probe set, through the daemon's own console
        # websocket: DHCP address, the offered route, the daemon's
        # resolver, NAT'd TCP out, and tap containment.
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "ip -4 addr | grep 172.31 && echo ADDR-$((6*7))",
            b"ADDR-42",
        )
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "ip route | grep default",
            b"default via 172.31",
        )
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "getent hosts deb.debian.org && echo DNS-$((6*7))",
            b"DNS-42",
        )
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "timeout 5 bash -c '</dev/tcp/deb.debian.org/80' "
            "&& echo TCP-$((6*7))",
            b"TCP-42",
        )
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "G=$(ip route | awk '/default/ {print $3}'); "
            'timeout 3 bash -c "</dev/tcp/$G/8660" 2>/dev/null '
            "&& echo API-$((2+2)) || echo API-$((6*7))",
            b"API-42",
        )

        # Stop and delete through the same surface.
        response = await client.post(f"/api/v1/workspaces/{wid}/stop")
        assert response.status_code == 200, response.text
        row = (await client.get(f"/api/v1/workspaces/{wid}")).json()
        assert row["status"] == "stopped", row
        response = await client.delete(f"/api/v1/workspaces/{wid}")
        assert response.status_code == 200, response.text
        listing = (await client.get("/api/v1/workspaces")).json()
        assert all(w["id"] != wid for w in listing), listing
    except BaseException:
        print(daemon_log_tail(state_dir))
        raise
    finally:
        if client is not None:
            await client.aclose()
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        proc.terminate()
        try:
            rc = await asyncio.to_thread(proc.wait, DAEMON_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = await asyncio.to_thread(proc.wait, 30)
            raise AssertionError(
                f"msksd did not exit on SIGTERM within "
                f"{DAEMON_EXIT_TIMEOUT_S}s\n" + daemon_log_tail(state_dir)
            )
        finally:
            out_file.close()
            err_file.close()
        # Uvicorn's graceful SIGTERM ends the process BY the signal
        # (it re-raises the captured signum after the lifespan
        # teardown), so rc == -15 here is the clean shape — the
        # graceful-path proof is the lifespan's own "Application
        # shutdown complete" line, which covers the net stack's
        # teardown and the sqlite close. A daemon that died before
        # serving, or hung and took the kill, fails above instead.
        assert rc in (0, -signal.SIGTERM), (
            f"msksd exited {rc} on SIGTERM (not the graceful path)\n"
            + daemon_log_tail(state_dir)
        )
        err_text = (state_dir / "daemon.err").read_text(errors="replace")
        assert "Application shutdown complete" in err_text, (
            "msksd exited without completing the lifespan shutdown\n"
            + daemon_log_tail(state_dir)
        )
        shutil.rmtree(state_dir, ignore_errors=True)

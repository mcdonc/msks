"""The #433 e2e: nix from the workspace console, as the unprivileged user.

Boots the real daemon against the NixOS image assets (the fold e2e's
shape) and proves the two #433 halves on one workspace:

- ``/etc/nixos`` carries the initial configuration of the host — the
  entry file, the module chain it imports, and the console helper's
  sources at the relative paths the imports need — present the moment
  the workspace is up, before any rebuild ran.
- nix works from the console logged in as the ``msks`` user: the
  session ``NIX_PATH`` carries the rebuild's own search-path entries,
  ``nix-build '<nixpkgs>'`` builds from the baked pin through the
  daemon, the stock ``nix-env -iA nixpkgs.fd`` idiom installs into the
  user's own profile, and ``nix-shell -p`` builds and enters an
  environment, substituting what the shipped store lacks from
  cache.nixos.org through the workspace's egress path.

The rebuild half of #433 — that the shipped configuration evaluates
and activates — is the fold e2e's ground: its unit runs a full
``nixos-rebuild switch`` inside the booted workspace, and this file
rides the same lane beside it. The Debian egress lane collects this
file and skips it: the nix posture is the NixOS image's, and the gate
keys on the NixOS archives being present.
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
import pytest
import websockets
from msks.client import consoleauth
from msks.client.console import ws_url

from test_smoke import (
    CONSOLE_ATTEMPTS,
    GUEST_DIR,
    collect_failure_evidence,
    default_route_iface,
    free_port,
    needs_egress,
)
from test_smoke.egress.test_daemon_e2e import (
    DAEMON_EXIT_TIMEOUT_S,
    await_ca,
    await_health,
    daemon_log_tail,
)

#: The per-probe budget: the nix-shell probe substitutes toolchain
#: paths from cache.nixos.org on the guest's two vCPUs while the
#: fold rebuild (low-weighted, but running) shares them — minutes,
#: not seconds, the same class the fold e2e budgets for.
NIX_PROBE_TIMEOUT_S = float(os.environ.get("TEST_NIX_PROBE_TIMEOUT_S", "480"))

#: The window a session's shell has to answer its liveness ping
#: (#75): a fresh session on a loaded guest reaches its first
#: prompt in seconds-to-tens-of-seconds, and a session still
#: silent past this is a wedge, not a slow shell.
LIVENESS_WINDOW_S = float(os.environ.get("TEST_LIVENESS_WINDOW_S", "90"))

NIXOS_ARCHIVES = sorted(GUEST_DIR.glob("workspace-nixos-*.tar"))


async def console_exec_as(
    url: str,
    token: str,
    ssl_ctx: ssl.SSLContext,
    workspace_id: str,
    command: str,
    marker: bytes,
    user: str,
    timeout_s: float = NIX_PROBE_TIMEOUT_S,
) -> None:
    """One console probe through the daemon's websocket, as a named
    session user — the same #75 fresh-session rule ``console_exec``
    holds, with the user riding the request's query string and a
    per-call budget (a nix command is minutes of guest work, not the
    console's own interactivity window).

    Connection-level hiccups and wedged sessions retry on a fresh
    one. The wedge (#75) needs its own probe: the pty's line
    discipline echoes the sent command even when the shell behind
    it never starts, so the echo proves nothing — each attempt
    first sends a guest-computed liveness ping whose output only a
    reading shell can produce, and silence past that window is a
    dead session retried fresh, not a working command waited on.
    Once the ping answers, the real command runs with every byte
    redirected — silence for minutes is the command working, and
    the per-call budget (not any chunk window) bounds the wait;
    its end fails the probe with the session's tail, and the
    command's own redirected output rides a follow-up probe.
    """
    address = ws_url(url, workspace_id, user=user)
    for _ in range(CONSOLE_ATTEMPTS):
        try:
            async with websockets.connect(
                address,
                additional_headers=[("Authorization", f"Bearer {token}")],
                ssl=ssl_ctx,
                max_size=2**22,
            ) as ws:
                lead = await consoleauth.auth_exchange(
                    ws, workspace_id, url, token, ssl_ctx
                )
                buf = lead.encode() if isinstance(lead, str) else bytes(lead)
                if marker in buf:
                    return
                # The liveness ping: a reading shell answers a
                # builtin instantly, however loaded the guest is —
                # and the echoed command text cannot contain the
                # evaluated marker, so the match is the shell's
                # own output.
                ping = b"ALIVE-" + str(6 * 7).encode()
                await ws.send(b"echo ALIVE-$((6*7))\n")
                loop = asyncio.get_running_loop()
                ping_deadline = loop.time() + LIVENESS_WINDOW_S
                while loop.time() < ping_deadline:
                    try:
                        chunk = await asyncio.wait_for(ws.recv(), 15)
                    except TimeoutError:
                        continue
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    buf += chunk
                    if ping in buf:
                        break
                else:
                    continue  # a wedged session: a fresh one retries
                await ws.send(command.encode() + b"\n")
                deadline = loop.time() + timeout_s
                while loop.time() < deadline:
                    try:
                        chunk = await asyncio.wait_for(ws.recv(), 15)
                    except TimeoutError:
                        continue
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    buf += chunk
                    if marker in buf:
                        return
                raise AssertionError(
                    f"marker {marker!r} not seen within {timeout_s}s "
                    f"for {command!r} as {user}; console tail:\n"
                    + buf[-2000:].decode(errors="replace")
                )
        except websockets.ConnectionClosed, TimeoutError, OSError:
            continue
    raise AssertionError(
        f"console probe failed after {CONSOLE_ATTEMPTS} attempts "
        f"(user {user}): {command!r}"
    )


@needs_egress
@pytest.mark.skipif(
    not NIXOS_ARCHIVES,
    reason=f"no NixOS guest assets under {GUEST_DIR}",
)
async def test_nixos_user_nix_e2e() -> None:
    """One workspace: the shipped /etc/nixos, then nix as msks."""
    state_dir = Path(f"/tmp/msks-nix-e2e-{uuid.uuid4().hex[:8]}")
    state_dir.mkdir(parents=True)
    token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=\n")
    (state_dir / "bootstrap-token").write_text(token + "\n")
    port = free_port()
    url = f"https://127.0.0.1:{port}"

    env = dict(os.environ)
    env.update(
        MSKSD_STATE_DIR=str(state_dir),
        MSKSD_BOOTSTRAP_TOKEN=token,
        MSKSD_HOST="127.0.0.1",
        MSKSD_PORT=str(port),
        MSKSD_EGRESS_ENABLED="true",
        MSKSD_EGRESS_UPLINK=default_route_iface(),
        MSKSD_DEFAULT_IMAGE=str(NIXOS_ARCHIVES[-1]),
    )
    msksd = shutil.which("msksd")
    command = (
        [msksd, "--config=none"]
        if msksd
        else [sys.executable, "-m", "msks.server.main", "--config=none"]
    )
    out_file = open(state_dir / "daemon.out", "wb")  # noqa: SIM115
    err_file = open(state_dir / "daemon.err", "wb")  # noqa: SIM115
    proc = subprocess.Popen(command, env=env, stdout=out_file, stderr=err_file)

    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    client = None
    minted: str | None = None
    wid = f"nix-e2e-{uuid.uuid4().hex[:8]}"
    try:
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

        response = await client.post("/api/v1/workspaces", json={"name": wid})
        assert response.status_code == 201, response.text
        minted = response.json()["id"]
        serial_log = state_dir / "vms" / minted / "serial.log"
        response = await client.post(f"/api/v1/workspaces/{wid}/start")
        assert response.status_code == 200, response.text

        # Item 1, the presence half: the shipped /etc/nixos — the
        # entry, the module chain, the shrinkwrap pair, the console
        # helper's sources at the ../src path the module's relative
        # imports resolve through — on the created workspace's own
        # root, before any rebuild ran. (The activation half — that
        # this configuration evaluates and activates — is the fold
        # e2e's nixos-rebuild switch in the same lane.)
        await console_exec_as(
            url,
            token,
            ssl_ctx,
            wid,
            "test -f /etc/nixos/configuration.nix "
            "&& test -f /etc/nixos/nix/guest-nixos-configuration.nix "
            "&& test -f /etc/nixos/nix/console-helper-pkg.nix "
            "&& test -f /etc/nixos/nix/agent-toolchain.nix "
            "&& test -f /etc/nixos/nix/pi-shrinkwrap-patch.py "
            "&& test -f /etc/nixos/src/console-helper/Cargo.toml "
            "&& echo CFG-$((6*7))",
            b"CFG-42",
            "root",
        )

        # Item 2: the session NIX_PATH carries the search path the
        # image ships — nixpkgs and the rebuild's own entries — so
        # <nixpkgs> resolves in the user's shell, not only in the
        # stripped-env rebuild path nix.conf serves.
        await console_exec_as(
            url,
            token,
            ssl_ctx,
            wid,
            'echo "$NIX_PATH" '
            "| grep -q 'nixpkgs=/nix/var/nix/profiles/per-user/root"
            "/channels/nixos' "
            '&& echo "$NIX_PATH" '
            "| grep -q 'nixos-config=/etc/nixos/configuration.nix' "
            "&& echo NP-$((6*7))",
            b"NP-42",
            "msks",
        )

        # A build from the baked pin through the daemon: fd rides
        # the shipped store, so this is eval-plus-reuse — the store
        # database answering for the closure the image registered.
        await console_exec_as(
            url,
            token,
            ssl_ctx,
            wid,
            "nix-build '<nixpkgs>' -A fd --no-out-link >/tmp/nb.txt 2>&1; "
            "R=$?; [ $R -ne 0 ] && tail -20 /tmp/nb.txt; "
            "echo NB-$R-$((6*7))",
            b"NB-0-42",
            "msks",
        )

        # The stock install idiom, into the user's own profile: the
        # NIX_PATH-derived default expression resolves nixpkgs with
        # no -f, no channel surgery, and the daemon-side profile
        # write lands in the msks user's own per-user profile.
        await console_exec_as(
            url,
            token,
            ssl_ctx,
            wid,
            "nix-env -iA nixpkgs.fd >/tmp/ne.txt 2>&1; "
            "R=$?; [ $R -eq 0 ] && ~/.nix-profile/bin/fd --version "
            "| head -1; [ $R -ne 0 ] && tail -20 /tmp/ne.txt; "
            "echo NE-$R-$((6*7))",
            b"NE-0-42",
            "msks",
        )

        # The shell: builds the env derivation (substituting the
        # toolchain paths the shipped store lacks from
        # cache.nixos.org through the workspace's egress path) and
        # runs the command inside it, all as the unprivileged user.
        await console_exec_as(
            url,
            token,
            ssl_ctx,
            wid,
            "nix-shell -p fd --run 'echo INSIDE-$((6*7))' "
            ">/tmp/ns.txt 2>&1; "
            "R=$?; [ $R -eq 0 ] && tail -1 /tmp/ns.txt; "
            "[ $R -ne 0 ] && tail -20 /tmp/ns.txt; "
            "echo NS-$R-$((6*7))",
            b"NS-0-42",
            "msks",
        )

        response = await client.post(f"/api/v1/workspaces/{wid}/stop")
        assert response.status_code == 200, response.text
        response = await client.delete(f"/api/v1/workspaces/{wid}")
        assert response.status_code == 200, response.text
    except BaseException:
        if minted is not None:
            collect_failure_evidence(state_dir, minted, serial_log)
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
            await asyncio.to_thread(proc.wait, 30)
            raise AssertionError(
                f"msksd did not exit on SIGTERM within "
                f"{DAEMON_EXIT_TIMEOUT_S}s\n" + daemon_log_tail(state_dir)
            )
        finally:
            out_file.close()
            err_file.close()
        assert rc in (0, -signal.SIGTERM), (
            f"msksd exited {rc} on SIGTERM\n" + daemon_log_tail(state_dir)
        )
        shutil.rmtree(state_dir, ignore_errors=True)

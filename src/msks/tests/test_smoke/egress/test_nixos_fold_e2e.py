"""The #427 fold e2e: the NixOS guest rebuilds itself to trust the
workspace's interception CA in the system store.

Boots the real daemon against the NixOS image assets, starts one
egress workspace, and waits for the fold unit — the background
oneshot that runs ``nixos-rebuild switch`` after cloud-init staged
the certificate — to go active. Then curl runs with an **empty
environment**: no ``SSL_CERT_FILE``, no ``NODE_EXTRA_CA_CERTS``, only
the rebuilt system's own bundle — a plain ``ok`` from the probe
service is the proof that the CA lives in the system trust store,
not the exports. A last probe pins the marker contract: the folded
marker holds the staged certificate's hash (the later-boot no-op).

The Debian egress lane collects this file and skips it: its image
has no fold unit (its ``update-ca-certificates`` link completes
trust at first boot already), and the gate keys on the NixOS
archive name.
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
from msks.spec.probe import PROBE_HOST

from test_smoke import (
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
    console_exec,
    daemon_log_tail,
)

#: The fold budget: the in-guest rebuild evaluates nixpkgs on the
#: guest's two vCPUs — minutes, not seconds — and this bounds the
#: wait for the oneshot to go active.
FOLD_TIMEOUT_S = float(os.environ.get("TEST_FOLD_TIMEOUT_S", "480"))

#: Poll cadence for the fold-wait loop below.
FOLD_POLL_S = 5.0

NIXOS_ARCHIVES = sorted(GUEST_DIR.glob("workspace-nixos-*.tar"))


async def dump_fold_journal(
    url: str, token: str, ssl_ctx: ssl.SSLContext, workspace_id: str
) -> None:
    """Print the fold unit's journal tail on a failure path.

    nixos-rebuild's stderr lives in the unit's journal, not on the
    serial console. ``console_exec`` swallows its buffer on a
    marker hit, so the probe asks for a marker the output can
    never contain — the resulting ``AssertionError`` carries the
    last 2000 bytes of pty output, which is the journal tail; a
    dead console prints nothing (the evidence collection in the
    handler below still copies the serial log).
    """
    try:
        await console_exec(
            url,
            token,
            ssl_ctx,
            workspace_id,
            "journalctl -u msks-interceptor-ca --no-pager "
            "-n 40; true; echo J-$((6*7))",
            b"J-42-NEVER",
        )
    except AssertionError as exc:
        print(
            f"fold e2e — msks-interceptor-ca journal tail:\n{exc}",
            flush=True,
        )


@needs_egress
@pytest.mark.skipif(
    not NIXOS_ARCHIVES,
    reason=f"no NixOS guest assets under {GUEST_DIR}",
)
async def test_nixos_fold_e2e_system_trust() -> None:
    """One workspace, one background rebuild: system-store trust."""
    state_dir = Path(f"/tmp/msks-fold-e2e-{uuid.uuid4().hex[:8]}")
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
    wid = f"fold-e2e-{uuid.uuid4().hex[:8]}"
    try:
        await await_ca(proc, state_dir)
        client = httpx.AsyncClient(
            base_url=url,
            verify=ssl.create_default_context(
                cafile=str(state_dir / "msks-ca.pem")
            ),
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=10.0),
            headers={"Authorization": f"Bearer {token}"},
        )
        await await_health(client, proc, state_dir)
        ssl_ctx = ssl.create_default_context(
            cafile=str(state_dir / "msks-ca.pem")
        )

        response = await client.post("/api/v1/workspaces", json={"name": wid})
        assert response.status_code == 201, response.text
        # The daemon mints its own instance id — the artifacts
        # (serial log, vm dir) key on it, while the name this test
        # typed keeps addressing every API surface
        # (test_daemon_e2e's minted pattern).
        minted = response.json()["id"]
        assert minted != wid
        serial_log = state_dir / "vms" / minted / "serial.log"
        response = await client.post(f"/api/v1/workspaces/{wid}/start")
        assert response.status_code == 200, response.text

        # The probe placeholder (#424): the sentinel rides as the
        # raw Basic blob, exactly as the probe e2e sends it.
        probe = await client.get("/api/v1/probe")
        assert probe.status_code == 200, probe.text
        sentinel = probe.json()["sentinel"]

        # The fold itself: cloud-init stages the certificate, the
        # oneshot rebuilds the system (minutes on two vCPUs — the
        # budget above), and the unit goes active. The status rides
        # the computed marker's text (ACT-active-42), so a timeout
        # fails naming the last observed state — activating, or
        # failed, whose serial log the CI evidence step collects.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FOLD_TIMEOUT_S
        while loop.time() < deadline:
            try:
                await console_exec(
                    url,
                    token,
                    ssl_ctx,
                    wid,
                    "A=$(systemctl is-active msks-interceptor-ca); "
                    "echo ACT-$A-$((6*7))",
                    b"ACT-active-42",
                )
                break
            except AssertionError as exc:
                # A failed unit never becomes active on retry —
                # fail now, naming the observed state; the CI
                # evidence step carries the serial log.
                if "ACT-failed-42" in str(exc):
                    await dump_fold_journal(url, token, ssl_ctx, wid)
                    raise AssertionError(
                        "the fold unit failed on the guest — see the "
                        "serial log"
                    ) from exc
                await asyncio.sleep(FOLD_POLL_S)
        else:
            await dump_fold_journal(url, token, ssl_ctx, wid)
            raise AssertionError(
                f"the fold unit never went active within "
                f"{FOLD_TIMEOUT_S}s — see the guest serial log"
            )

        # The system-trust proof: curl with an empty environment —
        # no SSL_CERT_FILE, no NODE_EXTRA_CA_CERTS, no PATH — by
        # absolute path, so the only trust material in play is the
        # rebuilt system's own bundle. `ok` says the CA is in it.
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            f"env -i /run/current-system/sw/bin/curl -sS "
            f'--max-time 30 -H "Authorization: Basic {sentinel}" '
            f"https://{PROBE_HOST}/ | grep -qx ok "
            f"&& echo SYS-$((6*7))",
            b"SYS-42",
        )

        # The marker contract (#427): the marker holds the staged
        # certificate's hash — the later-boot no-op's decision
        # input.
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "S=$(sha256sum /etc/msks/interceptor-ca.crt "
            "| cut -d' ' -f1); "
            "M=$(cat /var/lib/msks/interceptor-ca.folded); "
            '[ "$S" = "$M" ] && echo MK-$((6*7))',
            b"MK-42",
        )

        # The later-boot no-op: a stop/start cycle re-boots the
        # same guest (the marker and the folded profile survive on
        # the overlay), and the unit must go active WITHOUT a new
        # generation — the fold's rebuild left system-2-link; a
        # marker that failed to match would rebuild again and
        # mint system-3-link, minutes of a 2-vCPU boot the no-op
        # exists to spare.
        response = await client.post(f"/api/v1/workspaces/{wid}/stop")
        assert response.status_code == 200, response.text
        response = await client.post(f"/api/v1/workspaces/{wid}/start")
        assert response.status_code == 200, response.text
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "systemctl is-active msks-interceptor-ca "
            ">/dev/null && test -e "
            "/nix/var/nix/profiles/system-2-link "
            "&& test ! -e /nix/var/nix/profiles/system-3-link "
            "&& echo NOOP-$((6*7))",
            b"NOOP-42",
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

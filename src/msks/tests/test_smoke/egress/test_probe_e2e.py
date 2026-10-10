"""The probe endpoint e2e (#424): curl from inside a workspace VM.

The full operator verification against a real msksd process and a
real workspace VM — whichever guest image GUEST_DIR names (the
Debian lane points it at the Debian assets, the NixOS lane at the
NixOS ones): read the probe placeholder the daemon seeded at
first-time startup, then invoke curl from inside the guest against
``https://secretprobe.msks/`` with the sentinel as the raw Basic blob —
and assert the service answers ``ok``. The answer can only exist
if every link worked: the resolver's local answer, the redirect,
the splice and leaf mint, the sentinel→secret swap of the blob,
the upstream dial verified against the probe CA, and the fixed
credential the service checks. A second curl with a bogus blob
asserts the 401.
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


@needs_egress
async def test_probe_e2e_curl_from_the_workspace() -> None:
    """One workspace, one placeholder, one curl: ``ok`` (#424)."""
    archives = sorted(GUEST_DIR.glob("workspace-*.tar"))
    assert archives, (
        f"no workspace-*.tar under {GUEST_DIR} — run msks-build-guest "
        "before this smoke"
    )

    state_dir = Path(f"/tmp/msks-probe-e2e-{uuid.uuid4().hex[:8]}")
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
        MSKSD_DEFAULT_IMAGE=str(archives[-1]),
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
    wid = f"probe-e2e-{uuid.uuid4().hex[:8]}"
    vm_id = wid  # the minted id lands here after the create (#246)
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
        # The daemon minted the id (#246): the vm directory keys on
        # it, the typed name addresses the API.
        vm_id = response.json()["id"]
        response = await client.post(f"/api/v1/workspaces/{wid}/start")
        assert response.status_code == 200, response.text

        # curl first (plain NAT path; nothing intercepts yet): the
        # image may or may not ship it, and the probe needs it. The
        # Debian image installs it on demand; an image with no
        # package manager must already carry it (the NixOS image
        # ships it).
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            "if ! command -v curl >/dev/null 2>&1; then "
            "command -v apt-get >/dev/null 2>&1 "
            "&& apt-get update -qq && apt-get install -y -qq curl; "
            "fi; command -v curl >/dev/null 2>&1 && echo CURL-$((6*7))",
            b"CURL-42",
        )

        # The seeded probe placeholder (#424): the daemon-wide row
        # minted at first-time startup, read back with its sentinel
        # and the recipe. The workspace armed at its attach — the
        # row was live before the boot, and arming is
        # placeholder-driven — so the redirect is already up.
        probe = await client.get("/api/v1/probe")
        assert probe.status_code == 200, probe.text
        recipe = probe.json()
        assert recipe["host"] == PROBE_HOST
        assert recipe["secret"] == "bXNrczptc2tz"
        sentinel = recipe["sentinel"]

        # The name resolves to the tap address the resolver serves
        # (#424) — the address whose 443 the redirect owns.
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            f"getent hosts {PROBE_HOST} && echo NAME-$((6*7))",
            b"NAME-42",
        )

        # The probe itself: the sentinel rides as the raw Basic blob,
        # the swap rewrites it into the credential, the service
        # validates it — `ok` is the whole-chain proof.
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            f"curl -sS --max-time 30 --retry 4 --retry-delay 2 "
            f'--retry-all-errors -H "Authorization: Basic {sentinel}" '
            f"https://{PROBE_HOST}/ | grep -qx ok && echo PROBE-$((6*7))",
            b"PROBE-42",
        )

        # A bogus blob answers 401: the service, not the swap, said
        # no (the swap never matched the padded sentinel).
        await console_exec(
            url,
            token,
            ssl_ctx,
            wid,
            f"C=$(curl -sS --max-time 30 -o /dev/null -w '%{{http_code}}' "
            f'-H "Authorization: Basic {sentinel}XX" '
            f"https://{PROBE_HOST}/) ; echo HTTP-$C-$((6*7))",
            b"HTTP-401-42",
        )

        response = await client.post(f"/api/v1/workspaces/{wid}/stop")
        assert response.status_code == 200, response.text
        response = await client.delete(f"/api/v1/workspaces/{wid}")
        assert response.status_code == 200, response.text
    except BaseException:
        print(daemon_log_tail(state_dir))
        # The evidence collector resolves the vm directory by the
        # id it is keyed on — the typed name would find nothing.
        collect_failure_evidence(state_dir, vm_id)
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

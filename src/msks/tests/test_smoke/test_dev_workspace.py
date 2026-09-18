"""Dev-workspace bootstrap smoke (#77): the seed provisions the toolchain."""

import contextlib
import os
import shutil
import uuid
from pathlib import Path

from msks.app import build_app
from msks.microvm import VmSpec
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
    VMLINUX,
    await_dev_state,
    await_guest_up,
    collect_failure_evidence,
    default_route_iface,
    dev_workspace_seed,
    needs_egress,
    needs_local,
    run_in_console,
)


@needs_egress
@needs_local
async def test_local_dev_workspace_bootstrap() -> None:
    """The dev-workspace loop end to end (#77): a workspace created
    with egress and the scripts/dev-workspace.sh seed bootstraps the
    toolchain over its own NIC at first boot (uv with its own
    Python, the checkout, uv sync), the result survives stop/start
    without re-provisioning, and the `unit-tests` invocation runs
    to completion inside the guest.
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
        ),
        server=ServerSettings(db_path=state_dir / "smoke.db"),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    app.state.model.migrate()
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        # 8 GiB covers the in-guest suite (pytest -n auto across the
        # guest's cores); the bootstrap itself is downloads, not
        # builds. The overlay holds the venv and uv's Python.
        root_mib=20480,
        mem_mib=8192,
        egress=True,
        user_data=dev_workspace_seed(),
    )
    # The daemon verifies, never writes, ip_forward (#101 — the
    # appliance ships it as a sysctl); the root harness owns the dev
    # host's setting for the run and restores what it found.
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    forwarding_was = forwarding.read_text()
    forwarding.write_text("1")

    async def probe(marker_prefix: str, probe_cmd: str, app=None) -> None:
        # Each marker is gated on the probe's exit status so the
        # echoed command text cannot satisfy it (see run_in_console).
        await run_in_console(
            microvm,
            wid,
            f"{probe_cmd} && echo {marker_prefix}-$((6*7))",
            f"{marker_prefix}-42",
            app=app,
        )

    try:
        await app.state.net.start()
        await app.state.model.create_workspace(spec)
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        # First boot: the seed runs in cloud-final; poll its state
        # trail to "done" (each tool's marker gated on its presence).
        await await_dev_state(microvm, app, wid, b"done")
        await probe("UV", "command -v uv")
        await probe(
            "CLONE", "git -C /root/msks rev-parse --is-inside-work-tree"
        )
        await probe("SYNC", "test -x /root/msks/.venv/bin/pytest")

        # Persistence: stop/start keeps the toolchain and the
        # checkout on the overlay; cloud-init does not re-run the
        # seed (the state trail still says the one first-boot run).
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        serial_log.unlink(missing_ok=True)
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        await await_dev_state(microvm, app, wid, b"done")
        # Console-readiness after the reboot plus the persistence
        # proof: the venv survives, and the rerun log does not exist
        # — the seed executed exactly once (cloud-init state rode the
        # overlay), which the vacuous state poll alone cannot show.
        await probe("AGAIN", "test -x /root/msks/.venv/bin/pytest")
        await probe("NORERUN", "test ! -e /root/.msks-bootstrap/rerun.log")

        # Idempotence, the direct way: re-execute the seed verbatim
        # off the read-only cidata disk — every step's guard holds,
        # uv sync re-runs as a fast no-op, and the state trail ends
        # at done again with a zero exit status.
        await run_in_console(
            microvm,
            wid,
            "mkdir -p /mnt/cidata "
            "&& mount -r /dev/vdc /mnt/cidata 2>/dev/null; "
            "sh /mnt/cidata/user-data >/root/.msks-bootstrap/rerun.log 2>&1; "
            "echo R-$?",
            "R-0",
            app=app,
        )
        await await_dev_state(microvm, app, wid, b"done")

        # The suite, inside the guest, the way the `unit-tests` task
        # runs it (the task's exec line, from the venv uv built) —
        # note this is the coverage-gated CI invocation itself
        # (addopts), so a future coverage edge on main reddens this
        # smoke for a reason unrelated to the bootstrap: recognizable,
        # not a bootstrap bug. Launched in the background, then the
        # rc trail polled to done-0. The launch is guarded — a
        # retried round (#103 corruption ate the BG marker while the
        # input still executed) finds rc at running-or-done and
        # reuses the live/finished run instead of relaunching pytest
        # beside it.
        await run_in_console(
            microvm,
            wid,
            "if grep -qE '^(done-|running)' "
            "/root/.msks-bootstrap/unit-tests.rc 2>/dev/null; "
            "then echo BG-$((6*7)); "
            "else echo running >/root/.msks-bootstrap/unit-tests.rc; "
            "nohup sh -c 'cd /root/msks && uv run python -m pytest "
            "src/msks/tests -v -n auto "
            ">/root/.msks-bootstrap/unit-tests.log 2>&1; "
            "echo done-$? >/root/.msks-bootstrap/unit-tests.rc' "
            ">/dev/null 2>&1 & echo BG-$((6*7)); fi",
            "BG-42",
            app=app,
        )
        await await_dev_state(microvm, app, wid, b"done-0")
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        with contextlib.suppress(OSError):
            forwarding.write_text(forwarding_was)
        shutil.rmtree(state_dir, ignore_errors=True)

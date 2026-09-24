"""The baked agent toolchain (#266, #268): every tool runs, in
the booted image.

Both images ship the toolchain and the pin covers both: Debian
stages the official Node tarball plus the shared pins' npm trees
under /usr/local; NixOS rides nixpkgs' Node plus the same pins
through the system profile, with Claude Code's native binary
loader-patched (a stock NixOS ships no usable loader where the
published interpreter points — see nix/agent-toolchain.nix). The
observable outcome is the same either way: every tool answers on
a login PATH, pi's `env node` shebang resolves, the claude
symlink chain reaches a binary that runs, and the
model-discovery extension is planted where accounts copy it.
"""

import contextlib
import shutil
import uuid
from pathlib import Path

from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import (
    Settings,
    VmmSettings,
)

from test_smoke import (
    CMDLINE,
    INITRD,
    ROOTFS,
    SHUTDOWN_TIMEOUT_S,
    VMLINUX,
    await_guest_up,
    collect_failure_evidence,
    needs_local,
    run_in_console,
)


@needs_local
async def test_local_agent_toolchain() -> None:
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
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
        root_mib=2048,
        home_mib=256,
        egress=False,
    )
    try:
        await microvm.launch(spec)
        info = await microvm.info(wid)
        assert info.status.value == "running"
        await await_guest_up(serial_log)
        # The whole toolchain answers, as root. The version checks
        # pin the shared pins (pi, herdr, claude carry the same
        # versions in both images) and Node's major (the Debian
        # image's official tarball and nixpkgs' Node ride the same
        # 22.x line). Guest-computed sentinels, per
        # run_in_console's echo-collision rule.
        await run_in_console(
            microvm,
            wid,
            "node --version | grep -q '^v22\\.' && echo NODE-$((6*7))",
            "NODE-42",
        )
        await run_in_console(
            microvm,
            wid,
            "npm --version >/dev/null && npx --version >/dev/null "
            "&& echo NPX-$((6*7))",
            "NPX-42",
        )
        # pi: the `env node` shebang plus the installed tree —
        # pi --version exits before any extension or model work,
        # so this pins the toolchain, not the proxy posture.
        await run_in_console(
            microvm,
            wid,
            'test "$(pi --version)" = "0.87.1" && echo PI-$((6*7))',
            "PI-42",
        )
        await run_in_console(
            microvm,
            wid,
            'test "$(herdr --version)" = "herdr 0.9.1" && echo HERDR-$((6*7))',
            "HERDR-42",
        )
        # claude: the symlink chain down into the npm tree and, on
        # NixOS, through the loader-patched platform binary.
        await run_in_console(
            microvm,
            wid,
            'test "$(claude --version)" = "2.1.281 (Claude Code)" '
            "&& echo CLAUDE-$((6*7))",
            "CLAUDE-42",
        )
        # The extension: planted for the skeleton (every account
        # the identity seed provisions copies it) and for root —
        # and the delivery link itself: useradd -m copies the
        # skeleton into a fresh home, exactly the path a
        # seed-provisioned login user's extension takes. NixOS
        # populates /etc/skel at boot (tmpfiles) rather than
        # baking it, so this probe pins that machinery too.
        await run_in_console(
            microvm,
            wid,
            "test -f /etc/skel/.pi/agent/extensions/llm-models.ts "
            "&& test -f /root/.pi/agent/extensions/llm-models.ts "
            "&& echo EXT-$((6*7))",
            "EXT-42",
        )
        await run_in_console(
            microvm,
            wid,
            "useradd -m probe "
            "&& test -f /home/probe/.pi/agent/extensions/llm-models.ts "
            "&& echo SKEL-$((6*7))",
            "SKEL-42",
        )
        # The workspace user's PATH carries the toolchain too —
        # the same bins a seed-provisioned login user gets.
        await run_in_console(
            microvm,
            wid,
            "command -v node pi herdr claude >/dev/null "
            "&& echo UPATH-$((6*7))",
            "UPATH-42",
            user="msks",
        )
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
        shutil.rmtree(state_dir, ignore_errors=True)

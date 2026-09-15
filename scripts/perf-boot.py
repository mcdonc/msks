#!/usr/bin/env python3
"""Measure workspace boot: start -> interactive vsock shell.

The clock starts immediately before ``microvm.launch`` (the local
backend's equivalent of ``POST .../start``) and stops when the vsock
console answers with a shell prompt — the same readiness definition
as #37. Per-run breakdown:

- t_vmm          launch() returned (CH spawn + create + boot accepted)
- t_kernel       first serial byte (kernel decompressed and printing)
- t_login        serial shows the login getty (last userspace unit)
- t_console      vsock handshake completed (console service listening)
- t_prompt       the shell rendered its first prompt (interactive)

Guest-internal systemd timings are collected through the vsock shell
(``systemd-analyze``) and printed once per run.

Usage (from the repo root, inside the devenv shell):

    python scripts/perf-boot.py [--runs 3] [--keep]

Runs on the local backend with the built guest assets (.guest/, via
``devenv tasks run msks:build-guest``) and /dev/kvm. Exits nonzero if
any run fails to reach the prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import shutil
import statistics
import sys
import time
import uuid
from pathlib import Path

from msks.app import build_app
from msks.guestassets import load_guest_assets
from msks.microvm import VmSpec
from msks.settings import Settings, VmmSettings

LOGIN_MARKER = "msks-guest login:"
PROMPT_MARKER = b"root@msks-guest"


async def first_serial_byte(serial_log: Path, timeout_s: float = 30.0) -> float:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if serial_log.exists() and serial_log.stat().st_size > 0:
            return loop.time()
        await asyncio.sleep(0.005)
    raise TimeoutError(f"no serial output within {timeout_s}s")


async def wait_marker(path: Path, marker: str, timeout_s: float = 60.0) -> float:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if path.exists() and marker in path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return loop.time()
        await asyncio.sleep(0.01)
    raise TimeoutError(f"{marker!r} never appeared within {timeout_s}s")


async def read_until_prompt(reader, timeout_s: float = 30.0) -> float:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    buf = b""
    while loop.time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
        except TimeoutError:
            chunk = b""
        buf += chunk
        if PROMPT_MARKER in buf:
            return loop.time()
    tail = buf[-400:].decode("utf-8", "replace")
    raise TimeoutError(f"no shell prompt within {timeout_s}s; got: {tail!r}")


async def run_shell_command(reader, writer, command: str, timeout_s: float = 20.0):
    sentinel = f"__PERF_{uuid.uuid4().hex[:8]}__"
    writer.write(f"{command}; echo {sentinel}\n".encode())
    await writer.drain()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    out = b""
    while loop.time() < deadline:
        try:
            out += await asyncio.wait_for(reader.read(4096), timeout=1.0)
        except TimeoutError:
            pass
        # The terminal echo of the typed command contains the
        # sentinel text too; only the executed ``echo`` prints it
        # alone on a line, after the command's own output.
        if any(
            line.strip() == sentinel
            for line in out.decode("utf-8", "replace").splitlines()
        ):
            return out.decode("utf-8", "replace")
    return out.decode("utf-8", "replace") + "\n(timeout)"


def kernel_start_gap(serial_log: Path) -> str | None:
    """The kernel timestamp on the last printk: initrd cost, roughly."""
    try:
        text = serial_log.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    stamps = [ln[1 : ln.index("]")] for ln in text.splitlines() if is_stamp(ln)]
    return stamps[-1].strip() if stamps else None


def is_stamp(line: str) -> bool:
    """A kernel timestamp line: "[    1.234567] ..." (not systemd ANSI)."""
    if not line.startswith("["):
        return False
    digits = line[1:].split("]", 1)[0].strip().replace(".", "")
    return digits.isdigit()


def vmm_rss(pid: int | None) -> int | None:
    """The VMM process's peak resident set, in KiB."""
    if not pid:
        return None
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmHWM"):
            return int(line.split()[1])
    return None


def parse_meminfo(out: str) -> dict:
    """The named /proc/meminfo fields, in KiB, from shell output."""
    kib = {}
    for line in out.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable|AnonPages|Cached):\s+(\d+)", line)
        if match:
            kib[match.group(1)] = int(match.group(2))
    return kib


def record_guest_memory(result: dict, kib: dict) -> None:
    """Fold the KiB fields into the run record, in MiB."""
    if "MemTotal" in kib and "MemAvailable" in kib:
        result["guest_mem_total_mib"] = round(kib["MemTotal"] / 1024)
        result["guest_used_mib"] = round(
            (kib["MemTotal"] - kib["MemAvailable"]) / 1024, 1
        )
    for field in ("AnonPages", "Cached"):
        if field in kib:
            result[f"guest_{field.lower()}_mib"] = round(kib[field] / 1024, 1)


async def collect_guest_memory(reader, writer, result: dict) -> None:
    """Guest-side cost at first boot: what the fresh image's userspace
    holds when the first interactive prompt answers. MemTotal minus
    MemAvailable is the consumption number (available folds in
    reclaimable cache); AnonPages is the anonymous private set."""
    out = await run_shell_command(
        reader,
        writer,
        "grep -E '^(MemTotal|MemAvailable|AnonPages|Cached):' /proc/meminfo",
    )
    record_guest_memory(result, parse_meminfo(out))


async def measure_boot(microvm, spec, serial_log, result: dict) -> tuple:
    """Fill the timing dict; return it with the launch timestamp."""
    t0 = time.perf_counter()
    await microvm.launch(spec)
    result["t_vmm"] = time.perf_counter() - t0
    result["t_kernel"] = (await first_serial_byte(serial_log)) - t0
    # The console is the readiness path (#37 definition) — measured
    # CONCURRENTLY with the serial getty: the vsock console answers
    # long before the login prompt renders.
    login_task = asyncio.create_task(wait_marker(serial_log, LOGIN_MARKER))
    reader, writer = await microvm.console(spec.workspace_id, user="root")
    result["t_console"] = time.perf_counter() - t0
    result["t_prompt"] = (await read_until_prompt(reader)) - t0
    await collect_guest_memory(reader, writer, result)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    try:
        result["t_login"] = (
            await asyncio.wait_for(asyncio.shield(login_task), timeout=30.0)
        ) - t0
    except TimeoutError:
        result["t_login"] = None
    login_task.cancel()
    return result, t0


def setup_run(assets) -> tuple:
    """A fresh state dir, driver, spec, and serial-log path per run."""
    state_dir = Path(f"/tmp/msks-perf-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    wid = f"perf-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=assets.vmlinux,
        rootfs=assets.rootfs,
        initrd=assets.initrd,
        cmdline=assets.cmdline,
        egress=False,
    )
    return app.state.microvm, spec, serial_log, state_dir


async def collect_blame(microvm, wid: str) -> list[str]:
    reader, writer = await microvm.console(wid, user="root")
    blame = await run_shell_command(reader, writer, "systemd-analyze blame | head -12")
    writer.close()
    # Keep timing lines only; the first line is the echoed command
    # prompt, not blame output.
    return [
        line for line in blame.splitlines() if re.match(r"\s*[0-9]+[a-z]+\s+", line)
    ][:12]


def record_memory(result: dict, microvm, wid: str, spec) -> None:
    """Host-side cost: the VMM's resident set vs the guest config."""
    rss = vmm_rss(microvm.driver._pid(wid))
    if rss is not None:
        result["vmm_rss_mib"] = round(rss / 1024, 1)
        result["guest_mem_mib"] = spec.mem_mib


async def teardown(microvm, wid: str, state_dir: Path, keep: bool) -> None:
    try:
        await microvm.kill(wid)
    finally:
        # cleanup() deletes the vm dir (serial log included); keep
        # means the logs are wanted for diagnosis.
        if not keep:
            with contextlib.suppress(Exception):
                await microvm.cleanup(wid)
            shutil.rmtree(state_dir, ignore_errors=True)


async def one_run(assets, keep: bool = False) -> dict:
    microvm, spec, serial_log, state_dir = setup_run(assets)
    wid = spec.workspace_id
    result: dict = {"workspace": wid}
    try:
        result, _t0 = await measure_boot(microvm, spec, serial_log, result)
        result["kernel_last_stamp"] = kernel_start_gap(serial_log)
        record_memory(result, microvm, wid, spec)
        if not keep:
            result["blame"] = await collect_blame(microvm, wid)
    finally:
        await teardown(microvm, wid, state_dir, keep)
    return result


RUN_KEYS = (
    "t_vmm",
    "t_kernel",
    "t_console",
    "t_prompt",
    "t_login",
    "vmm_rss_mib",
    "guest_mem_mib",
)
P50_KEYS = ("t_vmm", "t_kernel", "t_console", "t_prompt")


def print_times(r: dict, keys: tuple) -> None:
    for key in keys:
        if r.get(key) is not None:
            unit = "s" if key.startswith("t_") else " MiB"
            print(f"  {key:<10} {r[key]}{unit}")


def print_extras(r: dict) -> None:
    if r.get("kernel_last_stamp"):
        print(f"  kernel-log  last printk at +{r['kernel_last_stamp']}s")
    if "vmm_rss_mib" in r:
        print(
            f"  memory      vmm rss {r['vmm_rss_mib']} MiB "
            f"(guest configured {r.get('guest_mem_mib')} MiB)"
        )
    if "guest_used_mib" in r:
        print(
            f"  guest mem   {r['guest_used_mib']} MiB used of "
            f"{r.get('guest_mem_total_mib')} MiB "
            f"(anon {r.get('guest_anonpages_mib')}, "
            f"cached {r.get('guest_cached_mib')})"
        )
    for line in r.get("blame", []):
        print(f"  blame| {line}")


def print_run(r: dict) -> None:
    print(f"run: workspace {r['workspace']}")
    print_times(r, RUN_KEYS)
    print_extras(r)
    print()


def print_p50s(runs: list[dict]) -> None:
    for key in P50_KEYS:
        series = [r[key] for r in runs if isinstance(r.get(key), float)]
        if len(series) > 1:
            print(f"p50 {key}: {statistics.median(series):.2f}s")


def report(runs: list[dict]) -> None:
    print(f"\n{len(runs)} run(s) against the local backend\n")
    for r in runs:
        print_run(r)
    print_p50s(runs)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--keep", action="store_true", help="skip systemd-analyze (keep console)"
    )
    args = parser.parse_args()
    assets = load_guest_assets()
    if assets is None:
        print(
            "guest assets not built: devenv tasks run msks:build-guest", file=sys.stderr
        )
        return 2
    runs = []
    for _ in range(args.runs):
        r = await one_run(assets, keep=args.keep)
        runs.append(r)
        print(json.dumps({k: v for k, v in r.items() if k != "blame"}, indent=None))
    report(runs)
    return verdict(runs)


def p50_prompt(runs: list[dict]) -> float | None:
    series = [r["t_prompt"] for r in runs if "t_prompt" in r]
    return statistics.median(series) if series else None


def verdict(runs: list[dict]) -> int:
    """0 when the p50 start->prompt beats the 5s goal (#37)."""
    p50 = p50_prompt(runs)
    if p50 is None:
        return 1
    outcome = "PASS" if p50 < 5 else "FAIL"
    print(f"\nRESULT start->interactive p50 = {p50:.2f}s (goal < 5.00s, {outcome})")
    return 0 if p50 < 5 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

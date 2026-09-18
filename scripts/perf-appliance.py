#!/usr/bin/env python3
"""Measure appliance boot: ``msks:appliance-up`` -> healthy API.

The clock starts when the opt-in up task is invoked and stops when
``GET /api/v1/health`` answers 200 on ``https://192.168.77.2:8660`` —
the appliance's readiness definition (#92): the VM booted, systemd
came up, msksd is serving TLS. Per-run breakdown:

- t_task       the up task returned (run script detached)
- t_token      the bootstrap token file appeared (host setup)
- t_health     first 200 from /health — **the readiness number**

The first run against a fresh state disk is COLD: it formats/copies
state, generates the token, and imports the default image into the
catalog — costs a warm boot never pays. Cold runs are labeled and
excluded from the p50; the comparison posture (old appliance vs new,
#92) is warm boots on the same host.

Usage (from the repo root, inside the devenv shell):

    python scripts/perf-appliance.py [--runs 3] [--fresh]

Needs /dev/kvm and the one-time host network (sudo bash
scripts/appliance-host-setup.sh — the appliance itself starts without
sudo; the up task builds missing artifacts itself). Exits nonzero if
any run fails to reach a healthy API.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / ".appliance"
BASE = "https://192.168.77.2:8660/api/v1"
UP_TIMEOUT_S = 300.0
HEALTH_TIMEOUT_S = 180.0


def devenv_task(task: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
    """Run one devenv task — the appliance's opt-in lifecycle (#141)."""
    return subprocess.run(
        ["bash", "-c", f"devenv tasks run {task}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        env={**os.environ, "DEVENV_TUI": "false"},
    )


def ensure_down(timeout: float = 300.0) -> None:
    """Stop any running appliance; no pidfile is already stopped.

    ``msks:appliance-down`` exits 0 both when it TERMs a live run
    script and when the pidfile is absent (the stopped state this
    helper establishes either way).
    """
    down = devenv_task("msks:appliance-down", timeout=timeout)
    assert down.returncode == 0, (
        f"msks:appliance-down failed:\n{down.stdout}{down.stderr}"
    )


async def poll_health(result: dict, t0: float) -> None:
    """Fill t_health; raise when the API never turns healthy."""
    client = httpx.AsyncClient(verify=False, timeout=2.0)
    last = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HEALTH_TIMEOUT_S
    try:
        while loop.time() < deadline:
            try:
                response = await client.get(f"{BASE}/health")
                last = f"{response.status_code}"
                if response.status_code == 200:
                    result["t_health"] = time.perf_counter() - t0
                    return
            except httpx.HTTPError as exc:
                last = repr(exc)
            await asyncio.sleep(0.1)
        serial = (APP_DIR / "serial.log").read_text(errors="replace")[-1500:]
        raise AssertionError(
            f"appliance API never became healthy ({last}); serial tail:\n{serial}"
        )
    finally:
        await client.aclose()


async def await_token(result: dict, t0: float, timeout_s: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        token = APP_DIR / "bootstrap-token"
        if token.is_file() and token.read_text().strip():
            result["t_token"] = time.perf_counter() - t0
            return
        await asyncio.sleep(0.05)


async def one_run(label: str) -> dict:
    result: dict = {"run": label}
    ensure_down()
    t0 = time.perf_counter()
    up = await asyncio.to_thread(devenv_task, "msks:appliance-up", timeout=UP_TIMEOUT_S)
    assert up.returncode == 0, f"msks:appliance-up failed:\n{up.stdout}{up.stderr}"
    result["t_task"] = time.perf_counter() - t0
    await asyncio.gather(await_token(result, t0), poll_health(result, t0))
    down = devenv_task("msks:appliance-down", timeout=120.0)
    assert down.returncode == 0, (
        f"msks:appliance-down failed:\n{down.stdout}{down.stderr}"
    )
    return result


def print_run(r: dict) -> None:
    parts = [f"run {r['run']}:"]
    for key in ("t_task", "t_token", "t_health"):
        if r.get(key) is not None:
            parts.append(f"{key}={r[key]:.2f}s")
    print("  " + "  ".join(parts), flush=True)


def warm_health_series(runs: list[dict]) -> list[float]:
    """t_health of the warm runs (the comparable posture; #92)."""
    return [r["t_health"] for r in runs if r["run"] != "cold" and "t_health" in r]


def report(runs: list[dict]) -> None:
    print(f"\n{len(runs)} run(s) against the supervised appliance\n")
    for r in runs:
        print_run(r)
    series = warm_health_series(runs)
    if len(series) > 1:
        print(f"\np50 t_health (warm): {statistics.median(series):.2f}s")


def assets_present() -> bool:
    """The built appliance artifacts exist (a clear error if not)."""
    for f in (APP_DIR / "vmlinux", APP_DIR / "rootfs.ext4"):
        if not f.is_file():
            print(
                f"appliance assets missing ({f}) — "
                "devenv tasks run msks:appliance-build",
                file=sys.stderr,
            )
            return False
    return True


def state_disk_path() -> Path:
    """The state disk the run script boots against."""
    return Path(os.environ.get("MSKSD_APPLIANCE_STATE", APP_DIR / "state.ext4"))


def fresh_state_disk() -> None:
    """Delete the state disk so run 1 is a cold boot."""
    with contextlib.suppress(FileNotFoundError):
        state_disk_path().unlink()


def all_healthy(runs: list[dict]) -> bool:
    return all("t_health" in r for r in runs)


def collect_runs(count: int, first: str) -> list[dict]:
    """Boot-and-measure ``count`` times; run 1 carries ``first``."""
    runs = []
    for i in range(count):
        label = first if i == 0 else f"warm-{i + 1}"
        runs.append(asyncio.run(one_run(label)))
    return runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="delete the state disk first (run 1 is then a cold boot)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not assets_present():
        return 2
    if args.fresh:
        fresh_state_disk()
        first = "cold"
    elif not state_disk_path().exists():
        # Without a state disk run 1 pays the cold costs (image import,
        # token generation, staging merge) however it is labeled — say
        # so and keep it out of the warm p50 instead of silently
        # counting a cold run as warm.
        print(
            f"perf-appliance: no state disk at {state_disk_path()}; "
            "run 1 will be a cold boot"
        )
        first = "cold"
    else:
        first = "warm-1"
    runs = collect_runs(args.runs, first)
    report(runs)
    return 0 if all_healthy(runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())

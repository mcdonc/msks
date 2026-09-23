"""The image conformance check (#258).

``msks image check <archive>`` boots an image exactly the way a
workspace boots — same driver, same overlay/home/seed artifacts,
same console path — and verifies the guest contract point by point
(``docs/images.md``, "What a guest must provide"). An image author
runs it on any host with ``/dev/kvm`` before publishing; the daemon
keeps verifying layout only at import, so the check is a
pre-import gate the operator runs, not one the daemon enforces.

This module composes the real app (``msks.app``) rather than a
client-side reimplementation: the point of the check is to exercise
the same machinery a workspace boot exercises. It therefore pulls
server-side composition into any process that imports it — the
client reaches for it only inside ``msks image check`` (a
deliberate deferred import, marked for the gate), so every remote
subcommand's process stays free of the server stack.

The check is read-only toward the host except for three things it
owns outright: a throwaway state dir under ``/tmp`` (removed unless
``--keep``), the workspace VMs it boots inside it, and — in the
``--egress`` pass — the host's ``ip_forward`` sysctl, which it sets
to 1 for the pass and restores to whatever it found.
"""

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .app import App, build_app
from .imagestore import ImageError, ImageRecord, import_archive
from .microvm import VmSpec
from .settings import (
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)

#: The contract points, in report order. Each names one row.
ARCHIVE = "archive"
BOOT = "boot"
CONSOLE = "console"
ROOT_RW = "root-rw"
HOME_LABEL = "home-label"
USER_DATA = "user-data"
ACPI_SHUTDOWN = "acpi-shutdown"
EGRESS_DHCP = "egress-dhcp"

#: Row statuses.
PASS = "pass"
FAIL = "fail"
SKIP = "skip"

#: Every point the core pass reports; the archive-failure path
#: skips them all.
CORE_POINTS = (
    BOOT,
    CONSOLE,
    ROOT_RW,
    HOME_LABEL,
    USER_DATA,
    ACPI_SHUTDOWN,
)

#: The ip_forward sysctl the --egress pass owns for its run (the
#: daemon verifies, never writes, it — #101; a root checker has no
#: deployment to ship it, so the pass flips it like the root smoke
#: harness does and restores what it found).
FORWARDING = Path("/proc/sys/net/ipv4/ip_forward")

#: How long one probe round-trip waits for its marker, and how long
#: a fresh-session retry sleeps between attempts. The guest-computed
#: marker ($((6*7)) -> 42) is the smoke suite's trick: the pty
#: echoes the sent bytes, so a marker that appeared in the command
#: text would match the echo and pass without the command's output.
PROBE_TIMEOUT_S = 30.0
RETRY_SLEEP_S = 2.0


@dataclass(frozen=True)
class CheckResult:
    """One contract point's outcome, operator-readable."""

    name: str
    status: str
    detail: str


def passed(name: str, detail: str) -> CheckResult:
    return CheckResult(name, PASS, detail)


def failed(name: str, exc: BaseException | str) -> CheckResult:
    text = str(exc) if isinstance(exc, str) else str(exc) or type(exc).__name__
    return CheckResult(name, FAIL, text)


def skipped(name: str, reason: str) -> CheckResult:
    return CheckResult(name, SKIP, reason)


def first_failure(results: list[CheckResult]) -> CheckResult | None:
    """The first FAIL row, for the exit summary: a red run names
    the contract point that broke first."""
    return next((row for row in results if row.status == FAIL), None)


def render(results: list[CheckResult]) -> str:
    """One line per contract point, statuses aligned."""
    width = max(len(row.name) for row in results)
    return "\n".join(
        f"{row.status.upper():<4} {row.name:<{width}}  {row.detail}"
        for row in results
    )


async def read_until(
    reader: asyncio.StreamReader, marker: bytes, timeout_s: float
) -> None:
    """Read until the marker's bytes arrive; timeout or close raises.

    The marker is guest-computed, so the pty's echo of the sent
    command cannot satisfy it — only the command's output can.
    """
    deadline = time.monotonic() + timeout_s
    seen = b""
    while marker not in seen:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"marker {marker.decode()!r} never arrived")
        try:
            chunk = await asyncio.wait_for(reader.read(4096), remaining)
        except TimeoutError:
            raise TimeoutError(
                f"marker {marker.decode()!r} never arrived"
            ) from None
        if not chunk:
            raise ConnectionError("console stream closed before the marker")
        seen += chunk


async def probe(
    microvm,
    workspace_id: str,
    command: str,
    marker: str,
    user: str | None,
) -> None:
    """One shell command over the vsock console, marker round-trip.

    A fresh session per call: a console that accepts the connection
    and echoes while the shell behind it stalls (the slow-boot
    shape the smoke harness documents) is retried by the caller's
    loop, never trusted on its echo alone. The per-round-trip
    budget is read off the module at call time so a test (or an
    embedder) can tighten it.
    """
    reader, writer = await microvm.console(workspace_id, user=user)
    try:
        writer.write(command.encode() + b"\n")
        await writer.drain()
        await read_until(reader, marker.encode(), PROBE_TIMEOUT_S)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def await_console(
    microvm,
    workspace_id: str,
    user: str | None,
    deadline_s: float,
) -> float:
    """Wait for a console marker round-trip inside the deadline.

    Returns the elapsed seconds — the boot row's detail. Retries in
    fresh sessions; a VMM that reports dead fails immediately.
    """
    started = time.monotonic()
    last: BaseException = TimeoutError("no attempt was made")
    while time.monotonic() - started < deadline_s:
        info = await microvm.info(workspace_id)
        if info.status.value != "running":
            raise ConnectionError(f"VMM is {info.status.value}, not running")
        try:
            await probe(
                microvm,
                workspace_id,
                f"echo CONF-$(({6 * 7}))",
                "CONF-42",
                user,
            )
            return time.monotonic() - started
        except (OSError, TimeoutError, ConnectionError) as exc:
            last = exc
        await asyncio.sleep(RETRY_SLEEP_S)
    raise TimeoutError(
        f"console never answered within {deadline_s:.0f}s: {last}"
    )


async def await_marker(
    microvm,
    workspace_id: str,
    command: str,
    marker: str,
    user: str | None,
    deadline_s: float,
) -> None:
    """Poll one probe until its marker arrives or the deadline ends.

    cloud-init runs seed payloads in its final stage — after the
    console is already up — so the payload's marker is polled, not
    expected at once.
    """
    started = time.monotonic()
    last: BaseException = TimeoutError("no attempt was made")
    while time.monotonic() - started < deadline_s:
        try:
            await probe(microvm, workspace_id, command, marker, user)
            return
        except (OSError, TimeoutError, ConnectionError) as exc:
            last = exc
        await asyncio.sleep(RETRY_SLEEP_S)
    raise TimeoutError(
        f"{marker!r} never arrived within {deadline_s:.0f}s: {last}"
    )


def console_user(record: ImageRecord) -> str | None:
    """The user the console point connects as.

    Prelude images negotiate the user in-band, so the first declared
    ``console_users`` entry exercises the handshake; legacy images
    serve the raw root shell and ignore the name on the wire.
    """
    if record.console_protocol == "prelude-v1":
        return record.console_users[0] if record.console_users else "root"
    return None


def sanitize(text: str) -> str:
    """One printable line from manifest-derived text.

    The image author controls these strings; the report is the
    tool's whole product, so a forged row (embedded newlines, ANSI
    escapes) must not survive into it.
    """
    line = text.splitlines()[0] if text else ""
    return "".join(ch for ch in line if ch.isprintable())


def console_detail(record: ImageRecord) -> str:
    if record.console_protocol == "prelude-v1":
        return (
            f"prelude-v1 handshake as {sanitize(console_user(record) or '')!r}"
        )
    return "legacy raw root shell"


def probe_user(record: ImageRecord) -> str | None:
    """The user the write/read probes connect as.

    The probes touch root-owned paths (the /root markers, the
    seed's file), so they run over a root console whenever the
    image serves one; an image whose ``console_users`` names only
    other users gets its first declared user instead — the probes
    then report what that account can actually reach. Legacy
    images serve the raw root shell (no user on the wire).
    """
    if record.console_protocol != "prelude-v1":
        return None
    if "root" in record.console_users:
        return "root"
    return console_user(record)


def default_uplink() -> str:
    """The default route's device — the uplink the --egress pass
    NATs behind when the operator named none."""
    route = subprocess.run(
        ["ip", "route", "show", "default"], capture_output=True, text=True
    ).stdout
    parts = route.split()
    for index, part in enumerate(parts):
        if part == "dev":
            return parts[index + 1]
    raise RuntimeError(f"no default route to NAT behind: {route!r}")


class ForwardingGuard:
    """Owns ip_forward for the --egress pass: set to 1, restore after."""

    def __init__(self) -> None:
        self.was: str | None = None

    def __enter__(self) -> ForwardingGuard:
        self.was = FORWARDING.read_text()
        FORWARDING.write_text("1")
        return self

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(OSError):
            FORWARDING.write_text(self.was)


def seed_payload(marker: str) -> str:
    """The user_data script the boot carries: writes its marker to
    /root for the user-data point to read back."""
    return f"#!/bin/sh\necho {marker} > /root/msks-conformance-ud\n"


def vm_spec(
    record: ImageRecord,
    workspace_id: str,
    *,
    user_data: str | None,
    egress: bool = False,
) -> VmSpec:
    """The spec a workspace of this image would boot."""
    return VmSpec(
        workspace_id=workspace_id,
        kernel=record.kernel,
        initrd=record.initrd,
        rootfs=record.rootfs,
        cmdline=record.cmdline,
        root_mib=2048,
        home_mib=256,
        egress=egress,
        user_data=user_data,
    )


async def discard(microvm, workspace_id: str) -> None:
    """Best-effort teardown of one checker VM."""
    with contextlib.suppress(Exception):
        await microvm.kill(workspace_id)
    with contextlib.suppress(Exception):
        await microvm.cleanup(workspace_id)


async def shutdown_row(
    microvm, workspace_id: str, timeout_s: float
) -> CheckResult:
    """The power-button press and its outcome, as one row."""
    try:
        await microvm.shutdown(workspace_id, timeout_s=timeout_s)
        info = await microvm.info(workspace_id)
        if info.status.value in ("stopped", "absent"):
            return passed(
                ACPI_SHUTDOWN, f"clean shutdown within {timeout_s:.0f}s"
            )
        return failed(
            ACPI_SHUTDOWN, f"status {info.status.value} after the press"
        )
    except BaseException as exc:
        return failed(ACPI_SHUTDOWN, exc)


async def boot_rows(
    microvm,
    record: ImageRecord,
    workspace_id: str,
    user: str | None,
    results: list[CheckResult],
    *,
    boot_timeout_s: float,
) -> bool:
    """Wait for the guest to answer the console; report boot and
    console. Returns whether the boot succeeded."""
    try:
        elapsed = await await_console(
            microvm, workspace_id, user, boot_timeout_s
        )
    except BaseException as exc:
        append_failure(results, exc, BOOT, *CORE_POINTS[1:])
        return False
    results.append(
        passed(
            BOOT,
            f"guest answered the console in {elapsed:.1f}s "
            f"(kernel {record.kernel_version or 'unknown'})",
        )
    )
    results.append(passed(CONSOLE, console_detail(record)))
    return True


async def write_markers(
    microvm,
    workspace_id: str,
    user: str | None,
    root_marker: str,
    *,
    boot_timeout_s: float,
) -> tuple[BaseException | None, BaseException | None]:
    """The first-boot write probes, captured instead of raised: a
    broken write reports its own row, the rest of the pass runs on.
    The home probe retries under the boot deadline — /home can still
    be mounting when the console first answers."""
    root_exc: BaseException | None = None
    home_exc: BaseException | None = None
    try:
        await probe(
            microvm,
            workspace_id,
            f"echo {root_marker} > /root/.msks-conformance "
            f"&& echo WROTE-$(({6 * 7}))",
            "WROTE-42",
            user,
        )
    except BaseException as exc:
        root_exc = exc
    try:
        await await_marker(
            microvm,
            workspace_id,
            f'test "$(blkid -s LABEL -o value '
            f'$(findmnt -n -o SOURCE /home))" = msks-home '
            f"&& touch /home/probe "
            f"&& echo HOME-MOUNTED-$(({6 * 7}))",
            "HOME-MOUNTED-42",
            user,
            boot_timeout_s,
        )
    except BaseException as exc:
        home_exc = exc
    return root_exc, home_exc


async def user_data_row(
    microvm,
    workspace_id: str,
    user: str | None,
    ud_marker: str | None,
    results: list[CheckResult],
    *,
    boot_timeout_s: float,
) -> None:
    """The seed point: run only for a provisioner-declaring image."""
    if ud_marker is None:
        results.append(skipped(USER_DATA, "manifest declares no provisioner"))
        return
    try:
        await await_marker(
            microvm,
            workspace_id,
            "cat /root/msks-conformance-ud",
            ud_marker,
            user,
            boot_timeout_s,
        )
        results.append(passed(USER_DATA, "seed payload ran on first boot"))
    except BaseException as exc:
        results.append(failed(USER_DATA, exc))


def verdict_row(
    name: str,
    detail: str,
    write_exc: BaseException | None,
    readback_exc: BaseException | None,
) -> CheckResult:
    """One persistence point's row: the write and the read-back
    both must succeed; the first failure names the stage."""
    if write_exc is not None:
        return failed(name, f"first-boot write failed: {write_exc}")
    if readback_exc is not None:
        return failed(name, f"restart read-back failed: {readback_exc}")
    return passed(name, detail)


async def persistence_rows(
    microvm,
    spec: VmSpec,
    user: str | None,
    root_marker: str,
    root_exc: BaseException | None,
    home_exc: BaseException | None,
    results: list[CheckResult],
    *,
    boot_timeout_s: float,
) -> None:
    """The stop/start leg on the SAME workspace: the root write
    (overlay) and the /home volume must survive the cycle."""
    try:
        await microvm.launch(spec)
        await await_console(microvm, spec.workspace_id, user, boot_timeout_s)
    except BaseException as exc:
        results.append(failed(ROOT_RW, f"second boot failed: {exc}"))
        results.append(failed(HOME_LABEL, f"second boot failed: {exc}"))
        return
    root_read: BaseException | None = None
    home_read: BaseException | None = None
    try:
        await await_marker(
            microvm,
            spec.workspace_id,
            "cat /root/.msks-conformance",
            root_marker,
            user,
            boot_timeout_s,
        )
    except BaseException as exc:
        root_read = exc
    try:
        await await_marker(
            microvm,
            spec.workspace_id,
            "test -f /home/probe && echo HOME-$((6*7))",
            "HOME-42",
            user,
            boot_timeout_s,
        )
    except BaseException as exc:
        home_read = exc
    results.append(
        verdict_row(
            ROOT_RW,
            "root is writable and the write survived a stop/start (overlay)",
            root_exc,
            root_read,
        )
    )
    results.append(
        verdict_row(
            HOME_LABEL,
            "/home mounted by label msks-home and its write survived "
            "a stop/start (volume)",
            home_exc,
            home_read,
        )
    )


async def run_core_pass(
    app: App,
    record: ImageRecord,
    state_dir: Path,
    results: list[CheckResult],
    *,
    boot_timeout_s: float,
    shutdown_timeout_s: float,
) -> None:
    """Boot the image with no NIC and walk the boot-time contract.

    One seeded workspace carries the whole pass: the first boot
    proves boot/console/writability and (when the image declares a
    provisioner) the seed, then the same workspace stops and starts
    again to prove the overlay and home volume persist. It lives in
    the throwaway state dir and is discarded with it.
    """
    microvm = app.state.microvm
    user = console_user(record)
    probes = probe_user(record)
    ud_marker = (
        f"UD-{uuid.uuid4().hex[:8]}"
        if record.provisioner == "cloud-init"
        else None
    )
    spec = vm_spec(
        record,
        f"conf-{uuid.uuid4().hex[:8]}",
        user_data=seed_payload(ud_marker) if ud_marker else None,
    )
    root_marker = f"ROOT-{uuid.uuid4().hex[:8]}"
    try:
        try:
            await microvm.launch(spec)
        except BaseException as exc:
            append_failure(results, exc, BOOT)
            return
        if not await boot_rows(
            microvm,
            record,
            spec.workspace_id,
            user,
            results,
            boot_timeout_s=boot_timeout_s,
        ):
            return
        root_exc, home_exc = await write_markers(
            microvm,
            spec.workspace_id,
            probes,
            root_marker,
            boot_timeout_s=boot_timeout_s,
        )
        await user_data_row(
            microvm,
            spec.workspace_id,
            probes,
            ud_marker,
            results,
            boot_timeout_s=boot_timeout_s,
        )
        results.append(
            await shutdown_row(microvm, spec.workspace_id, shutdown_timeout_s)
        )
        await persistence_rows(
            microvm,
            spec,
            probes,
            root_marker,
            root_exc,
            home_exc,
            results,
            boot_timeout_s=boot_timeout_s,
        )
    finally:
        await discard(microvm, spec.workspace_id)


def append_failure(
    results: list[CheckResult], exc: BaseException, *names: str
) -> None:
    """One failure row (first name) plus skips for the rest."""
    results.append(failed(names[0], exc))
    for name in names[1:]:
        results.append(skipped(name, f"unrunnable: {names[0]} failed"))


async def run_egress_pass(
    app: App,
    record: ImageRecord,
    state_dir: Path,
    results: list[CheckResult],
    *,
    boot_timeout_s: float,
) -> None:
    """Boot with a NIC through the daemon's net stack; the guest
    must take a global address over DHCP."""
    microvm = app.state.microvm
    user = probe_user(record)
    spec = vm_spec(
        record,
        f"conf-eg-{uuid.uuid4().hex[:8]}",
        user_data=None,
        egress=True,
    )
    try:
        app.state.model.migrate()
        await app.state.model.create_workspace(spec)
        with ForwardingGuard():
            await app.state.net.start()
            try:
                await microvm.launch(spec)
                await await_console(
                    microvm, spec.workspace_id, user, boot_timeout_s
                )
                await probe(
                    microvm,
                    spec.workspace_id,
                    f"ip -4 -o addr show scope global | grep -q . "
                    f"&& echo ADDR-$(({6 * 7}))",
                    "ADDR-42",
                    user,
                )
                results.append(
                    passed(
                        EGRESS_DHCP,
                        "guest took a global address over DHCP",
                    )
                )
            finally:
                await discard(microvm, spec.workspace_id)
    except BaseException as exc:
        append_failure(results, exc, EGRESS_DHCP)
    finally:
        with contextlib.suppress(Exception):
            await app.state.net.stop()


def settings_for(
    state_dir: Path, record: ImageRecord, egress: bool, uplink: str | None
) -> Settings:
    """The settings the throwaway app runs under. The uplink probe
    runs only for the egress pass — a core-only check must run on
    hosts with no route (and no iproute2) at all."""
    net = (
        NetSettings(enabled=True, uplink=uplink or default_uplink())
        if egress
        else NetSettings()
    )
    return Settings(
        vmm=VmmSettings(
            state_dir=state_dir,
            vsock_shell_port=record.vsock_shell_port,
        ),
        server=ServerSettings(db_path=state_dir / "conf.db"),
        net=net,
    )


def archive_failure_rows(
    archive: Path, state_dir: Path, exc: BaseException, egress: bool
) -> list[CheckResult]:
    """The archive-failure report: one FAIL row, everything else
    skipped.

    The import stages a private copy first; the report names the
    operator's archive, with the staging name — not its full
    throwaway path — as the detail.
    """
    text = str(exc) or type(exc).__name__
    first = text.splitlines()[0]
    first = first.replace(f"{state_dir}/images/", "")
    # A tarfile complaint opens a parenthetical it finishes on later
    # lines; the row keeps the named cause, not the dangling half.
    first = first.partition(" (")[0]
    rows = [failed(ARCHIVE, f"{archive}: {first}")]
    rows.extend(
        skipped(name, "unrunnable: archive failed")
        for name in CORE_POINTS + ((EGRESS_DHCP,) if egress else ())
    )
    return rows


async def egress_tail(
    app: App,
    record: ImageRecord,
    state_dir: Path,
    results: list[CheckResult],
    *,
    boot_timeout_s: float,
) -> None:
    """The --egress leg's routing: it runs only behind a green core
    boot, and reports a skip row otherwise."""
    if any(row.name == BOOT and row.status != PASS for row in results):
        results.append(skipped(EGRESS_DHCP, "skipped: core boot failed"))
        return
    await run_egress_pass(
        app,
        record,
        state_dir,
        results,
        boot_timeout_s=boot_timeout_s,
    )


async def check_image(
    archive: Path,
    *,
    egress: bool = False,
    uplink: str | None = None,
    boot_timeout_s: float = 120.0,
    shutdown_timeout_s: float = 120.0,
    keep_state: bool = False,
    state_dir: Path | None = None,
    app_factory: Callable[[Settings], App] = build_app,
) -> list[CheckResult]:
    """The full conformance pass over one archive.

    Returns every contract point's row, in report order. The state
    dir defaults to a throwaway under ``/tmp`` — shallow, for the
    AF_UNIX socket limit — and is removed unless ``keep_state``;
    ``state_dir`` relocates it.
    """
    if state_dir is None:
        state_dir = Path(f"/tmp/msks-conf-{uuid.uuid4().hex[:8]}")
    try:
        try:
            record = import_archive(archive, state_dir)
        except (ImageError, OSError) as exc:
            return archive_failure_rows(archive, state_dir, exc, egress)
        results = [
            passed(
                ARCHIVE,
                f"imported {record.ref} ({record.hash[:12]}), "
                f"{console_detail(record)}",
            )
        ]
        app = app_factory(settings_for(state_dir, record, egress, uplink))
        await run_core_pass(
            app,
            record,
            state_dir,
            results,
            boot_timeout_s=boot_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
        )
        if egress:
            await egress_tail(
                app,
                record,
                state_dir,
                results,
                boot_timeout_s=boot_timeout_s,
            )
        return results
    finally:
        if not keep_state:
            shutil.rmtree(state_dir, ignore_errors=True)


def check_arguments(parser: argparse.ArgumentParser) -> None:
    """The check subcommand's flags."""
    parser.add_argument("archive", help="the container-image tar to check")
    parser.add_argument(
        "--egress",
        action="store_true",
        help="also verify DHCP address acquisition through the "
        "daemon's net stack (requires root)",
    )
    parser.add_argument(
        "--uplink",
        default=None,
        help="uplink interface for --egress (default: the default route)",
    )
    parser.add_argument(
        "--boot-timeout-s",
        type=float,
        default=120.0,
        help="deadline for each boot and seed wait (default: 120)",
    )
    parser.add_argument(
        "--shutdown-timeout-s",
        type=float,
        default=120.0,
        help="deadline for the ACPI power-button shutdown (default: 120)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the throwaway state dir (serial logs) for inspection",
    )


def run_check(args: argparse.Namespace) -> int:
    """The check subcommand's body: run the pass, print, exit."""
    if args.egress and os.geteuid() != 0:
        print("msks: --egress needs root (tap/nftables/DHCP)", file=sys.stderr)
        return 2
    archive = Path(args.archive)
    if not archive.is_file():
        print(f"msks: no such archive: {archive}", file=sys.stderr)
        return 2
    results = asyncio.run(
        check_image(
            archive,
            egress=args.egress,
            uplink=args.uplink,
            boot_timeout_s=args.boot_timeout_s,
            shutdown_timeout_s=args.shutdown_timeout_s,
            keep_state=args.keep,
        )
    )
    print(render(results))
    failure = first_failure(results)
    if failure is not None:
        print(f"\nfirst failure: {failure.name} — {failure.detail}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """The standalone entry (``python -m msks.conformance``)."""
    parser = argparse.ArgumentParser(
        prog="msks-conformance",
        description=(
            "Boot an image and verify the guest contract "
            "(docs/images.md). Needs /dev/kvm; --egress additionally "
            "needs root and an egress-capable default route."
        ),
    )
    check_arguments(parser)
    return run_check(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover — the manual entry
    raise SystemExit(main())

"""The image conformance check (#258): rows, verdicts, and the pass
shape — against a fake console/microvm, no KVM."""

import argparse
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from msks.app import build_app
from msks.conformance import (
    ACPI_SHUTDOWN,
    ARCHIVE,
    BOOT,
    CONSOLE,
    EGRESS_DHCP,
    HOME_LABEL,
    ROOT_RW,
    USER_DATA,
    CheckResult,
    check_image,
    console_user,
    default_uplink,
    failed,
    first_failure,
    passed,
    render,
    skipped,
)
from msks.microvm import MicrovmError, VmInfo, VmSpec
from msks.microvm.spec import VmStatus
from msks.settings import Settings
from test_imagestore import build_containerdisk

from msks import conformance


class FakeReader:
    """A queue-backed stream the fake console pushes responses on."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, n: int) -> bytes:
        return await self.queue.get()


class FakeWriter:
    """Records writes; the console answers them synchronously."""

    def __init__(self, console: FakeConsole) -> None:
        self.console = console

    def write(self, data: bytes) -> None:
        self.console.answer(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeConsole:
    """Answers the checker's probe commands from a rule table.

    ``failures`` names substrings whose commands stay silent (the
    probe times out). The root marker written by the first boot and
    the seed payload's marker are learned from the specs the
    microvm records, then served to the persistence and user-data
    probes.
    """

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.root_marker: str | None = None
        self.ud_marker: str | None = None
        self.users: list[str | None] = []
        self.reader = FakeReader()

    def learn(self, spec: VmSpec) -> None:
        """Capture the markers the spec's commands will write."""
        if spec.user_data:
            words = spec.user_data.split()
            self.ud_marker = words[words.index("echo") + 1]

    def answer(self, data: bytes) -> None:
        command = data.decode()
        for needle in self.failures:
            if needle in command:
                return  # silence: the probe times out
        text = self.respond(command)
        if text is not None:
            self.reader.queue.put_nowait(text.encode())

    def respond(self, command: str) -> str | None:
        if "echo CONF-" in command:
            return "CONF-42\n"
        if "> /root/.msks-conformance" in command:
            self.root_marker = command.split()[1]
            return "WROTE-42\n"
        if "msks-home" in command:
            return "HOME-MOUNTED-42\n"
        if "msks-conformance-ud" in command:
            return f"{self.ud_marker}\n" if self.ud_marker else None
        if "cat /root/.msks-conformance" in command:
            return f"{self.root_marker}\n" if self.root_marker else None
        if "/home/probe" in command:
            return "HOME-42\n"
        if "addr show scope global" in command:
            return "ADDR-42\n"
        return None  # pragma: no cover — every probe appears above

    async def connect(self, workspace_id: str, user: str | None):
        self.users.append(user)
        self.reader = FakeReader()
        return self.reader, FakeWriter(self)


class CheckMicrovm:
    """The seam the checker drives, recorded, with the fake console."""

    def __init__(self, console: FakeConsole) -> None:
        self.pty = console
        self.calls: list[str] = []
        self.specs: list[VmSpec] = []
        self.statuses: dict[str, VmStatus] = {}
        self.fail_launch = False
        self.report_absent = False
        self.fail_launch_after: int | None = None

    async def launch(self, spec: VmSpec) -> None:
        if self.fail_launch or (
            self.fail_launch_after is not None
            and len(self.specs) > self.fail_launch_after
        ):
            raise RuntimeError("launch boom")
        self.calls.append("launch")
        self.specs.append(spec)
        self.statuses[spec.workspace_id] = VmStatus.RUNNING
        self.pty.learn(spec)

    async def info(self, workspace_id: str) -> VmInfo:
        if self.report_absent:
            return VmInfo(workspace_id, VmStatus.ABSENT)
        return VmInfo(
            workspace_id, self.statuses.get(workspace_id, VmStatus.ABSENT)
        )

    async def console(self, workspace_id: str, user: str | None = None):
        return await self.pty.connect(workspace_id, user)

    async def shutdown(
        self, workspace_id: str, timeout_s: float | None = None
    ) -> None:
        self.calls.append("shutdown")
        self.statuses[workspace_id] = VmStatus.STOPPED

    async def kill(self, workspace_id: str) -> None:
        self.calls.append("kill")

    async def cleanup(self, workspace_id: str) -> None:
        self.calls.append("cleanup")


class FakeNet:
    """The net seam the egress pass arms; start/stop recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")


def statuses(rows: list[CheckResult]) -> dict[str, str]:
    return {row.name: row.status for row in rows}


def make_app(microvm, net=None):
    def factory(settings: Settings):
        app = build_app(settings)
        app.state.microvm = microvm
        if net is not None:
            app.state.net = net
        return app

    return factory


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Failure loops resolve in milliseconds, the forwarding sysctl
    the egress pass owns becomes a scratch file, and the uplink
    probe reads nothing from the host."""
    monkeypatch.setattr(conformance, "RETRY_SLEEP_S", 0.01)
    monkeypatch.setattr(conformance, "PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(conformance, "default_uplink", lambda: "eth0")
    forwarding = tmp_path / "ip_forward"
    forwarding.write_text("0\n")
    monkeypatch.setattr(conformance, "FORWARDING", forwarding)
    return forwarding


def manifest(
    *,
    provisioner: bool = True,
    protocol: str = "prelude-v1",
) -> dict:
    """A schema-2 manifest with the knobs the checker reads."""
    document = {
        "schema": 2,
        "name": "debian",
        "version": "13.6",
        "cmdline": "console=ttyS0 root=/dev/vda ro",
        "vsock_shell_port": 1023,
    }
    if protocol:
        document["console_protocol"] = protocol
    if provisioner:
        document["capabilities"] = {"provisioner": "cloud-init"}
    return document


def archive_with(
    tmp_path: Path, manifest_document: dict, tag: str = "img"
) -> Path:
    """A minimal-but-valid containerDisk tar carrying the manifest."""
    members = {
        "boot/vmlinuz": b"kernel-bytes",
        "boot/initrd.img": b"initrd-bytes",
        "disk/rootfs.ext4": b"rootfs-bytes",
        "disk/image.json": json.dumps(manifest_document).encode(),
    }
    path = tmp_path / f"{tag}.tar"
    build_containerdisk(path, schema=False, members=members)
    return path


async def run_check(
    tmp_path: Path,
    console: FakeConsole,
    *,
    egress: bool = False,
    provisioner: bool = True,
    protocol: str | None = "prelude-v1",
) -> tuple[list[CheckResult], CheckMicrovm]:
    microvm = CheckMicrovm(console)
    path = archive_with(
        tmp_path, manifest(provisioner=provisioner, protocol=protocol)
    )
    rows = await check_image(
        path,
        egress=egress,
        boot_timeout_s=0.5,
        shutdown_timeout_s=0.5,
        app_factory=make_app(microvm, FakeNet() if egress else None),
    )
    return rows, microvm


def test_render_and_first_failure() -> None:
    """Rows render one line each; the exit summary picks the first
    FAIL even when later rows fail too."""
    rows = [
        passed(ARCHIVE, "ok"),
        failed(BOOT, TimeoutError("console never answered")),
        CheckResult(CONSOLE, "skip", "unrunnable: boot failed"),
        failed(HOME_LABEL, "no home"),
    ]
    text = render(rows)
    assert "PASS archive     ok" in text
    assert "FAIL boot        console never answered" in text
    assert "SKIP console" in text
    assert first_failure(rows) is rows[1]
    assert first_failure([rows[0]]) is None


def test_row_helpers_name_exceptions() -> None:
    """failed() stringifies; an exception with an empty message
    still names its type."""
    assert failed(BOOT, "plain text").detail == "plain text"
    assert failed(BOOT, RuntimeError()).detail == "RuntimeError"
    assert skipped(USER_DATA, "why").status == "skip"


def test_console_user_by_protocol() -> None:
    """Prelude connects as the first declared user; legacy connects
    raw (no user on the wire)."""

    class Record:
        console_protocol = "prelude-v1"
        console_users = ("root", "alice")

    class Legacy:
        console_protocol = "legacy"
        console_users = ("root",)

    assert console_user(Record()) == "root"
    assert console_user(Legacy()) is None


def test_default_uplink(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default route's dev wins; a route without one names the
    refusal."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="default via 192.0.2.1 dev eth0 proto dhcp"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert default_uplink() == "eth0"

    def no_route(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", no_route)
    with pytest.raises(RuntimeError, match="no default route"):
        default_uplink()


def test_forwarding_guard_restores(fast_retries: Path) -> None:
    """The guard sets 1 on entry and restores what it found."""
    with conformance.ForwardingGuard():
        assert fast_retries.read_text() == "1"
    assert fast_retries.read_text() == "0\n"


async def test_full_pass_prelude_with_provisioner(tmp_path: Path) -> None:
    """Every contract point passes against a cooperating guest."""
    console = FakeConsole()
    rows, microvm = await run_check(tmp_path, console)
    assert statuses(rows) == {
        ARCHIVE: "pass",
        BOOT: "pass",
        CONSOLE: "pass",
        USER_DATA: "pass",
        ACPI_SHUTDOWN: "pass",
        ROOT_RW: "pass",
        HOME_LABEL: "pass",
    }
    assert first_failure(rows) is None
    # The spec the checker booted carried the record's boot files
    # and the seed payload; the console negotiated the declared user.
    spec = microvm.specs[0]
    assert spec.kernel.name == "kernel"
    assert spec.cmdline == "console=ttyS0 root=/dev/vda ro"
    assert spec.user_data is not None
    assert spec.egress is False
    assert console.users[0] == "root"


async def test_full_pass_legacy_without_provisioner(tmp_path: Path) -> None:
    """A legacy image with no provisioner: raw-shell console, the
    user-data point skipped by name."""
    console = FakeConsole()
    rows, microvm = await run_check(
        tmp_path, console, provisioner=False, protocol=None
    )
    assert statuses(rows)[CONSOLE] == "pass"
    assert statuses(rows)[USER_DATA] == "skip"
    assert "no provisioner" in next(
        row.detail for row in rows if row.name == USER_DATA
    )
    # Legacy connects with no user on the wire, every session.
    assert microvm.pty.users
    assert set(microvm.pty.users) == {None}


async def test_archive_failure_skips_everything(tmp_path: Path) -> None:
    """A broken archive fails its own row and skips the rest,
    egress included; a valid tar with no container bookkeeping
    fails the same row with its single-line named error."""
    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not a tar")
    rows = await check_image(
        broken,
        egress=True,
        boot_timeout_s=0.5,
        app_factory=make_app(CheckMicrovm(FakeConsole())),
    )
    assert statuses(rows)[ARCHIVE] == "fail"
    assert all(
        statuses(rows)[name] == "skip"
        for name in (*conformance.CORE_POINTS, EGRESS_DHCP)
    )
    assert first_failure(rows).name == ARCHIVE

    import tarfile

    plain = tmp_path / "plain.tar"
    with tarfile.open(plain, "w"):
        pass
    rows = await check_image(
        plain,
        boot_timeout_s=0.5,
        app_factory=make_app(CheckMicrovm(FakeConsole())),
    )
    assert statuses(rows)[ARCHIVE] == "fail"
    assert "manifest.json" in first_failure(rows).detail


async def test_boot_failure_skips_dependents(tmp_path: Path) -> None:
    """A guest that never answers fails boot and skips the points
    that need a booted guest."""
    console = FakeConsole()
    console.failures.append("echo CONF-")  # the boot probe stays silent
    rows, microvm = await run_check(tmp_path, console)
    assert statuses(rows)[BOOT] == "fail"
    assert statuses(rows)[CONSOLE] == "skip"
    assert statuses(rows)[ACPI_SHUTDOWN] == "skip"
    assert microvm.calls.count("kill") >= 1  # nothing leaks


async def test_launch_failure_fails_boot(tmp_path: Path) -> None:
    """A launch that refuses outright fails boot, skips the rest."""
    console = FakeConsole()
    microvm = CheckMicrovm(console)
    microvm.fail_launch = True
    path = archive_with(tmp_path, manifest())
    rows = await check_image(
        path,
        boot_timeout_s=0.5,
        app_factory=make_app(microvm),
    )
    assert statuses(rows)[BOOT] == "fail"
    assert first_failure(rows).detail == "launch boom"


async def test_write_failure_fails_only_its_point(tmp_path: Path) -> None:
    """A root write that fails reports root-rw alone; home-label
    and the rest still run."""
    console = FakeConsole()
    console.failures.append("> /root/.msks-conformance")
    rows, _ = await run_check(tmp_path, console)
    assert statuses(rows)[ROOT_RW] == "fail"
    assert "first-boot write failed" in next(
        row.detail for row in rows if row.name == ROOT_RW
    )
    assert statuses(rows)[HOME_LABEL] == "pass"


async def test_readback_failure_fails_its_point(tmp_path: Path) -> None:
    """A marker that vanishes across the stop/start fails its point
    with the stage named."""
    console = FakeConsole()
    console.failures.append("cat /root/.msks-conformance")
    rows, _ = await run_check(tmp_path, console)
    assert statuses(rows)[ROOT_RW] == "fail"
    assert "restart read-back failed" in next(
        row.detail for row in rows if row.name == ROOT_RW
    )
    assert statuses(rows)[HOME_LABEL] == "pass"


async def test_user_data_failure(tmp_path: Path) -> None:
    """A seed that never runs fails the user-data point: the
    payload's marker never lands."""
    console = FakeConsole()
    console.failures.append("msks-conformance-ud")
    rows, _ = await run_check(tmp_path, console)
    assert statuses(rows)[USER_DATA] == "fail"


async def test_shutdown_status_after_press(tmp_path: Path) -> None:
    """A shutdown that leaves the VM running fails acpi-shutdown
    naming the status."""

    class StillRunning(CheckMicrovm):
        async def shutdown(self, workspace_id, timeout_s=None):
            pass  # the press is ignored; status stays running

    microvm = StillRunning(FakeConsole())
    path = archive_with(tmp_path, manifest())
    rows = await check_image(
        path,
        boot_timeout_s=0.5,
        app_factory=make_app(microvm),
    )
    assert statuses(rows)[ACPI_SHUTDOWN] == "fail"
    assert "running after the press" in next(
        row.detail for row in rows if row.name == ACPI_SHUTDOWN
    )


async def test_egress_pass_takes_dhcp_address(
    tmp_path: Path, fast_retries: Path
) -> None:
    """The --egress leg arms the net seam, flips ip_forward for its
    run, and restores it after."""
    console = FakeConsole()
    rows, microvm = await run_check(tmp_path, console, egress=True)
    assert statuses(rows)[EGRESS_DHCP] == "pass"
    assert fast_retries.read_text() == "0\n"  # restored
    egress_spec = microvm.specs[-1]  # the egress boot, with a NIC
    assert egress_spec.egress is True


async def test_egress_skipped_when_boot_failed(tmp_path: Path) -> None:
    """Egress follows a failed core boot with a skip row."""
    console = FakeConsole()
    console.failures.append("echo CONF-")
    rows, _ = await run_check(tmp_path, console, egress=True)
    assert statuses(rows)[EGRESS_DHCP] == "skip"
    assert "core boot failed" in next(
        row.detail for row in rows if row.name == EGRESS_DHCP
    )


async def test_egress_dhcp_failure(tmp_path: Path) -> None:
    """A guest that takes no address fails egress-dhcp."""
    console = FakeConsole()
    console.failures.append("addr show scope global")
    rows, _ = await run_check(tmp_path, console, egress=True)
    assert statuses(rows)[EGRESS_DHCP] == "fail"


def test_run_check_requires_root_for_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--egress without root is a usage error before any boot."""
    monkeypatch.setattr(conformance.os, "geteuid", lambda: 1000)
    assert conformance.run_check(check_args(egress=True, archive="x.tar")) == 2


def check_args(**overrides) -> argparse.Namespace:
    """The check subcommand's parsed-argument shape."""
    fields = {
        "archive": "img.tar",
        "egress": False,
        "uplink": None,
        "boot_timeout_s": 1.0,
        "shutdown_timeout_s": 1.0,
        "keep": False,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


def test_run_check_names_a_missing_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source that is not a file is a usage error naming it."""
    assert (
        conformance.run_check(check_args(archive=str(tmp_path / "nope.tar")))
        == 2
    )
    assert "no such archive" in capsys.readouterr().err


def test_run_check_exits_nonzero_naming_first_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rendered report and the first-failure summary reach
    stdout; a FAIL row exits 1."""

    async def fake_check(*args, **kwargs):
        return [failed(BOOT, "the guest never answered")]

    monkeypatch.setattr(conformance, "check_image", fake_check)
    monkeypatch.setattr(conformance.os, "geteuid", lambda: 0)
    path = archive_with(tmp_path, manifest())
    assert conformance.run_check(check_args(archive=str(path))) == 1
    out = capsys.readouterr().out
    assert "FAIL boot" in out
    assert "first failure: boot" in out


# --- the seam-level failure shapes (#258) -------------------------------


class JunkReader:
    """A stream with endless non-matching bytes."""

    async def read(self, n: int) -> bytes:
        return b"junk\n"


class ClosedReader:
    """A stream the guest closed."""

    async def read(self, n: int) -> bytes:
        return b""


async def test_read_until_names_its_deadlines() -> None:
    """A marker that never matches fails naming the marker; a closed
    stream fails naming the close."""
    from msks.conformance import read_until

    with pytest.raises(TimeoutError, match="never arrived"):
        await read_until(JunkReader(), b"MARK", 0.05)
    with pytest.raises(ConnectionError, match="closed"):
        await read_until(ClosedReader(), b"MARK", 0.05)


async def test_boot_names_a_dead_vmm(tmp_path: Path) -> None:
    """A VMM that reports dead while the console waits fails boot
    naming the status."""
    console = FakeConsole()
    microvm = CheckMicrovm(console)
    microvm.report_absent = True
    path = archive_with(tmp_path, manifest())
    rows = await check_image(
        path, boot_timeout_s=0.5, app_factory=make_app(microvm)
    )
    assert statuses(rows)[BOOT] == "fail"
    assert "absent" in first_failure(rows).detail


async def test_shutdown_failure_fails_acpi_row(tmp_path: Path) -> None:
    """A shutdown that raises fails the acpi row with the error."""

    class DyingShutdown(CheckMicrovm):
        async def shutdown(self, workspace_id, timeout_s=None):
            raise RuntimeError("power button ignored")

    microvm = DyingShutdown(FakeConsole())
    path = archive_with(tmp_path, manifest())
    rows = await check_image(
        path, boot_timeout_s=0.5, app_factory=make_app(microvm)
    )
    assert statuses(rows)[ACPI_SHUTDOWN] == "fail"
    assert "power button ignored" in next(
        row.detail for row in rows if row.name == ACPI_SHUTDOWN
    )


async def test_home_write_failure_fails_only_its_point(
    tmp_path: Path,
) -> None:
    """A home mount that never appears fails home-label alone."""
    console = FakeConsole()
    console.failures.append("msks-home")
    rows, _ = await run_check(tmp_path, console)
    assert statuses(rows)[HOME_LABEL] == "fail"
    assert "first-boot write failed" in next(
        row.detail for row in rows if row.name == HOME_LABEL
    )
    assert statuses(rows)[ROOT_RW] == "pass"


async def test_second_boot_failure_fails_both_points(
    tmp_path: Path,
) -> None:
    """A workspace that cannot restart fails both persistence points
    naming the second boot."""
    console = FakeConsole()
    microvm = CheckMicrovm(console)
    microvm.fail_launch_after = 0  # the persistence relaunch fails
    path = archive_with(tmp_path, manifest())
    rows = await check_image(
        path, boot_timeout_s=0.5, app_factory=make_app(microvm)
    )
    assert statuses(rows)[ROOT_RW] == "fail"
    assert statuses(rows)[HOME_LABEL] == "fail"
    assert "second boot failed" in next(
        row.detail for row in rows if row.name == ROOT_RW
    )


async def test_home_readback_failure_fails_its_point(
    tmp_path: Path,
) -> None:
    """A /home write that vanishes across the cycle fails home-label
    with the stage named."""
    console = FakeConsole()
    console.failures.append("test -f /home/probe")
    rows, _ = await run_check(tmp_path, console)
    assert statuses(rows)[HOME_LABEL] == "fail"
    assert "restart read-back failed" in next(
        row.detail for row in rows if row.name == HOME_LABEL
    )
    assert statuses(rows)[ROOT_RW] == "pass"


async def test_keep_state_preserves_the_state_dir(tmp_path: Path) -> None:
    """keep_state leaves the throwaway dir (serial logs) behind for
    inspection; the default removes it."""
    path = archive_with(tmp_path, manifest())
    own = tmp_path / "conf-state"
    await check_image(
        path,
        keep_state=True,
        state_dir=own,
        boot_timeout_s=0.5,
        app_factory=make_app(CheckMicrovm(FakeConsole())),
    )
    assert own.is_dir()
    shutil.rmtree(own, ignore_errors=True)
    await check_image(
        path,
        state_dir=own,
        boot_timeout_s=0.5,
        app_factory=make_app(CheckMicrovm(FakeConsole())),
    )
    assert not own.exists()


def test_run_check_exits_zero_on_a_clean_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A green report prints and exits 0."""

    async def fake_check(*args, **kwargs):
        return [passed(ARCHIVE, "imported mine:1.0")]

    monkeypatch.setattr(conformance, "check_image", fake_check)
    path = archive_with(tmp_path, manifest())
    assert conformance.run_check(check_args(archive=str(path))) == 0
    assert "PASS archive" in capsys.readouterr().out


def test_main_parses_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standalone entry builds the same parser and routes to
    run_check."""
    seen: list[argparse.Namespace] = []

    def fake_run(args):
        seen.append(args)
        return 7

    monkeypatch.setattr(conformance, "run_check", fake_run)
    assert conformance.main(["img.tar", "--keep"]) == 7
    assert seen[0].archive == "img.tar"
    assert seen[0].keep is True


# --- review fixes (#261) -------------------------------------------------


async def test_core_pass_never_probes_the_uplink(tmp_path: Path) -> None:
    """A core-only check runs on hosts with no route (and no
    iproute2): the uplink probe is egress-only."""
    from msks import conformance as mod

    def boom():
        raise AssertionError("uplink probed on a core-only check")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(mod, "default_uplink", boom)
    try:
        console = FakeConsole()
        rows, _ = await run_check(tmp_path, console)
        assert first_failure(rows) is None
    finally:
        monkey.undo()


def test_sanitize_strips_forged_report_text() -> None:
    """Manifest-derived strings cannot forge or obscure rows."""
    from msks.conformance import sanitize

    assert sanitize("clean") == "clean"
    assert sanitize("PASS home-label  fake\nreal line") == (
        "PASS home-label  fake"
    )
    assert sanitize("a\x1bb") == "ab"
    assert sanitize("line1\nline2") == "line1"
    assert sanitize("") == ""


def test_probe_user_prefers_root_when_served() -> None:
    """The write probes run as root when the image serves it; a
    non-root-only console keeps its first declared user."""

    class Record:
        console_protocol = "prelude-v1"
        console_users = ("dev",)

    class Rooty:
        console_protocol = "prelude-v1"
        console_users = ("dev", "root")

    from msks.conformance import probe_user

    assert probe_user(Record()) == "dev"
    assert probe_user(Rooty()) == "root"


async def test_unprivileged_console_still_checks(tmp_path: Path) -> None:
    """An image whose console serves only 'dev' passes with the
    probes run as that user (the fake console serves anyone)."""
    document = manifest(provisioner=True)
    document["console_users"] = ["dev"]
    microvm = CheckMicrovm(FakeConsole())
    rows = await check_image(
        archive_with(tmp_path, document, tag="devuser"),
        boot_timeout_s=0.5,
        app_factory=make_app(microvm),
    )
    assert statuses(rows)[CONSOLE] == "pass"
    assert statuses(rows)[ROOT_RW] == "pass"
    # The handshake point connected as the declared user; the probes
    # fell back to it too (the only console the image serves).
    assert microvm.pty.users[0] == "dev"


def test_cmd_image_check_defers_the_daemon_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the client CLI pulls no server-side composition;
    only running the check does (the client/server boundary)."""
    import subprocess
    import sys

    code = (
        "import sys, msks.client.cli as cli; "
        "assert 'msks.app' not in sys.modules, 'boundary laundered'; "
        "assert 'msks.conformance' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=src_root())


def src_root() -> Path:
    """The venv's site-packages root the installed tree runs from."""
    import msks

    return Path(msks.__file__).parent.parent


# --- second review fixes (#261) ------------------------------------------


async def test_forged_manifest_name_cannot_forge_rows(
    tmp_path: Path,
) -> None:
    """A manifest name carrying a fake PASS row lands in the report
    as one sanitized line — the archive row names the import, and
    no second row appears."""
    document = manifest()
    document["name"] = "debian\nPASS root-rw  root is writable (forged)"
    rows = await check_image(
        archive_with(tmp_path, document, tag="forged"),
        boot_timeout_s=0.5,
        app_factory=make_app(CheckMicrovm(FakeConsole())),
    )
    text = render(rows)
    assert text.count("PASS") == len(
        [row for row in rows if row.status == "pass"]
    )
    assert "PASS root-rw  root is writable (forged)" not in text.split("\n")[0]
    assert all("\n" not in row.detail for row in rows)


async def test_guest_refusal_text_cannot_forge_rows(tmp_path: Path) -> None:
    """A prelude refusal whose reason carries a fake row is one
    sanitized line in the boot failure detail."""

    class Refusing(CheckMicrovm):
        async def console(self, workspace_id, user=None):
            raise MicrovmError(
                "console refused user 'root': \nPASS root-rw forged"
            )

    microvm = Refusing(FakeConsole())
    rows = await check_image(
        archive_with(tmp_path, manifest()),
        boot_timeout_s=0.5,
        app_factory=make_app(microvm),
    )
    assert statuses(rows)[BOOT] == "fail"
    assert all("\n" not in row.detail for row in rows)
    assert "PASS root-rw forged" not in render(rows)


def test_check_flags_have_one_source() -> None:
    """The client and the standalone entry parse the same flag
    definitions — the leaf module is the single source."""
    from msks import conformance, conformance_args

    assert conformance.check_arguments is conformance_args.check_arguments


def test_uplink_without_egress_is_a_usage_error() -> None:
    """--uplink names the egress pass's uplink; alone it is a
    named usage error, not a silently ignored flag."""
    assert conformance.run_check(check_args(uplink="eth0", egress=False)) == 2


async def test_default_state_dir_is_private(tmp_path: Path) -> None:
    """The throwaway state dir is created 0700 (mkdtemp): the serial
    log and unpacked image are not world-readable on a shared host."""
    import tempfile as tempfile_mod

    made: list[Path] = []
    real_mkdtemp = tempfile_mod.mkdtemp

    def spy(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        made.append(path)
        return str(path)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tempfile_mod, "mkdtemp", spy)
    try:
        rows = await check_image(
            archive_with(tmp_path, manifest()),
            keep_state=True,
            boot_timeout_s=0.5,
            app_factory=make_app(CheckMicrovm(FakeConsole())),
        )
    finally:
        monkey.undo()
    assert first_failure(rows) is None
    assert made and (made[0].stat().st_mode & 0o777) == 0o700
    shutil.rmtree(made[0], ignore_errors=True)

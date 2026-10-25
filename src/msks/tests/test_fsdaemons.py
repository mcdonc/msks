"""The host-side attach path (#620, part #622).

Covers the virtiofsd argument grammar (the coherence and identity
contracts), the cloud-hypervisor fs device entries, the spawn and
teardown lifecycle (tracked daemons, the restarted-daemon pidfile
fallback, the recycled-pid guard), the start path's drift
refusal, and the doctor rows.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from msks.microvm.fsdaemons import (
    QUEUE_SIZE,
    WHOLE_RANGE,
    XATTRMAP,
    fs_daemon_args,
    fs_device_entries,
    fs_pidfile_path,
    fs_socket_path,
    sandbox_mode,
)
from msks.microvm.local import (
    LocalCloudHypervisor,
    fs_pid_serves,
    read_fs_pid,
    vm_config,
)
from msks.settings import (
    MountSettings,
    MountShare,
    Settings,
    VmmSettings,
)
from msks.spec.mounts import MountSpec, safe_share_name
from msks.spec.vm import VmSpec
from test_api import auth, client  # noqa: F401 (fixture)


def test_sandbox_follows_privilege() -> None:
    """A root daemon gets the namespace sandbox; an unprivileged
    one cannot unshare, so it runs unsandboxed — the share set
    itself stays operator-chosen either way."""
    assert sandbox_mode(0) == "namespace"
    assert sandbox_mode(1000) == "none"


def test_fs_daemon_args_carry_the_contracts(tmp_path: Path) -> None:
    """One daemon's argument vector: cache never (the coherence
    contract), whole-range uid/gid squash to the daemon's own
    identity, the xattr prefix map, the sandbox — and --readonly
    exactly when the attach is read-only."""
    socket = tmp_path / "fs-share.sock"
    args = fs_daemon_args(
        "/srv/share",
        "share",
        socket,
        uid=1000,
        gid=100,
        readonly=False,
        sandbox="none",
    )
    joined = " ".join(args)
    assert "--socket-path" in args and str(socket) in args
    assert "--shared-dir /srv/share" in joined
    assert "--tag share" in joined
    assert "--sandbox none" in joined
    assert "--cache never" in joined
    assert f"squash-guest:0:1000:{WHOLE_RANGE}" in joined
    assert f"squash-guest:0:100:{WHOLE_RANGE}" in joined
    assert XATTRMAP in args
    assert "--readonly" not in args
    ro = fs_daemon_args(
        "/srv/share",
        "share",
        socket,
        uid=0,
        gid=0,
        readonly=True,
        sandbox="namespace",
    )
    assert "--sandbox namespace" in " ".join(ro)
    assert "--readonly" in ro


def test_fs_device_entries_shape(tmp_path: Path) -> None:
    """The vm.create fs list: one device per share, sorted by tag,
    the queue sizing pinned, DAX off (absent from the body)."""
    entries = fs_device_entries(
        {"zeta": tmp_path / "z.sock", "alpha": tmp_path / "a.sock"}
    )
    assert entries == [
        {
            "tag": "alpha",
            "socket": str(tmp_path / "a.sock"),
            "num_queues": 1,
            "queue_size": QUEUE_SIZE,
        },
        {
            "tag": "zeta",
            "socket": str(tmp_path / "z.sock"),
            "num_queues": 1,
            "queue_size": QUEUE_SIZE,
        },
    ]


def test_vm_config_carries_fs_devices(tmp_path: Path) -> None:
    """The create body gains the fs list only when mounts attach —
    a mountless workspace's body is unchanged from part 1's
    shape."""
    spec = VmSpec(workspace_id="ws", kernel=Path("/k"), rootfs=Path("/r"))
    bare = vm_config(spec, [])
    assert "fs" not in bare
    fs = [
        {
            "tag": "share",
            "socket": "/sock",
            "num_queues": 1,
            "queue_size": QUEUE_SIZE,
        }
    ]
    assert vm_config(spec, [], fs=fs)["fs"] == fs


def test_share_names_must_be_filename_safe() -> None:
    """The name becomes a tag and a socket stem: letters, digits,
    dot, underscore, hyphen — anything else refuses at settings
    load."""
    assert safe_share_name("projects")
    assert safe_share_name("data.v2_old-1")
    assert not safe_share_name("a/b")
    assert not safe_share_name("a b")
    assert not safe_share_name("")


def test_pidfile_helpers(tmp_path: Path) -> None:
    """The socket and pidfile stems derive from the share name
    inside the workspace's vm directory."""
    assert fs_socket_path(tmp_path, "data") == tmp_path / "fs-data.sock"
    assert fs_pidfile_path(tmp_path, "data") == tmp_path / "fs-data.pid"


def test_read_fs_pid_tolerates_garbage(tmp_path: Path) -> None:
    """A pidfile that holds nothing a daemon wrote reads as None —
    swept, never trusted enough to kill."""
    good = tmp_path / "fs-good.pid"
    good.write_text("4242\n", encoding="utf-8")
    assert read_fs_pid(good) == 4242
    bad = tmp_path / "fs-bad.pid"
    bad.write_text("not-a-pid", encoding="utf-8")
    assert read_fs_pid(bad) is None
    assert read_fs_pid(tmp_path / "fs-gone.pid") is None


async def test_fs_pid_serves_matches_the_socket() -> None:
    """The recycled-pid guard (#56's rule, the fs shape): only a
    process whose command line names this workspace's socket dies
    by pidfile — a real process whose argv carries the socket
    qualifies, the same pid queried for another socket does not."""

    socket = Path("/unique/fs-probe.sock")
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        str(socket),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert fs_pid_serves(proc.pid, socket) is True
        assert fs_pid_serves(proc.pid, Path("/other/fs.sock")) is False
        assert fs_pid_serves(proc.pid + 1_000_000, socket) is False
    finally:
        proc.kill()
        await proc.wait()


async def test_start_refuses_drifted_share_paths(client, tmp_path):  # noqa: F811
    """The drift refusal (#620, part #622): a share whose path
    resolves elsewhere than the settings load pinned skips with
    mount.drift naming both ends — the symlink retargeted inside a
    shareable tree never walks the attach out of what the
    operator configured."""
    http, app, _stub = client
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-drift", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid, [{"share": "moved", "guest": "/mnt/moved", "mode": "rw"}]
    )
    # Load-time pin: the link resolves at `real`...
    # Load-time pin: the settings parser would realpath the link
    # to `real` at load — construct the pinned share directly.
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="moved", path=str(link), resolved=str(real)),)
    )
    # ...then the operator retargets it past the load.
    other = tmp_path / "other"
    other.mkdir()
    link.unlink()
    link.symlink_to(other)
    published = []
    real_publish = app.state.hub.publish

    async def spy_publish(event: str, data: dict) -> None:
        published.append((event, data))
        await real_publish(event, data)

    app.state.hub.publish = spy_publish
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    assert published[0][0] == "mount.drift"
    assert str(real) in published[0][1]["detail"]
    assert str(other) in published[0][1]["detail"]


async def test_start_passes_only_attachable_mounts(client, tmp_path):  # noqa: F811
    """The seam attaches exactly the attachable subset (#620,
    part #622): a skipped mount (share gone from the config)
    never reaches the spec the driver launches."""
    http, app, stub = client
    live = tmp_path / "live"
    live.mkdir()
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-mix", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid,
        [
            {"share": "gone", "guest": "/mnt/gone", "mode": "rw"},
            {"share": "live", "guest": "/mnt/live", "mode": "rw"},
        ],
    )
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="live", path=str(live)),)
    )
    launched = []
    real_launch = stub.launch

    async def capture_launch(spec):
        launched.append(spec)
        return await real_launch(spec)

    stub.launch = capture_launch
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    assert launched[0].mounts == (
        MountSpec(share="live", guest="/mnt/live", mode="rw"),
    )


# --- the daemon lifecycle ---------------------------------------------


class FakeProc:
    """A virtiofsd-shaped process double: returncode None until
    terminate, then 0 — the tracked-teardown path's shape."""

    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def driver_over(settings: Settings) -> LocalCloudHypervisor:
    """A driver over a throwaway app with a settings override and
    a spawn seam that records instead of exec'ing."""

    class App:
        pass

    app = App()
    app.state = type("State", (), {})()
    app.state.settings = settings
    driver = LocalCloudHypervisor(app)
    spawned: list[tuple[str, list[str]]] = []

    async def fake_spawn_fs(binary, args, log_path):
        spawned.append((binary, args))
        return FakeProc()

    driver._spawn_fs = fake_spawn_fs
    return driver, spawned


async def test_fs_daemons_spawn_and_stop(tmp_path: Path) -> None:
    """A start with two mounts spawns one daemon per share with
    the pinned flags, and the teardown stops exactly them and
    sweeps their pidfiles."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws1"
    vm_dir.mkdir(parents=True)
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path),
    )
    settings.mounts = MountSettings(
        shares=(
            MountShare(name="proj", path=str(share_dir)),
            MountShare(name="data", path=str(data_dir)),
        )
    )
    driver, spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws1",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(
            MountSpec(share="proj", guest="/mnt/proj"),
            MountSpec(share="data", guest="/mnt/data", mode="ro"),
        ),
    )
    entries = await driver._start_fs_daemons(spec, vm_dir)
    assert [entry["tag"] for entry in entries] == ["data", "proj"]
    assert len(spawned) == 2
    binary, args = spawned[0]
    assert binary == "virtiofsd"
    assert str(share_dir) in args
    assert fs_pidfile_path(vm_dir, "proj").is_file()
    assert fs_pidfile_path(vm_dir, "data").is_file()
    procs = driver._fs_procs["ws1"]
    await driver._stop_fs_daemons("ws1")
    assert [proc.terminated for proc in procs] == [True, True]
    assert not fs_pidfile_path(vm_dir, "proj").exists()
    assert not fs_pidfile_path(vm_dir, "data").exists()
    assert "ws1" not in driver._fs_procs


async def test_spawn_failure_unwinds_earlier_daemons(
    tmp_path: Path,
) -> None:
    """A daemon that fails to spawn unwinds the ones before it: no
    orphaned sockets, no half-attached row."""
    proj = tmp_path / "proj"
    proj.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    vm_dir = tmp_path / "vms" / "ws2"
    vm_dir.mkdir(parents=True)
    settings = Settings()
    docs = tmp_path / "docs"
    docs.mkdir()
    settings.mounts = MountSettings(
        shares=(
            MountShare(name="proj", path=str(proj)),
            MountShare(name="data", path=str(data)),
            MountShare(name="docs", path=str(docs)),
        )
    )
    driver, spawned = driver_over(settings)

    async def flaky_spawn(binary, args, log_path):
        if len(spawned) == 2:
            raise FileNotFoundError("virtiofsd")
        spawned.append((binary, args))
        return FakeProc()

    driver._spawn_fs = flaky_spawn
    spec = VmSpec(
        workspace_id="ws2",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(
            MountSpec(share="proj", guest="/mnt/proj"),
            MountSpec(share="data", guest="/mnt/data"),
            MountSpec(share="docs", guest="/mnt/docs"),
        ),
    )
    with pytest.raises(FileNotFoundError):
        await driver._start_fs_daemons(spec, vm_dir)
    assert len(spawned) == 2  # two spawned, the third failed
    assert "ws2" not in driver._fs_procs


async def test_restarted_daemon_kills_by_pidfile(tmp_path: Path) -> None:
    """The untracked teardown: a pidfile names a daemon a crashed
    predecessor left — this process kills it only when its command
    line names the workspace's socket, and sweeps the pidfile
    either way."""
    import asyncio

    vm_dir = tmp_path / "vms" / "ws3"
    vm_dir.mkdir(parents=True)
    settings = Settings()
    driver, _spawned = driver_over(settings)
    socket = fs_socket_path(vm_dir, "proj")
    pidfile = fs_pidfile_path(vm_dir, "proj")
    serving = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        str(socket),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pidfile.write_text(str(serving.pid), encoding="utf-8")
    driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
    await driver._kill_untracked_fs_daemons("ws3")
    assert not pidfile.exists()
    # SIGTERM delivery is asynchronous: poll the death briefly.
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(serving.pid, 0)
        except ProcessLookupError:
            break  # it died by its pidfile
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the serving daemon survived its pidfile kill")


# --- the doctor rows --------------------------------------------------


def test_doctor_reports_virtiofsd_and_share_paths(tmp_path: Path) -> None:
    """The warning-grade rows: virtiofsd's presence (a daemon
    without shares never execs it), and one row per configured
    share naming a missing path."""
    from msks.server.doctor import run_doctor

    missing = tmp_path / "missing"
    present = tmp_path / "present"
    present.mkdir()
    settings = Settings()
    settings.mounts = MountSettings(
        shares=(
            MountShare(name="gone", path=str(missing)),
            MountShare(name="here", path=str(present)),
        )
    )
    report = run_doctor(settings)
    rows = {result.name: result for result in report.results}
    assert rows["virtiofsd"].ok is True
    # A miss stays warning-grade: with the binary gone, the row
    # fails the run never — it only breaks the mounts feature.
    gone_settings = Settings()
    gone_settings.vmm.virtiofsd = "virtiofsd-absent"
    gone_rows = {
        result.name: result for result in run_doctor(gone_settings).results
    }
    assert gone_rows["virtiofsd-absent"].ok is False
    assert gone_rows["virtiofsd-absent"].is_warning is True
    share_gone = rows["share gone"]
    assert share_gone.ok is False
    assert str(missing) in share_gone.message
    assert rows["share here"].ok is True
    assert str(present) in rows["share here"].message


async def test_missing_share_path_skips_at_start(client, tmp_path):  # noqa: F811
    """A share path that vanished (or never existed) skips with
    the path named — the same event kind a config deletion
    carries, a detail the operator can act on."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-nopath", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid, [{"share": "gone", "guest": "/mnt/gone", "mode": "rw"}]
    )
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="gone", path=str(tmp_path / "nowhere")),)
    )
    published = []
    real_publish = app.state.hub.publish

    async def spy_publish(event: str, data: dict) -> None:
        published.append((event, data))
        await real_publish(event, data)

    app.state.hub.publish = spy_publish
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    assert published[0][0] == "mount.skipped"
    assert "path does not exist" in published[0][1]["detail"]


async def test_spawn_guards_name_every_refusal(tmp_path: Path) -> None:
    """The last-line guards: a share the route did not filter (it
    cannot through this seam) and a path that vanished between
    derivation and spawn each refuse by name."""
    from msks.microvm.errors import MicrovmError

    vm_dir = tmp_path / "vms" / "ws-g"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    driver, _spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws-g",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(share="ghost", guest="/mnt/ghost"),),
    )
    with pytest.raises(MicrovmError, match="is not in the config"):
        await driver._start_fs_daemons(spec, vm_dir)
    settings.mounts = MountSettings(
        shares=(MountShare(name="ghost", path=str(tmp_path / "void")),)
    )
    with pytest.raises(MicrovmError, match="missing or not a directory"):
        await driver._start_fs_daemons(spec, vm_dir)


async def test_live_socket_refuses_a_second_start(tmp_path: Path) -> None:
    """A socket still serving (a daemon from a prior life that the
    stale sweep cannot prove dead) refuses the spawn by name —
    never a silent second daemon behind one tag."""
    import socket as socket_mod

    from msks.microvm.errors import MicrovmError

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws-s"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(share_dir)),)
    )
    driver, _spawned = driver_over(settings)
    live = fs_socket_path(vm_dir, "proj")
    server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    server.bind(str(live))
    server.listen(1)
    try:
        spec = VmSpec(
            workspace_id="ws-s",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            mounts=(MountSpec(share="proj", guest="/mnt/proj"),),
        )
        with pytest.raises(MicrovmError, match="live virtiofs daemon"):
            await driver._start_fs_daemons(spec, vm_dir)
    finally:
        server.close()


async def test_stop_is_idempotent_and_kills_a_hang(tmp_path: Path) -> None:
    """Stopping twice is a no-op the second time, and a daemon
    that ignores SIGTERM dies by SIGKILL past the grace."""
    import msks.microvm.local as local_mod

    local_mod.FS_TERM_GRACE_S = 0.1
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws-h"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(share_dir)),)
    )
    driver, _spawned = driver_over(settings)

    class HangingProc(FakeProc):
        """Ignores terminate (returncode stays None) and parks in
        wait until kill."""

        def terminate(self) -> None:
            self.terminated = True  # but stays alive

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.02)
            return self.returncode

    async def hang_spawn(binary, args, log_path):
        return HangingProc()

    driver._spawn_fs = hang_spawn
    spec = VmSpec(
        workspace_id="ws-h",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(share="proj", guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    proc = driver._fs_procs["ws-h"][0]
    await driver._stop_fs_daemons("ws-h")
    assert proc.killed  # ignored the TERM, died by KILL
    await driver._stop_fs_daemons("ws-h")  # idempotent second call


def test_unsafe_share_name_refuses_at_load() -> None:
    """A name that would build ambiguous paths (a slash, a space)
    refuses at settings load with the rule named."""
    from msks.settings import mount_shares_from

    with pytest.raises(ValueError, match="letters, digits, dot"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [{"name": "a/b", "path": "/srv"}]
                )
            }
        )


async def test_real_spawn_wraps_a_missing_binary(tmp_path: Path) -> None:
    """The real spawn seam's named refusal: a virtiofsd binary
    that does not exist fails as MicrovmError naming the doctor,
    not a bare FileNotFoundError from the boot path."""
    from msks.microvm.errors import MicrovmError

    class BareApp:
        pass

    bare = BareApp()
    bare.state = type("State", (), {})()
    bare.state.settings = Settings()
    driver = LocalCloudHypervisor(bare)
    with pytest.raises(MicrovmError, match="virtiofsd binary not found"):
        await driver._spawn_fs("virtiofsd-absent", [], tmp_path / "log")


async def test_terminate_of_an_external_death_is_tolerated(
    tmp_path: Path,
) -> None:
    """A daemon that died between the registry and the teardown:
    terminate's ProcessLookupError is suppressed, the stop
    completes."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws-e"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(share_dir)),)
    )
    driver, _spawned = driver_over(settings)

    class ExternalDeath(FakeProc):
        """terminate finds nothing: the process died on its own."""

        def terminate(self) -> None:
            raise ProcessLookupError

    async def dying_spawn(binary, args, log_path):
        return ExternalDeath()

    driver._spawn_fs = dying_spawn
    spec = VmSpec(
        workspace_id="ws-e",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(share="proj", guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    await driver._stop_fs_daemons("ws-e")


async def test_untracked_sweep_tolerates_garbage_pidfiles(
    tmp_path: Path,
) -> None:
    """The restarted-daemon sweep: a garbage pidfile reads as None
    and is swept beside a serving one's kill, and the second
    sweep over an empty directory is a no-op."""
    vm_dir = tmp_path / "vms" / "ws-u"
    vm_dir.mkdir(parents=True)
    driver, _spawned = driver_over(Settings())
    socket = fs_socket_path(vm_dir, "proj")
    garbage = fs_pidfile_path(vm_dir, "junk")
    garbage.write_text("not-a-pid", encoding="utf-8")
    pidfile = fs_pidfile_path(vm_dir, "proj")
    serving = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        str(socket),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pidfile.write_text(str(serving.pid), encoding="utf-8")
    driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
    await driver._kill_untracked_fs_daemons("ws-u")
    assert not garbage.exists() and not pidfile.exists()
    await driver._kill_untracked_fs_daemons("ws-u")  # empty: no-op
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(serving.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the serving daemon survived its pidfile kill")


def test_drift_issue_passes_a_pinned_unchanged_path(tmp_path):
    """The no-baseline and unchanged-path shapes attach: a share
    that did not exist at load (resolved None) and one whose
    current resolution equals the load-time pin both clear the
    drift check."""
    from msks.server.api.rows import drift_issue

    share = tmp_path / "proj"
    share.mkdir()
    mount = MountSpec(share="proj", guest="/mnt/proj")
    assert drift_issue(mount, MountShare("proj", str(share))) is None
    assert (
        drift_issue(
            mount,
            MountShare("proj", str(share), resolved=str(share)),
        )
        is None
    )


async def test_stop_skips_an_already_exited_daemon(tmp_path: Path) -> None:
    """A tracked daemon that exited on its own between the
    registry and the teardown: the TERM pass finds a returncode
    and skips it, the stop completes."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws-x"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(share_dir)),)
    )
    driver, _spawned = driver_over(settings)

    class ExitedProc(FakeProc):
        """Died on its own before the teardown looked."""

        def __init__(self) -> None:
            super().__init__()
            self.returncode = 0

    async def exited_spawn(binary, args, log_path):
        return ExitedProc()

    driver._spawn_fs = exited_spawn
    spec = VmSpec(
        workspace_id="ws-x",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(share="proj", guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    await driver._stop_fs_daemons("ws-x")

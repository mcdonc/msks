"""The host-side attach path (#620, part #622).

Covers the virtiofsd argument grammar (the coherence and identity
contracts), the cloud-hypervisor fs device entries, the spawn and
teardown lifecycle (tracked daemons, the restarted-daemon pidfile
fallback, the recycled-pid guard), the start path's drift
refusal, and the doctor rows.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
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
    guest_user_ids,
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


async def socket_sleeper(
    socket: Path, ack: Path
) -> asyncio.subprocess.Process:
    """A process that stays alive with the socket in its argv —
    the virtiofsd stand-in for the pidfile-guard tests. GNU sleep
    refuses a non-numeric operand, so ``sleep 30 <socket>`` dies
    at once and races every later /proc read (#645); the test
    interpreter sleeps instead, carrying the socket as trailing
    argv. It touches ``ack`` when it reaches the sleep, and the
    helper waits for that word: a freshly exec'd pid's /proc
    view settles late — for a window after the spawn returns the
    cmdline reads empty — so the spawn's return is not yet proof
    the argv is readable."""
    ack.write_text("", encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, time; open(sys.argv[1], 'a').write('up'); time.sleep(30)",
        str(ack),
        str(socket),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10.0
        while ack.read_text(encoding="utf-8") == "":
            if proc.returncode is not None:
                raise AssertionError(
                    "the socket sleeper died at startup"
                    f" (exit {proc.returncode})"
                )
            if time.monotonic() > deadline:
                raise AssertionError(
                    "the socket sleeper never reached its sleep"
                )
            await asyncio.sleep(0.01)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    finally:
        ack.unlink(missing_ok=True)
    return proc


def test_sandbox_follows_privilege() -> None:
    """A root daemon gets the namespace sandbox; an unprivileged
    one cannot unshare, so it runs unsandboxed — the share set
    itself stays operator-chosen either way."""
    assert sandbox_mode(0) == "namespace"
    assert sandbox_mode(1000) == "none"


def test_guest_user_ids_follow_the_login() -> None:
    """The guest identity a share presents as (#631): the image's
    own account for the logins that ride it — none, root, msks —
    and the seed-provisioned login user's uid otherwise."""
    assert guest_user_ids(None) == (1000, 1000)
    assert guest_user_ids("") == (1000, 1000)
    assert guest_user_ids("root") == (1000, 1000)
    assert guest_user_ids("msks") == (1000, 1000)
    assert guest_user_ids("chrism") == (1001, 1001)


async def test_the_spawn_presents_as_the_login_user(
    tmp_path: Path,
) -> None:
    """The spawn path derives the squash-host target from the
    spec's login user (#631): a workspace that names one mounts
    its shares as that user's guest identity, a loginless one as
    the image's own account — pinned on the real arg vectors."""
    shared = tmp_path / "share"
    shared.mkdir()
    vm_dir = tmp_path / "vms" / "ws-owner"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="share", path=str(shared)),)
    )
    driver, spawned = driver_over(settings)
    for login_user, expect in ((None, 1000), ("chrism", 1001)):
        spawned.clear()
        spec = VmSpec(
            workspace_id="ws-owner",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            login_user=login_user,
            mounts=(MountSpec(share="share", guest="/mnt/s"),),
        )
        await driver._start_fs_daemons(spec, vm_dir)
        await driver._stop_fs_daemons("ws-owner")
        args = spawned[0][1]
        joined = " ".join(args)
        assert f"squash-host:0:{expect}:{WHOLE_RANGE}" in joined, (
            login_user,
            args,
        )


def test_fs_daemon_args_carry_the_contracts(tmp_path: Path) -> None:
    """One daemon's argument vector: cache never (the coherence
    contract), whole-range uid/gid squash to the daemon's own
    identity in both directions — writes from the guest land as
    the daemon's user, reads present as the workspace user's
    guest identity (#631) — the xattr prefix map, the sandbox,
    and --readonly exactly when the attach is read-only."""
    socket = tmp_path / "fs-share.sock"
    args = fs_daemon_args(
        "/srv/share",
        "share",
        socket,
        uid=1000,
        gid=100,
        guest_uid=1001,
        guest_gid=1002,
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
    # The host-to-guest half (#631): every host uid/gid presents
    # as the workspace user's guest identity.
    assert f"squash-host:0:1001:{WHOLE_RANGE}" in joined
    assert f"squash-host:0:1002:{WHOLE_RANGE}" in joined
    assert joined.count("--translate-uid") == 2
    assert joined.count("--translate-gid") == 2
    assert XATTRMAP in args
    assert "--readonly" not in args
    ro = fs_daemon_args(
        "/srv/share",
        "share",
        socket,
        uid=0,
        gid=0,
        guest_uid=1000,
        guest_gid=1000,
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
    assert "shared" not in bare["memory"]
    fs = [
        {
            "tag": "share",
            "socket": "/sock",
            "num_queues": 1,
            "queue_size": QUEUE_SIZE,
        }
    ]
    with_fs = vm_config(spec, [], fs=fs)
    assert with_fs["fs"] == fs
    # vhost-user maps guest RAM shared with the daemons; the live
    # boot pins the field (``shared`` — the v52 name, and an
    # unknown key like shared_memory is silently ignored and the
    # boot still refuses).
    assert with_fs["memory"]["shared"] is True


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


async def test_fs_pid_serves_matches_the_socket(tmp_path: Path) -> None:
    """The recycled-pid guard (#56's rule, the fs shape): only a
    process whose command line names this workspace's socket dies
    by pidfile — a real process whose argv carries the socket
    qualifies, the same pid queried for another socket does not."""

    socket = Path("/unique/fs-probe.sock")
    proc = await socket_sleeper(socket, tmp_path / "sleeper-up")
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


def socket_from_args(args: list[str]) -> Path:
    """The --socket-path value of one fake spawn's args."""
    return Path(args[args.index("--socket-path") + 1])


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
        socket_from_args(args).touch()
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
        socket_from_args(args).touch()
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
    serving = await socket_sleeper(socket, vm_dir / "sleeper-up")
    pidfile.write_text(str(serving.pid), encoding="utf-8")
    driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
    await driver._kill_untracked_fs_daemons("ws3")
    assert not pidfile.exists()
    # SIGTERM delivery is asynchronous: poll the death briefly.
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

    real_grace = local_mod.FS_TERM_GRACE_S
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
        socket_from_args(args).touch()
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
    try:
        await driver._stop_fs_daemons("ws-h")
        assert proc.killed  # ignored the TERM, died by KILL
        await driver._stop_fs_daemons("ws-h")  # idempotent second call
    finally:
        local_mod.FS_TERM_GRACE_S = real_grace


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
        socket_from_args(args).touch()
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
    serving = await socket_sleeper(socket, vm_dir / "sleeper-up")
    pidfile.write_text(str(serving.pid), encoding="utf-8")
    driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
    await driver._kill_untracked_fs_daemons("ws-u")
    assert not garbage.exists() and not pidfile.exists()
    await driver._kill_untracked_fs_daemons("ws-u")  # empty: no-op
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
        socket_from_args(args).touch()
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


async def test_share_name_length_is_capped_at_the_tag_limit() -> None:
    """virtiofsd refuses a tag longer than 36 bytes at exec; the
    name rule refuses at settings load instead — a 37-byte name
    never reaches a dying daemon."""
    from msks.settings import mount_shares_from

    assert safe_share_name("a" * 36)
    assert not safe_share_name("a" * 37)
    with pytest.raises(ValueError, match="36 bytes"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [{"name": "a" * 37, "path": "/srv"}]
                )
            }
        )
    assert not safe_share_name("café")


async def test_readonly_composes_from_both_sides(tmp_path: Path) -> None:
    """--readonly arrives when the mount asked ro, when the
    operator narrowed the share, and when both — and only then: a
    plain rw mount on a writable share spawns writable. The
    composition is pinned on the real arg vectors."""
    plain = tmp_path / "plain"
    plain.mkdir()
    narrowed = tmp_path / "narrowed"
    narrowed.mkdir()
    vm_dir = tmp_path / "vms" / "ws-ro"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(
            MountShare(name="plain", path=str(plain)),
            MountShare(name="narrowed", path=str(narrowed), writable=False),
        )
    )
    driver, spawned = driver_over(settings)
    # One spec per combination (the row rules refuse duplicate
    # guests); the transient narrowing shape (valid at set, denied
    # at the route after a reload) rides the fourth combination.
    for mount, expect_ro in (
        (MountSpec(share="plain", guest="/mnt/p"), False),
        (MountSpec(share="plain", guest="/mnt/p", mode="ro"), True),
        (MountSpec(share="narrowed", guest="/mnt/n", mode="ro"), True),
        (MountSpec(share="narrowed", guest="/mnt/n"), True),
    ):
        spawned.clear()
        spec = VmSpec(
            workspace_id="ws-ro",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            mounts=(mount,),
        )
        await driver._start_fs_daemons(spec, vm_dir)
        await driver._stop_fs_daemons("ws-ro")
        args = spawned[0][1]
        assert ("--readonly" in args) is expect_ro, (mount, args)


async def test_spawn_refuses_a_path_drifted_past_the_pin(
    tmp_path: Path,
) -> None:
    """The drift guard holds at spawn too: a share retargeted
    between the route's derivation and the daemon spawn never
    attaches the new target — the last line refuses by name, not
    only the filter."""
    from msks.microvm.errors import MicrovmError

    real = tmp_path / "real"
    real.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    share = MountShare(name="moved", path=str(link), resolved=str(real))
    vm_dir = tmp_path / "vms" / "ws-d"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(shares=(share,))
    driver, _spawned = driver_over(settings)
    link.unlink()
    link.symlink_to(other)  # past the pin, before the spawn
    spec = VmSpec(
        workspace_id="ws-d",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(share="moved", guest="/mnt/m"),),
    )
    with pytest.raises(MicrovmError, match="drifted since load"):
        await driver._start_fs_daemons(spec, vm_dir)


async def test_untracked_sweep_escalates_to_kill(tmp_path: Path) -> None:
    """A TERM-immortal daemon cannot wedge the workspace behind
    its own live socket: the sweep waits out the grace, KILLs,
    and the pidfile stays until the pid dies."""
    import signal as signal_mod

    import msks.microvm.local as local_mod

    real_grace = local_mod.FS_TERM_GRACE_S
    local_mod.FS_TERM_GRACE_S = 0.2
    try:
        vm_dir = tmp_path / "vms" / "ws-imm"
        vm_dir.mkdir(parents=True)
        driver, _spawned = driver_over(Settings())
        socket = fs_socket_path(vm_dir, "proj")
        immortal = await socket_sleeper(socket, vm_dir / "sleeper-up")
        # A TERM-immortal shape: a SIGSTOP'd process queues the
        # TERM undelivered (the death poll sees it alive), and
        # only the KILL lands — the escalation the sweep owes.
        os.kill(immortal.pid, signal_mod.SIGSTOP)
        await asyncio.sleep(0.2)
        pidfile = fs_pidfile_path(vm_dir, "proj")
        pidfile.write_text(str(immortal.pid), encoding="utf-8")
        driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
        await driver._kill_untracked_fs_daemons("ws-imm")
        assert not pidfile.exists()
        assert await local_mod.pid_dead_by(immortal.pid, 2.0)
    finally:
        local_mod.FS_TERM_GRACE_S = real_grace
        with contextlib.suppress(ProcessLookupError):
            os.kill(immortal.pid, signal_mod.SIGCONT)
        with contextlib.suppress(ProcessLookupError):
            immortal.kill()
        await immortal.wait()


async def test_pid_dead_by_reports_a_live_pid() -> None:
    """The death-wait's two answers: a dead pid is True at once, a
    live one runs out the short deadline and answers False (then
    dies by KILL in the caller)."""
    import msks.microvm.local as local_mod

    live = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await local_mod.pid_dead_by(live.pid, 0.1) is False
    finally:
        live.kill()
        await live.wait()
    assert await local_mod.pid_dead_by(live.pid, 1.0) is True


async def test_a_daemon_dying_at_exec_names_its_log(tmp_path: Path) -> None:
    """The readiness wait's first failure shape: a daemon whose
    proc already exited refuses naming the log, not an opaque CH
    connect error downstream."""
    from msks.microvm.errors import MicrovmError

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws-dead"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(share_dir)),)
    )
    driver, _spawned = driver_over(settings)

    class DiedAtExec(FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = 1

    async def dead_spawn(binary, args, log_path):
        return DiedAtExec()

    driver._spawn_fs = dead_spawn
    spec = VmSpec(
        workspace_id="ws-dead",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(share="proj", guest="/mnt/proj"),),
    )
    with pytest.raises(MicrovmError, match="exited at start"):
        await driver._start_fs_daemons(spec, vm_dir)
    assert "ws-dead" not in driver._fs_procs


async def test_a_silent_daemon_refuses_past_the_grace(tmp_path: Path) -> None:
    """The readiness wait's second shape: a proc that lives but
    never serves its socket refuses past the grace, naming the
    log."""
    import msks.microvm.local as local_mod
    from msks.microvm.errors import MicrovmError

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = tmp_path / "vms" / "ws-silent"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(share_dir)),)
    )
    driver, _spawned = driver_over(settings)
    refused: list[FakeProc] = []

    async def silent_spawn(binary, args, log_path):
        proc = FakeProc()  # alive, socket never appears
        refused.append(proc)
        return proc

    driver._spawn_fs = silent_spawn
    real_grace = local_mod.FS_TERM_GRACE_S
    local_mod.FS_TERM_GRACE_S = 0.2
    try:
        with pytest.raises(MicrovmError, match="never served"):
            await driver._start_fs_daemons(
                VmSpec(
                    workspace_id="ws-silent",
                    kernel=Path("/k"),
                    rootfs=Path("/r"),
                    mounts=(MountSpec(share="proj", guest="/mnt/proj"),),
                ),
                vm_dir,
            )
        assert "ws-silent" not in driver._fs_procs
        # The refused daemon dies inside the refusal (round 2's
        # fix, pinned): no untracked survivor behind a failed
        # start.
        assert [proc.killed for proc in refused] == [True]
    finally:
        local_mod.FS_TERM_GRACE_S = real_grace


async def test_stop_fs_pid_takes_the_term_fast_path() -> None:
    """A TERM-honoring daemon answers True on the fast path — no
    KILL needed."""
    import msks.microvm.local as local_mod

    sleeper = await asyncio.create_subprocess_exec(
        "sleep",
        "30",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await local_mod.stop_fs_pid(sleeper.pid) is True
    finally:
        with contextlib.suppress(ProcessLookupError):
            sleeper.kill()
        await sleeper.wait()


async def test_sweep_keeps_a_killproof_daemons_pidfile(tmp_path):
    """A daemon that outlives even SIGKILL (D-state) keeps its
    pidfile: the sweep never strands the only record of a live
    daemon."""
    import msks.microvm.local as local_mod

    vm_dir = tmp_path / "vms" / "ws-dstate"
    vm_dir.mkdir(parents=True)
    socket = fs_socket_path(vm_dir, "proj")
    live = await socket_sleeper(socket, vm_dir / "sleeper-up")
    pidfile = fs_pidfile_path(vm_dir, "proj")
    pidfile.write_text(str(live.pid), encoding="utf-8")

    async def killproof(pid):
        return False  # survived even SIGKILL

    real_stop = local_mod.stop_fs_pid
    local_mod.stop_fs_pid = killproof
    try:
        await local_mod.sweep_fs_pidfile(vm_dir, pidfile)
    finally:
        local_mod.stop_fs_pid = real_stop
        with contextlib.suppress(ProcessLookupError):
            live.kill()
        await live.wait()
    assert pidfile.exists()

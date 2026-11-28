"""The host-side attach path (#620, part #622).

Covers the virtiofsd argument grammar (the coherence and identity
contracts), the cloud-hypervisor fs device entries, the spawn and
teardown lifecycle (tracked daemons, the restarted-daemon pidfile
fallback, the recycled-pid guard), the start path's gate
refusal, and the doctor rows.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import msks.microvm.fsdaemons as fsdaemons_module
import pytest
from msks.microvm.errors import MicrovmError
from msks.microvm.fsdaemons import (
    QUEUE_SIZE,
    VIRTIOFSD_AS_LOCATIONS,
    VIRTIOFSD_FLOOR,
    WHOLE_RANGE,
    XATTRMAP,
    fs_daemon_args,
    fs_device_entries,
    fs_pidfile_path,
    fs_socket_path,
    guest_user_ids,
    helper_lane,
    resolve_virtiofsd_as,
    sandbox_mode,
    virtiofsd_floor_message,
    virtiofsd_version,
)
from msks.microvm.local import (
    LocalCloudHypervisor,
    bind_fs_socket,
    fs_exit_nudge,
    fs_pid_serves,
    proc_start_time,
    read_fs_identity,
    sweep_fs_pidfile,
    vm_config,
)
from msks.microvm.local.procs import pid_gone, start_time_from_stat
from msks.settings import MountSettings, Settings, VmmSettings
from msks.spec.mounts import MountSpec, mount_tag
from msks.spec.vm import VmSpec
from test_api import auth, client  # noqa: F401 (fixture)

# The shallow vm-dir roots this module's short_vm_dir() made —
# swept after each test however it ended.
_SHORT_ROOTS: list[Path] = []


@pytest.fixture(autouse=True)
def sweep_short_roots():
    """Drop the shallow roots short_vm_dir() made, after every
    test however it ended."""
    yield
    for root in _SHORT_ROOTS:
        shutil.rmtree(root, ignore_errors=True)
    _SHORT_ROOTS.clear()


def short_vm_dir(name: str) -> Path:
    """A vm directory under a shallow mkdtemp in /tmp (the
    local_driver env fixture's rule, the login-user test's
    above): the daemon binds each share's vhost socket itself
    (#1088), and under pytest-xdist the worker's basetemp gains
    a popen-gwN component that pushes a deep per-test tree past
    AF_UNIX's 108-byte path limit."""
    root = Path(tempfile.mkdtemp(prefix="msks-fs-", dir="/tmp"))
    _SHORT_ROOTS.append(root)
    vm_dir = root / name
    vm_dir.mkdir(parents=True)
    return vm_dir


async def socket_sleeper(
    socket: Path, ack: Path, *, ignore_term: bool = False
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
    the argv is readable. ``ignore_term`` installs a SIGTERM
    handler that keeps sleeping — the TERM-immortal shape every
    host can schedule (the SIGSTOP shape pins the stronger
    undelivered-signal case, which darwin's event loop cannot
    wait out)."""
    ack.write_text("", encoding="utf-8")
    body = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'a').write('up')\n"
        "time.sleep(30)\n"
        if ignore_term
        else "import sys, time; open(sys.argv[1], 'a')"
        ".write('up'); time.sleep(30)"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        body,
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
    the image's own account — pinned on the real arg vectors.

    The state dir lives under a shallow mkdtemp in /tmp (the
    local_driver env fixture's rule): under pytest-xdist the
    worker's basetemp gains a popen-gwN component, and the deep
    per-test tree pushes the socket path past AF_UNIX's limit —
    the stale probe then answers "path too long" instead of the
    taxonomy, and the guard refuses a spawn it should sweep.
    """
    shared = tmp_path / "share"
    shared.mkdir()
    state_dir = Path(tempfile.mkdtemp(prefix="msks-test-", dir="/tmp"))
    vm_dir = state_dir / "vms" / "ws-owner"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)
    try:
        for login_user, expect in ((None, 1000), ("chrism", 1001)):
            spawned.clear()
            spec = VmSpec(
                workspace_id="ws-owner",
                kernel=Path("/k"),
                rootfs=Path("/r"),
                login_user=login_user,
                mounts=(MountSpec(host=str(shared), guest="/mnt/s"),),
            )
            await driver._start_fs_daemons(spec, vm_dir)
            await driver._stop_fs_daemons("ws-owner")
            args = spawned[0][1]
            joined = " ".join(args)
            assert f"squash-host:0:{expect}:{WHOLE_RANGE}" in joined, (
                login_user,
                args,
            )
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_fs_daemon_args_carry_the_contracts() -> None:
    """One daemon's argument vector: cache never (the coherence
    contract), whole-range uid/gid squash to the exec identity
    in both directions — writes from the guest land as the uid
    the daemon execs the share as (#1088: the workspace owner's
    in the helper lane, the daemon's own in the shortcut lane),
    reads present as the workspace user's guest identity (#631)
    — the xattr prefix map, the sandbox, the listening fd, and
    --readonly exactly when the attach is read-only."""
    args = fs_daemon_args(
        "/srv/share",
        "share",
        7,
        uid=1000,
        gid=100,
        guest_uid=1001,
        guest_gid=1002,
        readonly=False,
        sandbox="none",
    )
    joined = " ".join(args)
    # The listening fd the daemon bound itself (#1088): the
    # socket is the child's by descriptor, never by path.
    assert "--fd" in args and args[args.index("--fd") + 1] == "7"
    assert "--socket-path" not in args
    assert "/srv/share" in args
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
        9,
        uid=0,
        gid=0,
        guest_uid=1000,
        guest_gid=1000,
        readonly=True,
        sandbox="namespace",
    )
    assert "--sandbox namespace" in " ".join(ro)
    assert "--readonly" in ro


def test_helper_lane_picks_the_exec_uid() -> None:
    """The lane rule (#1088): an owner other than the daemon's
    own uid execs through the helper as that uid; the daemon's
    own uid — and a spec without an owner, the shape built
    outside the row path — execs directly, no helper anywhere in
    the process tree."""
    assert helper_lane(4242, 1000) == 4242
    assert helper_lane(1000, 1000) is None
    assert helper_lane(None, 1000) is None
    assert helper_lane(0, 1000) == 0


def test_resolve_virtiofsd_as_passes_named_paths_through() -> None:
    """A value with a slash is the operator's own path (#1088):
    used exactly as named, whatever PATH and the known locations
    carry."""
    assert (
        resolve_virtiofsd_as(
            "/opt/virtiofsd-as", lambda n: "/usr/bin/x", lambda p: True
        )
        == "/opt/virtiofsd-as"
    )


def test_resolve_virtiofsd_as_tries_both_names_then_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare name resolves on PATH under either install name
    (#1088) — the stock virtiofsd-as first, then the NixOS
    wrapper's msks-virtiofsd-as — then the wheel's bundled
    binary, then the setup tools' install location."""
    monkeypatch.setattr(fsdaemons_module, "BUNDLED_VIRTIOFSD_AS", Path("/no"))
    assert (
        resolve_virtiofsd_as(
            "virtiofsd-as",
            lambda n: "/usr/bin/virtiofsd-as" if n == "virtiofsd-as" else None,
            lambda p: False,
        )
        == "/usr/bin/virtiofsd-as"
    )
    assert (
        resolve_virtiofsd_as(
            "virtiofsd-as",
            lambda n: (
                "/run/wrappers/bin/msks-virtiofsd-as"
                if n == "msks-virtiofsd-as"
                else None
            ),
            lambda p: False,
        )
        == "/run/wrappers/bin/msks-virtiofsd-as"
    )
    assert (
        resolve_virtiofsd_as(
            "virtiofsd-as",
            lambda n: None,
            lambda p: p == VIRTIOFSD_AS_LOCATIONS[0],
        )
        == VIRTIOFSD_AS_LOCATIONS[0]
    )
    assert (
        resolve_virtiofsd_as("virtiofsd-as", lambda n: None, lambda p: False)
        is None
    )


def test_resolve_virtiofsd_as_finds_the_bundled_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The platform wheel's bundled helper (#1087, #1088): the
    binary inside the package answers before the install
    locations, and nothing on PATH needs to carry it."""
    bundled = tmp_path / "bundled-virtiofsd-as"
    bundled.write_bytes(b"#!bin\n")
    bundled.chmod(0o755)
    monkeypatch.setattr(fsdaemons_module, "BUNDLED_VIRTIOFSD_AS", bundled)
    assert resolve_virtiofsd_as(
        "virtiofsd-as", lambda n: None, lambda p: True
    ) == str(bundled)


def test_xattrmap_denies_security_capability() -> None:
    """The xattr map denies ``security.capability`` to the guest
    and leaves the name unmapped (#657). Denied, because a root
    daemon keeps CAP_SETFCAP on purpose and would honor a
    guest's file-capability writes on host files — the map, not
    the daemon's privilege, holds the boundary. Unmapped,
    because a remapped name makes virtiofsd clear file
    capabilities itself on every write, and its ``fremovexattr``
    re-checks the inode's mode even through a fd opened writable
    at creation — so a file created without write bits (git's
    loose-object tempfiles) fails every write with EACCES.
    Unmapped, the host kernel clears capabilities on write
    itself. The ``bad`` rule must lead the catch-all ``map``
    rule: the first matching rule applies."""
    bad_rule, map_rule = XATTRMAP.split(":map:", 1)
    assert bad_rule == ":bad:client:security.capability::"
    assert map_rule == ":user.virtiofs.:"


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


def test_the_tag_derives_from_the_host_path() -> None:
    """The tag names a virtiofs device (#672): the hs- prefix plus
    the first eight hex of the canonical host path's sha256 —
    deterministic, so the same path is the same device across
    starts, and the same for every spelling of the path."""
    mount = MountSpec(host="/srv/projects", guest="/mnt/p")
    tag = mount_tag(mount)
    assert tag.startswith("hs-") and len(tag) == len("hs-") + 8
    assert mount_tag(MountSpec(host="/srv/projects", guest="/x")) == tag
    assert mount_tag(MountSpec(host="/srv/other", guest="/mnt/p")) != tag


def test_pidfile_helpers(tmp_path: Path) -> None:
    """The socket and pidfile stems derive from the share name
    inside the workspace's vm directory."""
    assert fs_socket_path(tmp_path, "data") == tmp_path / "fs-data.sock"
    assert fs_pidfile_path(tmp_path, "data") == tmp_path / "fs-data.pid"


def test_read_fs_identity_tolerates_garbage(tmp_path: Path) -> None:
    """A pidfile's identity pair (#1088): the pid and its start
    time when the daemon wrote both, the bare pid (a pre-#1088
    pidfile) as the legacy arm, and (None, None) for anything no
    daemon wrote — swept, never trusted enough to kill."""
    good = tmp_path / "fs-good.pid"
    good.write_text("4242 987654\n", encoding="utf-8")
    assert read_fs_identity(good) == (4242, 987654)
    legacy = tmp_path / "fs-legacy.pid"
    legacy.write_text("4242\n", encoding="utf-8")
    assert read_fs_identity(legacy) == (4242, None)
    bad = tmp_path / "fs-bad.pid"
    bad.write_text("not-a-pid", encoding="utf-8")
    assert read_fs_identity(bad) == (None, None)
    torn = tmp_path / "fs-torn.pid"
    torn.write_text("4242 not-a-start\n", encoding="utf-8")
    assert read_fs_identity(torn) == (None, None)
    assert read_fs_identity(tmp_path / "fs-gone.pid") == (None, None)


async def test_fs_pid_serves_matches_the_socket(tmp_path: Path) -> None:
    """The recycled-pid guard (#56's rule, the fs shape): only a
    process whose command line names this workspace's socket dies
    by pidfile — a real process whose argv carries the socket
    qualifies, the same pid queried for another socket does not."""

    # The parse's defensive arms first (#1088): a stat line that
    # is not one reads as unreadable, never as a number.
    assert start_time_from_stat(b"no-parens-at-all") is None
    assert start_time_from_stat(b"1 (sh) S 1") is None
    assert start_time_from_stat(b"1 (sh) S " + b"x " * 19 + b"99") is None
    assert (
        start_time_from_stat(
            b"123 (a name with spaces) R 1 2 3 4 5 6 7 8 9 10 11 "
            b"12 13 14 15 16 17 18 4242"
        )
        == 4242
    )

    socket = Path("/unique/fs-probe.sock")
    proc = await socket_sleeper(socket, tmp_path / "sleeper-up")
    try:
        # The legacy arm (#56's cmdline guard, the pre-#1088
        # pidfile's): a process whose argv carries the socket.
        assert fs_pid_serves(proc.pid, None, socket) is True
        assert fs_pid_serves(proc.pid, None, Path("/other/fs.sock")) is False
        assert fs_pid_serves(proc.pid + 1_000_000, None, socket) is False
        # The identity arm (#1088): a start time that matches is
        # the very process — whatever its argv now names — and a
        # start time that does not never is, recycled pid or no.
        # procfs is the Linux-only surface (#691): a darwin dev
        # host reads no start time and keeps the legacy arm.
        if sys.platform != "linux":
            pytest.skip("the identity arm reads /proc (Linux-only)")
        start = proc_start_time(proc.pid)
        assert start is not None
        assert fs_pid_serves(proc.pid, start, Path("/anywhere")) is True
        assert fs_pid_serves(proc.pid, start + 1, Path("/anywhere")) is False
        assert fs_pid_serves(proc.pid + 1_000_000, start, socket) is False
    finally:
        proc.kill()
        await proc.wait()


async def test_start_skips_a_gate_refused_host_path(  # noqa: F811
    client,  # noqa: F811
    tmp_path,
    caplog,
):
    """The host form's skip at start (#672): a mount whose root
    the policy gate no longer allows — here a symlink retargeted
    to walk out of the allowed root, the escape rule #667 names
    — skips with mount.skipped carrying the gate's message; the
    start proceeds."""
    http, app, _stub = client
    caplog.set_level(logging.INFO, logger="msks.events")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    link = allowed / "tree"
    link.symlink_to(allowed / "real")
    (allowed / "real").mkdir()
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-escape", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid, [{"host": str(link), "guest": "/mnt/tree", "mode": "rw"}]
    )
    # The operator retargets the symlink past the allowed root
    # after the set: the gate refuses at read, the skip is state.
    outside = tmp_path / "outside"
    outside.mkdir()
    link.unlink()
    link.symlink_to(outside)
    published = []
    real_publish = app.state.hub.publish

    async def spy_publish(event: str, data: dict, owner=None) -> None:
        published.append((event, data))
        await real_publish(event, data, owner=owner)

    app.state.hub.publish = spy_publish
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    # The start's own status frames (#1019) publish first; the
    # skip is the mount family's frame.
    skips = [frame for frame in published if frame[0] == "mount.skipped"]
    assert "outside the allowed host-share roots" in skips[0][1]["detail"]
    assert skips[0][1]["host"] == str(link)
    # #762: the mount-skip family's publish journals, one line
    # per frame.
    lines = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "msks.events"
    ]
    assert any(line["event"] == "mount.skipped" for line in lines)


async def test_start_passes_only_attachable_mounts(client, tmp_path):  # noqa: F811
    """The seam attaches exactly the attachable subset (#620,
    part #622): a skipped mount (its root no longer allowed)
    never reaches the spec the driver launches."""
    http, app, stub = client
    live = tmp_path / "live"
    live.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-mix", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid,
        [
            {"host": str(gone), "guest": "/mnt/gone", "mode": "rw"},
            {"host": str(live), "guest": "/mnt/live", "mode": "rw"},
        ],
    )
    # The reload narrows the allowed roots to live's alone: gone
    # skips at read, live attaches.
    app.state.settings.mounts = MountSettings(host_roots=(str(live),))
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
        MountSpec(host=str(live), guest="/mnt/live", mode="rw"),
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


def driver_over(
    settings: Settings, version: tuple[int, ...] | None = (1, 13, 3)
) -> LocalCloudHypervisor:
    """A driver over a throwaway app with a settings override
    and a spawn seam that records instead of exec'ing; the
    version seam reports at the floor by default (#655), so the
    lifecycle tests run hermetically — the real probe execs the
    host's virtiofsd. The serve grace runs at milliseconds
    (#1088): the wait is real, the budget is the suite's."""

    class App:
        pass

    app = App()
    app.state = type("State", (), {})()
    app.state.settings = settings
    driver = LocalCloudHypervisor(app)
    spawned: list[tuple[str, list[str], int]] = []

    async def fake_spawn_fs(binary, args, log_path, fd, missing):
        spawned.append((binary, args, fd))
        return FakeProc()

    driver._spawn_fs = fake_spawn_fs
    driver.serve_grace_s = 0.01

    async def fake_version(binary):
        return version

    driver._virtiofsd_version = fake_version
    return driver, spawned


async def test_fs_daemons_spawn_and_stop(tmp_path: Path) -> None:
    """A start with two mounts spawns one daemon per share with
    the pinned flags, and the teardown stops exactly them and
    sweeps their pidfiles."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vm_dir = short_vm_dir("ws1")
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path),
    )
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws1",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(
            MountSpec(host=str(share_dir), guest="/mnt/proj"),
            MountSpec(host=str(data_dir), guest="/mnt/data", mode="ro"),
        ),
    )
    entries = await driver._start_fs_daemons(spec, vm_dir)
    tags = [mount_tag(m) for m in spec.mounts]
    assert sorted(entry["tag"] for entry in entries) == sorted(tags)
    assert len(spawned) == 2
    binary, args, fd = spawned[0]
    assert binary == "virtiofsd"
    assert str(share_dir) in args
    # The listening fd the daemon bound itself (#1088) is the
    # one the argv names — the socket is the child's by
    # descriptor, never by path.
    assert args[args.index("--fd") + 1] == str(fd)
    assert "--socket-path" not in args
    assert fs_pidfile_path(vm_dir, mount_tag(spec.mounts[0])).is_file()
    assert fs_pidfile_path(vm_dir, mount_tag(spec.mounts[1])).is_file()
    # The daemon-bound socket files exist while the shares run
    # (#1088) and leave with the teardown — the child never
    # knew the path, so the daemon unlinks it.
    first_tag = mount_tag(spec.mounts[0])
    assert fs_socket_path(vm_dir, first_tag).exists()
    procs = driver._fs_procs["ws1"]
    await driver._stop_fs_daemons("ws1")
    assert [proc.terminated for proc in procs] == [True, True]
    assert not fs_pidfile_path(vm_dir, "proj").exists()
    assert not fs_pidfile_path(vm_dir, "data").exists()
    assert not fs_socket_path(vm_dir, "proj").exists()
    assert not fs_socket_path(vm_dir, "data").exists()
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
    vm_dir = short_vm_dir("ws2")
    settings = Settings()
    docs = tmp_path / "docs"
    docs.mkdir()
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)

    async def flaky_spawn(binary, args, log_path, fd, missing):
        if len(spawned) == 2:
            raise FileNotFoundError("virtiofsd")
        spawned.append((binary, args, fd))
        return FakeProc()

    driver._spawn_fs = flaky_spawn
    spec = VmSpec(
        workspace_id="ws2",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(
            MountSpec(host=str(proj), guest="/mnt/proj"),
            MountSpec(host=str(data), guest="/mnt/data"),
            MountSpec(host=str(docs), guest="/mnt/docs"),
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

    vm_dir = short_vm_dir("ws3")
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


async def test_restarted_daemon_kills_by_identity(tmp_path: Path) -> None:
    """The untracked teardown's #1088 arm: a pidfile carrying the
    pid and its kernel start time identifies the daemon whatever
    its argv now names — the --fd shape's cmdline names no socket
    path — and the sweep kills it and unlinks the socket beside
    the pidfile (the child never knew the path)."""
    import asyncio

    import msks.microvm.local.procs as procs_mod

    if not Path("/proc").exists():
        pytest.skip("the identity arm reads /proc (Linux-only)")
    vm_dir = short_vm_dir("ws-ident")
    driver, _spawned = driver_over(Settings())
    socket = fs_socket_path(vm_dir, "proj")
    socket.touch()
    pidfile = fs_pidfile_path(vm_dir, "proj")
    # The sleeper carries a decoy argv: nothing names the socket.
    decoy = tmp_path / "decoy-argv"
    serving = await socket_sleeper(decoy, tmp_path / "sleeper-up")
    start = procs_mod.proc_start_time(serving.pid)
    assert start is not None
    pidfile.write_text(f"{serving.pid} {start}", encoding="utf-8")
    driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
    await driver._kill_untracked_fs_daemons("ws-ident")
    assert not pidfile.exists()
    assert not socket.exists()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(serving.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the identity-matched daemon survived its pidfile kill")


async def test_the_helper_lane_execs_the_helper_as_the_owner(
    tmp_path: Path, monkeypatch
) -> None:
    """#1088's helper lane: a workspace owned by another uid execs
    the resolved helper with the owner uid, the listening fd, and
    the virtiofsd argv behind --; the identity squash lands as
    the owner's uid for both uid and gid (the helper's gid-takes-
    uid contract), and the sandbox is always none in this lane —
    the helper has dropped every capability by the exec, so even
    an owner uid 0 can never take the namespace sandbox."""
    import msks.microvm.local.fsproc as fsproc_mod

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-helper")
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path, virtiofsd_as="/fake/virtiofsd-as")
    )
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)
    monkeypatch.setattr(fsproc_mod, "dead_owner_cause", lambda uid: None)
    # The target probe answers the daemon's own resolved binary —
    # the coherence the compile-time pin buys, faked hermetically.
    monkeypatch.setattr(
        fsproc_mod,
        "helper_target",
        lambda helper: settings.vmm.virtiofsd,
    )
    for owner in (4242, 0):
        spawned.clear()
        spec = VmSpec(
            workspace_id=f"ws-helper-{owner}",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            owner_uid=owner,
            mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
        )
        await driver._start_fs_daemons(spec, vm_dir)
        binary, args, fd = spawned[0]
        assert binary == "/fake/virtiofsd-as"
        # The helper contract: <uid> --fd <n> -- <virtiofsd argv>.
        assert args[0] == str(owner)
        assert args[1] == "--fd" and args[2] == str(fd)
        assert args[3] == "--"
        joined = " ".join(args)
        assert f"squash-guest:0:{owner}:{WHOLE_RANGE}" in joined
        # gid takes the uid's value in the helper lane.
        assert args.count(f"squash-guest:0:{owner}:{WHOLE_RANGE}") == 2
        # The lane, not the uid, picks the sandbox: the helper's
        # child has no privilege to unshare with, uid 0 included.
        assert "--sandbox none" in joined
        assert "--sandbox namespace" not in joined


async def test_the_shortcut_lane_execs_directly(tmp_path: Path) -> None:
    """#1088's shortcut lane: a workspace owned by the daemon's
    own uid (and a spec with no owner) execs the virtiofsd binary
    directly — no helper token anywhere in the argv — with the
    daemon's own uid and gid exactly as before."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-direct")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)
    daemon_uid = os.getuid()
    for owner in (daemon_uid, None):
        await driver._start_fs_daemons(
            VmSpec(
                workspace_id=f"ws-direct-{owner}",
                kernel=Path("/k"),
                rootfs=Path("/r"),
                owner_uid=owner,
                mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
            ),
            vm_dir,
        )
    for binary, args, _fd in spawned:
        assert binary == "virtiofsd"
        # No helper tokens: the direct argv begins with the fd.
        assert args[0] == "--fd"
        joined = " ".join(args)
        assert f"squash-guest:0:{daemon_uid}:{WHOLE_RANGE}" in joined
        assert f"squash-guest:0:{os.getgid()}:{WHOLE_RANGE}" in joined


async def test_the_helper_lane_refuses_without_a_helper(
    tmp_path: Path, monkeypatch
) -> None:
    """A cross-owner share with no helper installed refuses by
    name (#1088) — the message names the configured setting and
    the override — never an exec of a path that is not there."""
    import msks.microvm.local.fsproc as fsproc_mod

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-nohelper")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    monkeypatch.setattr(fsproc_mod, "dead_owner_cause", lambda uid: None)
    monkeypatch.setattr(fsproc_mod, "resolve_virtiofsd_as", lambda name: None)
    spec = VmSpec(
        workspace_id="ws-nohelper",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        owner_uid=4242,
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    with pytest.raises(MicrovmError, match="owner-exec helper"):
        await driver._start_fs_daemons(spec, vm_dir)


async def test_the_seam_refuses_a_dead_owner_race(
    tmp_path: Path, monkeypatch
) -> None:
    """The race arm (#1088): the owner deleted between the
    derivation gate and the spawn refuses with the named cause —
    the same word the row's skip marker carries."""
    import msks.microvm.local.fsproc as fsproc_mod

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-race")
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path, virtiofsd_as="/fake/virtiofsd-as")
    )
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    monkeypatch.setattr(
        fsproc_mod,
        "dead_owner_cause",
        lambda uid: f"owner uid {uid} no longer exists",
    )
    spec = VmSpec(
        workspace_id="ws-race",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        owner_uid=4242,
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    with pytest.raises(MicrovmError, match="owner uid 4242 no longer exists"):
        await driver._start_fs_daemons(spec, vm_dir)


# --- the fd, the nudge, and the unsignalable child (#1088) ----


def test_bind_fs_socket_hands_a_listening_fd_at_the_path(
    tmp_path: Path,
) -> None:
    """The bind the spawn hands out (#1088): the returned fd is a
    listening socket bound at exactly the named path — the fd↔path
    correspondence the argv's --fd number and the teardown's
    unlink both lean on. The fd stays close-on-exec on every
    platform (#1105): the daemon's own execs never leak it."""
    import socket as socket_module
    import stat as stat_module

    socket_path = tmp_path / "fs-probe.sock"
    fd = bind_fs_socket(socket_path)
    try:
        st = os.fstat(fd)
        assert stat_module.S_ISSOCK(st.st_mode)
        # SO_ACCEPTCONN names listening where the platform
        # answers it; the connect below proves it everywhere
        assert socket_module_getsockopt(fd) in (1, None)
        assert not os.get_inheritable(fd)  # close-on-exec, portably
        probe = socket_module.socket(
            socket_module.AF_UNIX, socket_module.SOCK_STREAM
        )
        try:
            probe.connect(str(socket_path))  # listening, bound at the path
        finally:
            probe.close()
    finally:
        os.close(fd)


def socket_module_getsockopt(fd: int) -> int | None:
    """SO_ACCEPTCONN of one fd: 1 when the socket listens, or
    None where the platform cannot answer — darwin's getsockopt
    raises ENOPROTOOPT for it (#1105). The wrapper detaches — a
    socket object owns the fd it wraps, and the test holds it
    across the probe."""
    import errno as errno_module
    import socket as socket_module

    sock = socket_module.socket(fileno=fd)
    try:
        return sock.getsockopt(
            socket_module.SOL_SOCKET, socket_module.SO_ACCEPTCONN
        )
    except OSError as exc:  # pragma: no cover — darwin's
        # getsockopt answers ENOPROTOOPT for SO_ACCEPTCONN, a
        # platform surface the Linux suite cannot reach (#691's
        # rule); the dispatched macOS run is its coverage
        if exc.errno != errno_module.ENOPROTOOPT:
            raise
        return None
    finally:
        sock.detach()


async def test_the_nudge_exits_a_disconnected_client_server(
    tmp_path: Path,
) -> None:
    """The stop lever on a child the daemon cannot signal (#1088):
    a virtiofsd-shaped server — it accepts one connection and
    exits when that connection closes — dies from the daemon's
    connect-and-hang-up alone, no signal ever reaching it."""
    import sys as sys_module

    socket_path = tmp_path / "fs-nudge.sock"
    fd = bind_fs_socket(socket_path)
    body = (
        "import os, socket, sys\n"
        "s = socket.socket(fileno=int(sys.argv[1]))\n"
        "conn, _ = s.accept()\n"
        "conn.close()\n"
        "open(sys.argv[2], 'w').write('done')\n"
    )
    ack = tmp_path / "exited"
    server = await asyncio.create_subprocess_exec(
        sys_module.executable,
        "-c",
        body,
        str(fd),
        str(ack),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        pass_fds=(fd,),
    )
    os.close(fd)  # the child holds its own copy
    try:
        await asyncio.sleep(0.2)  # the server reaches accept()
        fs_exit_nudge(socket_path)
        await asyncio.wait_for(server.wait(), 5.0)
        assert ack.exists()  # it took the connection and saw it close
    finally:
        with contextlib.suppress(ProcessLookupError):
            server.kill()
        await server.wait()


def test_pid_gone_reads_permission_as_alive() -> None:
    """The unsignalable child's liveness (#1088): a signal the
    daemon may not send (EPERM) means the process exists — the
    death poll must not read the refusal as death."""
    assert pid_gone(1) is False


async def test_the_stop_tolerates_an_unsignalable_child(
    tmp_path: Path, monkeypatch
) -> None:
    """The helper lane's teardown shape (#1088): a child whose
    signals all answer EPERM still stops — the TERM is suppressed,
    the nudge fires the client-disconnect exit, and the death
    poll reports it — and a TERM-time PermissionError never
    escapes the tracked stop."""
    import msks.microvm.local.procs as procs_mod

    real_kill = os.kill

    def eperm_kill(pid, sig):
        if sig == 0:
            return real_kill(pid, 0)
        raise PermissionError(1, "Operation not permitted")

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-eperm")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws-eperm",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)

    class Immortal(FakeProc):
        """The EPERM shape: terminate and kill both refuse, and
        the wait blocks until the abandon window — the stop's
        kill arm reached with its signal refused."""

        def terminate(self) -> None:
            raise PermissionError(1, "Operation not permitted")

        def kill(self) -> None:
            self.killed = True
            raise PermissionError(1, "Operation not permitted")

        async def wait(self) -> int:
            import asyncio as asyncio_mod

            while self.returncode is None:
                await asyncio_mod.sleep(0.01)
            return self.returncode or 0

    driver._fs_procs["ws-eperm"][0] = immortal = Immortal()
    monkeypatch.setattr(procs_mod.os, "kill", eperm_kill)
    import msks.microvm.local.fsproc as fsproc_mod

    monkeypatch.setattr(fsproc_mod, "FS_TERM_GRACE_S", 0.05)
    await driver._stop_fs_daemons("ws-eperm")  # no raise, completes
    assert immortal.terminated is False
    assert immortal.killed is True  # the kill arm ran, EPERM and all


async def test_the_sweep_unlinks_only_an_identified_daemons_socket(
    tmp_path: Path,
) -> None:
    """An unidentified pidfile's socket stays (#1088): the sweep
    never deletes a socket file it could not prove a daemon dead
    behind — the spawn's own live check judges that file, and a
    live daemon refuses the next start by name."""
    vm_dir = short_vm_dir("ws-anon")
    socket = fs_socket_path(vm_dir, "proj")
    socket.touch()
    pidfile = fs_pidfile_path(vm_dir, "proj")
    pidfile.write_text("999999999 7", encoding="utf-8")  # no such pid
    await sweep_fs_pidfile(vm_dir, pidfile)
    assert not pidfile.exists()
    assert socket.exists()  # left for the spawn's stale/live verdict


async def test_the_helper_lane_refuses_a_drifted_target(
    tmp_path: Path, monkeypatch
) -> None:
    """The coherence probe (#1088): a helper whose compile-time
    virtiofsd target differs from the daemon's resolved binary
    refuses by name — the override reaches only the shares the
    daemon runs as itself, and the drift never reaches virtiofsd's
    argument parsing as an anonymous log tail."""
    import msks.microvm.local.fsproc as fsproc_mod

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-drift")
    settings = Settings(
        vmm=VmmSettings(
            state_dir=tmp_path,
            virtiofsd="/srv/virtiofsd-1.15",
            virtiofsd_as="/fake/virtiofsd-as",
        )
    )
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    monkeypatch.setattr(fsproc_mod, "dead_owner_cause", lambda uid: None)
    monkeypatch.setattr(
        fsproc_mod, "helper_target", lambda helper: "/srv/virtiofsd-1.10"
    )
    spec = VmSpec(
        workspace_id="ws-drift",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        owner_uid=4242,
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    with pytest.raises(
        MicrovmError,
        match=r"target is /srv/virtiofsd-1.10, but the daemon resolved",
    ):
        await driver._start_fs_daemons(spec, vm_dir)


async def test_the_helper_lane_refuses_a_target_probe_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """A helper that answers no target — an older binary without
    the arm, a path that is not the helper at all — refuses the
    cross-owner share by name (#1088)."""
    import msks.microvm.local.fsproc as fsproc_mod

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-probe")
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path, virtiofsd_as="/fake/virtiofsd-as")
    )
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    monkeypatch.setattr(fsproc_mod, "dead_owner_cause", lambda uid: None)
    monkeypatch.setattr(fsproc_mod, "helper_target", lambda helper: None)
    spec = VmSpec(
        workspace_id="ws-probe",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        owner_uid=4242,
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    with pytest.raises(MicrovmError, match="did not report its virtiofsd"):
        await driver._start_fs_daemons(spec, vm_dir)


def test_helper_target_parses_one_line(tmp_path: Path) -> None:
    """The probe's answer is one non-empty line, or nothing: a
    stub that echoes parses, a nonzero exit, a garbage answer,
    and a missing binary all read as None — the caller refuses by
    name."""
    from msks.microvm.fsdaemons import helper_target

    echo = tmp_path / "helper-echo"
    echo.write_text("#!/bin/sh\necho /srv/virtiofsd-1.15\n")
    echo.chmod(0o755)
    assert helper_target(str(echo)) == "/srv/virtiofsd-1.15"
    refuse = tmp_path / "helper-refuse"
    refuse.write_text("#!/bin/sh\nexit 2\n")
    refuse.chmod(0o755)
    assert helper_target(str(refuse)) is None
    garbage = tmp_path / "helper-garbage"
    garbage.write_text("#!/bin/sh\necho one\necho two\n")
    garbage.chmod(0o755)
    assert helper_target(str(garbage)) is None
    assert helper_target(str(tmp_path / "helper-absent")) is None


# --- the doctor rows --------------------------------------------------


def test_doctor_reports_virtiofsd_and_share_paths(tmp_path: Path) -> None:
    """The warning-grade row: virtiofsd's presence (a daemon
    without allowed roots never execs it). The per-share path
    rows are gone with the named form (#685) — the gates refuse
    a missing host path at set time, not the doctor."""
    from msks.server.doctor import run_doctor

    settings = Settings()
    # A floor-clearing double, not the host's own binary: a Mint
    # 22 box (1.10.0) would otherwise fail the suite.
    settings.vmm.virtiofsd = fake_virtiofsd(
        tmp_path / "virtiofsd", "virtiofsd 1.13.0"
    )
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    report = run_doctor(settings)
    rows = {result.name: result for result in report.results}
    assert rows[settings.vmm.virtiofsd].ok is True
    # A miss stays warning-grade: with the binary gone, the row
    # fails the run never — it only breaks the mounts feature.
    gone_settings = Settings()
    gone_settings.vmm.virtiofsd = "virtiofsd-absent"
    gone_rows = {
        result.name: result for result in run_doctor(gone_settings).results
    }
    assert gone_rows["virtiofsd-absent"].ok is False
    assert gone_rows["virtiofsd-absent"].is_warning is True


def fake_virtiofsd(path: Path, output: str) -> str:
    """An executable virtiofsd double whose ``--version`` prints
    ``output`` — the doctor row's probe target, hermetic against
    whatever the host carries."""
    path.write_text(f"#!/bin/sh\nprintf '%s\n' {output!r}\n")
    path.chmod(0o755)
    return str(path)


def test_doctor_names_a_below_floor_virtiofsd(tmp_path: Path) -> None:
    """The version gap the presence check hid (#655): a virtiofsd
    below 1.13.0 parses none of the mounts' flags, so its row
    warns naming the floor, the reported version, and the
    MSKSD_VIRTIOFSD override — and output without a version
    reads as the same refusal, because a binary that cannot
    report one predates every flag."""
    from msks.server.doctor import run_doctor

    floor = ".".join(str(part) for part in VIRTIOFSD_FLOOR)
    settings = Settings()
    settings.vmm.virtiofsd = fake_virtiofsd(
        tmp_path / "virtiofsd-old", "virtiofsd 1.10.0"
    )
    rows = {result.name: result for result in run_doctor(settings).results}
    row = rows[settings.vmm.virtiofsd]
    assert row.ok is False
    assert row.is_warning is True
    assert "1.10.0" in row.message
    assert floor in row.message
    assert "MSKSD_VIRTIOFSD" in row.message

    silent = Settings()
    silent.vmm.virtiofsd = fake_virtiofsd(
        tmp_path / "virtiofsd-mute", "some other banner"
    )
    rows = {result.name: result for result in run_doctor(silent).results}
    row = rows[silent.vmm.virtiofsd]
    assert row.ok is False
    assert row.is_warning is True
    assert "reported no version" in row.message


def test_the_floor_is_inclusive_and_pads_short_reports() -> None:
    """The floor comparison (#655): exactly-the-floor clears, a
    two-part report pads with zeros before it compares, and None
    (nothing reported) meets nothing."""
    from msks.microvm.fsdaemons import virtiofsd_meets_floor

    assert virtiofsd_meets_floor((1, 13, 0)) is True
    assert virtiofsd_meets_floor((1, 13)) is True
    assert virtiofsd_meets_floor((1, 13, 3)) is True
    assert virtiofsd_meets_floor((2, 0)) is True
    assert virtiofsd_meets_floor((1, 12, 9)) is False
    assert virtiofsd_meets_floor((1, 10, 0)) is False
    assert virtiofsd_meets_floor(None) is False


def test_virtiofsd_version_parses_the_reported_number() -> None:
    """The floor's parse (#655): the dotted number after the
    binary's name, ``None`` for anything else."""
    assert virtiofsd_version("virtiofsd 1.13.3\n") == (1, 13, 3)
    assert virtiofsd_version("virtiofsd 1.10.0") == (1, 10, 0)
    assert virtiofsd_version("virtiofsd 2.0") == (2, 0)
    assert virtiofsd_version("not a version") is None
    assert virtiofsd_version("") is None


def test_the_floor_message_names_floor_and_override() -> None:
    """The shared refusal text: the reported version, the floor,
    and the override that points at a newer build."""
    message = virtiofsd_floor_message((1, 10, 0))
    assert "1.10.0" in message
    assert "1.13.0" in message
    assert "MSKSD_VIRTIOFSD" in message
    assert "reported no version" in virtiofsd_floor_message(None)


async def test_the_start_refuses_a_below_floor_virtiofsd(
    tmp_path: Path,
) -> None:
    """The start path's floor probe (#655): a below-floor (or
    version-silent) virtiofsd refuses before any socket or
    pidfile lands, naming the floor and the override instead of
    dying at argument parsing behind an "exited at start" log
    tail; a missing binary keeps the spawn seam's named error."""
    from msks.microvm.errors import MicrovmError

    shared = tmp_path / "share"
    shared.mkdir()
    vm_dir = short_vm_dir("ws-old")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    spec = VmSpec(
        workspace_id="ws-old",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(shared), guest="/mnt/s"),),
    )
    for version in ((1, 10, 0), None):
        driver, _spawned = driver_over(settings, version=version)
        with pytest.raises(MicrovmError, match="1.13.0"):
            await driver._start_fs_daemons(spec, vm_dir)
        assert not list(vm_dir.glob("fs-*.sock")), version
        assert not list(vm_dir.glob("fs-*.pid")), version

    absent = Settings(vmm=VmmSettings(state_dir=tmp_path))
    absent.vmm.virtiofsd = str(tmp_path / "virtiofsd-absent")
    absent.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(absent)
    # The real probe: the missing binary keeps the spawn seam's
    # named error instead of the floor's.
    driver._virtiofsd_version = types.MethodType(
        LocalCloudHypervisor._virtiofsd_version, driver
    )
    with pytest.raises(
        MicrovmError, match="virtiofsd binary not found or not executable"
    ):
        await driver._start_fs_daemons(spec, vm_dir)


async def test_the_real_probe_reads_the_binary(tmp_path: Path) -> None:
    """The real probe against script doubles (#655): a binary
    reporting an old version parses to its tuple, and one whose
    ``--version`` exits nonzero reads as the below-floor refusal
    (an unreported version)."""
    from msks.microvm.errors import MicrovmError

    driver, _spawned = driver_over(Settings(), version=None)
    driver._virtiofsd_version = types.MethodType(
        LocalCloudHypervisor._virtiofsd_version, driver
    )
    old = fake_virtiofsd(tmp_path / "virtiofsd-old", "virtiofsd 1.10.0")
    assert await driver._virtiofsd_version(old) == (1, 10, 0)

    broken = tmp_path / "virtiofsd-broken"
    broken.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    broken.chmod(0o755)
    with pytest.raises(MicrovmError, match=r"exited 3: boom"):
        await driver._require_virtiofsd_floor(str(broken))

    unexecutable = tmp_path / "virtiofsd-noexec"
    unexecutable.write_text("#!/bin/sh\ntrue\n")
    unexecutable.chmod(0o644)
    with pytest.raises(
        MicrovmError, match="virtiofsd binary not found or not executable"
    ):
        await driver._require_virtiofsd_floor(str(unexecutable))

    import msks.microvm.local as local_mod

    real_timeout = local_mod.fsproc.VERSION_PROBE_TIMEOUT_S
    local_mod.fsproc.VERSION_PROBE_TIMEOUT_S = 0.2
    hang = tmp_path / "virtiofsd-hang"
    hang.write_text("#!/bin/sh\nsleep 5\n")
    hang.chmod(0o755)
    try:
        with pytest.raises(
            MicrovmError, match=r"did not answer within.+MSKSD_VIRTIOFSD"
        ):
            await driver._require_virtiofsd_floor(str(hang))
    finally:
        local_mod.fsproc.VERSION_PROBE_TIMEOUT_S = real_timeout


def test_doctor_names_a_probe_failing_virtiofsd(tmp_path: Path) -> None:
    """A virtiofsd whose ``--version`` exits nonzero keeps the
    probe-failed row shape the presence check had — the found
    path and the exit detail, with the package install hint."""
    from msks.server.doctor import run_doctor

    settings = Settings()
    settings.vmm.virtiofsd = str(tmp_path / "virtiofsd-x")
    (tmp_path / "virtiofsd-x").write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    (tmp_path / "virtiofsd-x").chmod(0o755)
    rows = {result.name: result for result in run_doctor(settings).results}
    row = rows[settings.vmm.virtiofsd]
    assert row.ok is False
    assert row.is_warning is True
    assert "probe failed" in row.message
    assert "boom" in row.message


async def test_missing_share_path_skips_at_start(client, tmp_path):  # noqa: F811
    """A share path that vanished (or never existed) skips with
    the path named — the same event kind a policy narrowing
    carries, a detail the operator can act on."""
    http, app, _stub = client
    tree = tmp_path / "tree"
    tree.mkdir()
    app.state.settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-nopath", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid, [{"host": str(tree), "guest": "/mnt/tree", "mode": "rw"}]
    )
    # The tree vanishes after the set: the gate refuses at read,
    # the skip is state.
    import shutil

    shutil.rmtree(tree)
    published = []
    real_publish = app.state.hub.publish

    async def spy_publish(event: str, data: dict, owner=None) -> None:
        published.append((event, data))
        await real_publish(event, data, owner=owner)

    app.state.hub.publish = spy_publish
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    # The start's own status frames (#1019) publish first; the
    # skip is the mount family's frame.
    skips = [frame for frame in published if frame[0] == "mount.skipped"]
    assert skips[0][1]["detail"]
    assert "does not exist or is not a directory" in skips[0][1]["detail"]


async def test_spawn_guards_name_every_refusal(tmp_path: Path) -> None:
    """The last-line guards: the gates re-read at spawn — a path
    the policy no longer allows, and one that vanished between
    the route's derivation and this spawn — each refuse by
    name."""
    from msks.microvm.errors import MicrovmError

    vm_dir = short_vm_dir("ws-g")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws-g",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(tmp_path / "void"), guest="/mnt/void"),),
    )
    with pytest.raises(MicrovmError, match="does not exist or is not a"):
        await driver._start_fs_daemons(spec, vm_dir)
    settings.mounts = MountSettings(host_roots=("/srv/elsewhere",))
    with pytest.raises(MicrovmError, match="outside the allowed"):
        await driver._start_fs_daemons(spec, vm_dir)


async def test_live_socket_refuses_a_second_start(tmp_path: Path) -> None:
    """A socket still serving (a daemon from a prior life that the
    stale sweep cannot prove dead) refuses the spawn by name —
    never a silent second daemon behind one tag."""
    import socket as socket_mod

    from msks.microvm.errors import MicrovmError

    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    # A shallow state dir (the AF_UNIX-length rule; see the
    # login-user spawn test): the test binds a real socket at the
    # vm dir, and under xdist the worker's deeper basetemp pushes
    # that path past the limit — the bind itself would fail.
    state_dir = Path(tempfile.mkdtemp(prefix="msks-test-", dir="/tmp"))
    vm_dir = state_dir / "vms" / "ws-s"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)
    live = fs_socket_path(
        vm_dir, mount_tag(MountSpec(host=str(share_dir), guest="/mnt/proj"))
    )
    server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    server.bind(str(live))
    server.listen(1)
    try:
        spec = VmSpec(
            workspace_id="ws-s",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
        )
        with pytest.raises(MicrovmError, match="live virtiofs daemon"):
            await driver._start_fs_daemons(spec, vm_dir)
    finally:
        server.close()
        shutil.rmtree(state_dir, ignore_errors=True)


async def test_stop_is_idempotent_and_kills_a_hang(tmp_path: Path) -> None:
    """Stopping twice is a no-op the second time, and a daemon
    that ignores SIGTERM dies by SIGKILL past the grace."""
    import msks.microvm.local as local_mod

    real_grace = local_mod.fsproc.FS_TERM_GRACE_S
    local_mod.fsproc.FS_TERM_GRACE_S = 0.1
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-h")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
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

    async def hang_spawn(binary, args, log_path, fd, missing):
        return HangingProc()

    driver._spawn_fs = hang_spawn
    spec = VmSpec(
        workspace_id="ws-h",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    proc = driver._fs_procs["ws-h"][0]
    try:
        await driver._stop_fs_daemons("ws-h")
        assert proc.killed  # ignored the TERM, died by KILL
        await driver._stop_fs_daemons("ws-h")  # idempotent second call
    finally:
        local_mod.fsproc.FS_TERM_GRACE_S = real_grace


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
    from msks.microvm.fsdaemons import virtiofsd_missing_message

    with pytest.raises(
        MicrovmError, match="virtiofsd binary not found or not executable"
    ):
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            await driver._spawn_fs(
                "virtiofsd-absent",
                [],
                tmp_path / "log",
                fd,
                virtiofsd_missing_message("virtiofsd-absent"),
            )
        finally:
            os.close(fd)


async def test_terminate_of_an_external_death_is_tolerated(
    tmp_path: Path,
) -> None:
    """A daemon that died between the registry and the teardown:
    terminate's ProcessLookupError is suppressed, the stop
    completes."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-e")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)

    class ExternalDeath(FakeProc):
        """terminate finds nothing: the process died on its own."""

        def terminate(self) -> None:
            raise ProcessLookupError

    async def dying_spawn(binary, args, log_path, fd, missing):
        return ExternalDeath()

    driver._spawn_fs = dying_spawn
    spec = VmSpec(
        workspace_id="ws-e",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    await driver._stop_fs_daemons("ws-e")
    assert (
        "ws-e" not in driver._fs_procs
    )  # the registry let the dead daemon go


async def test_untracked_sweep_tolerates_garbage_pidfiles(
    tmp_path: Path,
) -> None:
    """The restarted-daemon sweep: a garbage pidfile reads as None
    and is swept beside a serving one's kill, and the second
    sweep over an empty directory is a no-op."""
    vm_dir = short_vm_dir("ws-u")
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


async def test_stop_skips_an_already_exited_daemon(tmp_path: Path) -> None:
    """A tracked daemon that exited on its own between the
    registry and the teardown: the TERM pass finds a returncode
    and skips it, the stop completes."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-x")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)

    async def exited_spawn(binary, args, log_path, fd, missing):
        return FakeProc()

    driver._spawn_fs = exited_spawn
    spec = VmSpec(
        workspace_id="ws-x",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    # The daemon died on its own between the registry and the
    # teardown (an exited-at-spawn proc is the serve wait's
    # refusal now, not this path).
    proc = driver._fs_procs["ws-x"][0]
    proc.returncode = 0
    await driver._stop_fs_daemons("ws-x")
    assert (
        not proc.terminated
    )  # the TERM pass found the returncode and skipped it
    assert "ws-x" not in driver._fs_procs  # and the registry released it


async def test_readonly_composes_from_both_sides(tmp_path: Path) -> None:
    """--readonly arrives exactly when the mount asked ro (#685:
    the mode is the mount's alone — there is no operator share to
    narrow it). Pinned on the real arg vectors. The state dir
    sits under a shallow mkdtemp for the same AF_UNIX-length
    reason as the login-user spawn test above."""
    plain = tmp_path / "plain"
    plain.mkdir()
    state_dir = Path(tempfile.mkdtemp(prefix="msks-test-", dir="/tmp"))
    vm_dir = state_dir / "vms" / "ws-ro"
    vm_dir.mkdir(parents=True)
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)
    try:
        for mount, expect_ro in (
            (MountSpec(host=str(plain), guest="/mnt/p"), False),
            (MountSpec(host=str(plain), guest="/mnt/p", mode="ro"), True),
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
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="darwin's asyncio event loop never returns from its "
    "kevent wait while a spawned child is SIGSTOPped (Python "
    "3.14 stdlib behavior, reproducible with a five-line "
    "script); the escalation is pinned on the Linux CI host",
)
async def test_untracked_sweep_escalates_to_kill(tmp_path: Path) -> None:
    """A TERM-immortal daemon cannot wedge the workspace behind
    its own live socket: the sweep waits out the grace, KILLs,
    and the pidfile stays until the pid dies."""
    import signal as signal_mod

    import msks.microvm.local as local_mod

    real_grace = local_mod.fsproc.FS_TERM_GRACE_S
    local_mod.fsproc.FS_TERM_GRACE_S = 0.2
    try:
        vm_dir = short_vm_dir("ws-imm")
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
        local_mod.fsproc.FS_TERM_GRACE_S = real_grace
        with contextlib.suppress(ProcessLookupError):
            os.kill(immortal.pid, signal_mod.SIGCONT)
        with contextlib.suppress(ProcessLookupError):
            immortal.kill()
        await immortal.wait()


async def test_untracked_sweep_kills_a_term_ignoring_daemon(
    tmp_path: Path,
) -> None:
    """The same escalation on the TERM-ignored shape every host
    can schedule: the daemon's handler swallows the TERM, the
    grace runs out, and only the KILL lands — the pidfile goes
    with the pid."""
    import msks.microvm.local as local_mod

    real_grace = local_mod.fsproc.FS_TERM_GRACE_S
    local_mod.fsproc.FS_TERM_GRACE_S = 0.2
    try:
        vm_dir = short_vm_dir("ws-imm2")
        driver, _spawned = driver_over(Settings())
        socket = fs_socket_path(vm_dir, "proj")
        stubborn = await socket_sleeper(
            socket, vm_dir / "sleeper-up", ignore_term=True
        )
        pidfile = fs_pidfile_path(vm_dir, "proj")
        pidfile.write_text(str(stubborn.pid), encoding="utf-8")
        driver._dir = lambda workspace_id: vm_dir  # type: ignore[method-assign]
        await driver._kill_untracked_fs_daemons("ws-imm2")
        assert not pidfile.exists()
        assert await local_mod.pid_dead_by(stubborn.pid, 2.0)
    finally:
        local_mod.fsproc.FS_TERM_GRACE_S = real_grace
        with contextlib.suppress(ProcessLookupError):
            stubborn.kill()
        await stubborn.wait()


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
    vm_dir = short_vm_dir("ws-dead")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, _spawned = driver_over(settings)

    class DiedAtExec(FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = 1

    async def dead_spawn(binary, args, log_path, fd, missing):
        return DiedAtExec()

    driver._spawn_fs = dead_spawn
    spec = VmSpec(
        workspace_id="ws-dead",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
    )
    with pytest.raises(MicrovmError, match="exited at start"):
        await driver._start_fs_daemons(spec, vm_dir)
    assert "ws-dead" not in driver._fs_procs


async def test_a_silent_daemon_passes_as_serving(tmp_path: Path) -> None:
    """The readiness signal's #1088 shape: a proc alive past the
    serve grace is serving — under the --fd contract the socket
    file exists from the daemon's own bind, so no later signal
    can distinguish a serving daemon from a silent one at spawn
    time, and a start that would have refused on the missing
    socket file now registers the share (a wedged daemon is
    runtime state, the stop path's to handle — the same
    approximation the --socket-path shape carried, where bind
    always preceded accept)."""
    share_dir = tmp_path / "proj"
    share_dir.mkdir()
    vm_dir = short_vm_dir("ws-silent")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    driver, spawned = driver_over(settings)

    async def silent_spawn(binary, args, log_path, fd, missing):
        spawned.append((binary, args, fd))
        return FakeProc()  # alive, serves nothing, says nothing

    driver._spawn_fs = silent_spawn
    entries = await driver._start_fs_daemons(
        VmSpec(
            workspace_id="ws-silent",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            mounts=(MountSpec(host=str(share_dir), guest="/mnt/proj"),),
        ),
        vm_dir,
    )
    assert len(entries) == 1
    assert len(spawned) == 1
    assert "ws-silent" in driver._fs_procs


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
        assert (
            await local_mod.stop_fs_pid(sleeper.pid, Path("/no/such.sock"))
            is True
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            sleeper.kill()
        await sleeper.wait()


async def test_sweep_keeps_a_killproof_daemons_pidfile(tmp_path):
    """A daemon that outlives even SIGKILL (D-state) keeps its
    pidfile: the sweep never strands the only record of a live
    daemon."""
    import msks.microvm.local as local_mod

    vm_dir = short_vm_dir("ws-dstate")
    socket = fs_socket_path(vm_dir, "proj")
    live = await socket_sleeper(socket, vm_dir / "sleeper-up")
    pidfile = fs_pidfile_path(vm_dir, "proj")
    pidfile.write_text(str(live.pid), encoding="utf-8")

    async def killproof(pid, socket_path=None):
        return False  # survived even SIGKILL

    real_stop = local_mod.stop_fs_pid
    local_mod.fsproc.stop_fs_pid = killproof
    try:
        await local_mod.sweep_fs_pidfile(vm_dir, pidfile)
    finally:
        local_mod.fsproc.stop_fs_pid = real_stop
        with contextlib.suppress(ProcessLookupError):
            live.kill()
        await live.wait()
    assert pidfile.exists()


# --- the host form through the spawn path (#672) ---------------------


async def test_a_host_mount_spawns_under_its_derived_tag(
    tmp_path: Path,
) -> None:
    """The host form rides the same lifecycle keyed by its
    derived tag (#672): the spawn's shared directory comes from
    the #667 gates (identity mode over a real directory here),
    the readonly flag from the mount's mode alone, and the socket
    and pidfile carry the hs- tag — never a share lookup."""
    shared = tmp_path / "code"
    shared.mkdir()
    vm_dir = short_vm_dir("ws-h")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        host_mode="identity", host_roots=(str(tmp_path),)
    )
    driver, spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws-h",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(guest="/mnt/code", mode="rw", host=str(shared)),),
    )
    await driver._start_fs_daemons(spec, vm_dir)
    tag = mount_tag(spec.mounts[0])
    assert tag.startswith("hs-")
    assert (vm_dir / f"fs-{tag}.pid").exists()
    await driver._stop_fs_daemons("ws-h")
    binary, args, _fd = spawned[0]
    assert args[args.index("--tag") + 1] == tag
    assert args[args.index("--shared-dir") + 1] == str(shared)
    assert "--readonly" not in args


async def test_a_host_mount_spawn_refuses_an_ungated_path(
    tmp_path: Path,
) -> None:
    """The spawn re-runs the gates (the flip between the route's
    derivation and the spawn): a path the policy no longer allows
    refuses at spawn, naming the config — never a daemon over a
    tree the operator closed."""
    shared = tmp_path / "code"
    shared.mkdir()
    vm_dir = short_vm_dir("ws-x")
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path))
    settings.mounts = MountSettings(
        host_mode="identity", host_roots=(str(tmp_path / "other"),)
    )
    driver, spawned = driver_over(settings)
    spec = VmSpec(
        workspace_id="ws-x",
        kernel=Path("/k"),
        rootfs=Path("/r"),
        mounts=(MountSpec(guest="/m", mode="ro", host=str(shared)),),
    )
    with pytest.raises(MicrovmError, match="outside the allowed"):
        await driver._start_fs_daemons(spec, vm_dir)
    assert spawned == []

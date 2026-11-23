"""The virtiofs daemons behind host mounts (#620, part #622).

One virtiofsd per attached share, from the platform's own package
— nothing is vendored. This module owns the
argument grammar and the path shapes; the local driver owns the
process lifecycle — spawn before the VMM boots, teardown beside
the tap on every stop path. Under #1088 the daemon binds each
share's vhost socket itself and hands the listening fd across
the exec: a virtiofsd running as the workspace's owner needs no
write access to the daemon's tree, and no per-share directory is
chowned anywhere. The exec lanes: the workspace's owner, through
the setuid helper, when it differs from the daemon's own uid;
directly, exactly as before, when it does not.

The coherence contract (#620): every daemon runs ``--cache
never``, so simultaneous rw attaches to one share read and write
through the one host page cache — data writes are promptly
visible across guests, and lockfile coordination (``O_EXCL``
creates) works because the host kernel serializes it. What does
not coordinate between guests: ``flock``/``fcntl`` (per-daemon)
and ``mmap`` coherence.

The identity contract, both directions: every guest uid/gid
squashes to the daemon's own through ``--translate-uid/--translate-gid``
— a guest root's write and a guest user's write land as the same
host owner, and host-side ownership stays advisory, as with a
typical bind mount — and every host uid/gid squashes back to the
workspace user's guest identity, so every file in the share reads
as the logged-in user's own inside the guest (#631): the raw host
uids would otherwise name whatever guest account happens to hold
the number (the daemon's own uid reads as the image's built-in
``msks`` account, not the workspace's login user).
"""

import os
import re
import shutil
from pathlib import Path

#: One queue of 1024 descriptors per daemon: a microvm's mount
#: traffic is metadata-heavy and single-queue serializes fine;
#: the vhost-user-fs examples' sizing.
NUM_QUEUES = 1
QUEUE_SIZE = 1024

#: The virtiofsd version floor for host mounts (#655): the
#: argument vectors this module builds ride on
#: ``--translate-uid``/``--translate-gid`` with ``squash-guest:``
#: and ``squash-host:`` mappings (the identity contracts above)
#: and on ``--readonly``, and all three landed upstream in 1.13.0
#: — older builds reject them at argument parsing and exit at
#: start (Linux Mint 22's noble-based archive ships 1.10.0). No
#: pre-1.13 feature set carries the identity contracts or
#: host-side enforcement of ``ro`` mounts, so a below-floor
#: binary cannot serve a share at all: there is no compatible
#: degradation, only the floor.
VIRTIOFSD_FLOOR = (1, 13, 0)

_VERSION_RE = re.compile(r"virtiofsd\s+v?(\d+(?:\.\d+)+)")


def virtiofsd_meets_floor(version: tuple[int, ...] | None) -> bool:
    """Whether a parsed version clears the floor. ``None`` (a
    binary that reported no version) meets nothing: it predates
    every feature the mounts pass. Short reports pad with zeros
    before the comparison, so a two-part ``1.13`` reads as
    ``1.13.0`` and clears the floor it spells."""
    if version is None:
        return False
    width = max(len(version), len(VIRTIOFSD_FLOOR))
    padded = version + (0,) * (width - len(version))
    floor = VIRTIOFSD_FLOOR + (0,) * (width - len(VIRTIOFSD_FLOOR))
    return padded >= floor


#: Guest xattrs remap under one host prefix (the virtiofsd
#: documented mapping), so a guest cannot write host security
#: labels or root-owned namespaces through the share. One name
#: the map denies outright and leaves unmapped:
#: ``security.capability`` (``bad``). The deny is what holds
#: the boundary, whatever the daemon's privilege: a daemon run
#: as root keeps CAP_SETFCAP on purpose and would honor a
#: guest's capability writes, so the map, not the daemon's
#: privilege, says no. Unmapped, virtiofsd also stays out of
#: file-capability clearing: a remapped name makes it clear
#: capabilities itself on every write, and its ``fremovexattr``
#: re-checks the inode's mode even through a fd opened writable
#: at creation — a guest that creates a file without write bits
#: and keeps writing through the fd (git's loose-object
#: tempfiles, #657) then gets EACCES on every write. Unmapped,
#: the host kernel clears capabilities on write itself, on the
#: write path that never re-checks the mode. Posix ACLs stay
#: off: uid translation and ACLs cannot combine.
XATTRMAP = ":bad:client:security.capability:::map::user.virtiofs.:"

#: The uid/gid translation count: the guest range [0, 2^32-2]
#: squashes to the one host identity. The last guest uid
#: (4294967295, the kernel's nobody) falls through unmapped —
#: nothing legitimate in a guest writes as it.
WHOLE_RANGE = 4294967295

#: The guest uid/gid both images pin to their own workspace
#: account (#63): ``msks``, uid/gid 1000 (guest-debian declares
#: it; guest-nixos-configuration declares it).
IMAGE_USER_UID = 1000

#: The guest uid/gid a seed-provisioned login user takes (#248,
#: #631): ``useradd -m`` on the pristine image picks the first
#: free uid at or above 1000, and the image's own account holds
#: 1000 — so 1001 it is. An account the operator's user_data
#: premade at another uid keeps that uid (the seed tolerates it),
#: and the share then presents as the uid this rule names; the
#: operator who premakes accounts owns that edge.
LOGIN_USER_UID = 1001

#: The logins that ride the image's own accounts: no named user,
#: root, or the msks account itself — the workspace user whose
#: identity a share presents as is the image's.
IMAGE_LOGINS = frozenset({None, "", "root", "msks"})


def guest_user_ids(login_user: str | None) -> tuple[int, int]:
    """The guest uid/gid a share's files present as inside the
    guest (#631): the seed-provisioned login user's when the
    workspace names one, the image's own ``msks`` account
    otherwise — never a raw host uid, which would name whatever
    guest account happens to hold the number."""
    if login_user in IMAGE_LOGINS:
        return IMAGE_USER_UID, IMAGE_USER_UID
    return LOGIN_USER_UID, LOGIN_USER_UID


def fs_socket_path(vm_dir: Path, share: str) -> Path:
    """The vhost-user socket one share's daemon serves on, inside
    the workspace's own vm directory — the directory's lifetime
    is the daemon's. The daemon itself binds and listens this
    path (#1088) and hands the fd to the share's virtiofsd, so
    the file appears before the child execs and leaves when the
    daemon's teardown unlinks it."""
    return vm_dir / f"fs-{share}.sock"


def fs_pidfile_path(vm_dir: Path, share: str) -> Path:
    """The daemon's pidfile: a restarted daemon finds and stops a
    daemon its crashed predecessor left serving (the #56 pidfile
    fallback pattern)."""
    return vm_dir / f"fs-{share}.pid"


def sandbox_mode(uid: int) -> str:
    """The sandbox strength the exec uid can carry (#1088): a
    virtiofsd running as root gets the namespace sandbox
    (mount/pid/net namespaces plus pivot_root — the strongest
    escape guard); any other uid gets ``none``, because both
    isolating modes need privileges that uid does not hold. The
    uid is the one the daemon execs the share as — the
    workspace owner's in the helper lane, the daemon's own in
    the shortcut lane. The share set itself stays operator-
    chosen either way (#620's trust model)."""
    return "namespace" if uid == 0 else "none"


def helper_lane(owner_uid: int | None, daemon_uid: int) -> int | None:
    """The uid a share's virtiofsd execs as through the setuid
    helper (#1088): the workspace's owner when it differs from
    the daemon's own uid; None for the shortcut lane, which
    execs virtiofsd directly exactly as before — no helper
    anywhere in the process tree, the dev daemon's and every
    single-user host's unchanged shape. A spec without an owner
    (built outside the row path) takes the daemon's own uid and
    the shortcut with it."""
    owner = daemon_uid if owner_uid is None else owner_uid
    return None if owner == daemon_uid else owner


def virtiofsd_as_missing_message(configured: str) -> str:
    """The named refusal when a cross-owner share has no helper
    to exec through (#1088): the configured name, what installs
    one, and the override — the same voice as the virtiofsd
    message beside it."""
    return (
        f"the owner-exec helper was not found ({configured}); a share "
        "whose owner is not the daemon's own uid needs one — the "
        "setup tools install it, and MSKSD_VIRTIOFSD_AS names a path"
    )


#: The owner-exec helper's PATH names across installs (#1087):
#: the stock name and the NixOS wrapper's.
VIRTIOFSD_AS_NAMES = ("virtiofsd-as", "msks-virtiofsd-as")

#: The owner-exec helper's known locations (#1087): the binary
#: the platform wheel bundles inside the package (msks/bin, the
#: path the release workflow compiles into the wheel), then the
#: /usr/local/libexec/msks path the setup tools install at.
VIRTIOFSD_AS_LOCATIONS = ("/usr/local/libexec/msks/virtiofsd-as",)

#: The binary the platform wheel bundles beside the package
#: (#1087): msks/bin/virtiofsd-as, present only on a platform
#: wheel install; every other build ships no helper and the
#: shortcut lane needs none.
BUNDLED_VIRTIOFSD_AS = Path(__file__).parent.parent / "bin" / "virtiofsd-as"


def virtiofsd_as_candidates(which_fn, exists_fn) -> list[str | None]:
    """The helper's candidate paths in resolution order (#1088):
    each PATH name under either install name, then the wheel's
    bundled binary, then the setup tools' install location. None
    placeholders keep the order legible; the resolver takes the
    first real one."""
    found_on_path = [which_fn(name) for name in VIRTIOFSD_AS_NAMES]
    bundled = str(BUNDLED_VIRTIOFSD_AS)
    return [
        *found_on_path,
        bundled if exists_fn(bundled) else None,
        *[
            location if exists_fn(location) else None
            for location in VIRTIOFSD_AS_LOCATIONS
        ],
    ]


def resolve_virtiofsd_as(
    name: str,
    which_fn=shutil.which,
    exists_fn=lambda path: Path(path).is_file() and os.access(path, os.X_OK),
) -> str | None:
    """The owner-exec helper to exec through (#1088): a name with
    a slash is used exactly as named; a bare name takes the
    first candidate that answers. None when nothing carries it —
    the caller refuses a cross-owner share by name instead of
    exec'ing a path that is not there. The shortcut lane (owner
    == the daemon's own uid) never calls this: a dev daemon
    without a helper stays fully functional."""
    if "/" in name:
        return name
    for candidate in virtiofsd_as_candidates(which_fn, exists_fn):
        if candidate is not None:
            return candidate
    return None


def fs_daemon_args(
    shared_dir: str,
    tag: str,
    fd: int,
    *,
    uid: int,
    gid: int,
    guest_uid: int,
    guest_gid: int,
    readonly: bool,
    sandbox: str,
) -> list[str]:
    """One virtiofsd's argument vector. ``readonly`` composes from
    both sides of the config: a share the operator narrowed
    (``writable: false``) and a mount that asked for ``ro``.
    ``fd`` is the listening socket's descriptor number the
    daemon bound itself and hands across the exec (#1088) — the
    number survives the exec in both lanes (``pass_fds`` in the
    shortcut lane, the helper's own contract in the helper
    lane), so the child accepts the one connection it was given
    the socket for and never creates a path."""
    args = [
        "--fd",
        str(fd),
        "--shared-dir",
        shared_dir,
        "--tag",
        tag,
        "--sandbox",
        sandbox,
        # The coherence contract (#620): no FUSE client caching, so
        # every operation reaches the host — the mechanism behind
        # simultaneous rw attaches sharing one page cache.
        "--cache",
        "never",
        # The identity contract, guest to host: every guest
        # uid/gid squashes to the daemon's own — writes land as one
        # host owner, and no guest uid can reach a host identity
        # it did not bring.
        "--translate-uid",
        f"squash-guest:0:{uid}:{WHOLE_RANGE}",
        "--translate-gid",
        f"squash-guest:0:{gid}:{WHOLE_RANGE}",
        # The identity contract, host to guest (#631): every host
        # uid/gid squashes to the workspace user's guest identity,
        # so the share's files read as the logged-in user's own in
        # the guest — a raw host uid would name whatever guest
        # account happens to hold the number.
        "--translate-uid",
        f"squash-host:0:{guest_uid}:{WHOLE_RANGE}",
        "--translate-gid",
        f"squash-host:0:{guest_gid}:{WHOLE_RANGE}",
        # Guest xattrs stay under one host prefix; posix ACLs stay
        # off (translation and ACLs cannot combine).
        "--xattrmap",
        XATTRMAP,
    ]
    if readonly:
        args.append("--readonly")
    return args


def virtiofsd_version(output: str) -> tuple[int, ...] | None:
    """The version from ``virtiofsd --version`` output — the first
    dotted number after the binary's name (the flag prints since
    upstream 1.7.2, so every build that can report one does).
    ``None`` when the output names no version: a binary that
    cannot report one predates every feature the argument vectors
    need, and the callers refuse it as below the floor rather
    than guess at what it is."""
    match = _VERSION_RE.search(output)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def virtiofsd_floor_message(version: tuple[int, ...] | None) -> str:
    """The below-floor refusal every check shares (#655): the
    floor, the version the binary reported, and the override that
    points the daemon at a newer self-built one."""
    floor = ".".join(str(part) for part in VIRTIOFSD_FLOOR)
    found = (
        ".".join(str(part) for part in version)
        if version is not None
        else "a binary that reported no version"
    )
    return (
        f"virtiofsd {found} is older than the {floor} host mounts "
        f"require (uid/gid translation and --readonly landed "
        f"upstream in {floor}); build a newer virtiofsd and point "
        f"MSKSD_VIRTIOFSD at it"
    )


def virtiofsd_missing_message(binary: str) -> str:
    """The named error for a virtiofsd the daemon cannot exec
    (missing or not executable) — shared by the version probe
    and the spawn seam so the two refuse identically."""
    return (
        f"virtiofsd binary not found or not executable: {binary} "
        "(the mounts feature needs it; msksd doctor names the "
        "package)"
    )


def fs_device_entries(sockets: dict[str, Path]) -> list[dict]:
    """The ``fs`` device list for cloud-hypervisor's vm.create
    body: one vhost-user-fs device per daemon, keyed by the tag
    the guest mounts by. DAX stays off (no shared-memory window:
    the cache=never posture needs none)."""
    return [
        {
            "tag": share,
            "socket": str(path),
            "num_queues": NUM_QUEUES,
            "queue_size": QUEUE_SIZE,
        }
        for share, path in sorted(sockets.items())
    ]

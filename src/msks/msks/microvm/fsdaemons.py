"""The virtiofs daemons behind host mounts (#620, part #622).

One virtiofsd per attached share, from the platform's own package
(the FIPS posture: no vendored build). This module owns the
argument grammar and the path shapes; the local driver owns the
process lifecycle — spawn before the VMM boots, teardown beside
the tap on every stop path.

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

from pathlib import Path

#: One queue of 1024 descriptors per daemon: a microvm's mount
#: traffic is metadata-heavy and single-queue serializes fine;
#: the vhost-user-fs examples' sizing.
NUM_QUEUES = 1
QUEUE_SIZE = 1024

#: Guest xattrs remap under one host prefix (the virtiofsd
#: documented mapping), so a guest cannot write host security
#: labels or root-owned namespaces through the share. Posix ACLs
#: stay off: uid translation and ACLs cannot combine.
XATTRMAP = ":map::user.virtiofs.:"

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
    is the daemon's."""
    return vm_dir / f"fs-{share}.sock"


def fs_pidfile_path(vm_dir: Path, share: str) -> Path:
    """The daemon's pidfile: a restarted daemon finds and stops a
    daemon its crashed predecessor left serving (the #56 pidfile
    fallback pattern)."""
    return vm_dir / f"fs-{share}.pid"


def sandbox_mode(uid: int) -> str:
    """The sandbox strength the daemon's privilege can carry: a
    root daemon gets the namespace sandbox (mount/pid/net
    namespaces plus pivot_root — the strongest escape guard); an
    unprivileged one gets ``none``, because both isolating modes
    need privileges the daemon does not hold. The share set
    itself stays operator-chosen either way (#620's trust
    model)."""
    return "namespace" if uid == 0 else "none"


def fs_daemon_args(
    shared_dir: str,
    tag: str,
    socket_path: Path,
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
    (``writable: false``) and a mount that asked for ``ro``."""
    args = [
        "--socket-path",
        str(socket_path),
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

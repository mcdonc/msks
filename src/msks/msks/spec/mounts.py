"""The host-mount vocabulary (#620, part #621).

Named shares are the operator's trust decision: the config lists
the host trees a guest may see, and a workspace mount names a
share plus the guest path it lands at plus a mode. The modules
here own that grammar — the row shape, the guest-path rules, the
mode bound, and the start-time issue derivation — with no
filesystem or daemon imports, so the settings parser, the
routes, and the attach path all speak one vocabulary.
"""

import json
from dataclasses import dataclass
from pathlib import PurePosixPath

#: The attach modes: ``ro`` serves the share read-only, ``rw``
#: read-write (the default — a mount that names no mode).
MOUNT_MODES = ("ro", "rw")
MODE_RW = "rw"

#: The guest paths a mount never lands on: the root itself and
#: the pseudo-filesystems the kernel owns. A mount at or under
#: one of these is refused by name.
FORBIDDEN_GUEST_ROOTS = ("/proc", "/sys", "/dev", "/run")

#: The home volume owns the guest's ``/home`` (#620): a mount
#: inside it would shadow the volume's contents and muddy the
#: home export/import and resize accounting.
HOME_GUEST_ROOT = "/home"

#: The mount row's allowed keys — every other key is a named
#: error at parse, so a typo'd field fails at set, never at boot.
MOUNT_KEYS = frozenset({"share", "guest", "mode"})

#: The share config's allowed keys (#620): ``name`` and ``path``
#: name the share, ``writable`` bounds the modes a request may
#: take (default true — listing a share is the trust decision),
#: and ``multi`` widens rw attachment to any number of
#: workspaces (default true; ``multi: false`` makes rw attaches
#: exclusive, one workspace at a time).
SHARE_KEYS = frozenset({"name", "path", "writable", "multi"})


@dataclass(frozen=True)
class MountShare:
    """One operator-configured share: the host tree a mount
    serves. ``resolved`` is the realpath taken at settings load
    (None when the path does not exist then — the doctor warns);
    the attach path re-resolves and refuses drift (#622)."""

    name: str
    path: str
    writable: bool = True
    multi: bool = True
    resolved: str | None = None


@dataclass(frozen=True)
class MountSpec:
    """One workspace mount: the share it names, the guest path it
    lands at, and its mode. ``mode`` defaults to ``rw`` — the
    request names the mode, the share's config bounds it."""

    share: str
    guest: str
    mode: str = MODE_RW

    def as_dict(self) -> dict:
        return {"share": self.share, "guest": self.guest, "mode": self.mode}


@dataclass(frozen=True)
class MountIssue:
    """Why one mount does not attach at start (#620): the share
    is gone from the config (``skipped``), a reload narrowed the
    share to read-only under an ``rw`` mount (``denied``), or the
    mount wants ``rw`` on an exclusive share another workspace
    already holds (``conflict``). A start proceeds without the
    mount either way — the skip is state, published as an event
    and derived again on every read."""

    kind: str
    mount: MountSpec
    detail: str

    @property
    def event(self) -> str:
        """The event-stream name: one per issue kind."""
        return f"mount.{self.kind}"

    def payload(self, workspace_id: str) -> dict:
        """The event frame's data: the workspace, the mount, and
        the named reason."""
        return {
            "id": workspace_id,
            "share": self.mount.share,
            "guest": self.mount.guest,
            "mode": self.mount.mode,
            "detail": self.detail,
        }


def parse_mounts(raw: object) -> tuple[MountSpec, ...]:
    """Decode a row's mounts value: the JSON text a create or set
    wrote, or the already-decoded list. Anything else is a named
    error — the row is machine-written, so a bad shape is a bug
    to catch at the seam, not a boot-time surprise."""
    entries = decoded_mount_entries(raw)
    if entries is None:
        return ()
    return tuple(mount_entry(entry) for entry in entries)


def decoded_mount_entries(raw: object) -> list | None:
    """The decoded array behind a row's mounts value: JSON text
    decodes, a list passes through, anything else is a named
    error."""
    entries = raw
    if isinstance(raw, (str, bytes)):
        try:
            entries = json.loads(raw)
        except ValueError:
            raise ValueError("mounts must be a JSON array") from None
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise ValueError("mounts must be a JSON array")
    return entries


def mount_entry(entry: object) -> MountSpec:
    """One decoded mount: a mapping with ``share``, ``guest``, and
    an optional ``mode`` — any other key, a missing field, or an
    unknown mode is a named error. The guest path canonicalizes
    here (``PurePosixPath`` collapses ``//`` and ``.``), so the
    stored form is the form every later rule and the #622 attach
    reason over."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"mounts entries must be mappings, got {type(entry).__name__}"
        )
    check_mount_keys(entry)
    return MountSpec(
        share=mount_text_field(entry, "share"),
        guest=canonical_guest(mount_text_field(entry, "guest")),
        mode=mount_mode_field(entry),
    )


def canonical_guest(guest: str) -> str:
    """The canonical spelling of one guest path: collapsed
    separators and resolved ``.`` components, trailing slash
    dropped. ``..`` survives untouched — the row rules refuse it,
    and silently collapsing it would hide the traversal."""
    return str(PurePosixPath(guest))


def check_mount_keys(entry: dict) -> None:
    """The row's key grammar: ``share``, ``guest``, ``mode`` —
    any other key is a named error at parse."""
    unknown = set(entry) - MOUNT_KEYS
    if unknown:
        raise ValueError(f"mounts entry has unknown keys: {sorted(unknown)}")


def mount_text_field(entry: dict, key: str) -> str:
    """One non-empty string field (the share name, the guest
    path): absent or non-string is a named error."""
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"mounts entry needs a {key}")
    return value


def mount_mode_field(entry: dict) -> str:
    """The entry's mode: absent reads the ``rw`` default, any
    other value is a named error."""
    mode = entry.get("mode", MODE_RW)
    if mode not in MOUNT_MODES:
        raise ValueError(
            f"mount mode must be one of {list(MOUNT_MODES)}, got {mode!r}"
        )
    return mode


def dump_mounts(mounts: tuple[MountSpec, ...]) -> str | None:
    """The row's JSON text: None (the column's NULL) for no
    mounts, so a pre-#620 row and a mountless row read the same."""
    if not mounts:
        return None
    return json.dumps([mount.as_dict() for mount in mounts])


def validate_mounts(mounts: tuple[MountSpec, ...]) -> None:
    """The row-level rules (#620): the guest paths must be
    mountable and mutually disjoint, and a share appears once.
    Every refusal names the mount."""
    seen_shares: set[str] = set()
    seen_guests: dict[str, MountSpec] = {}
    for mount in mounts:
        check_guest_path(mount)
        check_share_once(mount, seen_shares)
        for prior in seen_guests.values():
            check_shadowing(prior, mount)
        seen_guests[mount.guest] = mount


def check_share_once(mount: MountSpec, seen: set[str]) -> None:
    """A share appears once per workspace: the second mount of
    one share is a named refusal."""
    if mount.share in seen:
        raise ValueError(
            f"share {mount.share!r} is mounted twice; a share "
            "appears once per workspace"
        )
    seen.add(mount.share)


def check_guest_path(mount: MountSpec) -> None:
    """One mount's guest path: absolute, no ``..`` component, and
    neither the root, a pseudo-filesystem, the home volume, nor
    anything under those — each refusal names the rule."""
    path = PurePosixPath(mount.guest)
    if not path.is_absolute():
        raise ValueError(f"guest path {mount.guest!r} must be absolute")
    if ".." in path.parts:
        raise ValueError(f"guest path {mount.guest!r} must not contain '..'")
    if mount.guest == "/" or not path.parts[1:]:
        raise ValueError(
            f"guest path {mount.guest!r} is the filesystem root; a "
            "mount lands below it"
        )
    check_guest_roots(mount, path)


def check_guest_roots(mount: MountSpec, path: PurePosixPath) -> None:
    """The rooted-tree refusals: the pseudo-filesystems and the
    home volume own their paths, and a mount never lands on or
    under one."""
    for root, why in (
        *[(r, f"at or under {r}") for r in FORBIDDEN_GUEST_ROOTS],
        (
            HOME_GUEST_ROOT,
            f"at or under {HOME_GUEST_ROOT}; /home is the home volume",
        ),
    ):
        if under_root(path, root):
            raise ValueError(f"guest path {mount.guest!r} sits {why}")


def under_root(path: PurePosixPath, root: str) -> bool:
    """Whether *path* is *root* or sits under it: a component-wise
    prefix comparison from the anchor, so ``/mnt/proc`` is not
    under ``/proc`` the way ``/proc/self`` is."""
    base = PurePosixPath(root).parts[1:]
    parts = path.parts[1:]
    return len(parts) >= len(base) and parts[: len(base)] == base


def check_shadowing(prior: MountSpec, mount: MountSpec) -> None:
    """Two mounts may not land on one path or nest: the deeper
    one is invisible under the shallower one, so both orders are
    refused, naming the pair."""
    if prior.guest == mount.guest:
        raise ValueError(
            f"guest path {mount.guest!r} is mounted twice "
            f"(shares {prior.share!r} and {mount.share!r})"
        )
    prior_parts = PurePosixPath(prior.guest).parts
    mount_parts = PurePosixPath(mount.guest).parts
    shallower, deeper = sorted((prior_parts, mount_parts), key=len)
    if tuple(deeper[: len(shallower)]) == shallower:
        raise ValueError(
            f"guest path {mount.guest!r} shadows {prior.guest!r} "
            f"(shares {mount.share!r} and {prior.share!r})"
        )


def validate_against_shares(
    mounts: tuple[MountSpec, ...],
    shares: dict[str, MountShare],
) -> None:
    """The set-time bound (#620): every named share exists in the
    config, and an ``rw`` mount needs a share whose config says
    ``writable``. The refusal names the mismatch — never a silent
    read-only downgrade."""
    for mount in mounts:
        share = shares.get(mount.share)
        if share is None:
            raise ValueError(
                f"no share named {mount.share!r} in the daemon's config"
            )
        if mount.mode == MODE_RW and not share.writable:
            raise ValueError(
                f"share {mount.share!r} is configured read-only; "
                f"mount it with mode 'ro'"
            )


def mount_issues(
    mounts: tuple[MountSpec, ...],
    shares: dict[str, MountShare],
    rw_holders: dict[str, str],
) -> tuple[MountIssue, ...]:
    """Derive why any of a row's mounts do not attach (#620):
    ``skipped`` for a share the config no longer lists (the set
    held it once; a reload deleted it), ``denied`` for an ``rw``
    mount on a share a reload narrowed to read-only (the set
    accepted it once; the narrowing is honored, never silently
    downgraded), ``conflict`` for an ``rw`` mount on a
    ``multi: false`` share another workspace
    holds read-write. ``rw_holders`` maps share name to the
    holding workspace's id — callers exclude the row itself, so
    a workspace never conflicts with its own attach. Read-only
    mounts never conflict: any number may sit beside one rw
    holder."""
    issues = []
    for mount in mounts:
        issue = mount_issue(mount, shares, rw_holders)
        if issue is not None:
            issues.append(issue)
    return tuple(issues)


def mount_issue(
    mount: MountSpec,
    shares: dict[str, MountShare],
    rw_holders: dict[str, str],
) -> MountIssue | None:
    """One mount's issue, or None when it attaches: the share
    left the config, a reload denied its mode, or an ``rw``
    mount wants a ``multi: false`` share another workspace
    holds. Read-only mounts never conflict."""
    share = shares.get(mount.share)
    if share is None:
        return MountIssue(
            kind="skipped",
            mount=mount,
            detail=(f"share {mount.share!r} is not in the daemon's config"),
        )
    return narrowed_issue(mount, share) or held_issue(mount, share, rw_holders)


def narrowed_issue(mount: MountSpec, share: MountShare) -> MountIssue | None:
    """The reload narrowing: an ``rw`` mount on a share the
    config flipped to read-only is denied — honored with an
    event and a marker, never a silent read-only downgrade."""
    if mount.mode == MODE_RW and not share.writable:
        return MountIssue(
            kind="denied",
            mount=mount,
            detail=(
                f"share {mount.share!r} is configured read-only; the "
                "mount wants rw (remount it with mode 'ro')"
            ),
        )
    return None


def held_issue(
    mount: MountSpec, share: MountShare, rw_holders: dict[str, str]
) -> MountIssue | None:
    """The exclusivity refusal: an ``rw`` mount on a
    ``multi: false`` share another workspace holds read-write.
    Read-only mounts and multi shares never conflict."""
    if mount.mode != MODE_RW or share.multi:
        return None
    holder = rw_holders.get(mount.share)
    if holder is None:
        return None
    return MountIssue(
        kind="conflict",
        mount=mount,
        detail=(
            f"share {mount.share!r} is exclusive and already "
            f"mounted rw by workspace {holder}"
        ),
    )

"""The host-mount vocabulary (#620, part #621).

Named shares are the operator's trust decision: the config lists
the host trees a guest may see, and a workspace mount names a
share plus the guest path it lands at plus a mode. The modules
here own that grammar — the row shape, the guest-path rules, the
mode bound, and the start-time issue derivation — with no
filesystem or daemon imports, so the settings parser, the
routes, and the attach path all speak one vocabulary.

The same module owns the host-share geometry of #667 (#671): the
reserved prefix a VM-hosted daemon's own VM mounts share roots
under, the host-mode rule that translates operator-typed host
paths to the paths inside that VM, and the validation that a
share-root template's mounts conform (every root under the
prefix, every mountPoint the prefix plus the host path). Those
rules are string-level by design — the daemon-side realpath and
policy gates (#672) and the VM's own mount state live above the
leaf.
"""

import hashlib
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
    dropped, the anchor pinned to one slash (pathlib preserves
    exactly two leading slashes per POSIX — a distinct anchor
    from ``/`` that would dodge the disjoint rules). ``..``
    survives untouched — the row rules refuse it, and silently
    collapsing it would hide the traversal."""
    path = PurePosixPath(guest)
    if path.parts[:1] == ("//",):
        return "/" + "/".join(path.parts[1:])
    return str(path)


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
    check_guest_charset(mount)
    check_guest_unit_length(mount)
    check_guest_roots(mount, path)


def check_guest_charset(mount: MountSpec) -> None:
    """A guest path is printable ASCII (#620, part #623): the path
    lands verbatim inside a systemd unit file the seed writes, so
    a control character (a newline above all) would break the unit
    or inject directives, and the unit name escapes bytes — a
    printable path keeps the mount point the operator typed the
    one the guest gets. Spaces print; they escape in the name."""
    if not all(" " <= ch <= "~" for ch in mount.guest):
        raise ValueError(f"guest path {mount.guest!r} must be printable ASCII")


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


#: The share-name length ceiling: the name becomes the virtiofs
#: tag, and virtiofsd refuses a tag longer than 36 bytes — the
#: refusal belongs at settings load, not as an exec-time death.
SHARE_NAME_MAX = 36


def safe_share_name(name: str) -> bool:
    """Whether a share name is tag- and filename-safe (#620, part
    #622): ASCII letters, digits, dot, underscore, hyphen, at most
    36 bytes — the name becomes the virtiofs tag (the daemon's own
    length limit) and a socket/pidfile stem inside the vm
    directory."""
    return (
        bool(name)
        and len(name.encode()) <= SHARE_NAME_MAX
        and all(safe_name_char(ch) for ch in name)
    )


def safe_name_char(ch: str) -> bool:
    """One character of a share name: an ASCII letter or digit,
    or the dot, underscore, and hyphen the tag grammar takes."""
    return (ch.isascii() and ch.isalnum()) or ch in "._-"


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
                f"share {mount.share!r} is configured read-only; "
                "widen it in the daemon's config, or recreate the "
                "workspace with mode 'ro'"
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


#: The characters systemd leaves unescaped in a path-shaped unit
#: name (#620, part #623): ASCII letters and digits, dot,
#: underscore, and colon. The hyphen is NOT among them — it is
#: what a separator escapes to, so a literal one escapes too, and
#: the mapping stays injective: two paths never collide on one
#: unit name. Everything else lands as ``\\xHH`` per UTF-8 byte
#: (pinned against live systemd-escape output).
UNIT_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:"
)


def systemd_escape_path(guest: str) -> str:
    """The systemd-escape -p spelling of one guest path: the shape
    a mount unit's file name takes (``systemd-escape -p
    --suffix=mount``). Pinned byte-for-byte against the tool's own
    output; a divergence would make systemd refuse the unit whose
    name no longer matches its Where= path. The dot escapes when
    it would lead the escaped string (systemd.unit(5)'s escaping
    rule: ``/.config`` is ``\\x2econfig``, a mid-name dot stays
    plain)."""
    out: list[str] = []
    for byte in guest.lstrip("/").encode():
        out.append(escape_byte(byte, leading=not out))
    return "".join(out)


def escape_byte(byte: int, leading: bool) -> str:
    """One path byte's escaped spelling: the separator becomes the
    dash, an allowed character stays itself, everything else is
    ``\\xHH`` — and a dot that would lead the escaped string
    escapes too, so the name never starts with a bare dot."""
    ch = chr(byte)
    if ch == "/":
        return "-"
    if ch == "." and leading:
        return "\\x2e"
    if ch in UNIT_NAME_CHARS:
        return ch
    return f"\\x{byte:02x}"


#: systemd's unit-name ceiling (UNIT_NAME_MAX): the whole name,
#: suffix included, in bytes. The loader refuses longer names, and
# systemd-escape does not — a longer path would mount silently
# never (the unit fails to load), so the rule lives at parse.
UNIT_NAME_MAX = 255


def mount_unit_name(mount: MountSpec) -> str:
    """The mount unit's file name: the escaped guest path with the
    mount suffix."""
    return f"{systemd_escape_path(mount.guest)}.mount"


def check_guest_unit_length(mount: MountSpec) -> None:
    """The escaped unit name fits systemd's loader: a longer name
    refuses at set, naming the limit — the failure it prevents is
    silent (the seed writes a unit the loader rejects, and the
    mount never appears)."""
    if len(mount_unit_name(mount).encode()) > UNIT_NAME_MAX:
        raise ValueError(
            f"guest path {mount.guest!r} escapes to a unit name "
            f"longer than {UNIT_NAME_MAX} bytes"
        )


def mount_parent(guest: str) -> str:
    """The guest path's parent, the ordering anchor: a mount unit
    RequiresMountsFor its parent chain, so a path under any mounted
    tree mounts after it."""
    parent = guest.rsplit("/", 1)[0]
    return parent or "/"


def mount_unit(mount: MountSpec) -> str:
    """One share's systemd mount unit (#620, part #623): the
    virtiofs tag on What, the guest path on Where, read-only
    exactly when the mount's mode says so (the host daemon holds
    the same line on the device itself — part #622 — so the two
    refuse writes together). The marker comment is the prune loop's
    ownership test: msks owns the units it wrote, and only
    those."""
    options = "" if mount.mode == "rw" else "Options=ro\n"
    return (
        "# msks mount unit (#620): the seed writes and prunes this\n"
        "# file; change the workspace's mounts, not the unit.\n"
        "[Unit]\n"
        f"Description=msks share {mount.share} at {mount.guest}\n"
        f"RequiresMountsFor={mount_parent(mount.guest)}\n"
        "[Mount]\n"
        f"What={mount.share}\n"
        f"Where={mount.guest}\n"
        "Type=virtiofs\n"
        f"{options}"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def mounts_fingerprint(mounts: tuple[MountSpec, ...]) -> str:
    """The identity of one mount set: a sha256 over the
    share/guest/mode lines. The seed's marker beside it carries the
    fingerprint of the set it baked, so a start whose set differs
    renews the seed (part #623) — and an unchanged set never
    rebuilds."""
    if not mounts:
        # The empty set's fingerprint is the empty string: the
        # marker's contract — a mountless seed's marker says ""
        # and an all-mounts-removed renewal writes exactly that.
        return ""
    lines = sorted(
        f"{mount.share}:{mount.guest}:{mount.mode}" for mount in mounts
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def sh_quote(text: str) -> str:
    """One string as a single-quoted POSIX sh word: the printable
    charset leaves quotes and backslashes as the only
    metacharacters to respect, and single quoting neutralizes both
    except the quote itself, which closes and reopens around a
    backslashed one (``a'\\''b``)."""
    return "'" + text.replace("'", "'\\''") + "'"


#: Where the seed parks the per-boot installer: cloud-init runs
#: scripts under this directory on every boot (#620, part #623) —
#: the runtime unit dir is volatile, so something must reinstall
#: the units each boot.
PER_BOOT_DIR = "/var/lib/cloud/scripts/per-boot"
PER_BOOT_INSTALLER = f"{PER_BOOT_DIR}/50-msks-mounts"


def per_boot_installer() -> str:
    """The script cloud-init runs on every boot: copy the parked
    unit files into systemd's runtime unit dir and start them.
    That dir is the one writable unit home every systemd guest
    owns — /etc/systemd/system is a store symlink on the NixOS
    image and the distro's own on Debian — and it is volatile, so
    the installer is what makes the mounts survive a reboot."""
    return (
        "#!/bin/sh\n"
        "# msks (#620): reinstall the seed's mount units each boot.\n"
        "set -eu\n"
        "command -v systemctl >/dev/null 2>&1 || exit 0\n"
        "install -d -m 0755 /run/systemd/system\n"
        "for u in /etc/msks/mount-units/*.mount; do\n"
        '  [ -e "$u" ] || continue\n'
        '  cp "$u" /run/systemd/system/\n'
        "done\n"
        "systemctl daemon-reload 2>/dev/null || true\n"
        "for u in /etc/msks/mount-units/*.mount; do\n"
        '  [ -e "$u" ] || continue\n'
        '  systemctl start "${u##*/}" 2>/dev/null || true\n'
        "done\n"
    )


def mounts_seed_block(mounts: tuple[MountSpec, ...]) -> str:
    """The seed script's mounts half (#620, part #623): park one
    systemd mount unit per share under /etc/msks/mount-units (the
    name the guest path escapes to), make the mount points, prune
    the parked units the set no longer names (a share gone from
    the config stops, unmounts, and leaves with it), then write
    and run the per-boot installer that carries the units into
    systemd's runtime dir — volatile, so the installer is what
    makes the mounts survive a reboot, and the only unit home the
    two guest images share (/etc/systemd/system is a store symlink
    on NixOS). Idempotent: a re-provision rewrites the same units
    and starts what is started. Every step is best-effort — a
    guest without systemd keeps booting and says so on stderr.
    The empty set is a real payload: the prune-only block is what
    removes the last mount's units."""
    manifest = "".join(f"{mount_unit_name(mount)}\n" for mount in mounts)
    writes = "".join(
        # Best-effort mount-point make: systemd creates the point
        # itself when the unit starts, so a failure here (a
        # read-only parent the operator's own payload made) says so
        # and leaves the unit to carry the mount — and never aborts
        # the seed script's other halves (set -eu runs this).
        f"install -d -m 0755 {sh_quote(mount.guest)} 2>/dev/null "
        "|| echo 'msks: could not make a mount point' >&2\n"
        f"cat > '/etc/msks/mount-units/{mount_unit_name(mount)}' "
        f"<<'MSEOF'\n{mount_unit(mount)}MSEOF\n"
        for mount in mounts
    )
    return (
        "if command -v systemctl >/dev/null 2>&1; then\n"
        "install -d -m 0755 /etc/msks /etc/msks/mount-units "
        f"{PER_BOOT_DIR}\n"
        "cat > /etc/msks/mount.units <<'MSEOF'\n"
        f"{manifest}MSEOF\n"
        f"{writes}"
        "for u in /etc/msks/mount-units/*.mount; do\n"
        '  [ -e "$u" ] || continue\n'
        '  grep -qxF "${u##*/}" /etc/msks/mount.units && continue\n'
        '  systemctl stop "${u##*/}" 2>/dev/null || true\n'
        '  rm -f "$u" "/run/systemd/system/${u##*/}"\n'
        "done\n"
        f"cat > '{PER_BOOT_INSTALLER}' <<'MSEOF'\n"
        f"{per_boot_installer()}MSEOF\n"
        f"chmod 0755 '{PER_BOOT_INSTALLER}'\n"
        f"sh '{PER_BOOT_INSTALLER}'\n"
        "else\n"
        "echo 'msks: host-share mounts need systemd; this guest "
        "mounts none of them' >&2\n"
        "fi\n"
    )


# --- The host-share geometry (#667, part #671) -----------------

#: The reserved prefix every share root mounts under inside a
#: VM-hosted daemon's own VM — the msksd VM Lima runs on a Mac
#: (the WSL ``/mnt/c`` shape: the operator's ``/Users/chrism``
#: appears at ``/mnt/msks/Users/chrism``). One constant for the
#: whole feature: the Lima template pins its mountPoints to it,
#: the daemon's host-mode translation composes host paths onto
#: it (#672), and every refusal message names it. A namespace
#: the Linux FHS never claims, so a share root cannot shadow VM
#: system state the way a same-path mount would — the structural
#: safety that replaces an allowlist of "safe" host paths.
SHARE_ROOT_PREFIX = "/mnt/msks"

#: The daemon's host modes (#667): ``identity`` — a bare-Linux
#: daemon, share roots are its own local paths and the path the
#: client sends is the path on disk; ``prefix`` — the daemon runs
#: inside its own VM (the Lima/macOS host), share roots are host
#: directories mounted under SHARE_ROOT_PREFIX and translation
#: composes the prefix. The daemon's settings pick the mode
#: (#672); the two names are the whole of the rule.
HOST_MODE_IDENTITY = "identity"
HOST_MODE_PREFIX = "prefix"
HOST_MODES = (HOST_MODE_IDENTITY, HOST_MODE_PREFIX)


def canonical_host_path(host_path: str) -> str:
    """One operator-typed host path in canonical form: absolute,
    no ``..`` component, separators collapsed, trailing slash
    dropped. The same shape rule the guest paths take (a ``..``
    is refused, never silently collapsed), so a path the client
    sends cannot dodge the prefix and policy checks with
    spelling tricks."""
    if not isinstance(host_path, str) or not host_path:
        raise ValueError("a share path must be a non-empty string")
    check_host_path_shape(host_path)
    return canonical_guest(str(PurePosixPath(host_path)))


def check_host_path_shape(host_path: str) -> None:
    """The shape refusals for one host path: absolute, no
    traversal component, and not the filesystem root — the
    last because a share root is one host tree, and the whole
    filesystem is not one."""
    path = PurePosixPath(host_path)
    if not path.is_absolute():
        raise ValueError(f"share path {host_path!r} must be absolute")
    if ".." in path.parts:
        raise ValueError(f"share path {host_path!r} must not contain '..'")
    if not path.parts[1:]:
        raise ValueError(
            "a share root is one host tree, not the filesystem root"
        )


def translate_host_path(mode: str, host_path: str) -> str:
    """The path one share root takes inside the daemon's own
    namespace (#667): under ``identity`` the host path itself;
    under ``prefix`` SHARE_ROOT_PREFIX with the host path
    composed on (``/Users/chrism/code`` →
    ``/mnt/msks/Users/chrism/code``). The inverse spelling of the
    same rule is `untranslate_vm_path`; both refuse a path the
    rule cannot carry, naming it."""
    check_host_mode(mode)
    canonical = canonical_host_path(host_path)
    if mode == HOST_MODE_IDENTITY:
        return canonical
    # String composition, not a pathlib join: the host path is
    # absolute, and joining an absolute path onto a base replaces
    # the base outright.
    return SHARE_ROOT_PREFIX + canonical


def untranslate_vm_path(mode: str, vm_path: str) -> str:
    """The operator-facing host path a daemon-namespace path
    spells: under ``identity`` the path itself; under ``prefix``
    the part under SHARE_ROOT_PREFIX, refused when the path does
    not sit under it. The refusal-message half of the
    translation — the daemon reports paths the operator typed,
    not the geometry of its own VM."""
    check_host_mode(mode)
    path = PurePosixPath(canonical_guest(vm_path))
    if mode == HOST_MODE_IDENTITY:
        return str(path)
    if not under_root(path, SHARE_ROOT_PREFIX):
        raise ValueError(
            f"path {vm_path!r} is not under {SHARE_ROOT_PREFIX}; a "
            "prefix-mode host path always is"
        )
    base = PurePosixPath(SHARE_ROOT_PREFIX).parts[1:]
    rest = path.parts[1 + len(base) :]
    if not rest:
        raise ValueError(
            f"{SHARE_ROOT_PREFIX} is the reserved prefix itself; a "
            "share root mounts under it"
        )
    return "/" + "/".join(rest)


def check_host_mode(mode: str) -> None:
    """One of the two host modes; anything else is a named error
    — the mode comes from the daemon's own settings, so a bad
    value is a configuration bug to catch at load, not a path
    decision at attach."""
    if mode not in HOST_MODES:
        raise ValueError(f"host mode must be one of {list(HOST_MODES)}")


@dataclass(frozen=True)
class ShareRoot:
    """One share root a VM-hosted daemon's template mounts (#667):
    the host tree (``location``, as Lima rendered it — ``~`` and
    ``{{.User}}`` already expanded) and the path it mounts at
    inside the daemon VM. The pair is validated at construction —
    the mountPoint is always the reserved prefix plus the host
    path, the suffix-preservation invariant the daemon's
    translation and the client's paths both rely on."""

    location: str
    mount_point: str


def share_root_row(location: str, mount_point: str | None) -> ShareRoot:
    """Validate one rendered template mount (#667): the location
    is a canonical host path and the mountPoint is exactly
    SHARE_ROOT_PREFIX plus it — the WSL shape, checked on the
    rendered pair (Lima expanded ``~`` and ``{{.User}}`` before
    the validator ever runs). A mountPoint that misses the
    prefix, lands beside it, or carries no mountPoint at all
    (Lima's same-path default — the shadowing the prefix exists
    to prevent) is a named refusal."""
    host_path = canonical_host_path(location)
    if mount_point is None:
        raise ValueError(
            f"mount of {location!r} carries no mountPoint; Lima's "
            "default mounts the host path itself — share roots "
            f"mount under {SHARE_ROOT_PREFIX}"
        )
    expected = translate_host_path(HOST_MODE_PREFIX, host_path)
    rendered = canonical_guest(mount_point)
    if rendered != expected:
        raise ValueError(
            f"mount of {location!r} at {mount_point!r} breaks the "
            "reserved-prefix shape; the share root mounts at "
            f"{expected}"
        )
    return ShareRoot(location=host_path, mount_point=rendered)


def share_roots_from_template(rows: object) -> tuple[ShareRoot, ...]:
    """The share roots a Lima template's ``mounts`` list declares,
    validated as a set (#667): every row conforms
    (`share_root_row`), and no location mounts twice — the only
    duplicate the shape admits, since the invariant derives each
    mountPoint from its location. Rows may nest — an operator
    mounting a deeper tree beside a wider one composes the two
    virtiofs mounts the way a nested bind mount would, and
    translation stays well-defined (each host path composes onto
    the prefix either way). Lima's own row keys (``sshfs``,
    ``9p``, …) pass through untouched: Lima validates its
    grammar, this module the geometry."""
    if not isinstance(rows, list):
        raise ValueError("a template's mounts must be a list")
    roots = tuple(share_root_mount(row) for row in rows)
    seen_locations: set[str] = set()
    for root in roots:
        if root.location in seen_locations:
            raise ValueError(
                f"host path {root.location!r} is mounted twice in the template"
            )
        seen_locations.add(root.location)
    return roots


def share_root_mount(row: object) -> ShareRoot:
    """One template row to a share root: a mapping carrying
    ``location`` and ``mountPoint`` as strings — the rendered
    pair `share_root_row` validates. A row of any other shape is
    a named error (the caller parsed the template; a bad row is
    a template bug to catch here, not at VM boot)."""
    if not isinstance(row, dict):
        raise ValueError(
            "template mounts entries must be mappings, "
            f"got {type(row).__name__}"
        )
    return share_root_row(row.get("location"), row.get("mountPoint"))

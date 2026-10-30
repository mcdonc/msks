"""The daemon-side host-share gates (#667, part #672).

The vocabulary the gates speak is string-level and lives in
``msks.spec.mounts`` (the reserved prefix, the host modes, the
host-path shape rules); this module is the filesystem-facing
half — the parts that must look at the daemon's own namespace:

- which share roots the daemon's VM actually has mounted (the
  first gate: on a VM-hosted daemon, a host tree reaches the
  daemon only if the Lima template mounted it under the reserved
  prefix — /proc/mounts is the ground truth, not the template's
  intent);
- the realpath that closes the symlink escapes (#667: a
  ``~/projects`` symlink to ``/`` must not turn an allowed root
  into the whole filesystem), and the existence check that keeps
  a missing path a create-time refusal rather than a boot-time
  mount failure.

The two gates, both refusing with their remedy (#667's refusal
taxonomy): the mount gate names VM recreation (the template
decides what is mounted), the policy gate names the daemon's
config (the operator decides what is shareable). The policy
gate's allowed roots are typed as host paths and translated by
the same host-mode rule as the request, so one spelling — the
operator's — flows through the whole chain.
"""

import os
from pathlib import Path, PurePosixPath

from .spec.mounts import (
    HOST_MODE_PREFIX,
    SHARE_ROOT_PREFIX,
    canonical_host_path,
    check_host_mode,
    translate_host_path,
    under_root,
)

#: Where the mounted-roots enumeration reads: the kernel's mount
#: table. A parameter, not a constant, so the gate is testable
#: against fixture text.
PROC_MOUNTS = "/proc/mounts"

#: The hop-1 transport the msks Lima template allows (#671): vz
#: virtiofs, the only one the repro suites validated. The mount
#: gate reads mounts of exactly this type under the reserved
#: prefix.
SHARE_MOUNT_FSTYPE = "virtiofs"


def mounted_share_roots(source: str | Path = PROC_MOUNTS) -> tuple[str, ...]:
    """The share roots mounted under the reserved prefix, from the
    kernel's mount table: every ``virtiofs`` mount point under
    ``/mnt/msks``, canonicalized. Empty on a bare-Linux host (no
    such mounts exist — the mount gate does not run there) and on
    a VM host before the template's mounts land."""
    text = Path(source).read_text() if source else ""
    roots = [
        str(path)
        for line in text.splitlines()
        if (path := share_root_line(line)) is not None
    ]
    return tuple(sorted(roots))


def share_root_line(line: str) -> PurePosixPath | None:
    """One mount-table line as a share root, or None: the
    ``virtiofs`` mounts under the reserved prefix, with the
    mount point's octal escapes decoded — everything else
    (other filesystems, mounts outside the prefix, malformed
    lines) is not a share root."""
    fields = line.split()
    if len(fields) < 3 or fields[2] != SHARE_MOUNT_FSTYPE:
        return None
    path = PurePosixPath(unescape_mount_field(fields[1]))
    return path if under_root(path, SHARE_ROOT_PREFIX) else None


def unescape_mount_field(field: str) -> str:
    """One /proc/mounts field with its octal escapes decoded: the
    kernel writes space, tab, newline, and backslash as ``\\040``,
    ``\\011``, ``\\012``, and ``\\134`` — a mount point carrying a
    space reads escaped, and the gate must compare the real
    path."""
    if "\\" not in field:
        return field
    out = []
    index = 0
    while index < len(field):
        index = unescape_one(field, out, index)
    return "".join(out)


def unescape_one(field: str, out: list[str], index: int) -> int:
    """Consume one character (or one octal escape) at *index*,
    appending its decoding to *out*, and return the next index."""
    if field[index] == "\\":
        octal = field[index + 1 : index + 4]
        if len(octal) == 3 and all(ch in "01234567" for ch in octal):
            out.append(chr(int(octal, 8)))
            return index + 4
    out.append(field[index])
    return index + 1


def resolve_path(path: str) -> str:
    """``os.path.realpath``, as a seam: the gate tests map the
    reserved prefix's paths onto throwaway directories by
    overriding this and `path_is_dir` — the two places the gate
    touches the daemon's own filesystem."""
    return os.path.realpath(path)


def path_is_dir(path: str) -> bool:
    """``os.path.isdir`` — the seam's second half."""
    return os.path.isdir(path)


def check_host_share(
    mode: str,
    allowed_roots: tuple[str, ...],
    host_path: str,
    source: str | Path = PROC_MOUNTS,
) -> str:
    """Run one host-path share through the two gates (#667) and
    return the daemon-namespace directory virtiofsd will serve —
    the translated path, realpath'd. The mount gate runs on VM
    hosts only (a bare-Linux daemon's whole filesystem is its
    own); the policy gate runs everywhere, realpath first so a
    symlink planted inside an allowed root cannot walk the share
    out of it. Each refusal names its gate and its remedy:
    recreation of the VM for an unmounted root, a config edit for
    an unallowed one."""
    check_host_mode(mode)
    canonical = canonical_host_path(host_path)
    translated = translate_host_path(mode, canonical)
    if mode == HOST_MODE_PREFIX:
        check_mount_gate(canonical, translated, mounted_share_roots(source))
    resolved = resolve_path(translated)
    check_policy_gate(mode, allowed_roots, canonical, resolved)
    check_present(canonical, resolved)
    return resolved


def check_mount_gate(
    canonical: str, translated: str, roots: tuple[str, ...]
) -> None:
    """The VM mount gate (#667): on a prefix-mode host, the
    translated path must sit under a root the daemon's VM
    actually has mounted — vz mounts are fixed at VM start, so
    the remedy is recreating the VM from a template that mounts
    the root, never a config edit."""
    for root in roots:
        if under_root(PurePosixPath(translated), root):
            return
    mounted = ", ".join(roots) if roots else "none are mounted"
    raise ValueError(
        f"host path {canonical!r} is not mounted in this daemon's VM "
        f"(the mounted share roots under {SHARE_ROOT_PREFIX}: "
        f"{mounted}); recreate the VM from a Lima template that "
        "mounts the root it lives under"
    )


def check_policy_gate(
    mode: str, allowed_roots: tuple[str, ...], canonical: str, resolved: str
) -> None:
    """The policy gate (#667): the share path, resolved in the
    daemon's own namespace, must sit under an allowed root — each
    root a host path translated and realpath'd by the same rule,
    so the comparison sees what the daemon would serve, not what
    was typed. A symlink that resolves outside every allowed root
    is refused here (#667's escape rule). The remedy is the
    daemon's config: the operator widens the allowed roots."""
    for root in allowed_roots:
        root_canonical = canonical_host_path(root)
        root_translated = translate_host_path(mode, root_canonical)
        if under_root(PurePosixPath(resolved), resolve_path(root_translated)):
            return
    raise ValueError(
        f"host path {canonical!r} is outside the allowed host-share "
        "roots; add it (or a parent directory) to the daemon's "
        "config (MSKSD_MOUNT_HOST_ROOTS) and reload"
    )


def check_present(canonical: str, resolved: str) -> None:
    """The existence check: a path through both gates that does
    not exist is a plain, named refusal at set time — the
    alternative is virtiofsd refusing at boot, the boot-time
    surprise #667's create-time gates exist to prevent."""
    if not path_is_dir(resolved):
        raise ValueError(
            f"host path {canonical!r} resolves to {resolved!r}, "
            "which does not exist or is not a directory"
        )

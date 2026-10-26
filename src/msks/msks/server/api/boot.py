"""Create-time boot resolution: the image catalog record, the
create's label and instance id, the kernel/initrd/rootfs/
cmdline fields a create fills from it (#14, #246), and the host's
timezone captured with them (#633)."""

import os
import re
import secrets
from pathlib import Path

from fastapi import HTTPException

from ...imagestore import ImageError, default_image, resolve
from .schemas import WorkspaceCreate


def image_record(app, body: WorkspaceCreate):
    """The requested catalog record, or the default when omitted."""
    state_dir = app.state.settings.vmm.state_dir
    if body.image is not None:
        try:
            record = resolve(body.image, state_dir)
        except ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"no such image: {body.image}"
            )
        return record
    return default_image(state_dir)


def create_name(body: WorkspaceCreate) -> str | None:
    """The create's label (#246): ``name``, the legacy ``id``
    spelling of it, or None for a nameless workspace.

    Both fields carry the same constraints, so a client one version
    behind keeps working (its ``id`` is treated as the name); sending
    both is accepted when they agree and refused when they differ —
    two different labels for one workspace is a client bug, not a
    coin flip.
    """
    if body.name is not None and body.id is not None:
        if body.name != body.id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "name and id disagree; send one (id is the "
                    "pre-#246 spelling of name)"
                ),
            )
        return body.name
    return body.name if body.name is not None else body.id


async def mint_workspace_id(model) -> str:
    """One fresh #246 instance id: 10 hex digits (5 random bytes).

    Short enough to read and copy from a listing, long enough that
    collisions need ~1M live workspaces to become likely — and the
    mint re-rolls while a candidate already answers on this daemon
    (an id AND a name block it: ref resolution prefers the id, so a
    workspace named like another's id would be shadowed by it). The
    insert's primary-key index is the backstop for a same-id race
    between two creates of different names.
    """
    while True:
        candidate = secrets.token_hex(5)
        if await model.get_workspace(candidate) is None:
            return candidate


def validated_user_data(body: WorkspaceCreate) -> str | None:
    """The #41 payload: present-and-nonempty.

    cloud-init runs both payload forms (#! scripts and cloud-config
    YAML), so the daemon accepts both; an image declares its
    provisioner in ``image.json`` for the operator and the docs, not
    for create-time policing. Explicit boot artifacts have no
    manifest at all — the same acceptance applies.
    """
    if body.user_data is None:
        return None
    if not body.user_data.strip():
        raise HTTPException(status_code=400, detail="user_data is empty")
    return body.user_data


def resolve_boot(app, body: WorkspaceCreate, workspace_id: str) -> dict:
    """Fill kernel/initrd/rootfs/cmdline from the image catalog.

    Explicit fields win over the image; the image wins over the
    default; nothing resolves at all is a client error. The result
    also carries the #14 facts: the catalog hash the overlay will
    bind to (None for explicit boot artifacts) and the artifact
    sizes — and the #633 fact: the host's timezone, captured here
    so the row and its seed pin what the create saw. ``workspace_id``
    is the daemon-minted instance id (#246) — the artifact paths
    derive from it, never from the operator's label.
    """
    record = image_record(app, body)
    kernel, rootfs = boot_pair(body, record)
    if (body.kernel is None) != (body.rootfs is None):
        raise HTTPException(
            status_code=400, detail="kernel and rootfs come together"
        )
    return {
        "id": workspace_id,
        "kernel": kernel,
        "initrd": default_initrd(body, record),
        "rootfs": rootfs,
        "cmdline": default_cmdline(body, record),
        "cpus": body.cpus,
        "mem_mib": body.mem_mib,
        "image_hash": bound_image_hash(body, record),
        "egress": body.egress,
        "user_data": validated_user_data(body),
        "login_user": body.user,
        # The host's timezone, captured at create (#633): the seed
        # bakes it and the row pins it — a later host change never
        # drifts a workspace already minted.
        "timezone": host_timezone(),
        **artifact_sizes(app, body),
    }


#: The zoneinfo roots a host's /etc/localtime symlink names: glibc's
#: classic tree first, the fallback the musl and usr-merged layouts
#: carry.
ZONEINFO_ROOTS = ("/usr/share/zoneinfo/", "/etc/zoneinfo/")

#: The IANA zone-name grammar the block interpolates: path-less
#: segments of letters, digits, underscore, dash, and plus (the
#: ``Etc/GMT+5`` family), joined by single slashes. Anything else
#: — a traversal, a quote, whitespace — reads as no timezone, so a
#: hostile or mangled host file never reaches the guest's shell.
ZONE_NAME_RE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z")

#: The spellings that read as UTC: nothing rides for them, so a
#: UTC host's bare workspaces keep the two-disk, no-seed boot.
UTC_NAMES = frozenset({"UTC", "Etc/UTC", "Universal", "Zulu"})


def host_timezone(
    localtime: str = "/etc/localtime", zone_file: str = "/etc/timezone"
) -> str | None:
    """The host's timezone as a zone name, None for UTC or an
    unreadable clock (#633): the /etc/localtime symlink's target
    under a zoneinfo root first, the /etc/timezone file's first
    line (Debian's companion) as the fallback — a copied rather
    than linked localtime, or neither file, is UTC by refusal,
    never a guess."""
    return linked_zone(localtime) or file_zone(zone_file)


def linked_zone(localtime: str) -> str | None:
    """The zone the /etc/localtime symlink names: its target under
    a zoneinfo root, None when the file is not a link (a copied
    zone) or names nothing valid."""
    try:
        target = os.readlink(localtime)
    except OSError:
        return None
    for root in ZONEINFO_ROOTS:
        if target.startswith(root):
            zone = zone_name(target[len(root) :])
            if zone is not None:
                return zone
    return None


def file_zone(zone_file: str) -> str | None:
    """The zone /etc/timezone names: the file's first line, None
    when the file is absent or unreadable."""
    try:
        first = Path(zone_file).read_text(encoding="utf-8").splitlines()[0]
    except OSError, UnicodeDecodeError, IndexError:
        return None
    return zone_name(first.strip())


def zone_name(candidate: str) -> str | None:
    """One candidate as a zone name: valid grammar and not a UTC
    spelling — else None, the nothing-that-rides value."""
    if not candidate or candidate in UTC_NAMES:
        return None
    if ".." in candidate or not ZONE_NAME_RE.fullmatch(candidate):
        return None
    return candidate


def bound_image_hash(body: WorkspaceCreate, record) -> str | None:
    """The catalog hash the workspace's overlay binds to, if any.

    Binding follows the root disk: the image's rootfs only carries a
    workspace that did not override it with an explicit path."""
    if body.rootfs is None and record is not None:
        return record.hash
    return None


def artifact_sizes(app, body: WorkspaceCreate) -> dict:
    """root/home sizes: the request's, else the settings defaults."""
    vmm = app.state.settings.vmm
    return {
        "root_mib": body.root_mib
        if body.root_mib is not None
        else vmm.root_mib,
        "home_mib": body.home_mib
        if body.home_mib is not None
        else vmm.home_mib,
    }


def boot_pair(body: WorkspaceCreate, record) -> tuple[str, str]:
    """kernel/rootfs: explicit fields win, the record fills the rest."""
    if body.kernel is not None and body.rootfs is not None:
        return body.kernel, body.rootfs
    if record is None:
        raise HTTPException(
            status_code=400,
            detail="kernel/rootfs (or image, or a default image) required",
        )
    return fill(body.kernel, record.kernel), fill(body.rootfs, record.rootfs)


def fill(explicit: str | None, from_record: Path) -> str:
    """One field: the explicit value, else the record's path."""
    return explicit if explicit is not None else str(from_record)


def default_initrd(body: WorkspaceCreate, record) -> str | None:
    """The image's initrd when booting wholly from the catalog."""
    if body.initrd is None and record is not None and body.kernel is None:
        return str(record.initrd)
    return body.initrd


def default_cmdline(body: WorkspaceCreate, record) -> str:
    """The image's cmdline, or the legacy default with no record."""
    if body.cmdline is not None:
        return body.cmdline
    if record is not None:
        return record.cmdline
    return "console=ttyS0 root=/dev/vda rw"

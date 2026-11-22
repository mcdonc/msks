"""The row serializers: ORM rows to the API's dict shapes.

One function per row type the API answers with, beside the
workspace resolver and the field collector the write paths
share — every dict the model emits takes its exact shape here.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from ..spec.mounts import dump_mounts, parse_mounts
from ..spec.vm import VmSpec
from .proxies import WorkspaceProxy
from .secrets import Placeholder, SecretAudit
from .workspaces import (
    AmbiguousRef,
    Workspace,
    daemon_uid,
    sees_all_rows,
)


async def resolve_workspace(
    session, ref: str, viewer: int | None = None
) -> Workspace | None:
    """One workspace row by id-or-name ref (#246), inside a session
    the caller owns: id first (the canonical preference, not a
    tiebreak — live ids and names are one disjoint namespace), then
    the name.

    *viewer* is the owner filter (#1082): a uid the filter does not
    bypass sees and addresses its own rows alone, and a row another
    owner holds answers None — indistinguishable from an absent
    one, the 404 mask every route-facing lookup takes. ``None`` is
    the daemon's own unfiltered read. Name resolution for a
    bypassing viewer spans every owner: a name more than one owner
    holds refuses to pick (:class:`AmbiguousRef`) rather than
    silently aim a verb at the wrong workspace.
    """
    row = await session.get(Workspace, ref)
    if row is not None:
        if sees_all_rows(viewer) or row.owner_uid == viewer:
            return row
        return None
    return await resolve_by_name(session, ref, viewer)


async def resolve_by_name(
    session, ref: str, viewer: int | None
) -> Workspace | None:
    """The name half of ref resolution, scoped by the owner filter
    (#1082): a bypassing viewer spans every owner (and a name more
    than one of them holds refuses to pick), every other viewer
    resolves against its own rows alone."""
    stmt = select(Workspace).where(Workspace.name == ref)
    if not sees_all_rows(viewer):
        stmt = stmt.where(Workspace.owner_uid == viewer)
    rows = (await session.scalars(stmt)).all()
    if len(rows) > 1:
        raise AmbiguousRef(ref, sorted({row.owner_uid for row in rows}))
    return rows[0] if rows else None


def workspace_fields(
    spec: VmSpec,
    image_hash: str | None,
    host: str | None,
    name: str | None = None,
    owner_uid: int | None = None,
) -> dict:
    """The ORM column values a VmSpec maps to. *owner_uid* (#1082)
    is the creating peer's credential; None means the daemon's own
    uid — the TCP surface's mapping."""
    return {
        "id": spec.workspace_id,
        "owner_uid": daemon_uid() if owner_uid is None else owner_uid,
        "name": name,
        "kernel": str(spec.kernel),
        "initrd": None if spec.initrd is None else str(spec.initrd),
        "rootfs": str(spec.rootfs),
        "cmdline": spec.cmdline,
        "cpus": spec.cpus,
        "mem_mib": spec.mem_mib,
        "image_hash": image_hash,
        "host": host,
        "root_mib": spec.root_mib,
        "home_mib": spec.home_mib,
        "egress": spec.egress,
        "egress_mode": spec.egress_mode,
        "secret_coverage": spec.secret_coverage,
        "egress_allowlist": json.dumps(spec.egress_allowlist)
        if spec.egress_allowlist
        else None,
        "user_data": spec.user_data,
        "ssh_pubkey": spec.ssh_pubkey,
        "login_user": spec.login_user,
        # The host's timezone captured at create (#633): NULL is
        # UTC-or-unreadable — a UTC host and a pre-#633 row read
        # the same.
        "timezone": spec.timezone,
        # The workspace's mounts (#620, part #624): the create-time
        # set rides the spec; dump_mounts keeps the no-mounts row
        # NULL (a pre-#620 row and a mountless row read the same).
        "mounts": dump_mounts(spec.mounts),
        "status": "created",
    }


def proxy_dict(row: WorkspaceProxy) -> dict:
    """The API-facing dict for a proxy row. The two hosts are
    direction-shaped: a forward row carries ``listen_host`` (the
    daemon-side bind) and no ``target_host`` (the target is the
    guest); a remote row carries ``target_host`` (the daemon-side
    dial) and no ``listen_host`` (the bind is the workspace's tap
    address, a runtime fact the row cannot pin)."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "direction": row.direction,
        "listen_host": row.listen_host,
        "listen_port": row.listen_port,
        "target_host": row.target_host,
        "target_port": row.target_port,
        "label": row.label,
        "created_at": row.created_at.isoformat(),
    }


def placeholder_dict(row: Placeholder) -> dict:
    """The API-facing dict for a placeholder row.

    The sentinel is included — mint's response and the on-demand
    row fetch (#440) print it — but list views built from these
    dicts drop it. ``workspaces`` is the coverage set (#339):
    ``[]`` is the owner-wide row, and ``owner_uid`` (#1083) names
    the minting owner.
    """
    return {
        "id": row.id,
        "owner_uid": row.owner_uid,
        "workspaces": json.loads(row.workspaces),
        "name": row.name,
        "sentinel": row.sentinel,
        "dests": json.loads(row.dests),
        "backend_ref": row.backend_ref,
        "created_at": row.created_at.isoformat(),
        "expires_at": (
            None if row.expires_at is None else row.expires_at.isoformat()
        ),
    }


def audit_dict(row: SecretAudit) -> dict:
    """The API-facing dict for an audit row; ``workspaces`` is the
    placeholder's coverage at the event (#339) and ``owner_uid``
    the minting owner it records (#1083)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "owner_uid": row.owner_uid,
        "workspaces": json.loads(row.workspaces),
        "name": row.name,
        "dests": json.loads(row.dests),
        "created_at": row.created_at.isoformat(),
    }


def booted_sizes_of(row: Workspace) -> dict | None:
    """The booted-sizes snapshot as a dict (#898), None when the
    column is empty or unreadable. The dict carries exactly the
    deferred half's three fields, so a comparison against the
    row's own sizes is a field-for-field equality."""
    if not row.booted_sizes:
        return None
    try:
        parsed = json.loads(row.booted_sizes)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {
        "cpus": parsed.get("cpus"),
        "mem_mib": parsed.get("mem_mib"),
        "root_mib": parsed.get("root_mib"),
    }


def workspace_dict(row: Workspace) -> dict:
    """The API-facing dict for a workspace row. The mounts list
    serves the parsed form: a row a pre-#685 daemon wrote keeps
    its stored shape in the column until the next set, but every
    read serves the entries the one grammar admits (#685's drop
    at read) — no surface ever sees a named entry back.
    ``mounts_pending`` (#756) compares the configured set against
    the set the last start attached: True while the two disagree
    (an edit the next start takes, a mount never yet attached),
    False once they agree again. ``sizes_pending`` (#898) reads
    the same shape on sizes: the row's deferred half (root
    growth and the vcpu/memory topology) against the snapshot
    the last start booted — True while they disagree or no start
    has booted one, False once a start takes the row's sizes."""
    configured = parse_mounts(row.mounts)
    desired = {
        "cpus": row.cpus,
        "mem_mib": row.mem_mib,
        "root_mib": row.root_mib,
    }
    return {
        "id": row.id,
        # The owning uid (#1082): the peer credential that created
        # the row. The client's display of it lands with #1084's
        # ``--user`` work; the row carries it from day one.
        "owner_uid": row.owner_uid,
        "name": row.name,
        "kernel": row.kernel,
        "initrd": row.initrd,
        "rootfs": row.rootfs,
        "cmdline": row.cmdline,
        "cpus": row.cpus,
        "mem_mib": row.mem_mib,
        "image_hash": row.image_hash,
        "host": row.host,
        "root_mib": row.root_mib,
        "home_mib": row.home_mib,
        "egress": row.egress,
        "egress_slice": row.egress_slice,
        "egress_mode": row.egress_mode or "allow",
        "secret_coverage": row.secret_coverage or "all",
        "egress_allowlist": json.loads(row.egress_allowlist)
        if row.egress_allowlist
        else [],
        "mounts": [mount.as_dict() for mount in configured],
        "mounts_pending": set(configured)
        != set(parse_mounts(row.attached_mounts)),
        "sizes_pending": desired != booted_sizes_of(row),
        "user_data": row.user_data,
        "ssh_pubkey": row.ssh_pubkey,
        "login_user": row.login_user,
        "timezone": row.timezone,
        "status": row.status,
        # The seed's epoch (#803, #1069): the start path reads the
        # row through this dict, so carrying it is what lets the
        # renewal suffix bake an instance-id the guest has not
        # consumed. Zero until the first boot-time bake; the
        # create-time bake keeps the plain id.
        "seed_epoch": row.seed_epoch,
        "created_at": row.created_at.isoformat(),
    }

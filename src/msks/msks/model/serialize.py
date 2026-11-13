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
from .workspaces import Workspace


async def resolve_workspace(session, ref: str) -> Workspace | None:
    """One workspace row by id-or-name ref (#246), inside a session
    the caller owns: id first (the canonical preference, not a
    tiebreak — live ids and names are one disjoint namespace), then
    the unique name."""
    row = await session.get(Workspace, ref)
    if row is None:
        row = await session.scalar(
            select(Workspace).where(Workspace.name == ref)
        )
    return row


def workspace_fields(
    spec: VmSpec,
    image_hash: str | None,
    host: str | None,
    name: str | None = None,
) -> dict:
    """The ORM column values a VmSpec maps to."""
    return {
        "id": spec.workspace_id,
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
    ``[]`` is the daemon-wide row.
    """
    return {
        "id": row.id,
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
    placeholder's coverage at the event (#339)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "workspaces": json.loads(row.workspaces),
        "name": row.name,
        "dests": json.loads(row.dests),
        "created_at": row.created_at.isoformat(),
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
    False once they agree again."""
    configured = parse_mounts(row.mounts)
    return {
        "id": row.id,
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
        "user_data": row.user_data,
        "ssh_pubkey": row.ssh_pubkey,
        "login_user": row.login_user,
        "timezone": row.timezone,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }

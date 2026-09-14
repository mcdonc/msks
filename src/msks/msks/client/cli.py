"""The ``msks`` CLI: ``ls``, ``create``, ``start``, ``stop``, ``rm``, and
``shell`` subcommands.

Every command speaks the daemon's REST surface with the same client
conventions (#21): ``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for
a bearer token, ``MSKSC_CAFILE`` to pin the certificate. The
interactive shell command lives in :mod:`msks.client.shell`.
"""

import argparse
import asyncio
import json
import sys

from .rest import (
    api_call,
    api_client,
    env_token,
    env_url,
    request,
)
from .shell import run_workspace_shell


def format_workspace(row: dict) -> str:
    """One listing line: id, status, image hash, host."""
    image = (row.get("image_hash") or "-")[:12]
    host = row.get("host") or "-"
    return f"{row['id']:<24} {row['status']:<9} {image:<13} {host}"


def render_ls(rows: list[dict], as_json: bool) -> str:
    """The whole listing: aligned lines, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return "\n".join(format_workspace(row) for row in rows)


def cmd_ls(as_json: bool = False, transport=None) -> int:
    """``msks ls``: every workspace the daemon knows."""
    rows = asyncio.run(
        api_call(
            "GET", env_url(), env_token(), "/api/v1/workspaces", transport=transport
        )
    )
    text = render_ls(rows, as_json)
    if text:
        print(text)
    return 0


def cmd_create(body: dict, start: bool = False, transport=None) -> int:
    """``msks create``: one workspace, optionally booted."""
    asyncio.run(create_workspace(env_url(), env_token(), body, start, transport))
    return 0


async def create_workspace(url, token, body, start, transport) -> dict:
    """POST the workspace, print its id, then boot it when asked.

    The id prints before the boot attempt: a failed start must not
    hide that the workspace exists — recover with ``msks start``.
    """
    async with api_client(url, token, transport) as client:
        row = await request(client, "POST", "/api/v1/workspaces", json_body=body)
        print(f"created {row['id']}")
        if not start:
            return row
        try:
            await request(client, "POST", f"/api/v1/workspaces/{row['id']}/start")
        except SystemExit as exc:
            raise SystemExit(
                f"{exc}\nmsks: {row['id']} is created; "
                f"boot it later with: msks start {row['id']}"
            ) from exc
        print(f"attach with: msks shell {row['id']}")
        row["status"] = "running"
        return row


def cmd_start(workspace_id: str, transport=None) -> int:
    """``msks start``: boot a created workspace."""
    row = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/start",
            transport=transport,
        )
    )
    print(f"{workspace_id} {row['status']}")
    return 0


def cmd_stop(workspace_id: str, transport=None) -> int:
    """``msks stop``: power a workspace off, gracefully."""
    row = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/stop",
            transport=transport,
        )
    )
    print(f"{workspace_id} {row['status']}")
    return 0


def cmd_rm(workspace_ids: list[str], transport=None) -> int:
    """``msks rm``: delete workspaces and their persistent data.

    Ids are removed one at a time, in order; a failure stops the
    run with the API's one-line error (already-removed ids stay
    removed — and stay confirmed on stdout).
    """
    url, token = env_url(), env_token()
    for workspace_id in workspace_ids:
        asyncio.run(
            api_call(
                "DELETE",
                url,
                token,
                f"/api/v1/workspaces/{workspace_id}",
                transport=transport,
            )
        )
        print(f"{workspace_id} deleted")
    return 0


def create_body(args: argparse.Namespace) -> dict:
    """The POST body: only the fields the operator set."""
    fields = {
        "id": args.workspace_id,
        "image": args.image,
        "kernel": args.kernel,
        "initrd": args.initrd,
        "rootfs": args.rootfs,
        "cmdline": args.cmdline,
        "cpus": args.cpus,
        "mem_mib": args.mem_mib,
        "root_mib": args.root_mib,
        "home_mib": args.home_mib,
    }
    body = {name: value for name, value in fields.items() if value is not None}
    if args.egress is not None:
        body["egress"] = args.egress
    return body


def build_parser() -> argparse.ArgumentParser:
    """The ``msks`` command line."""
    parser = argparse.ArgumentParser(
        prog="msks", description="msks client: workspace microvms over the daemon API"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("ls", help="list workspaces on the daemon")
    listing.add_argument("--json", action="store_true", help="one JSON document")
    create = sub.add_parser("create", help="create a workspace")
    create.add_argument("workspace_id", help="the id to create (DNS-label charset)")
    create.add_argument("--image", help="catalog ref: name:version, name, or hash")
    create.add_argument("--kernel", help="explicit kernel path (skips the catalog)")
    create.add_argument("--initrd", help="explicit initrd path")
    create.add_argument("--rootfs", help="explicit rootfs path (skips the catalog)")
    create.add_argument("--cmdline", help="explicit kernel cmdline")
    create.add_argument("--cpus", type=int, help="vcpu count (default 2)")
    create.add_argument("--mem-mib", type=int, help="guest memory, MiB (default 1024)")
    create.add_argument("--root-mib", type=int, help="persistent root size, MiB")
    create.add_argument("--home-mib", type=int, help="persistent home size, MiB")
    create.add_argument(
        "--egress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="boot with a virtio-net NIC onto a per-VM appliance tap "
        "(#52; the default is yes — use --no-egress to boot NIC-less)",
    )
    create.add_argument(
        "--start", action="store_true", help="boot the workspace immediately"
    )
    starter = sub.add_parser("start", help="boot a created workspace")
    starter.add_argument("workspace_id", help="the workspace to boot")
    stopper = sub.add_parser("stop", help="power a workspace off")
    stopper.add_argument("workspace_id", help="the workspace to stop")
    remover = sub.add_parser("rm", help="delete workspaces and their data")
    remover.add_argument(
        "workspace_ids", nargs="+", help="the workspaces to delete, in order"
    )
    shell = sub.add_parser("shell", help="interactive shell in a workspace")
    shell.add_argument("workspace_id", help="the workspace to attach to")
    return parser


def main(argv: list[str] | None = None, transport=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args, transport)
    except KeyboardInterrupt:
        # A Ctrl-C during a long boot: one line, not a traceback (a
        # raw-mode session never gets here — Ctrl-C reaches the guest).
        print("msks: interrupted", file=sys.stderr)
        raise SystemExit(130) from None


def command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per subcommand: its zero-argument body."""
    return {
        "ls": lambda: cmd_ls(args.json, transport=transport),
        "create": lambda: cmd_create(
            create_body(args), args.start, transport=transport
        ),
        "start": lambda: cmd_start(args.workspace_id, transport=transport),
        "stop": lambda: cmd_stop(args.workspace_id, transport=transport),
        "rm": lambda: cmd_rm(args.workspace_ids, transport=transport),
        "shell": lambda: run_workspace_shell(args.workspace_id),
    }


def dispatch(args: argparse.Namespace, transport=None) -> int:
    """Run one parsed command."""
    return command_table(args, transport)[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())

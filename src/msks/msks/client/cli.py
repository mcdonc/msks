"""The ``msks`` CLI: ``list``, ``create``, and ``shell`` subcommands.

Every command speaks the daemon's REST surface with the same client
conventions (#21): ``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for
a bearer token, ``MSKSC_CAFILE`` to pin the certificate. The
interactive shell command lives in :mod:`msks.client.shell`.
"""

import argparse
import asyncio
import json

import httpx

from .shell import env_token, env_url, run_workspace_shell, ssl_context


async def api_call(
    method: str,
    url: str,
    token: str,
    path: str,
    json_body: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
):
    """One authenticated REST call; failures exit with one line.

    ``transport`` is the seam the tests plug an in-process API (or a
    mock) into; the real client dials ``url`` with the #21 TLS story.
    """
    async with httpx.AsyncClient(
        base_url=url,
        transport=transport,
        headers={"Authorization": f"Bearer {token}"},
        verify=None if transport is not None else ssl_context(),
    ) as client:
        try:
            response = await client.request(method, path, json=json_body)
            response.raise_for_status()
        except httpx.TransportError as exc:
            raise SystemExit(f"msks: cannot reach {url}: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise SystemExit(
                f"msks: {exc.response.status_code}: {error_detail(exc.response)}"
            ) from exc
        return response.json()


def error_detail(response: httpx.Response) -> str:
    """The API's ``detail`` field, or the raw body when it is absent."""
    try:
        return response.json()["detail"]
    except ValueError, KeyError, TypeError:
        return response.text.strip() or "no detail"


def format_workspace(row: dict) -> str:
    """One listing line: id, status, image hash, host."""
    image = (row.get("image_hash") or "-")[:12]
    host = row.get("host") or "-"
    return f"{row['id']:<24} {row['status']:<9} {image:<13} {host}"


def render_list(rows: list[dict], as_json: bool) -> str:
    """The whole listing: aligned lines, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return "\n".join(format_workspace(row) for row in rows)


def cmd_list(as_json: bool = False, transport=None) -> int:
    """``msks list``: every workspace the daemon knows."""
    rows = asyncio.run(
        api_call(
            "GET", env_url(), env_token(), "/api/v1/workspaces", transport=transport
        )
    )
    text = render_list(rows, as_json)
    if text:
        print(text)
    return 0


def cmd_create(body: dict, start: bool = False, transport=None) -> int:
    """``msks create``: one workspace, optionally booted."""
    row = asyncio.run(create_workspace(env_url(), env_token(), body, start, transport))
    print(f"created {row['id']}" + (" (running)" if start else ""))
    if start:
        print(f"attach with: msks shell {row['id']}")
    return 0


async def create_workspace(url, token, body, start, transport) -> dict:
    """POST the workspace; with ``start``, boot it before returning."""
    row = await api_call(
        "POST", url, token, "/api/v1/workspaces", json_body=body, transport=transport
    )
    if start:
        await api_call(
            "POST",
            url,
            token,
            f"/api/v1/workspaces/{row['id']}/start",
            transport=transport,
        )
        row["status"] = "running"
    return row


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
    return {name: value for name, value in fields.items() if value is not None}


def build_parser() -> argparse.ArgumentParser:
    """The ``msks`` command line."""
    parser = argparse.ArgumentParser(
        prog="msks", description="msks client: workspace microvms over the daemon API"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="list workspaces on the daemon")
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
        "--start", action="store_true", help="boot the workspace immediately"
    )
    shell = sub.add_parser("shell", help="interactive shell in a workspace")
    shell.add_argument("workspace_id", help="the workspace to attach to")
    return parser


def main(argv: list[str] | None = None, transport=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "shell":
        return run_workspace_shell(args.workspace_id)
    if args.command == "list":
        return cmd_list(args.json, transport=transport)
    return cmd_create(create_body(args), args.start, transport=transport)


if __name__ == "__main__":
    raise SystemExit(main())

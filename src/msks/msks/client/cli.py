"""The ``msks`` CLI: ``ls``, ``create``, ``start``, ``stop``, ``rm``,
``shell``, and the ``image`` catalog subcommands.

Every command speaks the daemon's REST surface with the same client
conventions (#21): ``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for
a bearer token, ``MSKSC_CAFILE`` to pin the certificate. The
interactive shell command lives in :mod:`msks.client.shell`.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ..imagestore import is_hash_shape, version_key
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


# --- The image catalog (#65) ---

HEX_DIGITS = set("0123456789abcdef")

#: How many refs an error line spells out before "… (+N more)".
CATALOG_REF_CAP = 8


def format_image(row: dict) -> str:
    """One catalog line: ref, short hash, default flag, kernel."""
    flag = "default" if row["default"] else "-"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    ref = f"{row['name']}:{row['version']}"
    return f"{ref:<24} {row['hash'][:12]:<13} {flag:<8} {kernel}"


def render_image_ls(rows: list[dict], as_json: bool) -> str:
    """The whole catalog: aligned lines, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return "\n".join(format_image(row) for row in rows)


async def fetch_images(url, token, transport) -> list[dict]:
    """The catalog listing (GET /api/v1/images)."""
    return await api_call("GET", url, token, "/api/v1/images", transport=transport)


async def import_image(url, token, source, transport) -> dict:
    """POST the import; print the registered reference and hash."""
    record = await api_call(
        "POST",
        url,
        token,
        "/api/v1/images",
        json_body={"source": source},
        transport=transport,
    )
    print(f"imported {record['ref']} ({record['hash'][:12]})")
    return record


async def remove_image(url, token, ref, transport) -> dict:
    """Resolve ``ref`` against the listing, DELETE by hash.

    Resolution and delete share one client so a refusal (409 names
    the workspace that boots the image) reads as one exchange.
    """
    async with api_client(url, token, transport) as client:
        rows = await request(client, "GET", "/api/v1/images")
        row = resolve_image_ref(ref, rows)
        result = await request(client, "DELETE", f"/api/v1/images/{row['hash']}")
    print(f"{row['name']}:{row['version']} deleted")
    return result


async def describe_image(url, token, ref, transport) -> dict:
    """Print one image's full record from the listing data."""
    rows = await fetch_images(url, token, transport)
    row = resolve_image_ref(ref, rows)
    print("\n".join(info_lines(row)))
    return row


def info_lines(row: dict) -> list[str]:
    """The full record: boot facts the listing carries."""
    default = "yes" if row["default"] else "no"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    provisioner = row.get("provisioner") or "- (none declared)"
    return [
        f"ref      {row['name']}:{row['version']}",
        f"hash     {row['hash']}",
        f"kernel   {kernel}",
        f"cmdline  {row['cmdline']}",
        f"console  vsock port {row['vsock_shell_port']}",
        f"seed     provisioner {provisioner}",
        f"default  {default}",
    ]


def resolve_image_ref(ref: str, rows: list[dict]) -> dict:
    """The one listing row a catalog reference points at.

    Accepts the daemon's forms — hash, ``name@hash``,
    ``name:version``, bare ``name`` (newest version) — plus a unique
    hash prefix (``ls`` prints 12 chars). A miss or an ambiguous
    prefix exits with the catalog spelled out.
    """
    matches = image_ref_matches(ref, rows)
    if not matches:
        raise SystemExit(no_image_message(ref, rows))
    if len(matches) > 1:
        raise SystemExit(ambiguous_image_message(ref, matches))
    return matches[0]


def image_ref_matches(ref: str, rows: list[dict]) -> list[dict]:
    """Dispatch on the reference's shape, mirroring the daemon's
    precedence (:mod:`msks.imagestore` ``resolve``): @, :, full
    hash, then bare token."""
    if "@" in ref:
        return pin_matches(ref, rows)
    if ":" in ref:
        return name_version_matches(ref, rows)
    if is_hash_shape(ref):
        return hash_prefix_matches(rows, ref)
    return bare_ref_matches(ref, rows)


def bare_ref_matches(ref: str, rows: list[dict]) -> list[dict]:
    """A bare token: a catalog name resolves by name (a hex-looking
    name must not be captured as a hash prefix — the daemon resolves
    it by name); anything else may be a unique hash prefix. All rows
    tied at the newest version return together: two imports of the
    same name:version (a rebuilt archive) surface as the ambiguity
    they are, never a silent arbitrary pick."""
    named = [row for row in rows if row["name"] == ref]
    if named:
        return top_version_rows(named)
    return hash_prefix_matches(rows, ref)


def top_version_rows(candidates: list[dict]) -> list[dict]:
    """The candidates sitting at the newest version — the daemon's
    ordering (``imagestore.version_key``), so client and daemon
    agree on what "newest" means."""
    top = max(version_key(row["version"]) for row in candidates)
    return [row for row in candidates if version_key(row["version"]) == top]


def pin_matches(ref: str, rows: list[dict]) -> list[dict]:
    """``name@hash``: identity and content both pinned."""
    name, _, digest = ref.partition("@")
    require_hash_digest(ref, digest)
    return [
        row for row in rows if row["name"] == name and row["hash"].startswith(digest)
    ]


def require_hash_digest(ref: str, digest: str) -> None:
    """A pin's hash part is a full 64-hex digest (the daemon's
    ``is_hash_shape``); anything else is a named error."""
    if not is_hash_shape(digest):
        raise SystemExit(f"msks: malformed image hash in {ref!r}")


def name_version_matches(ref: str, rows: list[dict]) -> list[dict]:
    """``name:version``: the exact pair."""
    name, _, version = ref.partition(":")
    return [row for row in rows if row["name"] == name and row["version"] == version]


def hash_prefix_matches(rows: list[dict], prefix: str) -> list[dict]:
    """Hashes the prefix selects; empty and non-hex select nothing
    (an empty reference must not match every row)."""
    if not prefix or not set(prefix) <= HEX_DIGITS:
        return []
    return [row for row in rows if row["hash"].startswith(prefix)]


def catalog_refs(rows: list[dict]) -> str:
    """The catalog (or a match set) spelled out for an error, capped
    so a large catalog stays one readable line."""
    refs = [f"{row['name']}:{row['version']}" for row in rows]
    shown = ", ".join(refs[:CATALOG_REF_CAP])
    if len(refs) <= CATALOG_REF_CAP:
        return shown or "(the catalog is empty)"
    return f"{shown}, … (+{len(refs) - CATALOG_REF_CAP} more)"


def no_image_message(ref: str, rows: list[dict]) -> str:
    return f"msks: no image matches {ref!r} — catalog: {catalog_refs(rows)}"


def ambiguous_image_message(ref: str, matches: list[dict]) -> str:
    # name:version is deliberately absent from the advice: the usual
    # ambiguity is two imports of the same name:version (a rebuilt
    # archive), where only the hash forms still identify one image;
    # the matches therefore carry their short hashes.
    return (
        f"msks: {ref!r} matches {len(matches)} images "
        f"({match_refs(matches)}); use the full hash or name@hash"
    )


def match_refs(matches: list[dict]) -> str:
    """The matched images with short hashes — when the refs read the
    same (re-imported archive), the hashes are the discriminator."""
    return ", ".join(
        f"{row['name']}:{row['version']} ({row['hash'][:12]})" for row in matches
    )


def cmd_image_ls(as_json: bool = False, transport=None) -> int:
    """``msks image ls``: the whole catalog, default marked."""
    rows = asyncio.run(fetch_images(env_url(), env_token(), transport))
    text = render_image_ls(rows, as_json)
    if text:
        print(text)
    return 0


def cmd_image_import(source: str, transport=None) -> int:
    """``msks image import``: register a daemon-side archive."""
    asyncio.run(import_image(env_url(), env_token(), source, transport))
    return 0


def cmd_image_rm(ref: str, transport=None) -> int:
    """``msks image rm``: drop one image, by any reference form."""
    asyncio.run(remove_image(env_url(), env_token(), ref, transport))
    return 0


def cmd_image_info(ref: str, transport=None) -> int:
    """``msks image info``: one image's full record."""
    asyncio.run(describe_image(env_url(), env_token(), ref, transport))
    return 0


def read_user_data(path: str) -> str:
    """The #41 payload: a file's contents, or stdin for ``-``."""
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"msks: cannot read user-data file {path}: {exc}") from None


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
    if args.user_data is not None:
        body["user_data"] = read_user_data(args.user_data)
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
        "--user-data",
        metavar="FILE",
        help="first-boot provisioning payload (a shell script or "
        "cloud-config) delivered on the workspace's cidata seed disk "
        "(#41); - reads stdin. Create-time only",
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
    image = sub.add_parser("image", help="manage the daemon's image catalog (#65)")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_ls = image_sub.add_parser("ls", help="list catalog images")
    image_ls.add_argument("--json", action="store_true", help="one JSON document")
    image_import = image_sub.add_parser(
        "import",
        help="register an image archive from a daemon-side path",
    )
    image_import.add_argument(
        "source",
        help="archive path as the daemon sees it (its own filesystem; "
        "the file is read by the daemon, not uploaded by this command)",
    )
    image_rm = image_sub.add_parser("rm", help="remove an image from the catalog")
    image_rm.add_argument(
        "ref",
        help="name:version, bare name (newest), name@hash (full hash), "
        "or hash (a unique hash prefix works too)",
    )
    image_info = image_sub.add_parser("info", help="show one image's full record")
    image_info.add_argument(
        "ref",
        help="name:version, bare name, name@hash, or hash "
        "(a unique hash prefix works too)",
    )
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
        "image": lambda: image_command_table(args, transport)[args.image_command](),
    }


def image_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``image`` subcommand."""
    return {
        "ls": lambda: cmd_image_ls(args.json, transport=transport),
        "import": lambda: cmd_image_import(args.source, transport=transport),
        "rm": lambda: cmd_image_rm(args.ref, transport=transport),
        "info": lambda: cmd_image_info(args.ref, transport=transport),
    }


def dispatch(args: argparse.Namespace, transport=None) -> int:
    """Run one parsed command."""
    return command_table(args, transport)[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())

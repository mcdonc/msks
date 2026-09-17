"""The ``msks`` CLI: ``ls``, ``create``, ``start``, ``stop``, ``rm``,
``console``, ``forward``, ``ssh``, ``key``, the ``image`` catalog
subcommands, and the ``home`` volume moves.

Every command speaks the daemon's REST surface with the same client
conventions (#21): ``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for
a bearer token, ``MSKSC_CAFILE`` to pin the certificate. The
interactive console command lives in :mod:`msks.client.console`.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from ..identity import KEY_TYPES, mint
from ..imagestore import is_hash_shape, version_key
from .console import run_workspace_shell
from .forward import run_workspace_forward
from .rest import (
    STREAM_WINDOW_B,
    api_call,
    api_client,
    download,
    env_token,
    env_url,
    request,
    upload,
)
from .rest import (
    fetch_ssh_key as rest_fetch_ssh_key,
)
from .ssh import data_dir, run_workspace_ssh


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


def cmd_create(
    body: dict,
    start: bool = False,
    transport=None,
    key_type: str | None = None,
    pubkey: str | None = None,
) -> int:
    """``msks create``: one workspace, optionally booted.

    ``key_type`` names the client-mint mode (#121): minted locally,
    public half sent, private half kept. ``pubkey`` is an
    operator-supplied public line (#132): sent as-is, no private
    half anywhere msks manages.
    """
    asyncio.run(
        create_workspace(
            env_url(), env_token(), body, start, transport, key_type, pubkey
        )
    )
    return 0


async def create_workspace(
    url,
    token,
    body,
    start,
    transport,
    key_type: str | None = None,
    pubkey: str | None = None,
) -> dict:
    """POST the workspace, print its id, then boot it when asked.

    The id prints before the boot attempt: a failed start must not
    hide that the workspace exists — recover with ``msks start``.
    In the client-mint mode (#121) the keypair is minted here — the
    private half never crosses the wire — and is persisted (mode
    0600, client data root) only after the create succeeded, so a
    refused create leaves no orphaned key behind. With an
    operator-supplied key (#132) only the public line travels and
    nothing is written client-side: the private half stays wherever
    the operator keeps it.
    """
    private_pem, public = await identity_material(body, key_type, pubkey)
    async with api_client(url, token, transport) as client:
        row = await request(client, "POST", "/api/v1/workspaces", json_body=body)
        print(f"created {row['id']}")
        if public is not None:
            await verify_no_escrow(client, row["id"], public)
        if private_pem is not None:
            path = write_client_identity(row["id"], private_pem)
            print(f"client identity (mode 0600): {path}")
        if not start:
            return row
        try:
            await request(client, "POST", f"/api/v1/workspaces/{row['id']}/start")
        except SystemExit as exc:
            raise SystemExit(
                f"{exc}\nmsks: {row['id']} is created; "
                f"boot it later with: msks start {row['id']}"
            ) from exc
        print(f"attach with: msks console {row['id']}")
        row["status"] = "running"
        return row


async def identity_material(
    body: dict, key_type: str | None, pubkey: str | None
) -> tuple[str | None, str | None]:
    """Prepare the create's identity: ``(private_pem, public_line)``.

    The mint produces both halves; an operator-supplied line is the
    public half alone. Either way the public line rides the body as
    ``ssh_pubkey``; a daemon-mint create supplies neither and keeps
    the body free of key material.
    """
    if key_type is not None:
        private_pem, public = await asyncio.to_thread(mint, key_type)
    elif pubkey is None:
        return None, None
    else:
        private_pem, public = None, pubkey
    body["ssh_pubkey"] = public
    return private_pem, public


async def verify_no_escrow(client, workspace_id: str, public: str) -> None:
    """Confirm the daemon kept the no-escrow promise (#121).

    A daemon one version behind this client drops the unknown
    ``ssh_pubkey`` field (pydantic ignores extras) and silently mints
    its own pair — the create reports success while the daemon
    escrows a private half the operator was told does not exist.
    The key fetch answers for it: the served public line must carry
    the supplied key material and the private half must be null.
    """
    key = await request(client, "GET", f"/api/v1/workspaces/{workspace_id}/ssh-key")
    served = key.get("public_key", "").split()[:2]
    if key.get("private_key") is not None or served != public.split()[:2]:
        raise SystemExit(
            f"msks: {workspace_id} was created, but the daemon did not "
            "keep the no-escrow promise: it holds its own minted "
            "identity for the workspace (a daemon older than this "
            "client's supplied-key support). The daemon's version of "
            "msks must be updated before creating without "
            "--daemon-mint; remove the escrowed workspace with: "
            f"msks rm {workspace_id}"
        )


def write_client_identity(workspace_id: str, private_pem: str) -> Path:
    """The client-minted private half, persisted mode 0600 (#121).

    The file is created 0600 from the first byte (open-write-chmod
    would leave a umask-window where the workspace's only private
    half is group-readable), the mode forced again on a pre-existing
    file, under the data root (not the cache: this half must survive
    cache sweeps). No escrow cuts both ways: a failed write is loud —
    the workspace exists with the public half planted, and the
    private half exists nowhere on disk.
    """
    root = data_dir() / workspace_id
    path = root / "identity"
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_pem)
    except OSError as exc:
        raise SystemExit(
            f"msks: {workspace_id} was created, but its client-minted "
            f"identity could not be written to {path}: {exc}\n"
            "The private half now exists nowhere on disk — ssh cannot "
            "use this workspace's identity. Use the console, or delete "
            "and recreate the workspace"
        ) from exc
    return path


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


async def fetch_ssh_key(url, token, workspace_id, transport) -> dict:
    """GET the workspace's identity: type, public half, private half
    (null for a client-minted workspace, #121).

    A re-export of :func:`msks.client.rest.fetch_ssh_key` (the call
    moved to rest.py when ``msks ssh`` (#112) began sharing it);
    ``msks key`` prints it, ``msks ssh`` stages the private half in
    memory for the duration of a connection.
    """
    return await rest_fetch_ssh_key(url, token, workspace_id, transport)


def write_private_key(key: dict, out: str) -> None:
    """Materialize the private half at ``out``, mode 0600 (#111).

    The mode is forced on every write, an existing file included —
    open()'s mode argument only applies at creation, and a
    pre-existing group- or world-readable file must not stay that
    way under new contents. The path is always the operator's own
    choice: the command writes the key to the file the operator
    named and to stdout, and nowhere else.
    """
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as exc:
        raise SystemExit(f"msks: cannot write key file {out}: {exc}") from None
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key["private_key"])


def require_daemon_half(key: dict, workspace_id: str) -> None:
    """Refuse the private forms for a workspace whose private half
    the daemon never held (#121, #132): the error names the two
    places that half can be instead of printing nothing."""
    if key["private_key"] is None:
        raise SystemExit(
            f"msks key: {workspace_id} carries a client-minted identity — "
            "the daemon never held its private half. It lives on the "
            "client that minted it (under the client data root, "
            f"{data_dir() / workspace_id / 'identity'}), or it is a key "
            "you supplied at create — use that key directly"
        )


def cmd_key(
    workspace_id: str, as_private: bool = False, out: str | None = None, transport=None
) -> int:
    """``msks key``: the workspace's ssh identity.

    Prints the public half (safe to display anywhere); ``--private``
    prints the private half, ``--out`` writes the private half to a
    file with mode 0600 and prints nothing but its path. A
    client-minted workspace (#121) serves its public half; its
    private half never reached the daemon, so the private forms
    explain where that half lives instead.
    """
    key = asyncio.run(fetch_ssh_key(env_url(), env_token(), workspace_id, transport))
    if as_private or out is not None:
        require_daemon_half(key, workspace_id)
    if out is not None:
        write_private_key(key, out)
        print(out)
    elif as_private:
        print(key["private_key"], end="")
    else:
        print(key["public_key"])
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


# --- Home-volume export/import (#80) ---


def home_path(workspace_id: str) -> str:
    """The workspace's home-volume byte-stream endpoint."""
    return f"/api/v1/workspaces/{workspace_id}/home"


async def run_home_export(
    url: str, token: str, workspace_id: str, out: str, transport
) -> int:
    """GET the volume stream and write it to ``out``; the bytes."""
    async with api_client(url, token, transport) as client:
        if out == "-":
            return await download(client, home_path(workspace_id), sys.stdout.buffer)
        try:
            with open(out, "wb") as sink:
                return await download(client, home_path(workspace_id), sink)
        except OSError as exc:
            raise SystemExit(f"msks: cannot write {out}: {exc}") from None


def volume_source(path: str):
    """The opened upload body: stdin for ``-``, else the file.

    Opening happens here, not inside the stream: an unreadable
    source fails with one line before any network activity.
    """
    if path == "-":
        return sys.stdin.buffer
    try:
        return open(path, "rb")
    except OSError as exc:
        raise SystemExit(f"msks: cannot read volume image {path}: {exc}") from None


async def file_windows(source) -> AsyncIterator[bytes]:
    """Yield an open binary source's bytes in stream windows.

    Reads run off the event loop; stdin keeps its shell-owned
    lifecycle (only the file the command opened is closed).
    """
    try:
        while window := await asyncio.to_thread(source.read, STREAM_WINDOW_B):
            yield window
    finally:
        if source is not sys.stdin.buffer:
            source.close()


async def run_home_import(
    url: str, token: str, workspace_id: str, source_path: str, transport
) -> int:
    """PUT the volume stream; the daemon's reported byte count.

    The source opens here and closes in the ``finally`` even when
    the exchange dies before the body is consumed (a dead dial):
    the file never waits for garbage collection.
    """
    source = volume_source(source_path)
    owns = source is not sys.stdin.buffer
    try:
        async with api_client(url, token, transport) as client:
            reply = await upload(client, home_path(workspace_id), file_windows(source))
        return imported_bytes(reply)
    finally:
        if owns:
            source.close()


def imported_bytes(reply: dict) -> int:
    """The reply's byte count, or the one-line refusal when a 2xx
    answer carries none (a daemon that is not this protocol)."""
    count = reply.get("bytes")
    if not isinstance(count, int):
        raise SystemExit("msks: the daemon's import reply carried no byte count")
    return count


def cmd_home_export(workspace_id: str, out: str | None = None, transport=None) -> int:
    """``msks home export``: download a workspace's /home volume."""
    target = out if out is not None else f"{workspace_id}.ext4"
    try:
        total = asyncio.run(
            run_home_export(env_url(), env_token(), workspace_id, target, transport)
        )
    except BrokenPipeError:
        raise SystemExit(broken_pipe_line(target)) from None
    if target == "-":
        # Bytes own stdout; the confirmation goes to stderr.
        print(f"msks: exported {workspace_id} ({total} bytes)", file=sys.stderr)
    else:
        print(f"exported {workspace_id} ({total} bytes) to {target}")
    return 0


def broken_pipe_line(target: str) -> str:
    """The one-line report for a reader that went away (#80).

    ``- | head`` or a downstream compressor on a full disk closes
    the pipe mid-stream; stdout is unusable from here on, so its
    buffered remains are pointed at devnull before the interpreter
    flush would traceback on them again at exit.
    """
    with contextlib.suppress(OSError, ValueError, AttributeError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    return f"msks: the export's reader closed early ({target})"


def cmd_home_import(workspace_id: str, source_path: str, transport=None) -> int:
    """``msks home import``: replace a workspace's /home volume."""
    url, token = env_url(), env_token()
    total = asyncio.run(
        run_home_import(url, token, workspace_id, source_path, transport)
    )
    print(f"imported {total} bytes into {workspace_id}")
    return 0


def read_user_data(path: str) -> str:
    """The #41 payload: a file's contents, or stdin for ``-``."""
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
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
        "--daemon-mint",
        action="store_true",
        help="let the daemon mint the workspace's ssh identity and "
        "escrow both halves (#111) instead of the client mint — the "
        "create default (#121) mints on this client, sends the public "
        "half only, and keeps the private half (mode 0600 under the "
        "client data root, ~/.local/share/msks/<id>/identity, where "
        "msks ssh finds it). The k8s backend serves no identity and "
        "needs this flag",
    )
    create.add_argument(
        "--pubkey",
        metavar="FILE",
        help="use a public key you already own as the workspace's ssh "
        "identity (#132): the file's one line travels to the daemon, "
        "any well-formed key type, and the private half stays wherever "
        "you keep it (nothing is written client-side). - reads stdin",
    )
    create.add_argument(
        "--key-type",
        choices=sorted(KEY_TYPES),
        help="the client mint's key type (default ecdsa, the same "
        "FIPS-approvable default the daemon mints)",
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
    console = sub.add_parser("console", help="interactive shell in a workspace")
    console.add_argument("workspace_id", help="the workspace to attach to")
    console.add_argument(
        "--user",
        default="root",
        help="shell user: root or the image's workspace user (default: root)",
    )
    forward = sub.add_parser(
        "forward", help="bridge a workspace TCP port to stdio or a local port"
    )
    forward.add_argument("workspace_id", help="the workspace to reach")
    forward.add_argument("port", type=int, help="the guest TCP port to reach")
    forward.add_argument(
        "--local",
        type=int,
        metavar="PORT",
        help="bind 127.0.0.1:PORT instead of stdio; every accepted "
        "connection gets its own forward",
    )
    key = sub.add_parser(
        "key",
        help="fetch a workspace's ssh identity (#111; the public half "
        "alone for a client-minted #121 workspace)",
    )
    key.add_argument("workspace_id", help="the workspace whose identity to fetch")
    key_private = key.add_mutually_exclusive_group()
    key_private.add_argument(
        "--private",
        action="store_true",
        help="print the private half instead of the public line",
    )
    key_private.add_argument(
        "--out",
        metavar="FILE",
        help="write the private half to FILE (mode 0600) instead of printing",
    )
    ssh = sub.add_parser(
        "ssh", help="ssh into a workspace over the forward, identity staged in memory"
    )
    ssh.add_argument("workspace_id", help="the workspace to log into")
    ssh.add_argument(
        "passthrough",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="arguments passed to ssh verbatim ('-l root' is the recovery "
        "login; '-A' forwards the session agent)",
    )
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
    home = sub.add_parser(
        "home", help="move a workspace's /home volume through the daemon (#80)"
    )
    home_sub = home.add_subparsers(dest="home_command", required=True)
    home_export = home_sub.add_parser(
        "export", help="download a workspace's /home volume"
    )
    home_export.add_argument(
        "workspace_id", help="the workspace whose volume to download"
    )
    home_export.add_argument(
        "file",
        nargs="?",
        default=None,
        help="output file (default: <workspace_id>.ext4); - writes stdout",
    )
    home_import = home_sub.add_parser(
        "import", help="replace a workspace's /home volume from an ext4 image"
    )
    home_import.add_argument(
        "workspace_id", help="the workspace whose volume to replace"
    )
    home_import.add_argument(
        "file", help="the ext4 volume image to upload; - reads stdin"
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


def create_identity(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """The create's identity mode: ``(mint key type, supplied line)``.

    The client mint is the default (#121): absent flags mint locally
    (ecdsa, the same FIPS-approvable default the daemon mints).
    ``--daemon-mint`` hands the identity to the daemon (escrow on the
    local backend; the k8s backend serves no identity either way).
    ``--pubkey`` supplies an operator key (#132) — any well-formed
    type, no mint, nothing written client-side. The three modes are
    exclusive (:func:`check_identity_conflicts` names the pairings).
    """
    check_identity_conflicts(args)
    if args.daemon_mint:
        return None, None
    if args.pubkey is not None:
        return None, read_pubkey(args.pubkey)
    return args.key_type or "ecdsa", None


#: The identity-mode flag conflicts, as message → attribute names:
#: each pairing would look meaningful but is not, so it is rejected
#: with the conflict named.
IDENTITY_CONFLICTS = (
    ("--key-type conflicts with --daemon-mint", ("key_type", "daemon_mint")),
    ("--pubkey conflicts with --daemon-mint", ("pubkey", "daemon_mint")),
    (
        "--key-type needs the client mint (--pubkey carries its own key)",
        ("key_type", "pubkey"),
    ),
)


def check_identity_conflicts(args: argparse.Namespace) -> None:
    """Reject the flag pairings that would look meaningful but are
    not, with the conflict named."""
    for message, flags in IDENTITY_CONFLICTS:
        present = [bool(getattr(args, flag, None)) for flag in flags]
        if all(present):
            raise SystemExit(f"msks: {message}")


def read_pubkey(path: str) -> str:
    """One public key line from a file (or stdin with ``-``), checked
    lightly here so a typo fails before any network roundtrip — the
    daemon's shape validation is the authority."""
    return checked_pubkey_line(pubkey_text(path))


def pubkey_text(path: str) -> str:
    """The file's (or stdin's) raw text, with a one-line read error."""
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"msks: cannot read public key file {path}: {exc}") from exc


def checked_pubkey_line(text: str) -> str:
    """Exactly one public key line, stripped — a .pub file's shape."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SystemExit(
            f"msks: the public key file must carry exactly one line "
            f"(found {len(lines)})"
        )
    line = lines[0].strip()
    if len(line.split()) < 2:
        raise SystemExit("msks: that line does not look like a public key")
    return line


def run_create(args: argparse.Namespace, transport) -> int:
    """Resolve the identity mode once — the resolver may read stdin
    (``--pubkey -``) or reject a flag pairing, so it runs a single
    time — then create."""
    key_type, pubkey = create_identity(args)
    return cmd_create(
        create_body(args),
        args.start,
        transport=transport,
        key_type=key_type,
        pubkey=pubkey,
    )


def command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per subcommand: its zero-argument body."""
    return {
        "ls": lambda: cmd_ls(args.json, transport=transport),
        "create": lambda: run_create(args, transport),
        "start": lambda: cmd_start(args.workspace_id, transport=transport),
        "stop": lambda: cmd_stop(args.workspace_id, transport=transport),
        "rm": lambda: cmd_rm(args.workspace_ids, transport=transport),
        "console": lambda: run_workspace_shell(args.workspace_id, args.user),
        "forward": lambda: run_workspace_forward(
            args.workspace_id, args.port, args.local
        ),
        "key": lambda: cmd_key(
            args.workspace_id, args.private, args.out, transport=transport
        ),
        "ssh": lambda: run_workspace_ssh(
            args.workspace_id, args.passthrough, transport=transport
        ),
        "image": lambda: image_command_table(args, transport)[args.image_command](),
        "home": lambda: home_command_table(args, transport)[args.home_command](),
    }


def image_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``image`` subcommand."""
    return {
        "ls": lambda: cmd_image_ls(args.json, transport=transport),
        "import": lambda: cmd_image_import(args.source, transport=transport),
        "rm": lambda: cmd_image_rm(args.ref, transport=transport),
        "info": lambda: cmd_image_info(args.ref, transport=transport),
    }


def home_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``home`` subcommand."""
    return {
        "export": lambda: cmd_home_export(
            args.workspace_id, args.file, transport=transport
        ),
        "import": lambda: cmd_home_import(
            args.workspace_id, args.file, transport=transport
        ),
    }


def dispatch(args: argparse.Namespace, transport=None) -> int:
    """Run one parsed command."""
    return command_table(args, transport)[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())

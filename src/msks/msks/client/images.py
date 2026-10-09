"""The image catalog commands (#65): ``msks image ls``, ``import``,
``rename``, ``check``, ``rm``, ``info``, and ``default`` — the
group the CLI's typer layer (:mod:`msks.client.cli`) dispatches
into. Reference resolution mirrors the daemon's precedence
(:mod:`msks.imagestore` ``resolve``), duplicated here per the
client-isolation rule; the local ``image check`` runs the
standalone conformance entry as a child process so the daemon
composition never loads in the client (#397).
"""

import asyncio
import contextlib
import json
import signal
import subprocess
import sys
from datetime import datetime

from ..spec.images import is_hash_shape, newest_rank
from .context import env_token, env_url
from .rest import api_call, api_client, request
from .tabular import listing_text

HEX_DIGITS = set("0123456789abcdef")

#: How many refs an error line spells out before "… (+N more)".
CATALOG_REF_CAP = 8


def origin_cell(row: dict) -> str:
    """The archive's own pair (#340): shown beside a differing
    registered pair; a dash when the row was never renamed (or a
    daemon predating the origin fields answers)."""
    name = row.get("origin_name")
    version = row.get("origin_version")
    if not name or not version:
        return "-"
    if (name, version) == (row["name"], row["version"]):
        return "-"
    return f"{name}:{version}"


def imported_cell(image: dict) -> str:
    """One import-time cell: local time to the minute, or ``-``
    when the daemon predates stamps (#186) or sends a moment the
    clock cannot parse or place."""
    stamp = image.get("imported")
    if not stamp:
        return "-"
    # OverflowError rides extreme stamps: a year-1 moment has no
    # representation in some local zones. TypeError covers a
    # daemon sending a non-string.
    with contextlib.suppress(ValueError, TypeError, OverflowError):
        return (
            datetime.fromisoformat(stamp)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    return "-"


def image_cells(row: dict, with_origin: bool = False) -> list[str]:
    """One catalog row's cells: ref, origin (#340) when the
    listing carries a renamed row, short hash, default flag,
    kernel, and the import time (#283) — the same stamped-or-dash
    cell the storage table renders."""
    flag = "default" if row["default"] else "-"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    ref = f"{row['name']}:{row['version']}"
    cells = [ref]
    if with_origin:
        cells.append(origin_cell(row))
    cells.extend([row["hash"][:12], flag, kernel, imported_cell(row)])
    return cells


def render_image_ls(rows: list[dict], as_json: bool) -> str:
    """The whole catalog: the aligned table, or one JSON document.

    The origin column (#340) appears when at least one row's
    registered pair differs from its manifest pair."""
    if as_json:
        return json.dumps(rows, indent=2)
    with_origin = any(origin_cell(row) != "-" for row in rows)
    headers = ["ref", "hash", "default", "kernel", "imported"]
    if with_origin:
        headers.insert(1, "origin")
    return listing_text(
        headers, [image_cells(row, with_origin) for row in rows]
    )


async def fetch_images(url, token, transport) -> list[dict]:
    """The catalog listing (GET /api/v1/images)."""
    return await api_call(
        "GET", url, token, "/api/v1/images", transport=transport
    )


async def import_image(
    url, token, source, transport, name=None, version=None
) -> dict:
    """POST the import; print the registered reference and hash.

    A ``name``/``version`` override (#340) rides the same request
    — only the given keys are sent."""
    body = {"source": source}
    for key, value in (("name", name), ("version", version)):
        if value is not None:
            body[key] = value
    record = await api_call(
        "POST",
        url,
        token,
        "/api/v1/images",
        json_body=body,
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
        result = await request(
            client, "DELETE", f"/api/v1/images/{row['hash']}"
        )
    print(f"{row['name']}:{row['version']} deleted")
    return result


async def rename_image(url, token, ref, name, version, transport) -> dict:
    """Resolve ``ref`` against the listing, PATCH the rename
    (#340) — the rm shape: resolution and rename share one client,
    and every reference form reads as one exchange."""
    async with api_client(url, token, transport) as client:
        rows = await request(client, "GET", "/api/v1/images")
        row = resolve_image_ref(ref, rows)
        body = {
            key: value
            for key, value in (("name", name), ("version", version))
            if value is not None
        }
        result = await request(
            client,
            "PATCH",
            f"/api/v1/images/{row['hash']}",
            json_body=body,
        )
    print(
        f"renamed {row['name']}:{row['version']} to {result['ref']} "
        f"({row['hash'][:12]})"
    )
    return result


async def designate_image(url, token, ref, transport) -> dict:
    """Resolve ``ref`` against the listing, POST the designation
    (#270) — the rm shape: resolution and designation share one
    client, and every reference form (a unique hash prefix, an
    ambiguous one named) reads as one exchange.
    """
    async with api_client(url, token, transport) as client:
        rows = await request(client, "GET", "/api/v1/images")
        row = resolve_image_ref(ref, rows)
        result = await request(
            client,
            "POST",
            "/api/v1/images/default",
            json_body={"ref": row["hash"]},
        )
    print(
        f"designated {row['name']}:{row['version']} "
        f"({row['hash'][:12]}) as the default image"
    )
    return result


async def unset_default_image(url, token, transport) -> dict:
    """DELETE the designation; report the fallback that applies
    (#270) — the sole catalog entry a bare create still boots, or
    the named refusal when nothing answers for one.
    """
    result = await api_call(
        "DELETE",
        url,
        token,
        "/api/v1/images/default",
        transport=transport,
    )
    print(unset_message(result))
    return result


def unset_message(result: dict) -> str:
    """The result line: the designation is gone, and the line names
    what a bare create boots now — the sole entry the fallback
    rule picks, or the need for an explicit --image."""
    fallback = result.get("fallback")
    if fallback is None:
        return (
            "default designation removed; a bare create needs --image "
            "until an image is designated"
        )
    return (
        f"default designation removed; a bare create falls back to "
        f"the sole entry {fallback['ref']} ({fallback['hash'][:12]})"
    )


async def describe_image(url, token, ref, transport) -> dict:
    """Print one image's full record from the listing data."""
    rows = await fetch_images(url, token, transport)
    row = resolve_image_ref(ref, rows)
    print("\n".join(info_lines(row)))
    return row


def info_pairs(row: dict) -> list[list[str]]:
    """The record's label/value pairs: boot facts the listing
    carries."""
    default = "yes" if row["default"] else "no"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    provisioner = row.get("provisioner") or "- (none declared)"
    return [
        ("ref", f"{row['name']}:{row['version']}"),
        ("origin", origin_ref_of(row)),
        ("hash", row["hash"]),
        ("kernel", kernel),
        ("cmdline", row["cmdline"]),
        ("seed", f"provisioner {provisioner}"),
        ("default", default),
    ]


def origin_ref_of(row: dict) -> str:
    """The archive's own pair (#340): both pairs always carry, so
    a renamed row's provenance reads from the record alone. A
    daemon predating the fields answers the registered pair."""
    name = row.get("origin_name") or row["name"]
    version = row.get("origin_version") or row["version"]
    return f"{name}:{version}"


def info_lines(row: dict) -> list[str]:
    """The full record, the label column aligned the same way every
    listing aligns (#271)."""
    return listing_text(None, info_pairs(row)).splitlines()


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
    ordering (``spec.images.newest_rank`` over the row's import
    stamp), so client and daemon agree on what "newest" means.
    Rows sharing the winning version string return together: two
    imports under one reference (a rebuilt archive) surface as the
    ambiguity they are, never a silent arbitrary pick."""
    best = max(candidates, key=row_rank)
    return [row for row in candidates if row["version"] == best["version"]]


def row_rank(row: dict) -> tuple:
    """A listing row's bare-name ordering (#448): the shared newest
    rank over the row's imported stamp."""
    return newest_rank(row["version"], row.get("imported"), row["hash"])


def pin_matches(ref: str, rows: list[dict]) -> list[dict]:
    """``name@hash``: identity and content both pinned."""
    name, _, digest = ref.partition("@")
    require_hash_digest(ref, digest)
    return [
        row
        for row in rows
        if row["name"] == name and row["hash"].startswith(digest)
    ]


def require_hash_digest(ref: str, digest: str) -> None:
    """A pin's hash part is a full 64-hex digest (the daemon's
    ``is_hash_shape``); anything else is a named error."""
    if not is_hash_shape(digest):
        raise SystemExit(f"msks: malformed image hash in {ref!r}")


def name_version_matches(ref: str, rows: list[dict]) -> list[dict]:
    """``name:version``: the exact pair."""
    name, _, version = ref.partition(":")
    return [
        row
        for row in rows
        if row["name"] == name and row["version"] == version
    ]


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
        f"{row['name']}:{row['version']} ({row['hash'][:12]})"
        for row in matches
    )


def cmd_image_ls(as_json: bool = False, transport=None) -> int:
    """``msks image ls``: the whole catalog, default marked."""
    rows = asyncio.run(fetch_images(env_url(), env_token(), transport))
    text = render_image_ls(rows, as_json)
    if text:
        print(text)
    return 0


def cmd_image_import(
    source: str,
    name: str | None = None,
    version: str | None = None,
    transport=None,
) -> int:
    """``msks image import``: register a daemon-side archive or
    fetch one from an https:// URL (#258), under an optional
    name/version override (#340)."""
    asyncio.run(
        import_image(env_url(), env_token(), source, transport, name, version)
    )
    return 0


def check_argv(args) -> list[str]:
    """The standalone entry's argv for one parsed check surface."""
    argv = [sys.executable, "-m", "msks.conformance", args.archive]
    if args.egress:
        argv.append("--egress")
    if args.uplink is not None:
        argv += ["--uplink", args.uplink]
    argv += ["--boot-timeout-s", str(args.boot_timeout_s)]
    argv += ["--shutdown-timeout-s", str(args.shutdown_timeout_s)]
    if args.keep:
        argv.append("--keep")
    return argv


def cmd_image_check(args) -> int:
    """``msks image check``: the local conformance pass (#258), run
    as its own process.

    The engine composes the daemon's app (msks.app); the client
    runs it as ``python -m msks.conformance`` and passes the exit
    code and output through, so the daemon composition never loads
    in the client process (#397). ``args`` is the parsed check
    surface (:class:`msks.conformance_args.CheckOptions`) — the
    typer layer constructs it, so this module needs no edge at
    the shared surface beyond the check's own help constants.

    A Ctrl-C reaches the child directly (it shares the terminal's
    foreground process group); the parent holds its own copy in a
    note-only handler while the child's graceful teardown runs —
    the pass restores the host's ip_forward and removes its state
    dir on interrupt — and then surfaces the interrupt to the
    CLI's one-line handler (exit 130)."""
    interrupted = False

    def note_interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous = signal.signal(signal.SIGINT, note_interrupt)
    try:
        proc = subprocess.Popen(check_argv(args))
        code = proc.wait()
    finally:
        signal.signal(signal.SIGINT, previous)
    if interrupted:
        raise KeyboardInterrupt
    return code


def cmd_image_rm(ref: str, transport=None) -> int:
    """``msks image rm``: drop one image, by any reference form."""
    asyncio.run(remove_image(env_url(), env_token(), ref, transport))
    return 0


def cmd_image_info(ref: str, transport=None) -> int:
    """``msks image info``: one image's full record."""
    asyncio.run(describe_image(env_url(), env_token(), ref, transport))
    return 0


def checked_rename_args(name: str | None, version: str | None) -> None:
    """Reject the argument shape that says nothing (#340): a
    rename with neither key."""
    if name is None and version is None:
        raise SystemExit("msks: pass --name and/or --version")


def cmd_image_rename(
    ref: str,
    name: str | None = None,
    version: str | None = None,
    transport=None,
) -> int:
    """``msks image rename``: change a cataloged image's registered
    name/version (#340); the bytes, the hash, and the manifest
    origin stay."""
    checked_rename_args(name, version)
    asyncio.run(
        rename_image(env_url(), env_token(), ref, name, version, transport)
    )
    return 0


def checked_default_args(ref: str | None, unset: bool) -> None:
    """Reject the argument shapes that say nothing — both a
    reference and --unset, or neither (#270)."""
    if unset and ref is not None:
        raise SystemExit("msks: pass a reference or --unset, not both")
    if not unset and ref is None:
        raise SystemExit(
            "msks: pass an image reference, or --unset to clear "
            "the designation"
        )


def cmd_image_default(
    ref: str | None, unset: bool = False, transport=None
) -> int:
    """``msks image default``: designate the image a bare create
    boots (#270), or clear the designation with --unset."""
    checked_default_args(ref, unset)
    if unset:
        asyncio.run(unset_default_image(env_url(), env_token(), transport))
    else:
        asyncio.run(designate_image(env_url(), env_token(), ref, transport))
    return 0

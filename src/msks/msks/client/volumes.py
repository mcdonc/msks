"""The disk-surface commands: ``msks storage`` (#184) and the
``msks home`` volume moves (#80) — the group the CLI's typer layer
(:mod:`msks.client.cli`) dispatches into. The storage report reads
the state-disk budget with its per-workspace cost/ceiling table and
the catalog costs; the home commands stream a workspace's /home
volume out to a file and back in from one, through the daemon.
"""

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator

from .context import call, env_token, env_url
from .images import imported_cell
from .resize import display_name
from .rest import STREAM_WINDOW_B, api_client, download, upload
from .tabular import framed_text

#: One mebibyte, the unit the API's MiB-denominated sizes render in
#: (the client's local copy of the unit; #397 keeps the client off
#: daemon modules for a constant).
MIB = 1024 * 1024


def human_bytes(count: int) -> str:
    """Bytes in binary units: 512K, 812M, 23.4G — a whole number
    whenever the decimal would add nothing (2G, 40G)."""
    gib = count / 1024**3
    if gib >= 1:
        tenths = round(gib, 1)
        if tenths == int(tenths):
            return f"{int(tenths)}G"
        return f"{tenths:.1f}G"
    if count >= 1024**2:
        return f"{round(count / 1024**2)}M"
    return f"{max(count // 1024, 0)}K"


def cost_pair(cost_bytes: int, ceiling_mib: int) -> str:
    """One cost/ceiling cell: the blocks a workspace pays beside the
    virtual size its guest sees as the quota."""
    return f"{human_bytes(cost_bytes)} / {human_bytes(ceiling_mib * MIB)}"


def state_line(state: dict) -> str:
    """The budget line: what the state disk holds and how close it is
    to the named pressure condition."""
    return (
        f"state disk    used {human_bytes(state['used'])} "
        f"of {human_bytes(state['total'])}    "
        f"free {human_bytes(state['free'])}    pressure {state['pressure']}"
    )


def render_storage(
    report: dict, as_json: bool, workspace_id: str | None = None
) -> str:
    """The whole capacity report: budget line, per-workspace
    cost/ceiling table, catalog costs — or one JSON document."""
    if as_json:
        return json.dumps(report, indent=2)
    parts = [state_line(report["state"])]
    workspaces = workspace_table(narrowed(report["workspaces"], workspace_id))
    if workspaces:
        parts.append(workspaces)
    if images := image_cost_table(report["images"]):
        parts.append(images)
    return "\n\n".join(parts)


def storage_cells(ws: dict) -> list[str]:
    """One workspace row's cells: label, both cost/ceiling cells,
    total cost — the name is the human key (#246), the id rides in
    the ``--json`` document."""
    return [
        display_name(ws),
        cost_pair(ws["root_bytes"], ws["root_mib"]),
        cost_pair(ws["home_bytes"], ws["home_mib"]),
        human_bytes(ws["root_bytes"] + ws["home_bytes"]),
    ]


def narrowed(workspaces: list[dict], ref: str | None) -> list[dict]:
    """The rows one table shows: every row, or the one workspace the
    ref names — its id or its name (#246)."""
    if ref is None:
        return workspaces
    return [ws for ws in workspaces if ref in (ws["id"], ws.get("name"))]


def workspace_table(workspaces: list[dict]) -> str:
    """The framed per-workspace cost/ceiling table — empty
    when none."""
    return framed_text(
        ["workspace", "root cost/ceiling", "home cost/ceiling", "cost"],
        [storage_cells(ws) for ws in workspaces],
    )


def image_cost_table(images: list[dict]) -> str:
    """The catalog cost table.

    Rows that carry their import time (#186) show it, so entries
    sharing a reference read as distinct; a daemon predating
    stamps keeps the two-column table.
    """
    stamped = any(image.get("imported") for image in images)
    headers = ["image", "imported", "cost"] if stamped else ["image", "cost"]
    rows = [
        [
            f"{image['name']}:{image['version']}",
            *([imported_cell(image)] if stamped else []),
            human_bytes(image["bytes"]),
        ]
        for image in images
    ]
    return framed_text(headers, rows)


def cmd_storage(
    workspace_id: str | None = None,
    as_json: bool = False,
    transport=None,
) -> int:
    """``msks storage``: the state-disk budget and its consumers."""
    report = asyncio.run(call("GET", "/api/v1/storage", transport=transport))
    if workspace_id is not None and not narrowed(
        report["workspaces"], workspace_id
    ):
        raise SystemExit(f"msks: no such workspace: {workspace_id}")
    print(render_storage(report, as_json, workspace_id))
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
            return await download(
                client, home_path(workspace_id), sys.stdout.buffer
            )
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
        raise SystemExit(
            f"msks: cannot read volume image {path}: {exc}"
        ) from None


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
            reply = await upload(
                client, home_path(workspace_id), file_windows(source)
            )
        return imported_bytes(reply)
    finally:
        if owns:
            source.close()


def imported_bytes(reply: dict) -> int:
    """The reply's byte count, or the one-line refusal when a 2xx
    answer carries none (a daemon that is not this protocol)."""
    count = reply.get("bytes")
    if not isinstance(count, int):
        raise SystemExit(
            "msks: the daemon's import reply carried no byte count"
        )
    return count


def cmd_home_export(
    workspace_id: str, out: str | None = None, transport=None
) -> int:
    """``msks home export``: download a workspace's /home volume."""
    target = out if out is not None else f"{workspace_id}.ext4"
    try:
        total = asyncio.run(
            run_home_export(
                env_url(), env_token(), workspace_id, target, transport
            )
        )
    except BrokenPipeError:
        raise SystemExit(broken_pipe_line(target)) from None
    if target == "-":
        # Bytes own stdout; the confirmation goes to stderr.
        print(
            f"msks: exported {workspace_id} ({total} bytes)", file=sys.stderr
        )
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


def cmd_home_import(
    workspace_id: str, source_path: str, transport=None
) -> int:
    """``msks home import``: replace a workspace's /home volume."""
    total = asyncio.run(
        run_home_import(
            env_url(), env_token(), workspace_id, source_path, transport
        )
    )
    print(f"imported {total} bytes into {workspace_id}")
    return 0

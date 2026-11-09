"""The workspace proxy commands (#807, #808): ``msks proxy add``,
``ls``, and ``rm`` — the daemon-owned, persisted TCP proxies.

A proxy row is one listener the daemon holds for a workspace. Two
directions, named with ssh's letters and ssh's spec grammar
(``listen : target``, left to right):

- ``-L [BIND:]PORT:HOST:PORT`` (ssh's own shape): the daemon
  binds ``PORT`` (loopback, or ``BIND`` — every interface with
  ``0.0.0.0``) on its own host, and every accepted connection
  bridges to the workspace's port. ``HOST`` names the workspace
  itself, the far end — exactly the role it plays in ssh's
  ``-L`` — and admits two spellings: ``localhost`` and
  ``127.0.0.1``. Anything else refuses; a daemon-side service is
  the ``-R`` direction's job.
- ``RPORT:HOST:TPORT`` (the ssh ``-R`` shape): the workspace's
  gateway listens on ``RPORT``, and every guest connection bridges
  to the daemon-side service ``HOST:TPORT``.

``add`` is declarative: an identical existing row is returned as
it stands, a different row on the same bind is refused with the
row that holds it, and the daemon owns every listener — the rows
persist and every workspace start re-establishes them. The
ephemeral session shapes (stdio, a client-side ``--local`` bind)
live on ``msks forward``.
"""

import asyncio
import json
from urllib.parse import quote

import typer

from .context import call
from .tabular import listing_text

app = typer.Typer(help="manage the daemon's persisted workspace proxies")


def spec_port(raw: str, spec: str) -> int:
    """One port field: 1-65535 or the one-line exit."""
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"msks: -{spec} wants ports, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"msks: port {port} is out of range (1-65535)")
    return port


def split_leading_host(raw: str, flag: str) -> tuple[str | None, str]:
    """A leading ``[bracketed]`` IPv6 host and the spec after its
    colon, or ``(None, raw)`` when the spec does not start with
    one. A closing bracket that never comes refuses."""
    if not raw.startswith("["):
        return None, raw
    end = raw.find("]")
    if end == -1:
        raise SystemExit(
            f"msks: -{flag} wants the IPv6 host bracket closed, got {raw!r}"
        )
    return raw[1:end], raw[end + 2 :]


LOCAL_FORM = (
    "-L wants [BIND:]PORT:HOST:PORT with HOST localhost "
    "(an IPv6 bind arrives bracketed)"
)

#: The spellings the -L host field admits. Both name the same
#: endpoint — the workspace itself, the far end of the tunnel —
#: the way ssh's -L target names the far end.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")

LOCAL_HOST_RULE = (
    "-L's host field names the workspace itself; only localhost "
    "or 127.0.0.1 is allowed — to reach a daemon-side service, "
    "use -R"
)


def local_host(raw: str) -> None:
    """The -L spec's host field: the workspace itself, spelled
    either way. Anything else is the ask ssh would send to the
    far end's dialer — the workspace has none — so it refuses
    with the rule and the direction that serves it."""
    if raw not in LOOPBACK_HOSTS:
        raise SystemExit(f"msks: {LOCAL_HOST_RULE} (got {raw!r})")


def local_fields(rest: str) -> tuple:
    """(bind, listen, guest) for a spec with no bracketed lead:
    three fields are PORT:HOST:PORT, four name the bind address
    first."""
    fields = rest.split(":")
    if len(fields) == 3:
        listen, host, guest = fields
        local_host(host)
        return None, listen, guest
    if len(fields) == 4:
        bind, listen, host, guest = fields
        local_host(host)
        return bind, listen, guest
    raise SystemExit(f"msks: {LOCAL_FORM}, got {rest!r}")


def bracketed_local(host: str, rest: str) -> tuple:
    """(bind, listen, guest) for a spec whose lead is bracketed:
    the host is the bind, the rest is PORT:HOST:PORT."""
    fields = rest.split(":")
    if len(fields) != 3:
        raise SystemExit(f"msks: {LOCAL_FORM}, got {host!r}:{rest}")
    listen, target, guest = fields
    local_host(target)
    return host, listen, guest


def parse_local_spec(raw: str) -> dict:
    """``-L [BIND:]PORT:HOST:PORT`` — ssh's own shape: the
    daemon-side listener and the workspace port it reaches, the
    host field naming the workspace (localhost)."""
    host, rest = split_leading_host(raw, "L")
    if host is None:
        bind, listen, guest = local_fields(rest)
    else:
        bind, listen, guest = bracketed_local(host, rest)
    body = {
        "direction": "forward",
        "listen_port": spec_port(listen, "L"),
        "target_port": spec_port(guest, "L"),
    }
    if bind is not None:
        body["listen_host"] = bind
    return body


REMOTE_FORM = "-R wants RPORT:HOST:TPORT (an IPv6 host arrives bracketed)"


def remote_fields(raw: str) -> tuple:
    """(listen, host, target) for the remote spec: the gateway
    port, then the daemon-side service."""
    first, separator, rest = raw.partition(":")
    if not separator:
        raise SystemExit(f"msks: {REMOTE_FORM}, got {raw!r}")
    host, tail = split_leading_host(rest, "R")
    if host is not None:
        return first, host, tail
    fields = rest.split(":")
    if len(fields) != 2:
        raise SystemExit(f"msks: {REMOTE_FORM}, got {raw!r}")
    return first, fields[0], fields[1]


def parse_remote_spec(raw: str) -> dict:
    """``-R RPORT:HOST:TPORT``: the gateway listener and the
    daemon-side service it bridges to."""
    listen, host, target = remote_fields(raw)
    return {
        "direction": "remote",
        "listen_port": spec_port(listen, "R"),
        "target_host": host,
        "target_port": spec_port(target, "R"),
    }


def proxy_endpoint(row: dict) -> tuple[str, str]:
    """One row's (listen, target) display pair: a forward proxy
    listens on the daemon host and reaches the guest; a remote one
    listens on the workspace's gateway and reaches the daemon's
    side."""
    if row["direction"] == "remote":
        return (
            f"gateway:{row['listen_port']}",
            f"{row.get('target_host') or '127.0.0.1'}:{row['target_port']}",
        )
    return (
        f"{row.get('listen_host') or '127.0.0.1'}:{row['listen_port']}",
        f"guest:{row['target_port']}",
    )


def proxy_line(row: dict) -> str:
    """One row's one-line summary for the add and rm output."""
    listen, target = proxy_endpoint(row)
    label = f" [{row['label']}]" if row.get("label") else ""
    reason = f" — {row['reason']}" if row.get("reason") else ""
    return (
        f"{row['direction']} proxy {row['id']}: {listen} -> "
        f"{target}{label} ({row.get('status', 'stopped')}{reason})"
    )


def row_status(row: dict) -> str:
    """One row's status cell: any reason rides along, failed or
    not — a listening row the guest cannot reach says so."""
    status = row.get("status", "stopped")
    if row.get("reason"):
        return f"{status} ({row['reason']})"
    return status


async def create_proxy(workspace_id: str, body: dict, transport=None) -> dict:
    """POST one proxy row on its workspace's route — a name ref
    rides through and the daemon resolves it; the answer carries
    the row with its live status."""
    return await call(
        "POST",
        f"/api/v1/workspaces/{quote(workspace_id, safe='')}/proxies",
        json_body=body,
        transport=transport,
    )


async def proxy_rows(transport=None) -> list[dict]:
    """Every proxy row the daemon holds, live status attached."""
    return await call("GET", "/api/v1/proxies", transport=transport)


async def remove_proxy(
    workspace_id: str, proxy_id: int, transport=None
) -> dict:
    """DELETE one proxy row by id, on its workspace's route."""
    return await call(
        "DELETE",
        f"/api/v1/workspaces/{quote(workspace_id, safe='')}"
        f"/proxies/{proxy_id}",
        transport=transport,
    )


@app.command("add")
def cmd_proxy_add(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace the proxy serves (name or id)"
    ),
    local: str | None = typer.Option(
        None,
        "-L",
        "--local",
        metavar="[BIND:]PORT:HOST:PORT",
        help="forward: the daemon binds PORT (loopback, or BIND) and "
        "bridges to the workspace's port — HOST names the workspace "
        "itself (localhost)",
    ),
    remote: str | None = typer.Option(
        None,
        "-R",
        "--remote",
        metavar="RPORT:HOST:TPORT",
        help="reverse: the workspace's gateway binds RPORT and bridges "
        "to the daemon-side service HOST:TPORT",
    ),
    label: str | None = typer.Option(
        None, "--label", metavar="TEXT", help="a note the listings show"
    ),
) -> int:
    """Store one persisted proxy (an identical row returns as is)."""
    if (local is None) == (remote is None):
        raise typer.BadParameter("pass exactly one of -L or -R")
    body = (
        parse_local_spec(local)
        if local is not None
        else parse_remote_spec(remote)
    )
    if label is not None:
        body["label"] = label
    row = asyncio.run(create_proxy(workspace_id, body, ctx.obj))
    print(f"msks: {proxy_line(row)}")
    return 0


@app.command("ls")
def cmd_proxy_ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(
        False, "--json", help="print the rows as JSON"
    ),
) -> int:
    """List every stored proxy, with live status."""
    rows = asyncio.run(proxy_rows(ctx.obj))
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    print(
        listing_text(
            ["ID", "WORKSPACE", "DIR", "LISTEN", "TARGET", "LABEL", "STATUS"],
            [
                [
                    str(row["id"]),
                    row["workspace_id"],
                    row["direction"],
                    *proxy_endpoint(row),
                    row.get("label") or "",
                    row_status(row),
                ]
                for row in rows
            ],
        )
    )
    return 0


@app.command("rm")
def cmd_proxy_rm(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose proxy is removed (name or id)"
    ),
    proxy_id: int = typer.Argument(..., help="the proxy row's id"),
) -> int:
    """Remove one stored proxy (its listener closes at once)."""
    asyncio.run(remove_proxy(workspace_id, proxy_id, ctx.obj))
    print(f"msks: removed proxy {proxy_id}")
    return 0

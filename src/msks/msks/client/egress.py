"""The ``msks egress`` client: decide, watch, and inspect consent (#69)."""

import asyncio
import json
import urllib.parse

import websockets

from .rest import api_client, env_token, env_url, request, ssl_context

DURATIONS = ("once", "5m", "15m", "tilrestart", "forever")


def events_url(base_url: str, token: str) -> str:
    """The events websocket URL for a daemon base URL."""
    scheme, sep, rest = base_url.partition("://")
    if sep:
        scheme = "wss" if scheme == "https" else "ws"
    else:
        scheme, rest = "wss", base_url
    query = urllib.parse.quote_plus(token)
    return f"{scheme}://{rest.rstrip('/')}/api/v1/events?token={query}"


def dest_label(row: dict) -> str:
    """One destination as the operator reads it. A portless
    destination (a non-TCP/UDP flow) reads as all-ports — an allow
    for it opens every port on the host for the duration, and the
    label says so."""
    host = row["dest_host"]
    if row["dest_port"] in (0, None):
        return f"{host} (all ports)"
    return f"{host}:{row['dest_port']}"


def request_line(request: dict) -> str:
    """One pending request, one line. The id prints in full — it is
    what ``msks egress decide`` takes."""
    row = request["request"]
    return (
        f"{row['id']}  {dest_label(row):40s}  "
        f"{row['decision']}  {row['requested_at']:.0f}"
    )


def rules_line(rules: dict) -> str:
    """The rules view, one block."""
    lines = [f"workspace {rules['workspace_id']} (mode {rules['mode']})"]
    if rules["allow_list"]:
        lines.append("allowlist: " + ", ".join(rules["allow_list"]))
    for verdict, rows in (
        ("allowed", rules["allowed"]),
        ("denied", rules["denied"]),
    ):
        for row in rows:
            duration = row.get("duration") or "-"
            lines.append(f"{verdict:7s} {dest_label(row):40s} {duration}")
    return "\n".join(lines)


async def run_rules(workspace_id: str, transport=None) -> int:
    """``msks egress rules``: the in-effect view."""
    async with api_client(env_url(), env_token(), transport) as client:
        rules = await request(
            client, "GET", f"/api/v1/workspaces/{workspace_id}/egress"
        )
    print(rules_line(rules))
    return 0


async def run_requests(
    workspace_id: str, decision: str | None, transport=None
) -> int:
    """``msks egress requests``: the consent rows."""
    path = f"/api/v1/workspaces/{workspace_id}/egress/requests"
    if decision is not None:
        path += f"?decision={decision}"
    async with api_client(env_url(), env_token(), transport) as client:
        rows = await request(client, "GET", path)
    for row in rows:
        print(
            f"{row['id']}  {dest_label(row):40s}  {row['decision']:8s}"
            f"  {row.get('duration') or '-':10s}  "
            f"{row['requested_at']:.0f}"
        )
    return 0


async def run_decide(
    workspace_id: str,
    request_id: str,
    decision: str,
    duration: str,
    transport=None,
) -> int:
    """``msks egress decide``: one verdict on a held request."""
    if decision not in ("allow", "deny"):
        raise SystemExit("msks: decision must be allow or deny")
    if duration not in DURATIONS:
        raise SystemExit(
            f"msks: duration must be one of {', '.join(DURATIONS)}"
        )
    async with api_client(env_url(), env_token(), transport) as client:
        reply = await request(
            client,
            "POST",
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
            {"decision": decision, "duration": duration},
        )
    print(
        f"{request_id} {reply['verdict']['decision']} "
        f"({reply['verdict'].get('duration', '')})"
    )
    return 0


async def run_revoke(
    workspace_id: str, request_id: str, transport=None
) -> int:
    """``msks egress revoke``: undo an in-effect verdict."""
    async with api_client(env_url(), env_token(), transport) as client:
        await request(
            client,
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        )
    print(f"{request_id} revoked")
    return 0


async def run_watch(
    workspace_id: str | None, decide: bool, duration: str
) -> int:
    """``msks egress watch``: stream egress frames as a decider.

    Registers this client as a decider (interactive workspaces hold
    SYNs only while a decider is connected), prints every request,
    and — with ``--decide`` — prompts y/n on the terminal instead of
    only hinting at the decide command.
    """
    token = env_token()
    url = env_url()
    async for ws in websockets.connect(**connect_args(url, token)):
        try:
            await watch_one(ws, workspace_id, decide, duration, url, token)
        except websockets.ConnectionClosed:
            continue  # reconnect; the server re-sends the snapshot
    return 0


def connect_args(url: str, token: str) -> dict:
    """The events websocket's connect kwargs (a plain-ws URL takes
    no ssl argument)."""
    return {
        "uri": events_url(url, token),
        "ssl": None if url.startswith("http://") else ssl_context(),
        "max_size": 2**22,
    }


async def watch_one(ws, workspace_id, decide, duration, url, token) -> None:
    """One connection's lifetime: announce, then print every
    frame."""
    if workspace_id is not None:
        await ws.send(
            json.dumps({"type": "egress.decider", "workspace": workspace_id})
        )
    async for raw in ws:
        await handle_frame(json.loads(raw), decide, duration, url, token)


async def handle_frame(
    frame: dict, decide: bool, duration: str, url: str, token: str
) -> None:
    """Print one frame; optionally prompt for a verdict."""
    event = frame.get("event")
    data = frame.get("data", {})
    if event == "egress.request":
        row = data["request"]
        print(request_line(data), flush=True)
        if decide:
            await maybe_decide(row, duration, url, token)
    elif event == "egress.resolved":
        print(
            f"{data['request_id']}  resolved: {data['decision']}",
            flush=True,
        )
    elif event == "egress.rules":
        print(rules_line(data), flush=True)


async def maybe_decide(row: dict, duration: str, url: str, token: str) -> None:
    """The y/n prompt for a pending request (``--decide``): y
    allows for the duration; anything else denies now — the held
    connection fails fast instead of waiting out the timeout."""
    answer = await asyncio.to_thread(input, f"allow {dest_label(row)}? [y/N] ")
    decision = "allow" if answer.strip().lower() in ("y", "yes") else "deny"
    async with api_client(url, token) as client:
        await request(
            client,
            "POST",
            f"/api/v1/workspaces/{row['workspace_id']}/egress/requests/"
            f"{row['id']}",
            {"decision": decision, "duration": duration},
        )
    print(f"{row['id']} {decision}d", flush=True)

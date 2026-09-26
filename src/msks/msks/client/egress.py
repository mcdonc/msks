"""The ``msks egress`` client: decide, watch, and inspect consent (#69)."""

import asyncio
import json

import websockets

from . import wsauth
from .rest import api_client, env_token, env_url, request, ssl_context
from .tabular import listing_text

DURATIONS = ("once", "5m", "15m", "tilrestart", "forever")

#: The egress modes a switch names (#280); the daemon owns the
#: tokens — the client duplicates them per the CLI-isolation rule.
MODES = ("allow", "static", "interactive")


def events_url(base_url: str) -> str:
    """The events websocket URL for a daemon base URL. The token
    rides the handshake's auth subprotocol offer (#116), never the
    URL."""
    scheme, sep, rest = base_url.partition("://")
    if sep:
        scheme = "wss" if scheme == "https" else "ws"
    else:
        scheme, rest = "wss", base_url
    return f"{scheme}://{rest.rstrip('/')}/api/v1/events"


def dest_label(row: dict) -> str:
    """One destination as the operator reads it. A portless
    destination (a non-TCP/UDP flow) reads as all-ports — an allow
    for it opens every port on the host for the duration, and the
    label says so."""
    host = row["dest_host"]
    if row["dest_port"] in (0, None):
        return f"{host} (all ports)"
    return f"{host}:{row['dest_port']}"


def request_cells(row: dict) -> list[str]:
    """One request row's cells. The id prints in full — it is what
    ``msks egress decide`` takes."""
    return [
        row["id"],
        dest_label(row),
        row["decision"],
        row.get("duration") or "-",
        f"{row['requested_at']:.0f}",
    ]


def requests_text(rows: list[dict]) -> str:
    """The consent listing as one aligned table (#271)."""
    return listing_text(
        ["id", "destination", "decision", "duration", "requested at"],
        [request_cells(row) for row in rows],
    )


def request_line(request: dict) -> str:
    """One pending request, one line — the watch stream's shape.
    The id prints in full — it is what ``msks egress decide``
    takes."""
    row = request["request"]
    line = (
        f"{row['id']}  {dest_label(row)}  {row['decision']}  "
        f"{row['requested_at']:.0f}"
    )
    return line


def verdict_rows(rules: dict) -> list[list[str]]:
    """The in-effect verdict rows: verdict, destination, duration."""
    return [
        [verdict, dest_label(row), row.get("duration") or "-"]
        for verdict, section in (
            ("allowed", rules["allowed"]),
            ("denied", rules["denied"]),
        )
        for row in section
    ]


def rules_line(rules: dict) -> str:
    """The rules view, one block: heading, allowlist, then the
    verdict rows as one aligned table (#271)."""
    lines = [f"workspace {rules['workspace_id']} (mode {rules['mode']})"]
    if rules["allow_list"]:
        lines.append("allowlist: " + ", ".join(rules["allow_list"]))
    rows = verdict_rows(rules)
    if rows:
        lines.append(
            listing_text(["verdict", "destination", "duration"], rows)
        )
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
    text = requests_text(rows)
    if text:
        print(text)
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


async def run_mode(
    workspace_id: str,
    mode: str,
    allow: list[str] | None,
    offline: bool = False,
    transport=None,
) -> int:
    """``msks egress mode`` (#280): switch the workspace's posture.

    A running workspace swaps live (established connections
    survive); a stopped one builds the new posture at its next
    start. ``allow`` replaces the static allowlist when given;
    omitted, the workspace keeps the list it carries.
    """
    if mode not in MODES:
        raise SystemExit(f"msks: mode must be one of {', '.join(MODES)}")
    body = {"mode": mode}
    if allow is not None:
        body["allow_list"] = allow
    if offline:
        body["confirm_empty"] = True
    async with api_client(env_url(), env_token(), transport) as client:
        reply = await request(
            client,
            "PUT",
            f"/api/v1/workspaces/{workspace_id}/egress/policy",
            body,
        )
    effect = (
        "in effect now"
        if reply.get("applied")
        else ("takes effect at next start")
    )
    print(f"{workspace_id}: egress mode {reply['mode']} ({effect})")
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
    try:
        async for ws in websockets.connect(**connect_args(url, token)):
            try:
                # The handshake's echo check (#116): a daemon that did not
                # select the auth subprotocol is closing with its refusal
                # or a middlebox rewrote the handshake — named and exited
                # here, never pumped.
                await wsauth.require_echo(ws)
                await watch_one(ws, workspace_id, decide, duration, url, token)
            except websockets.ConnectionClosed:
                continue  # reconnect; the server re-sends the snapshot
    except wsauth.UnusableToken as exc:
        # One line without the token in it — the websocket library's
        # own refusal embeds the credential whole (#116 review).
        raise SystemExit(f"msks: {exc}") from None
    return 0


def connect_args(url: str, token: str) -> dict:
    """The events websocket's connect kwargs: the auth subprotocol
    offer beside the URL (a plain-ws URL takes no ssl argument)."""
    return {
        "uri": events_url(url),
        "subprotocols": wsauth.subprotocols(token),
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

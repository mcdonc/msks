"""The event-stream websocket (#69, #305): the hub relay, the
decider registration that lands a client this workspace's pending
holds and rules view, and the recorded placeholder lifecycle it
opens on."""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...events import relay
from .deps import authed_accept

LOG = logging.getLogger(__name__)


async def decider_loop(app, socket: WebSocket, client_id: int) -> None:
    """Read client control frames until disconnect (#69).

    A client announces itself as a consent decider with
    ``{"type": "egress.decider", "workspace": "<id>"}``; the
    registration lands it this workspace's pending holds and rules
    view directly (before the hub broadcast could), and its socket
    staying open is its liveness. Any other inbound frame is
    ignored — the relay task owns delivery.
    """
    while (message := await next_frame(socket)) is not False:
        if message is not None:
            await register_decider(app, socket, client_id, message)


async def next_frame(socket: WebSocket) -> dict | None | bool:
    """The next inbound frame: a decoded decider frame, None for
    an ignored message, or False when the socket closed (the
    loop's stop signal)."""
    try:
        raw = await socket.receive_text()
    except WebSocketDisconnect, RuntimeError:
        return False
    return decode_frame(raw)


#: The registration replay's row bound (#305): the newest rows the
#: audit table contributes to the decider's screen at connect.
SECRET_REPLAY_LIMIT = 100


async def register_decider(app, socket, client_id: int, message: dict):
    """One ``egress.decider`` frame: register the socket as this
    workspace's decider and land it the pending snapshot and rules
    view directly (before any hub broadcast could), then replay
    the recorded placeholder lifecycle (#305)."""
    workspace = message.get("workspace")
    if not isinstance(workspace, str):
        return
    row = await app.state.model.get_workspace(workspace)
    if row is None:
        # Say so: a decider pointed at a typo'd workspace would
        # otherwise wait on a silent, promptless connection.
        await socket.send_json(
            {
                "event": "egress.decider_rejected",
                "data": {"reason": "unknown workspace"},
            }
        )
        return
    # The frame names the workspace by id or name (#246); consent
    # state keys on the row's immutable id.
    workspace = row["id"]
    app.state.deciders.register(client_id, workspace)
    # Rules first: the TUI adopts the server's resolved workspace id
    # from the first rules frame (#297), so pending requests sent
    # before it would be silently dropped by the owns() check.
    rules = await app.state.consent.rules_frame(workspace)
    if rules is not None:
        await socket.send_json({"event": "egress.rules", "data": rules})
    for pending in await app.state.consent.snapshot(workspace):
        await socket.send_json({"event": "egress.request", "data": pending})
    await replay_secret_audit(app, socket, workspace)


def audit_frame(row: dict) -> dict:
    """One audit row as a ``secret.*`` frame's data (#305, #339):
    the coverage list and its legacy single-workspace spelling
    (``*`` for the daemon-wide row) beside the identity, with the
    mint kind alone naming its allowlist — the live shapes the
    replay matches."""
    data = {
        "audit_id": row["id"],
        "workspace_id": row["workspaces"][0] if row["workspaces"] else "*",
        "workspaces": row["workspaces"],
        "name": row["name"],
        "ts": audit_epoch(row["created_at"]),
    }
    if row["kind"] == "mint":
        data["dests"] = row["dests"]
    return data


async def replay_secret_audit(app, socket, workspace_id: str) -> None:
    """The workspace's recorded placeholder lifecycle (#305): the
    audit table's newest rows covering it, oldest first, as
    ``secret.*`` frames — the decider's connection opens on the
    recorded mints, revokes, and expiries instead of an empty
    live tail. Swaps and sightings stay live-only: they are
    per-request wire events, and the audit table records
    lifecycle alone. A read failure skips the replay and logs —
    registration keeps the rules view and pending snapshot it
    already landed."""
    try:
        rows = await app.state.model.list_workspace_audit(
            workspace_id, limit=SECRET_REPLAY_LIMIT
        )
    except Exception:  # noqa: BLE001 - best-effort, logged
        LOG.warning(
            "secret-audit replay for %s failed; the decider opens "
            "on the live stream alone",
            workspace_id,
            exc_info=True,
        )
        return
    if len(rows) == SECRET_REPLAY_LIMIT:
        LOG.info(
            "secret-audit replay for %s reached its %d-row limit; "
            "older recorded events may exist and stay off the "
            "screen",
            workspace_id,
            SECRET_REPLAY_LIMIT,
        )
    for row in rows:
        await socket.send_json(
            {"event": f"secret.{row['kind']}", "data": audit_frame(row)}
        )


def audit_epoch(iso: str) -> float:
    """An audit row's stored timestamp as epoch — the wire events'
    ``ts`` domain (the daemon stamps wall-clock). Naive UTC is the
    sqlite round-trip's shape (the dialect strips tzinfo at bind);
    a value that carries an offset converts — it is never silently
    reinterpreted as UTC."""
    moment = datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC).timestamp()
    return moment.astimezone(UTC).timestamp()


def decode_frame(raw: str) -> dict | None:
    """A JSON object frame, or None for anything else."""
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("type") != (
        "egress.decider"
    ):
        return None
    return message


def router(app, hub) -> APIRouter:
    """The event-stream websocket."""
    api = APIRouter()

    @api.websocket("/api/v1/events")
    async def events(socket: WebSocket) -> None:
        # The token rides the handshake's Authorization header
        # (#216) — the same scheme as the REST surface — never the
        # URL. A bad token accepts bare and closes 4401 — the close
        # code the clients' refused handling keys on.
        if not await authed_accept(app, socket):
            return
        queue = hub.subscribe()
        client_id = id(queue)
        try:
            receiver = asyncio.create_task(
                decider_loop(app, socket, client_id)
            )
            sender = asyncio.create_task(relay(queue, socket.send))
            done, pending = await asyncio.wait(
                {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            # The socket closing ends any decider authority this
            # client held (#69): interactivity follows the socket.
            app.state.deciders.deregister(client_id)
            hub.unsubscribe(queue)

    return api

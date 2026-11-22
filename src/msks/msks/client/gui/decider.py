"""The workspace page's decider registration (#999, phase 5.5
of #924): a second events connection the page owns beside the
window's feed — the same dial seams :mod:`msks.client.gui.feed`
dials, one registration frame sent the moment each connection
serves, and the page's own lifetime the task's. The daemon's
registry keys a decider's authority to its socket (#69): the
socket closing retires the registration, so the link's task —
started at the page's open, cancelled at its hide, cancelled
again with the bridge — is the registration's whole lifetime,
the TUI's decide app's own retire-by-closing (#467) in the
GUI's shape.

The registration answers with the pending snapshot: one
``egress.request`` frame per held flow, the workspace's
``egress.rules`` posture frame first, and the recorded
placeholder lifecycle behind them. A re-registration (a drop
and its reconnect, a page hidden and shown again) replays the
snapshot — the controller resets at the registration, so holds
that resolved during the outage do not linger as ghosts. A
workspace the daemon cannot resolve answers
``egress.decider_rejected``; the link ends there, the reason
crossing to ``on_rejected`` for the panel to surface.

The protocol state is the TUI's own pure controller
(:class:`msks.client.tui.consent.ConsentController` — stdlib
only, no Textual): the frame parsing, the pending-hold map, and
the per-workspace ``owns()`` check that keeps a foreign
workspace's frames from planting ghost holds (#280 review) all
stay one implementation behind both clients. The controller is
also the presenter's seam (#1000): the page hands the consent
presenter's own controller to the link, so the frames land on
the state the panel's rows build from — one source of truth
— while the link builds its own when none comes. The module holds
no Qt beside it: :class:`DeciderLink` is plain asyncio over the
shared dial seams, its callbacks the bridge wires to signals —
so the suite drives a link against a fake socket with no
binding and no daemon, and the coverage gate lands on it whole.
A dropped link says nothing of its own: the window's feed
flashes the daemon-wide outage on the same failure, and the
link keeps re-registering under it — the feed's ladder (#946)
between every attempt, reset by every connection that served.
"""

import asyncio
import json

import websockets

from ..egress import env_dial_events
from ..egress import refused as refused_close
from ..tui.consent import REJECTED, ConsentController
from .feed import RECONNECT_DELAYS, REFUSED_RETRY_S, backoff, close_quietly


def registration_frame(workspace_id: str) -> str:
    """The decider's registration (#999): the one control frame
    that claims a workspace's holds on the events socket — the
    same wire text the TUI's decider link and the standalone
    watch loop send."""
    return json.dumps({"type": "egress.decider", "workspace": workspace_id})


class DeciderLink:
    """One workspace's decider registration over one events
    connection, feeding a :class:`ConsentController`; the page
    starts and stops the task, and the bridge wires
    ``on_frame(outcome, payload)`` — each landed frame's
    application — and ``on_rejected(reason)`` to signals. The
    unwired link still runs: frames land in the controller and
    the outcomes tell to nobody, the feed's own bare
    contract."""

    def __init__(
        self,
        workspace_id: str,
        controller: ConsentController | None = None,
        *,
        hold_timeout: float = 120.0,
        ws_factory=None,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        self.workspace_id = workspace_id
        # The state seam (#1000): a controller handed in is the
        # one the frames land on — the consent presenter owns
        # the rows that read it — and the link builds its own
        # when none comes, the registration's whole state its
        # own.
        self.controller = controller or ConsentController(
            hold_timeout=hold_timeout, workspace_id=workspace_id
        )
        #: The daemon's refusal of the registration, as the
        #: page's flash reads it; empty while none arrived.
        self.reject_reason = ""
        self.reconnect_delays = reconnect_delays
        self.ws_factory = ws_factory or env_dial_events
        self.on_frame = None
        self.on_rejected = None

    async def run(self) -> None:
        """Register and serve until cancelled — the feed's own
        ladder between every attempt: reset by a connection that
        served, climbed by one that never reached its serve
        loop, the fixed slow interval on the daemon's token
        refusal. The daemon's refusal of the registration ends
        the loop instead: a workspace the daemon cannot resolve
        stays unresolved by reconnecting."""
        attempt = 0
        while True:
            served, refused, rejected = await self.pump_one()
            if rejected:
                return
            if refused:
                await asyncio.sleep(REFUSED_RETRY_S)
                continue
            attempt = 0 if served else attempt + 1
            await asyncio.sleep(backoff(self.reconnect_delays, attempt))

    async def pump_one(self) -> tuple[bool, bool, bool]:
        """One connection's lifetime; ``(served, refused,
        rejected)`` — whether it reached its serve loop (the
        ladder resets on the next attempt), whether the daemon's
        close was its token refusal (the fixed slow interval
        instead), and whether the daemon refused the
        registration outright (the loop's stop). A dial that
        fails — the environment's own refusal included, the
        feed's reading of the same arm — tells nobody and
        reconnects: the feed flashes the outage the same dial
        feeds."""
        try:
            ws = await self.ws_factory().__aenter__()
        except SystemExit, Exception:
            return False, False, False
        try:
            return await self.serve(ws)
        except websockets.ConnectionClosed as exc:
            return True, refused_close(exc), False
        except Exception:
            return True, False, False
        finally:
            await close_quietly(ws)

    async def serve(self, ws) -> tuple[bool, bool, bool]:
        """Register this workspace, reset the protocol state,
        then feed the controller every frame until the stream
        ends. The reset before the first frame is the replay's
        other half: the registration snapshot re-lands the holds
        and the rules, and a hold that resolved while the link
        was down is gone with the reset, not lingered."""
        await ws.send(registration_frame(self.workspace_id))
        self.controller.reset()
        async for raw in ws:
            if self.land(raw):
                return True, False, True
        return True, False, False

    def land(self, raw: str) -> bool:
        """One wire text through the controller: the outcome and
        payload cross to ``on_frame``, and the daemon's refusal
        of the registration — True, the loop's stop signal —
        carries its reason to ``on_rejected``. Junk skips inside
        the controller, never fatal to the connection."""
        outcome, payload = self.controller.apply_frame(raw)
        if self.on_frame is not None:
            self.on_frame(outcome, payload)
        if outcome != REJECTED:
            return False
        self.reject_reason = payload or "the daemon refused the registration"
        if self.on_rejected is not None:
            self.on_rejected(self.reject_reason)
        return True

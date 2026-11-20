"""The consent presenter (phase 5.5 of #924, #1000): the
decider's state and verdict seam — holds added and resolved
over the events frames, the row models the panel renders, and
the REST call that answers a hold. The state is the TUI's own
pure controller
(:class:`msks.client.tui.consent.ConsentController` — stdlib
only, no Textual): frame parsing, the pending-hold map, the
per-workspace ``owns()`` check, and the countdown math stay one
implementation behind both clients, the same reuse the decider
registration (#999) already makes.

One controller instance is the whole seam between the two
halves: the presenter owns it, the page hands it to the
bridge's registration, and the link's frames land on the state
the rows build from — one source of truth, so the
re-registration's reset drops the holds that resolved during an
outage from the same map the rows read. The presenter imports
nothing beside that controller: no Qt, no client-side seam, so
the module runs wherever the suite runs and the cycle gate
stays green.

The verdict rides the bridge's one verb
(:meth:`msks.client.gui.bridge.Bridge.call`): the decide call
is the same exchange ``msks egress decide`` makes over the
shared data layer, the verdict and the duration chosen in the
same action, each verdict its own flight under its own key. A
row clears on the daemon's resolved frame, never on the click
itself — the decide call answers nothing locally, and a
refused verdict flashes in the panel while the row stands.
"""

import time
from dataclasses import dataclass

from ..tui.consent import (
    DURATION_DEFAULT,
    ConsentController,
    ConsentRequest,
    fmt_duration,
)

#: The decide flights' key stem (#1000): each verdict its own
#: key, the per-flight rule the tabs teach — a late refusal
#: from an earlier flight reaches nothing.
DECIDE_STEM = "consent-decide"


def dest_label(request: ConsentRequest) -> str:
    """One held request's destination label: ``host:port``,
    with port 0 — a non-TCP/UDP flow — reading as all ports
    (the TUI queue's own spelling, one label behind both
    clients)."""
    if request.dest_port == 0:
        return f"{request.dest_host} (all ports)"
    return f"{request.dest_host}:{request.dest_port}"


@dataclass(frozen=True)
class ConsentRow:
    """One held request as the panel renders it (#1000): the
    request id the verdict names, the destination label, and
    the hold's countdown — the seconds left and the label built
    from them, read fresh on every render. Plain data with
    nothing Qt-bound in it."""

    id: str
    dest: str
    remaining: float
    countdown: str


class ConsentPresenter:
    """The decider's state and verdict seam (#1000): the
    controller the link drives through the page's registration,
    the rows the panel renders off it, and the decide flight
    that answers a hold. The presenter owns the controller —
    the page hands it to ``bridge.start_decider`` — and the
    rows read no other state."""

    def __init__(
        self,
        workspace_id: str,
        bridge,
        *,
        hold_timeout: float = 120.0,
        clock=time.time,
    ) -> None:
        self.workspace_id = workspace_id
        self.bridge = bridge
        self.controller = ConsentController(
            hold_timeout=hold_timeout,
            clock=clock,
            workspace_id=workspace_id,
        )
        self.decide_seq = 0
        self.decide_key = ""

    def frame(self, raw: str) -> tuple[str, object]:
        """One wire frame through the controller — the link's
        own seam and the suite's own shape: ``egress.request``
        adds a hold, ``egress.resolved`` drops one, and
        ``egress.rules`` replaces the posture snapshot, with a
        foreign workspace's frames changing nothing. The
        outcome pair crosses back for the caller that reads
        it."""
        return self.controller.apply_frame(raw)

    def rows(self) -> list[ConsentRow]:
        """The panel's row models: the controller's pending
        holds, oldest first through its ``ordered()``."""
        return [self.row(request) for request in self.controller.ordered()]

    def row(self, request: ConsentRequest) -> ConsentRow:
        """One hold's row: the destination label and the
        countdown — the frame's ``expires_at`` (the daemon's
        settings-driven deadline) when it carried one, the
        controller's ``hold_timeout`` otherwise, both through
        the controller's own clock."""
        remaining = self.controller.remaining(request)
        return ConsentRow(
            id=request.id,
            dest=dest_label(request),
            remaining=remaining,
            countdown=fmt_duration(remaining),
        )

    def decide(
        self, request_id: str, decision: str, duration: str = DURATION_DEFAULT
    ):
        """One verdict through the bridge (#1000): the decide
        call the shared data layer serves — the same exchange
        ``msks egress decide`` makes — with the duration chosen
        in the same action and ``tilrestart`` the default. The
        row the verdict answers stands until the daemon's
        resolved frame drops it; the flight's key lands on
        ``decide_key`` for the panel's failure match."""
        self.decide_seq += 1
        self.decide_key = f"{DECIDE_STEM}-{self.decide_seq}"
        return self.bridge.call(
            self.decide_key,
            "decide",
            self.workspace_id,
            request_id,
            decision,
            duration,
        )

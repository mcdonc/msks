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
outage from the same map the rows read. The controller the
presenter owns is the shared shape (:class:`SharedController`):
the link applies frames on the bridge's worker loop while the
panel renders rows on the GUI thread, and the lock keeps the
render's snapshot whole — the TUI's own single-threaded
controller needs none of it. The presenter imports nothing
beside that controller: no Qt, no client-side seam, so the
module runs wherever the suite runs and the cycle gate stays
green.

The verdict rides the bridge's one verb
(:meth:`msks.client.gui.bridge.Bridge.call`): the decide call
is the same exchange ``msks egress decide`` makes over the
shared data layer, the verdict and the duration chosen in the
same action. Every verdict's key comes from one module-wide
sequence and joins the outstanding set the panel matches
against — a queue's rapid verdicts each keep their own key, and
a fresh presenter's flights never reuse a gone one's. A row
clears on the daemon's resolved frame, never on the click
itself — the decide call answers nothing locally, and a
refused verdict flashes in the panel while the row stands.
"""

import itertools
import threading
import time
from dataclasses import dataclass

from ..tui.consent import (
    DURATION_DEFAULT as DURATION_DEFAULT,
)
from ..tui.consent import (
    DURATIONS as DURATIONS,
)
from ..tui.consent import (
    ConsentController,
    ConsentRequest,
    fmt_duration,
)

#: The decide flights' key stem (#1000): the panel matches its
#: flash against the stem's keys.
DECIDE_STEM = "consent-decide"

#: The decide flights' sequence (#1000): one counter behind
#: every presenter the page builds — the page rebuilds the
#: presenter at every open, and a per-presenter count would
#: reuse a gone presenter's keys while its last flight still
#: flies, the misattribution the per-flight rule exists to
#: prevent.
DECIDE_FLIGHTS = itertools.count(1)


class SharedController(ConsentController):
    """The consent controller two threads share (#1000): the
    link applies frames on the bridge's worker loop while the
    panel renders rows on the GUI thread, so every apply, every
    reset, and the ordered snapshot the rows read run under one
    lock — a render never iterates a map the worker is landing
    the next frame into. On a GIL build the tear needs a
    free-threaded interleave to reach, but the snapshot
    contract is enforced rather than incidental to CPython's
    internals. The TUI's own controller stays lock-free: its
    loop applies and renders on one thread."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.Lock()

    def apply_frame(self, raw: str) -> tuple[str, object]:
        """One frame's application under the lock — the link's
        seam on the worker loop."""
        with self.lock:
            return super().apply_frame(raw)

    def reset(self) -> None:
        """The re-registration's reset under the lock."""
        with self.lock:
            super().reset()

    def ordered(self) -> list[ConsentRequest]:
        """The pending holds as a snapshot list, built under the
        lock — the rows iterate it off the worker's reach."""
        with self.lock:
            return super().ordered()


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
    the hold's countdown — the seconds left beside the label
    the presenter builds from them (the compact
    :func:`msks.client.tui.consent.fmt_duration` spelling; the
    TUI's queue shows raw seconds, and the row carries both so
    the panel reads either), read fresh on every render. Plain
    data with nothing Qt-bound in it."""

    id: str
    dest: str
    remaining: float
    countdown: str


class ConsentPresenter:
    """The decider's state and verdict seam (#1000): the shared
    controller the link drives through the page's registration,
    the rows the panel renders off it, and the decide flights
    that answer holds. The presenter owns the controller — the
    page hands it to ``bridge.start_decider`` — and the rows
    read no other state."""

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
        self.controller = SharedController(
            hold_timeout=hold_timeout,
            clock=clock,
            workspace_id=workspace_id,
        )
        #: The verdict flights out and unanswered (#1000): the
        #: panel matches each decide answer against the set and
        #: retires the key with it — a queue's verdicts each
        #: keep their own key, and the keys never repeat across
        #: the presenters the page builds.
        self.decide_keys: set[str] = set()

    def rows(self) -> list[ConsentRow]:
        """The panel's row models: the controller's pending
        holds, oldest first through its ``ordered()`` — a
        snapshot the worker's next frame cannot tear."""
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
        flight's key joins the outstanding set for the panel's
        answer match. The row the verdict answers stands until
        the daemon's resolved frame drops it."""
        key = f"{DECIDE_STEM}-{next(DECIDE_FLIGHTS)}"
        self.decide_keys.add(key)
        return self.bridge.call(
            key,
            "decide",
            self.workspace_id,
            request_id,
            decision,
            duration,
        )

    def retire(self, key: str) -> None:
        """One answered flight's key leaves the outstanding set
        (#1000): the panel's half of the match, told with the key
        each decide answer carried — a key no flight holds is a
        quiet no-op, and a stale epoch's answer retires
        nothing."""
        self.decide_keys.discard(key)

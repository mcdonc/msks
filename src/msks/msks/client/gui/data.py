"""The GUI's screen actions over the REST transport (phase 2 of
#924): the daemon's surface through the shared data layer
(:mod:`msks.client.data`) — the GUI holds no direct
daemon-state access.

Every screen action is a :class:`~msks.client.data.RestData`
method — the same exchange the matching CLI subcommand makes,
through the one door, its failures carried as
:class:`~msks.client.data.RestFailure` (#904). The TUI's
data (:mod:`msks.client.tui.data`) builds on the same one —
the two clients share the door, and the per-client surface
grows here and there as their screens diverge. The screens plug
fakes here for the tests, and the bridge
(:mod:`msks.client.gui.bridge`) runs these coroutines off the
GUI thread.
"""

from ..data import RestData


class GuiData(RestData):
    """The screens' daemon calls; one instance per window, shared
    with the bridge."""

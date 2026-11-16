"""The workspace TUI's data (#309): the daemon's REST surface
through the shared data layer (:mod:`msks.client.data`) — the
TUI holds no direct daemon-state access.

Every screen action is a :class:`~msks.client.data.RestData`
method — the same exchange the matching CLI subcommand makes,
through the one door, its failures carried as
:class:`~msks.client.data.RestFailure` (#904): the screens'
guards flash the message and the tree stands. The class stays
the tree's own name (the app, the screens, and the suites build
it) while the methods live on the shared layer; the GUI's data
(:mod:`msks.client.gui.data`) builds on the same one — the two
clients share the door, and the per-client surface grows here
and there as their screens diverge.
"""

from ..data import RestData


class TuiData(RestData):
    """The screens' daemon calls; one instance per app run."""

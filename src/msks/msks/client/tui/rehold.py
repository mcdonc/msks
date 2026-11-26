"""The listing's bridge re-hold watch (#1127): the mixin the
main screen composes. Each bridge-relevant frame the listing's
connection lands crosses here — a listener that answers a spawn
this session made says the recovery once, and a failure signal
(the holder died, or a running start left none) goes to the
app's re-hold policy for its ruling: a weigh starts the host's
own rows read (:meth:`weigh_rehold`, the daemon call the screen
keeps beside its other reads) that gates the spawn, and the
give-up line names the remedy the row's cause already carries.
The deferrals the policy's windows swallow drain on the host's
tick. The mixin reads its host the way ``HelpKeys`` does — the
app, the standing rows, the connection — and owns no state of
its own.
"""

from ..bridge import GAVE_UP, became_running, bridge_serving, holder_lost
from .consent_ui import flash_safe
from .rows import workspace_label


class ReholdWatch:
    """The re-hold watch's methods over a listing screen (#1127):
    the connection's ``on_bridge`` callback, the rulings, and the
    tick's drain — :class:`~msks.client.tui.main_screen.MainScreen`
    composes them and provides the rows read, ``weigh_rehold``.
    A plain mixin, so its bindings list never reaches the screen
    and its workers start on the host."""

    def bridge_frame(self, msg: dict) -> None:
        """One bridge-relevant frame to the app's re-hold watch
        (#1127): a listener that answers a spawn this session
        made says the recovery once, and a failure signal (the
        holder died, or a running start left none) goes to the
        policy for its ruling — a weigh runs the rows read, and
        the give-up line names the remedy the row's cause
        already carries."""
        serving = bridge_serving(msg)
        if serving is not None:
            self.note_reheld(serving)
            return
        lost = holder_lost(msg) or became_running(msg)
        if lost is not None:
            self.weigh_bridge_signal(lost)

    def note_reheld(self, workspace_id: str) -> None:
        """A listener came up (#1127): the one that answers a
        spawn this session made says the recovery once, on the
        surface the operator stands on."""
        if self.app.data.reholder.serving(workspace_id):
            label = flash_safe(self.bridge_label(workspace_id))
            self.app.flash_standing(f"bridge re-held — {label}")

    def weigh_bridge_signal(self, workspace_id: str) -> None:
        """One failure signal's ruling (#1127), landed: the
        shared path decides the give-up line or the weigh."""
        ruling = self.app.data.reholder.signal(workspace_id)
        self.land_ruling(workspace_id, ruling)

    def land_ruling(self, workspace_id: str, ruling: str | None) -> None:
        """One ruling's effect (#1127): the give-up line names the
        remedy the row's cause already carries, and a weigh runs
        the rows read that gates the spawn."""
        if ruling is None:
            return
        if ruling == GAVE_UP:
            label = flash_safe(self.bridge_label(workspace_id))
            self.app.flash_standing(
                f"bridge not re-held — {label}; "
                "the row's cause names 'msks proxy hold'"
            )
            return
        self.run_worker(self.weigh_rehold(workspace_id), group="rehold")

    def watch_rehold(self) -> None:
        """The re-hold deferrals' drain (#1127): the failure
        frames are edge-triggered, so a signal the window
        swallowed waits as a mark — the tick re-rules each one
        whose window closed, exactly as a fresh signal would. A
        screen without its link decides nothing: the signals
        land on the connection it would hold."""
        if self.link is None:
            return
        for workspace_id, ruling in self.app.data.reholder.take_due():
            self.land_ruling(workspace_id, ruling)

    def bridge_label(self, workspace_id: str) -> str:
        """The workspace's label for a re-hold line: the standing
        row's, the id alone for one the listing does not hold."""
        row = next(
            (row for row in self.rows if row["id"] == workspace_id), None
        )
        return workspace_label(row) if row is not None else workspace_id

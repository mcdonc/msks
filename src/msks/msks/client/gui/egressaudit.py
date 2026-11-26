"""The Egress tab's audit trail (#1051, over #1042): the
workspace's consent rows as the daemon recorded them — every
answered hold, every NXDOMAIN denial a static posture made —
listed newest first, one read on the page's own refresh cadence
beside the posture read, filterable by decision. A denial row
carries the deny-to-allowlist action: the denied name crosses
to the tab on ``allow_wanted``, and the tab's own write path —
the policy route the allowlist editor drives — adds it to the
static allowlist. The trail reports; it does not answer: allow
and deny of a live hold stay in the consent panel (#999,
#1000).

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the Egress
tab (itself lazily imported) owns the import.
"""

from PySide6 import QtCore, QtWidgets

from ..ages import clock_label
from .forms import ElidedNote, ModePicker, muted_note, read_only_table
from .kinds import ERROR

#: The audit read's key stem (#1051): each flight carries its
#: own key off it, the per-flight rule the tabs share.
AUDIT_STEM = "egress-audit"

#: The page bound (#1051): the newest rows the trail asks for,
#: the route's own default.
AUDIT_LIMIT = 100

#: The filter's unfiltered entry (#1051): the combo's first
#: item, the one whose read names no decision.
FILTER_ALL = "all"

#: The decisions the trail filters by (#1051), display order —
#: the daemon's own tokens, duplicated here per the
#: CLI-isolation rule (the durations' and modes' own precedent).
DECISIONS = ("pending", "allowed", "denied", "expired", "revoked")

#: The decision the deny-to-allowlist action reads (#1051): the
#: rows whose name wants allowing — a decider's denial and a
#: static posture's recorded NXDOMAIN denial alike.
DENIED = "denied"

#: The trail's heading (#1051).
AUDIT_HEADING = "audit trail"

#: The empty trail's stand-in (#1051): the muted note beneath
#: the table while the read carries no rows.
AUDIT_EMPTY = "no consent rows"

#: The trail's columns (#1051): the destination as the operator
#: reads it, the decision, the duration, and the local
#: wall-clock label — the same day-aware spelling the secrets
#: audit's rows carry (#1064).
AUDIT_COLUMNS = ("DESTINATION", "DECISION", "DURATION", "REQUESTED")

#: The columns that hug their text (#1025): every column but
#: the destination, which shares the pane's width.
AUDIT_CONTENT = ("DECISION", "DURATION", "REQUESTED")

#: The tab grid's rows, shared by both columns (#1154): the
#: heading row on top, the stretching row in the middle, the
#: footer row under it. The tab places the left column's bands
#: in them and the trail its own, so one row is one height for
#: both columns.
HEADING_ROW = 0
BODY_ROW = 1
FOOTER_ROW = 2

#: The deny-to-allowlist action's label (#1051).
ALLOW_BUTTON = "Allowlist"


def dest_text(row: dict) -> str:
    """One recorded row's destination label: ``host:port``, with
    port 0 — a non-TCP/UDP flow — reading as all ports, the
    consent queue's own spelling."""
    host = str(row.get("dest_host") or "")
    port = row.get("dest_port")
    if port in (0, None):
        return f"{host} (all ports)"
    return f"{host}:{port}"


def row_text(row: dict) -> tuple[str, str, str, str]:
    """One recorded row's cells: the destination label, the
    decision, the duration with the TUI's own dash stand-in, and
    the requested stamp's local wall-clock label — blank on a
    stamp the row did not carry."""
    stamp = row.get("requested_at")
    moment = clock_label(stamp) if isinstance(stamp, (int, float)) else ""
    return (
        dest_text(row),
        str(row.get("decision") or ""),
        str(row.get("duration") or "-"),
        moment,
    )


class AuditTrail(QtCore.QObject):
    """The tab's audit surface (#1051): the filter, the table of
    recorded consent rows newest first, and the deny-to-allowlist
    action on a selected denial row — the denied name crossing
    to the tab on ``allow_wanted`` for the policy write the tab
    owns. The read flies on the page's own cadence and on every
    pushed ``egress.resolved`` frame (the trail's newest row is
    a verdict), one flight at a time, a filter change cancelling
    the standing read and reflying under the picked decision.

    The trail is no widget of its own (#1154): it places its
    three bands — the heading row, the table, the footer — into
    the tab's grid, in the column the tab names, beside the left
    column's bands. One grid row is one height for both columns,
    so the table's top edge is the allowlist list's and its
    bottom edge the verdicts list's at every window height,
    whatever height the style gives the filter's combo beside a
    label. The footer is one row, the Revoke row's shape: the
    empty state's stand-in, the refusal note, and the Allowlist
    button beside one another."""

    allow_wanted = QtCore.Signal(str)

    def __init__(
        self, bridge, grid: QtWidgets.QGridLayout, column: int
    ) -> None:
        super().__init__()
        self.bridge = bridge
        self.workspace_id = ""
        self.rows: list[dict] = []
        self.pending = False
        self.key = ""
        self.flight = None
        self.seq = 0
        self.heading = QtWidgets.QLabel(AUDIT_HEADING)
        self.filter = ModePicker()
        self.filter.addItems([FILTER_ALL, *DECISIONS])
        # The filter's pick is the commit, the switcher's own
        # rule: ``activated`` is the operator's own choice, and a
        # programmatic index change decides nothing.
        self.filter.activated.connect(self.refly)
        self.heading_row = QtWidgets.QWidget()
        head = QtWidgets.QHBoxLayout(self.heading_row)
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.heading)
        head.addStretch(1)
        head.addWidget(self.filter)
        self.table = read_only_table(AUDIT_COLUMNS, AUDIT_CONTENT)
        # The table carries no minimum of its own (#1051): the
        # grid's right column stacks it under its own chrome, and
        # the window's minimum reads the tallest column's — the
        # table shares what the pane gives instead of holding the
        # window open.
        self.table.setMinimumHeight(0)
        self.table.itemSelectionChanged.connect(self.sync_buttons)
        self.empty = muted_note(AUDIT_EMPTY)
        self.empty.setVisible(False)
        self.allow_button = QtWidgets.QPushButton(ALLOW_BUTTON)
        self.allow_button.clicked.connect(self.allow)
        self.allow_button.setEnabled(False)
        # The note keeps its one line (#1154 review): a refusal's
        # words are the route's own, and a line of them wider
        # than the pane would hold the window open to its width.
        self.note = ElidedNote()
        self.note.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.footer = QtWidgets.QWidget()
        foot = QtWidgets.QHBoxLayout(self.footer)
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addWidget(self.empty)
        foot.addWidget(self.note)
        foot.addWidget(self.allow_button)
        grid.addWidget(self.heading_row, HEADING_ROW, column)
        grid.addWidget(self.table, BODY_ROW, column)
        grid.addWidget(self.footer, FOOTER_ROW, column)
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def host(self, workspace_id: str) -> None:
        """The page's open (#1051): a workspace switch cancels
        the read the old workspace still had out — its answer is
        not the new workspace's to take — blanks the table, and
        lets the new workspace's own read fill it. The picked
        filter stays: it names a decision, not a workspace."""
        if workspace_id == self.workspace_id:
            return
        if self.flight is not None and not self.flight.done():
            self.flight.cancel()
        self.flight = None
        self.pending = False
        self.key = ""
        self.workspace_id = workspace_id
        self.rows = []
        self.note.say("")
        self.repaint_rows()

    def decision(self) -> str | None:
        """The picked filter as the read's query: None — the
        query omitted, every decision — on the unfiltered
        entry."""
        picked = self.filter.currentText()
        return None if picked == FILTER_ALL else picked

    def poke(self) -> None:
        """One audit read through the bridge (#1051) — the page's
        cadence and the tab's own pokes share it. A read already
        in flight is skipped, the listing's own guard."""
        if not self.workspace_id or self.pending:
            return
        self.pending = True
        self.seq += 1
        self.key = f"{AUDIT_STEM}-{self.seq}"
        self.flight = self.bridge.call(
            self.key,
            "egress_requests",
            self.workspace_id,
            self.decision(),
            AUDIT_LIMIT,
        )

    @QtCore.Slot()
    def refly(self) -> None:
        """A picked filter's own action (#1051): the standing
        read — armed for the decision the table last shows —
        cancels, and the read flies again under the picked
        decision. A read the picker's programmatic changes never
        reach decides nothing."""
        if not self.workspace_id:
            return
        if self.flight is not None and not self.flight.done():
            self.flight.cancel()
        self.pending = False
        self.poke()

    def push(self, frame: dict) -> None:
        """One pushed frame (#1051): an ``egress.resolved`` — the
        daemon publishes one on every verdict — is the trail's
        newest row landing, and the read flies again; anything
        else is not the trail's to take."""
        if frame.get("event") != "egress.resolved":
            return
        data = frame.get("data") or {}
        if data.get("workspace_id") != self.workspace_id:
            return
        self.poke()

    def take(self, rows: object) -> None:
        """The landed rows into the trail's standing state
        (#1051): an answer that is not a list blanks the table —
        the tab's own tolerance, the daemon's shape never sending
        one."""
        if not isinstance(rows, list):
            self.rows = []
        else:
            self.rows = [row for row in rows if isinstance(row, dict)]
        self.repaint_rows()

    def shown(self) -> list[tuple[str, ...]]:
        """The table's rows as text — the repaint guard's own
        read."""
        return [
            tuple(
                self.table.item(line, column).text()
                for column in range(self.table.columnCount())
            )
            for line in range(self.table.rowCount())
        ]

    def repaint_rows(self) -> None:
        """The table from the standing rows (#1051): one line per
        row, the empty state's stand-in beneath while none
        stands, and the Allowlist button following the selection.
        A rebuild the rows already show skips itself: the page's
        cadence repaints every few seconds, and the selection the
        Allowlist action reads must survive the repaints that
        change nothing — an answer that changes the rows rebuilds
        the table and the selection goes with it."""
        cells = [row_text(row) for row in self.rows]
        if self.shown() != cells:
            self.table.setRowCount(0)
            for cells_of_row in cells:
                line = self.table.rowCount()
                self.table.insertRow(line)
                for column, text in enumerate(cells_of_row):
                    cell = QtWidgets.QTableWidgetItem(text)
                    self.table.setItem(line, column, cell)
        # The stand-in follows the rows' emptiness outside the
        # rebuild guard (#1051 review): a first read whose rows
        # are empty builds nothing, and the stand-in must still
        # say itself.
        self.empty.setVisible(not self.rows)
        self.sync_buttons()

    def selected_row(self) -> dict | None:
        """The selected row's own record, or None while no row
        stands selected."""
        index = self.table.currentRow()
        if not 0 <= index < len(self.rows):
            return None
        return self.rows[index]

    @QtCore.Slot()
    def sync_buttons(self) -> None:
        """The action's state (#1051): the Allowlist button on a
        selected denial row — the one row whose name wants
        allowing."""
        row = self.selected_row()
        held = row is not None and row.get("decision") == DENIED
        self.allow_button.setEnabled(held)

    @QtCore.Slot()
    def allow(self) -> None:
        """The deny-to-allowlist action (#1051): the selected
        denial row's name crosses to the tab on
        ``allow_wanted`` — the tab's own write path composes the
        policy write. A row without a name decides nothing."""
        row = self.selected_row()
        if row is None or row.get("decision") != DENIED:
            return
        host = str(row.get("dest_host") or "")
        if host:
            self.allow_wanted.emit(host)

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """The read's answer (#1051): the rows repaint and a
        standing refusal note clears — the read the note named
        came back. Another key's answer is not this trail's to
        take."""
        if key != self.key or not self.key:
            return
        self.pending = False
        self.flight = None
        self.note.say("")
        self.take(result)

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """The read's refusal (#1051): the route's own words land
        on the note line, the rows stand, and the next poke
        flies. Another key's failure is not this trail's to
        show."""
        if key != self.key or not self.key:
            return
        self.pending = False
        self.flight = None
        self.note.say(f"refused: {failure}", ERROR)

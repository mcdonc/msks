"""The workspace listing's cell layer (phase 3 of #924,
#939/#964): the column set the list screen renders — the five
TUI fields and the row-actions column — with the sort-rank item
classes and the cell text helpers that turn the presenter's row
models into table cells. Plain widget glue, no screen state:
the screen module (:mod:`msks.client.gui.workspaces`) owns the
flights, the fills, and the actions.

This module imports PySide6 at module scope — the screens'
own contract: nothing imports it at CLI parse time (#927).
"""

from PySide6 import QtCore, QtWidgets

from .rows import (
    WorkspaceRow,
    pending_marker,
    restart_note,
    state_kind,
    workspace_label,
)
from .tints import SEMANTIC_ROLE

RANK_ROLE = QtCore.Qt.ItemDataRole.UserRole

#: The image cell's whole value (#939), under its sort role:
#: the display text ellipsizes when the pane cannot fit every
#: column, and the whole value is what the column sorts by.
IMAGE_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

#: The created cell's sort data (#939): the parsed creation
#: moment — a header click orders by date, never by the
#: relative label's spelling.
CREATED_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

#: The workspace's id on every row's name item (#939): the
#: selection follows the workspace, not the row slot, across a
#: refresh's rebuild.
ROW_ID_ROLE = QtCore.Qt.ItemDataRole.UserRole + 3

#: The cell margin the contents sizing pays (#939): the budget
#: an ellipsized cell's text renders within.
CELL_MARGIN = 8

#: The toggle's icon per action (#972): the VCR pair — the play
#: gesture for a start, the stop sign for a stop.
TOGGLE_ICONS = {
    "start": QtWidgets.QStyle.StandardPixmap.SP_MediaPlay,
    "stop": QtWidgets.QStyle.StandardPixmap.SP_MediaStop,
}

#: The table's data columns (#939), in display order — the TUI
#: listing's own five, the fields the row models carry.
COLUMNS = ("NAME", "STATUS", "ID", "IMAGE", "CREATED")

NAME_COLUMN = 0
STATUS_COLUMN = 1
ID_COLUMN = 2
IMAGE_COLUMN = 3
CREATED_COLUMN = 4

#: The row-actions column (#972): last, with no header label and
#: a header that sorts nothing — one cell of buttons per row,
#: the row's own actions beside its facts.
ACTION_COLUMN = len(COLUMNS)


def read_only_item(
    item: QtWidgets.QTableWidgetItem,
) -> QtWidgets.QTableWidgetItem:
    """One cell that renders but never edits (#939): the
    listing is a view over the daemon's state, and an edit would
    paint a value only the display holds — the next refresh
    throws it away. The cell stays selectable and enabled."""
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item


def action_reply_text(label: str, reply: object) -> str:
    """The action's own flash (#940, the TUI's #776 mask): the
    workspace's label beside the status the daemon's answer
    carries — the outcome shows the moment the action lands,
    whatever the next listing flight does. An answer without a
    readable status still names its workspace."""
    status = reply.get("status") if isinstance(reply, dict) else None
    return f"{label} {status}" if status else label


def clone_reply_text(reply: object) -> str:
    """The clone's own flash (#1063, the TUI's #773 words): the
    new row's label beside its id — the clone made a different
    workspace than the one the operator clicked, so the source's
    label would misname it. An answer without a readable row
    still names its own word."""
    if isinstance(reply, dict) and reply.get("id"):
        return f"cloned into {workspace_label(reply)} (id {reply['id']})"
    return "cloned"


def state_text(model: WorkspaceRow) -> str:
    """The status cell's text (#939, #986): the unresolved-mounts
    marker ahead of the pending-changes marker ahead of the state
    word — the TUI status cell's shape (``✗1 ↻2 running``) — the
    word alone on a row that waits on nothing."""
    markers = " ".join(
        marker
        for marker in (model.issue_marker, pending_marker(model))
        if marker is not None
    )
    if not markers:
        return model.state
    return f"{markers} {model.state}"


class StateItem(QtWidgets.QTableWidgetItem):
    """The status cell as a sort item (#939): the markers beside
    the word, the lifecycle rank carried as the item's sort data
    — a column click orders the rows by the vocabulary's own
    sequence, a state outside it last. The cell's tooltip names
    the restart the pending marker stands for (#986): the ↻N a
    narrow cell renders, said as the sentence the strips read.
    The cell carries its kind under :data:`SEMANTIC_ROLE`
    (#1160), and the delegate paints the word in it."""

    def __init__(self, model: WorkspaceRow) -> None:
        super().__init__(state_text(model))
        self.setData(RANK_ROLE, model.state_rank)
        self.setData(SEMANTIC_ROLE, state_kind(model))
        note = restart_note(model)
        if note is not None:
            self.setToolTip(note)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(RANK_ROLE) < other.data(RANK_ROLE)


class ImageItem(QtWidgets.QTableWidgetItem):
    """The image cell (#939): the whole value under the sort
    role — an ellipsized display still sorts by the value it
    stands for."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.setData(IMAGE_ROLE, value)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(IMAGE_ROLE) < other.data(IMAGE_ROLE)


class CreatedItem(QtWidgets.QTableWidgetItem):
    """The created cell (#939): the relative label reads, the
    parsed moment sorts — a header click orders by date, never
    by the label's spelling (``10mo ago`` sits a year away from
    ``1y ago`` in text order). A missing stamp reads as the
    oldest and sinks on the newest-first read."""

    def __init__(self, model: WorkspaceRow) -> None:
        super().__init__(model.created)
        self.setData(CREATED_ROLE, model.created_rank)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(CREATED_ROLE) < other.data(CREATED_ROLE)

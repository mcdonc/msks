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

from .rows import WorkspaceRow

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


def state_text(model: WorkspaceRow) -> str:
    """The status cell's text (#939): the unresolved-mounts
    marker ahead of the state word — the TUI status cell's shape
    (``✗1 stopped``) — the word alone on a healthy row."""
    if model.issue_marker is None:
        return model.state
    return f"{model.issue_marker} {model.state}"


class StateItem(QtWidgets.QTableWidgetItem):
    """The status cell as a sort item (#939): the marker beside
    the word, the lifecycle rank carried as the item's sort data
    — a column click orders the rows by the vocabulary's own
    sequence, a state outside it last."""

    def __init__(self, model: WorkspaceRow) -> None:
        super().__init__(state_text(model))
        self.setData(RANK_ROLE, model.state_rank)

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

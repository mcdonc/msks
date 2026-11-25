"""The Images tab's button rows (#1161): the actions the two
groups' context menus hold, on screen under each table —
Import… under the published pane, Details…, Set as the default,
and Remove… under the catalog — acting on the held row and
standing disabled until one holds, the Workspaces tab's rule.
The mixin builds the four buttons, reads the held row of either
table, and syncs the buttons with the selection and the
flights' gates; the clicks route to the tab's own action
methods, the ones the menus drive. A host tab mixes this in
beside its QWidget base, supplies the two tables, the two model
lists, the gates (``progress``, ``action_pending``), and calls
:meth:`ButtonRows.sync_actions` at every flight's start and
end.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the tab
module loads it on the window path.
"""

from PySide6 import QtCore, QtWidgets

from ..listtable import ListTable
from .cells import ROW_INDEX_ROLE


class ButtonRows:
    """The two groups' button rows (#1161): a button acts on its
    group's held row and stands disabled until one holds. The
    catalog's three share the action gate — a remove or default
    flight out holds them all — and Import… stands down while
    the import's dialog holds the wait."""

    def build_buttons(self) -> None:
        """The four buttons and the tables' selection signal
        (#1161): built after the tables and the gates, before
        the layout takes the buttons — the first fill's hold
        enables them through the signal."""
        self.detail_button = QtWidgets.QPushButton("Details…", self)
        self.detail_button.clicked.connect(self.detail_held)
        self.default_button = QtWidgets.QPushButton("Set as the default", self)
        self.default_button.clicked.connect(self.default_held)
        self.remove_button = QtWidgets.QPushButton("Remove…", self)
        self.remove_button.clicked.connect(self.remove_held)
        self.import_button = QtWidgets.QPushButton("Import…", self)
        self.import_button.clicked.connect(self.import_held)
        self.catalog_table.itemSelectionChanged.connect(self.sync_actions)
        self.published_table.itemSelectionChanged.connect(self.sync_actions)
        self.sync_actions()

    def held_row(self, table: ListTable) -> int:
        """The group's held row (#1161, the Workspaces tab's
        rule): the current row's, else the first selected row's
        — a selection without a current row still holds — or
        -1 with nothing held."""
        rows = sorted({index.row() for index in table.selectedIndexes()})
        if not rows:
            return -1
        row = table.currentIndex().row()
        return row if row in rows else rows[0]

    def held_model(self, table: ListTable, models: list) -> object | None:
        """The model a group's held row carries (#1161): read
        through its first cell's index, or None with nothing
        held — a row whose first cell is gone, or carries no
        index, holds nothing."""
        item = table.item(self.held_row(table), 0)
        if item is None:
            return None
        index = item.data(ROW_INDEX_ROLE)
        return models[index] if index is not None else None

    def held_catalog(self) -> object | None:
        """The catalog row its button row acts on (#1161)."""
        return self.held_model(self.catalog_table, self.catalog_models)

    def held_published(self) -> object | None:
        """The published row Import… acts on (#1161)."""
        return self.held_model(self.published_table, self.published_models)

    @QtCore.Slot()
    def sync_actions(self) -> None:
        """The button rows' state (#1161, the Workspaces tab's
        rule): the catalog's three take a held row besides the
        action gate, and Import… takes a held row besides the
        import gate. The tables' selection signal and every
        flight's start and end ride here."""
        held = self.held_catalog() is not None and not self.action_pending
        self.detail_button.setEnabled(held)
        self.default_button.setEnabled(held)
        self.remove_button.setEnabled(held)
        self.import_button.setEnabled(
            self.held_published() is not None and self.progress is None
        )

    @QtCore.Slot()
    def detail_held(self) -> None:
        """The catalog group's Details… (#1161): the held row
        opens the detail — the menu's own path — with nothing
        held deciding nothing."""
        held = self.held_catalog()
        if held is not None:
            self.open_detail_row(held)

    @QtCore.Slot()
    def default_held(self) -> None:
        """The catalog group's Set as the default (#1161): the
        held row posts the designation — the menu's own path,
        asking for nothing, as now."""
        held = self.held_catalog()
        if held is not None:
            self.default_row(held)

    @QtCore.Slot()
    def remove_held(self) -> None:
        """The catalog group's Remove… (#1161): the held row
        removes behind the confirmation the menu keeps — with
        nothing held deciding nothing."""
        held = self.held_catalog()
        if held is not None:
            self.remove_row(held)

    @QtCore.Slot()
    def import_held(self) -> None:
        """The published group's Import… (#1161): the held row
        starts the import the menu and the activation start —
        with nothing held deciding nothing."""
        held = self.held_published()
        if held is not None:
            self.start_import(held)

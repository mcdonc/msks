"""Render pending consent rows and forward explicit verdict actions.

The presenter owns hold state. Replies retire verdict keys and report failures;
only the next presenter snapshot removes a row. This module loads with the GUI,
not with CLI argument parsing.
"""

from functools import partial

from PySide6 import QtCore, QtWidgets

from .consent import DURATION_DEFAULT, DURATIONS, ConsentPresenter
from .kinds import ERROR
from .tints import TintedLabel

#: How long before a hold times out to deny its countdown reads
#: in the error tint (#1160): the seconds left to answer.
LAST_SECONDS = 15


def countdown_kind(remaining: float):
    """The countdown's tint (#1160): the error tint under
    :data:`LAST_SECONDS`, the chrome's own text above."""
    return ERROR if remaining <= LAST_SECONDS else None


class HoldRow(QtWidgets.QWidget):
    """Stable request controls with actions that wrap at narrow widths."""

    def __init__(self, model, send, parent) -> None:
        super().__init__(parent)
        self.dest = QtWidgets.QLabel(model.dest, self)
        self.dest.setObjectName(f"destination-{model.id}")
        self.dest.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.dest.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.dest.setToolTip(model.dest)
        self.dest.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.countdown = TintedLabel(model.countdown, self)
        self.countdown.setObjectName(f"countdown-{model.id}")
        self.countdown.set_kind(countdown_kind(model.remaining))
        self.countdown.setAccessibleName(f"Time remaining for {model.dest}")
        self.actions = QtWidgets.QWidget(self)
        buttons = QtWidgets.QHBoxLayout(self.actions)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.allow = self.action("allow", model, send)
        buttons.addWidget(self.allow)
        buttons.addWidget(self.action("deny", model, send))
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setColumnStretch(0, 1)
        self.grid.addWidget(self.dest, 0, 0)
        self.grid.addWidget(self.countdown, 0, 1)
        self.wrapped = None
        self.reflow(800)

    def action(self, decision, model, send):
        """Send the default on a main click or the chosen menu duration."""
        button = QtWidgets.QToolButton(self.actions)
        button.setText(decision.title())
        button.setObjectName(f"{decision}-{model.id}")
        button.setAccessibleName(f"{decision.title()} {model.dest}")
        button.setToolTip(
            f"{decision.title()} until restart, or choose a duration"
        )
        button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup
        )
        menu = QtWidgets.QMenu(button)
        menu.setAccessibleName(
            f"{decision.title()} {model.dest} for a duration"
        )
        for duration in DURATIONS:
            choice = menu.addAction(duration)
            choice.triggered.connect(partial(send, decision, duration))
        button.setMenu(menu)
        button.clicked.connect(partial(send, decision, DURATION_DEFAULT))
        return button

    def reflow(self, width: int) -> None:
        """Keep actions readable when destination space becomes narrow."""
        needed = (
            self.actions.sizeHint().width() + self.countdown.sizeHint().width()
        )
        wrap = width < needed + self.fontMetrics().horizontalAdvance("m" * 24)
        if wrap == self.wrapped:
            return
        self.wrapped = wrap
        self.grid.removeWidget(self.actions)
        if wrap:
            self.grid.addWidget(
                self.actions, 1, 0, 1, 2, QtCore.Qt.AlignmentFlag.AlignRight
            )
        else:
            self.grid.addWidget(self.actions, 0, 2)
        self.grid.invalidate()


class ConsentPanel(QtWidgets.QWidget):
    """A pending-only section over an injected presenter and bridge."""

    focus_exhausted = QtCore.Signal()

    def __init__(self, bridge, parent=None) -> None:
        super().__init__(parent)
        self.presenter: ConsentPresenter | None = None
        self.epoch = 0
        self.flash_seq = 0
        self.cells: dict[str, HoldRow] = {}
        self.heading = QtWidgets.QLabel("Pending connections", self)
        self.scroll = QtWidgets.QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.column = QtWidgets.QWidget(self.scroll)
        self.rows_layout = QtWidgets.QVBoxLayout(self.column)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.column)
        self.column.installEventFilter(self)
        self.note = TintedLabel("", self)
        self.note.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        self.note.hide()
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.scroll)
        layout.addWidget(self.note)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Maximum,
        )
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        self.hide()

    def host(self, presenter: ConsentPresenter | None) -> None:
        """Replace the presenter and invalidate the old visit's callbacks."""
        self.presenter = presenter
        self.epoch += 1
        self.flash_seq += 1
        self.note.clear()
        self.note.hide()
        for cell in self.cells.values():
            self.remove_cell(cell)
        self.cells.clear()
        self.refresh()

    def refresh(self) -> None:
        """Render the snapshot while preserving surviving controls."""
        models = self.presenter.rows() if self.presenter is not None else []
        ids = [model.id for model in models]
        focused, position = self.focused_position()
        for rid in list(self.cells):
            if rid not in ids:
                self.remove_cell(self.cells.pop(rid))
        self.sync_rows(models)
        self.setVisible(bool(models))
        self.resize_rows()
        self.restore_focus(focused, position, ids)

    def remove_cell(self, cell) -> None:
        """Retire controls from the widget tree before deferred destruction."""
        self.rows_layout.removeWidget(cell)
        cell.hide()
        cell.setParent(None)
        cell.deleteLater()

    def sync_rows(self, models) -> None:
        """Reuse widgets by id and retain the presenter's ordering."""
        for index, model in enumerate(models):
            if model.id not in self.cells:
                send = partial(
                    self.decide, self.presenter, self.epoch, model.id
                )
                self.cells[model.id] = HoldRow(model, send, self.column)
            cell = self.cells[model.id]
            cell.dest.setText(model.dest)
            cell.dest.setToolTip(model.dest)
            cell.countdown.setText(model.countdown)
            cell.countdown.set_kind(countdown_kind(model.remaining))
            self.rows_layout.insertWidget(index, cell)
            cell.show()

    def focused_position(self):
        """Find the focused request and its position in the visible
        order. A row's open menu needs no lookup of its own: the
        button keeps the focus while its menu stands (a QMenu is
        its own window, never a child of the row), so the focus
        check below already names the row the menu belongs to."""
        focus = QtWidgets.QApplication.focusWidget()
        for rid, cell in self.cells.items():
            if cell.isAncestorOf(focus):
                return rid, self.rows_layout.indexOf(cell)
        return None, 0

    def restore_focus(self, focused, position, ids) -> None:
        """Only removal of a focused row can change the keyboard's owner."""
        if focused is None or focused in ids:
            return
        if ids:
            target = ids[min(position, len(ids) - 1)]
            self.cells[target].allow.setFocus()
        else:
            self.focus_exhausted.emit()

    def resize_rows(self) -> None:
        """Bound the queue to three readable rows, with the rest scrollable."""
        for cell in self.cells.values():
            cell.reflow(self.scroll.viewport().width())
        heights = [cell.sizeHint().height() for cell in self.cells.values()]
        spacing = self.rows_layout.spacing()
        self.scroll.setFixedHeight(
            sum(heights[:3]) + max(0, min(3, len(heights)) - 1) * spacing
        )

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.LayoutRequest:
            self.resize_rows()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_rows()

    def decide(
        self, presenter, epoch, rid, decision, duration, checked=False
    ) -> None:
        """A stale menu never answers another visit's request."""
        if presenter is not self.presenter or epoch != self.epoch:
            return
        if not any(row.id == rid for row in presenter.rows()):
            return
        presenter.decide(rid, decision, duration)

    def on_succeeded(self, key: str, result: object) -> None:
        if self.presenter is not None and key in self.presenter.decide_keys:
            self.presenter.retire(key)

    def on_failed(self, key: str, error: object) -> None:
        if self.presenter is None or key not in self.presenter.decide_keys:
            return
        self.presenter.retire(key)
        self.flash_seq += 1
        seq = self.flash_seq
        self.note.say(f"decide failed: {error}", ERROR)
        self.note.show()
        QtCore.QTimer.singleShot(4000, self, partial(self.clear_stale, seq))

    def clear_stale(self, seq: int) -> None:
        if seq == self.flash_seq:
            self.note.say("")
            self.note.hide()

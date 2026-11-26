"""The window's settle: the content stays clear until it stands
in its final shape and the window's first frame is up, then
shows at once and the tables' rows rise from the top down,
each a few milliseconds behind the row above it. Every tab
change settles the page it shows the same way — the main
strip's pages behind the gate, the workspace page's own tabs
with the rise alone. The opening runs once per window — a
connection pick's re-dial rebuilds the content tree without
it — and a listing's later refills rebuild rows in place.
"""

from PySide6 import QtCore, QtGui, QtWidgets

#: The rows' rise: a step per row, capped so a long listing
#: settles within ``STAGGER_MS``, each row's own rise lasting
#: ``RISE_MS`` from its step and lifting ``RISE_PX`` pixels it
#: undoes as it comes.
ROW_STEP_MS = 45
STAGGER_MS = 450
RISE_MS = 160
RISE_PX = 4
#: The gate (#1177): the content stays clear until the bridge
#: has no call in flight and then a quiet spell passes without
#: a layout change under the content — the pages clear their
#: tables on show and refill them a moment later, and the
#: columns and headers move with the refill. The cap shows the
#: content regardless, so a slow daemon cannot leave the window
#: blank.
QUIET_MS = 50
SETTLE_CAP_MS = 1500


def rise(elapsed: int) -> float:
    """One row's progress from its step (``elapsed`` ms since
    the step, negative before it): clear at zero, opaque at
    one, easing out in between."""
    if elapsed <= 0:
        return 0.0
    if elapsed >= RISE_MS:
        return 1.0
    linear = elapsed / RISE_MS
    return 1.0 - (1.0 - linear) ** 2


def cell_widgets(table: QtWidgets.QTableWidget, row: int):
    """The index widgets a row carries — the action cells'
    buttons — in column order."""
    model = table.model()
    for column in range(table.columnCount()):
        widget = table.indexWidget(model.index(row, column))
        if widget is not None:
            yield widget


def visible_tables(content: QtWidgets.QWidget):
    """The tables under ``content`` that stand visible; a table
    under a hidden page of a stacked page — the workspace
    page's list behind its detail page — takes no part."""
    for table in content.findChildren(QtWidgets.QTableWidget):
        if table.isVisible():
            yield table


class Settle(QtCore.QObject):
    """The gate over ``content``: :meth:`hold` clears it now
    and shows it once it stands in its final shape — the
    bridge running idle (every refresh the show asked has
    landed and the pages took their shape), then a quiet spell
    with no layout change under the content, and, at the
    opening through :meth:`after_first_paint`, the window's
    first paint pass having returned. The first paint of a
    fresh process loads the style and its fonts and can hold
    the GUI thread for half a second; the content shows after
    it, never in the middle of it. The cap (``SETTLE_CAP_MS``)
    shows the content whatever the gate still waits on.

    The clear is an opacity effect at zero on the content, not
    the window: a window-level fade is ignored under Wayland,
    and the frame maps at once while the pages settle. The
    effect renders the subtree through a pixmap, so it comes
    off at the show — the terminals paint straight from then
    on. A bridge left out gates nothing but the quiet spell
    and the paint.
    """

    def __init__(self, content: QtWidgets.QWidget, bridge=None) -> None:
        super().__init__(content)
        self.content = content
        self.bridge = bridge
        self.window: QtWidgets.QWidget | None = None
        self.effect: QtWidgets.QGraphicsOpacityEffect | None = None
        self.waiting = False
        self.painted = True
        self.on_idle = False
        self.quiet = QtCore.QTimer(self)
        self.quiet.setSingleShot(True)
        self.quiet.setInterval(QUIET_MS)
        self.quiet.timeout.connect(self.quiet_passed)
        self.cap = QtCore.QTimer(self)
        self.cap.setSingleShot(True)
        self.cap.setInterval(SETTLE_CAP_MS)
        self.cap.timeout.connect(self.show)

    def after_first_paint(self, window: QtWidgets.QWidget) -> None:
        """The opening: the gate held also on the window's
        first paint pass — the frame is on its way to the
        screen when it returns, and the pages show nothing
        before it."""
        self.window = window
        self.hold()

    def hold(self) -> None:
        """Clear the content now and show it when it stands in
        its final shape; a hold under way starts over. The
        bridge's flights are read a loop iteration later: the
        refreshes the show asked for are issued in this same
        synchronous run, whichever slot order the strip
        delivers them in."""
        self.release_gate()
        self.clear()
        self.waiting = True
        self.painted = self.window is None
        self.on_idle = False
        if self.window is not None:
            self.window.installEventFilter(self)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.cap.start()
        QtCore.QTimer.singleShot(0, self.gate)

    def clear(self) -> None:
        """The content at opacity zero, under the effect."""
        if self.effect is None:
            self.effect = QtWidgets.QGraphicsOpacityEffect(self.content)
            self.content.setGraphicsEffect(self.effect)
        self.effect.setOpacity(0.0)

    def gate(self) -> None:
        """The bridge's turn: a flight in flight holds the
        show until ``idle``; none starts the quiet spell. The
        connection goes on before the count is read — the
        count drops on the bridge's worker thread, and an
        ``idle`` raised between a read and a connect would be
        missed."""
        if not self.waiting:
            return
        if self.bridge is not None:
            self.bridge.idle.connect(self.idle)
            if self.bridge.in_flight > 0:
                self.on_idle = True
                return
            self.bridge.idle.disconnect(self.idle)
        self.quiet.start()

    def idle(self) -> None:
        if self.bridge is not None:
            self.bridge.idle.disconnect(self.idle)
        self.on_idle = False
        self.quiet.start()

    def quiet_passed(self) -> None:
        if self.waiting and self.painted and not self.on_idle:
            self.show()

    def eventFilter(self, watched, event) -> bool:
        if self.first_paint(watched, event):
            return False
        if event.type() in (
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.LayoutRequest,
        ):
            self.hold_quiet(watched)
        return False

    def first_paint(self, watched, event) -> bool:
        """The window's first paint pass, the opening's last
        hold: the frame is on its way to the screen once the
        pass returns, and the zero-interval timer lands on the
        loop iteration after it."""
        if (
            self.window is None
            or watched is not self.window
            or event.type() != QtCore.QEvent.Type.Paint
        ):
            return False
        self.window.removeEventFilter(self)
        self.painted = True
        QtCore.QTimer.singleShot(0, self.quiet_passed)
        return True

    def hold_quiet(self, watched) -> None:
        """A resize or a layout request under the content while
        the gate waits starts the quiet spell over: the content
        shows only once nothing under it moves for a spell."""
        if not self.waiting:
            return
        inside = watched is self.content or (
            isinstance(watched, QtWidgets.QWidget)
            and self.content.isAncestorOf(watched)
        )
        if inside:
            self.quiet.start()

    def show(self) -> None:
        """The content shown: the gate's watchers off, the
        effect off the content."""
        self.release_gate()
        if self.effect is not None:
            self.content.setGraphicsEffect(None)
            self.effect = None

    def release_gate(self) -> None:
        """The gate's watchers off: the window's paint filter,
        the application's layout filter, the bridge's idle, and
        both timers."""
        self.waiting = False
        self.quiet.stop()
        self.cap.stop()
        if self.window is not None:
            self.window.removeEventFilter(self)
        app = QtWidgets.QApplication.instance()
        app.removeEventFilter(self)
        if self.on_idle and self.bridge is not None:
            self.bridge.idle.disconnect(self.idle)
            self.on_idle = False


class StaggerDelegate(QtWidgets.QStyledItemDelegate):
    """The rows' painter while a table staggers: each cell
    paints through the table's own delegate at its row's
    progress — clear at zero, opaque and in place at one, the
    rise of ``RISE_PX`` pixels undone by the progress. A cell
    under a progress of zero paints nothing."""

    def __init__(self, original, stagger) -> None:
        super().__init__(stagger.table)
        self.original = original
        self.stagger = stagger

    def paint(self, painter: QtGui.QPainter, option, index) -> None:
        progress = self.stagger.progress_of(index.row())
        if progress <= 0.0:
            return
        painter.save()
        painter.setOpacity(progress)
        painter.translate(0, round((1.0 - progress) * RISE_PX))
        self.original.paint(painter, option, index)
        painter.restore()

    def sizeHint(self, option, index) -> QtCore.QSize:
        return self.original.sizeHint(option, index)


class RowStagger(QtCore.QObject):
    """A table's rows, top-down: every row painted clear at
    once, then rising one per step once the table first paints
    — a page held clear behind the gate keeps its tables from
    painting until it shows, so a clock started at the landing
    runs out unseen. The rows stay in place and the view's
    state stands (the listing's hold reads its rows through
    the selection, which hiding rows would blank), so the
    clear comes from painting: the wrapping delegate for the
    cells, an opacity effect for each action cell's own widget.
    An empty table waits for its rows to land. The step shrinks
    for a long listing so the whole table stands within
    ``STAGGER_MS``.
    """

    def __init__(self, table: QtWidgets.QTableWidget) -> None:
        super().__init__(table)
        self.table = table
        self.rows = table.rowCount()
        self.progress: list[float] = [0.0] * self.rows
        self.step = ROW_STEP_MS
        self.original = table.itemDelegate()
        self.delegate = StaggerDelegate(self.original, self)
        table.setItemDelegate(self.delegate)
        self.effects: dict[
            QtWidgets.QWidget, QtWidgets.QGraphicsOpacityEffect
        ] = {}
        self.anim: QtCore.QVariantAnimation | None = None
        self.live = True
        table.model().rowsInserted.connect(self.rows_landed)
        self.read_rows()
        if self.rows > 0:
            self.arm_cells()
            table.viewport().installEventFilter(self)

    def read_rows(self) -> None:
        self.rows = self.table.rowCount()
        self.progress = [0.0] * self.rows
        self.step = max(1, min(ROW_STEP_MS, STAGGER_MS // max(self.rows, 1)))

    def rows_landed(self, *_args) -> None:
        """The page's refill landed its rows: read them and arm
        their cells, and wait for the table's paint when the
        clock has not started — an empty table armed at the
        tab change gets its rows this way; a table refilled
        under a running stagger has the new rows join the
        clock."""
        self.read_rows()
        self.arm_cells()
        if self.anim is None:
            self.table.viewport().installEventFilter(self)

    def progress_of(self, row: int) -> float:
        if 0 <= row < len(self.progress):
            return self.progress[row]
        return 1.0

    def arm_cells(self) -> None:
        """Every action cell's widget at its row's progress —
        a fresh widget (a refill rebuilt the row) gets its
        effect here, and a widget the refill dropped leaves
        the set with its row."""
        armed = {}
        for row in range(self.rows):
            for widget in cell_widgets(self.table, row):
                effect = self.effects.get(widget)
                if effect is None:
                    effect = QtWidgets.QGraphicsOpacityEffect(widget)
                    widget.setGraphicsEffect(effect)
                effect.setOpacity(self.progress[row])
                armed[widget] = effect
        self.effects = armed

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QtCore.QEvent.Type.Paint:
            # The cells armed before this paint: a row that
            # landed after the arm carries widgets the arm has
            # not seen, and they would show for this one frame.
            watched.removeEventFilter(self)
            self.arm_cells()
            self.start()
        return False

    def start(self) -> None:
        """The clock over the whole stagger: each row's rise
        starts ``step`` after the one above it."""
        if self.anim is not None:
            self.anim.stop()
            self.anim.deleteLater()
        anim = QtCore.QVariantAnimation(self)
        anim.setDuration(self.step * self.rows + RISE_MS)
        anim.setStartValue(0)
        anim.setEndValue(anim.duration())
        anim.valueChanged.connect(self.tick)
        anim.finished.connect(self.landed)
        self.anim = anim
        anim.start()

    def tick(self, elapsed: int) -> None:
        self.progress = [
            rise(int(elapsed) - row * self.step) for row in range(self.rows)
        ]
        self.arm_cells()
        self.table.viewport().update()

    def landed(self) -> None:
        self.live = False
        self.release()
        self.anim.deleteLater()
        self.anim = None
        self.delegate.deleteLater()
        self.deleteLater()

    def release(self) -> None:
        """The table back on its own delegate, the action cells
        without their effects."""
        self.table.setItemDelegate(self.original)
        for widget in self.effects:
            widget.setGraphicsEffect(None)
        self.effects = {}
        self.table.viewport().update()

    def rewind(self) -> None:
        """A stagger under way or still waiting, asked again:
        every row back to clear and the clock over from the
        start; a table still empty keeps waiting for its
        rows."""
        self.read_rows()
        if self.rows == 0:
            return
        self.arm_cells()
        self.start()


def stagger_rows(table: QtWidgets.QTableWidget) -> None:
    """A table's rows, clear now and rising from its first
    paint; a stagger still under way or waiting on the table
    rewinds. An empty table waits for its rows."""
    for running in table.findChildren(RowStagger):
        if running.live:
            running.rewind()
            return
    RowStagger(table)


def stagger_page(page: QtWidgets.QWidget) -> None:
    """The rows of every table the page shows (#1177): clear
    now, rising from the page's first paint."""
    for table in visible_tables(page):
        stagger_rows(table)


def stagger_on_change(strip: QtCore.QObject, page_of) -> None:
    """A strip's tab changes settle the page each shows
    (#1177): the rows of the page's visible tables rise from
    its first paint. ``page_of(index)`` answers the page a
    tab shows — a tab widget's ``widget``, or a bar reading
    its stack. The ask lands a loop iteration after the
    change: the page shows on the same signal, and which of
    its tables stand visible reads true only then."""

    def settled(index: int) -> None:
        page = page_of(index)
        if page is not None:
            QtCore.QTimer.singleShot(0, lambda: stagger_page(page))

    strip.currentChanged.connect(settled)

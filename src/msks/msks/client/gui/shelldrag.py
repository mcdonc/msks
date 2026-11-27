"""The shell tabs' drag gestures (#1203): a left-drag on a strip's bar
carries a tab as a Qt drag-and-drop — the tab's own image under the
pointer — that every strip of the workspace accepts. Along the strip,
the other tabs make room as the pointer passes their midpoints, and
the tab takes the place it is released at. Released off every strip,
the tab tears off into a window of its own (:mod:`.shellwindow`),
placed under the pointer where the desktop lets a client place a
window. Released over another strip — the page's, or a torn-off
window's — the strip shows where the shell will land while the
pointer hovers, an accent line the bar paints, and the shell seats
there at that place. A drag the desktop cancels, as Escape does,
returns the tab to the place the press found it.

The drag is Qt's own :class:`QtGui.QDrag` rather than a pointer
carried by hand. On Wayland a client knows no screen-wide pointer
position, so a target found by global coordinates is never found;
the desktop routes a drag-and-drop to the surface under the pointer
and hands each target its own local position, on every platform. The
source bar sees the drag too, so the reorder along it stays live. A
left-click that never moves past the drag distance stays the click
it always was, and the double-click still renames. The strips the
tab leaves and takes change only after the drag ends, and a window
that loses its last shell under the drag waits for the drag's end to
close: the drag's source bar sits in it, under the desktop's loop.
A cancelled drag is told apart from a release no strip took by the
Escape press itself, read through a filter the controller stands on
the application inside the drag's loop — the desktop sends the
source bar a leave on a cancel as on any departure, so the drag's
events alone cannot tell the two apart.

The release index counts the tabs whose midpoint the pointer has
passed, the dragged tab itself left out. Tabs of different widths
would otherwise trade places under a pointer resting on one spot —
each swap moving a different tab under it — and the midpoint rule
leaves a band between a neighbor's two midpoints (before and after
a swap) where a still pointer flips nothing.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time.
"""

from functools import partial

from PySide6 import QtCore, QtGui, QtWidgets

#: The insertion marker's width, pixels (#1203): the accent line
#: the bar paints where a dragged shell will land.
MARKER_PX = 2

#: The drag's mime format (#1203): the workspace and the session's
#: number, ``workspace:number`` in UTF-8 — a strip of another
#: workspace, or another application, refuses the drag.
MIME_FORMAT = "application/x-msks-shell"

LEFT = QtCore.Qt.MouseButton.LeftButton
MOVE = QtCore.Qt.DropAction.MoveAction


def shell_mime(workspace_id: str, no: int) -> QtCore.QMimeData:
    """The drag's payload (#1203): the workspace and the session's
    number."""
    data = QtCore.QMimeData()
    data.setData(MIME_FORMAT, f"{workspace_id}:{no}".encode())
    return data


def mime_shell(data) -> tuple[str, int] | None:
    """The payload's workspace and number, or None for a drag that
    carries no shell (#1203) — another application's, or a malformed
    one."""
    if not data.hasFormat(MIME_FORMAT):
        return None
    text = bytes(data.data(MIME_FORMAT)).decode(errors="replace")
    workspace_id, _, number = text.partition(":")
    if not workspace_id or not number.isdigit():
        return None
    return workspace_id, int(number)


def make_drag(source) -> QtGui.QDrag:
    """The drag over a source widget (#1203): the one seam the suite
    stands in for, the real drag's loop being the desktop's."""
    return QtGui.QDrag(source)


def drop_index(bar, x: int, dragged: int | None, count: int) -> int:
    """Where a release at ``x`` lands the carried tab among the
    strip's first ``count`` tabs (#1203): the number of those tabs
    whose midpoint the pointer has passed, the tab at ``dragged``
    — the one already in the strip and moving — left out of the
    count."""
    index = 0
    for i in range(count):
        if i != dragged and bar.tabRect(i).center().x() < x:
            index += 1
    return index


class ShellDrag(QtCore.QObject):
    """The drag controller over one hub's strips (#1203): every
    strip registers its bar as it builds (:meth:`watch`), the bars
    take drops, and the press that may become a drag is the state
    below. The drop's effect — a reorder, a move between strips —
    is kept in ``pending`` and applied once the drag's own loop
    ended (:meth:`ended`)."""

    #: The bar events the controller reads (#1203), each with its
    #: handler; every other event on a bar is the bar's own.
    HANDLERS = {
        QtCore.QEvent.Type.MouseButtonPress: "press",
        QtCore.QEvent.Type.MouseMove: "move",
        QtCore.QEvent.Type.MouseButtonRelease: "release",
        QtCore.QEvent.Type.DragEnter: "hover",
        QtCore.QEvent.Type.DragMove: "hover",
        QtCore.QEvent.Type.DragLeave: "left",
        QtCore.QEvent.Type.Drop: "drop",
    }

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self.hub = hub
        self.strips: list = []
        #: The strip a left press landed on a shell tab of, with
        #: the session and the pointer's place in the tab — the
        #: drag's source; None once the drag ended or none stands.
        self.source = None
        self.pressed = None
        self.grab = QtCore.QPoint()
        self.origin = QtCore.QPoint()
        #: The tab's place when the press landed on it (#1203): a
        #: cancelled drag returns the tab there.
        self.home = -1
        #: True from the drag's start to its end: the moves are the
        #: drag's now, not the pointer's, and a window losing its
        #: last shell meanwhile waits for the end to close.
        self.dragging = False
        #: True once the Escape press that cancels the drag reached
        #: the application filter (:meth:`watch_cancel`).
        self.cancelled = False
        #: The application while the cancel filter stands on it,
        #: None otherwise.
        self.cancel_watch = None
        #: The windows waiting out a drag to close (#1203): each
        #: lost its last shell while the drag ran.
        self.held: list = []
        #: The drop a strip took (#1203), applied after the drag
        #: ended: the strip to seat the shell in, or None for the
        #: source's own reorder.
        self.pending = None

    def watch(self, strip) -> None:
        """A strip joins the controller (#1203): its bar takes the
        drag's drops, its events reach the filter, and the strip
        leaves again when Qt deletes it — a torn-off window going
        down with its last shell."""
        strip.tabBar().setAcceptDrops(True)
        strip.tabBar().installEventFilter(self)
        self.strips.append(strip)
        strip.destroyed.connect(partial(self.forget, strip))

    def forget(self, strip) -> None:
        """A strip's teardown (#1203): the controller drops it, and
        a press standing on it ends with it."""
        self.strips = [s for s in self.strips if s is not strip]
        if self.source is strip:
            self.source = self.pressed = None

    def strip_of(self, bar):
        """The strip whose bar ``bar`` is, or None for a bar the
        controller no longer watches."""
        for strip in self.strips:
            if strip.tabBar() is bar:
                return strip
        return None

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """The watched bar's events take their handlers (#1203);
        the application's, while the cancel filter stands, are read
        for the Escape press alone; every other event passes."""
        if obj is self.cancel_watch:
            return self.read_cancel(event)
        name = self.HANDLERS.get(event.type())
        if name is None:
            return False
        strip = self.strip_of(obj)
        if strip is None:
            return False
        return getattr(self, name)(strip, event)

    def read_cancel(self, event) -> bool:
        """Whether this is the Escape press that cancels the drag
        (#1203): noted, never consumed — the desktop's own drag
        filter reads it next."""
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Escape
        ):
            self.cancelled = True
        return False

    def watch_cancel(self) -> None:
        """Stand the cancel filter on the application (#1203), from
        inside the drag's loop: the desktop's drag machinery stood
        its own filter first, and Qt runs the newest filter first,
        so this one reads the Escape press before that one consumes
        it."""
        # The application stands by construction: the drag started
        # from a mouse event it delivered.
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        self.cancel_watch = app

    def unwatch_cancel(self) -> None:
        """Take the cancel filter off the application (#1203) once
        the drag's loop ended."""
        if self.cancel_watch is not None:
            self.cancel_watch.removeEventFilter(self)
            self.cancel_watch = None

    # --- the press that becomes a drag ---------------------------------

    def press(self, strip, event) -> bool:
        """A left press on a shell tab is noted and passed on
        (#1203): the click it is today — the bar selects the tab —
        until the pointer moves past the drag distance. The "+" and
        the bar's blank stretch name no tab, and a press under a
        running drag is the drag's."""
        if self.dragging or event.button() != LEFT:
            return self.dragging
        index = strip.tabBar().tabAt(event.position().toPoint())
        if not 0 <= index < strip.shell_count():
            return False
        self.source = strip
        self.pressed = strip.session_at(index)
        self.home = index
        self.grab = (
            event.position().toPoint()
            - strip.tabBar().tabRect(index).topLeft()
        )
        self.origin = event.globalPosition().toPoint()
        return False

    def move(self, strip, event) -> bool:
        """A move with the button standing (#1203): past the drag
        distance the press becomes the drag; before it, and with
        the pressed shell gone, the move is the pointer's own."""
        if self.pressed is None or not event.buttons() & LEFT:
            return False
        if not self.armed():
            self.source = self.pressed = None
            return False
        global_ = event.globalPosition().toPoint()
        if (global_ - self.origin).manhattanLength() < (
            QtWidgets.QApplication.startDragDistance()
        ):
            return False
        self.start(strip)
        return True

    def armed(self) -> bool:
        """Whether the pressed shell still stands (#1203): its tab
        can close under the pointer, and a closed shell's widget is
        gone."""
        return (
            self.hub.find(self.source.hosted, self.pressed.no) is self.pressed
        )

    def release(self, strip, event) -> bool:
        """A release (#1203): the press is over — a click that never
        dragged, or the drag's own end already handled by
        :meth:`ended`. The release is the bar's."""
        if event.button() == LEFT:
            self.source = self.pressed = None
        return False

    def start(self, strip) -> None:
        """The press became the drag (#1203): the dragged tab
        current, the drag carrying the shell's mime and the tab's
        own image under the pointer, the cancel filter standing on
        the application from inside the loop, and the desktop's
        loop running until the release; the action it answers
        settles the drag."""
        self.dragging = True
        self.cancelled = False
        bar = strip.tabBar()
        strip.setCurrentWidget(self.pressed.widget)
        drag = make_drag(bar)
        drag.setMimeData(shell_mime(strip.hosted, self.pressed.no))
        rect = bar.tabRect(strip.indexOf(self.pressed.widget))
        drag.setPixmap(bar.grab(rect))
        drag.setHotSpot(self.grab)
        self.pending = None
        QtCore.QTimer.singleShot(0, self, self.watch_cancel)
        action = drag.exec(MOVE)
        self.unwatch_cancel()
        self.ended(action)

    def ended(self, action) -> None:
        """The drag's loop ended (#1203): a drop a strip took is
        applied now — the window that loses its shell may close,
        and the drag's source stays whole until this point — a
        drag no strip took is settled without one
        (:meth:`unplaced`), and a window that lost its last shell
        under the drag closes."""
        strip, session, pending = self.source, self.pressed, self.pending
        home, cancelled = self.home, self.cancelled
        self.clear()
        if action == MOVE and pending is not None:
            pending()
        else:
            self.unplaced(strip, session, home, cancelled)
        self.let_go()

    def unplaced(self, strip, session, home: int, cancelled: bool) -> None:
        """No strip took the drag (#1203): a cancelled one returns
        its tab to the place the press found; any other tears off
        into a window placed at the pointer where the desktop lets
        a client place a window. A shell gone meanwhile moves
        nothing."""
        if (
            strip is None
            or self.hub.find(strip.hosted, session.no) is not session
        ):
            return
        if cancelled:
            strip.restore_tab(session, home)
            return
        pointer = QtGui.QCursor.pos()
        strip.tear_off(session.no, at=pointer - self.grab)

    def let_go(self) -> None:
        """The drag ended (#1203): every window that waited it out
        closes if it still holds nothing."""
        held, self.held = self.held, []
        for window in held:
            window.shell_gone(window.workspace_id, 0)

    def hold(self, window) -> None:
        """A window that lost its last shell while a drag ran
        (#1203) waits for the drag's end to close: its bar may be
        the drag's source, under the desktop's own loop."""
        self.held.append(window)

    def clear(self) -> None:
        """The drag's state cleared (#1203) and every marker off."""
        self.source = self.pressed = self.pending = None
        self.dragging = False
        self.home = -1
        self.cancelled = False
        for watched in self.strips:
            watched.tabBar().insertion = None

    # --- the drag over a strip -----------------------------------------

    def shell_under(self, strip, event):
        """The session the drag carries when this strip may take it
        (#1203): the payload names a standing shell of the hosted
        workspace — a strip showing another workspace, or one whose
        bar is hidden, refuses the drag — and the action is a move;
        None otherwise."""
        if not event.proposedAction() & MOVE:
            return None
        if not self.shown(strip):
            return None
        payload = mime_shell(event.mimeData())
        if payload is None or payload[0] != strip.hosted:
            return None
        return self.hub.find(*payload)

    def shown(self, strip) -> bool:
        """Whether the strip may take a drop (#1203): its workspace
        hosted and its bar visible — the page on its Shells tab, a
        torn-off window up."""
        return bool(strip.hosted) and strip.tabBar().isVisible()

    def hover(self, strip, event) -> bool:
        """The drag entering or moving over a bar (#1203): over its
        own source the tab moves among the others live — they make
        room — and over another strip the bar shows where the shell
        will land."""
        session = self.shell_under(strip, event)
        if session is None:
            event.ignore()
            return True
        event.acceptProposedAction()
        bar = strip.tabBar()
        if strip is self.source:
            bar.insertion = None
            self.shift(strip, session, event.position().toPoint().x())
        else:
            bar.insertion = drop_index(
                bar, event.position().toPoint().x(), None, strip.shell_count()
            )
        return True

    def left(self, strip, event) -> bool:
        """The drag left a bar (#1203): its marker goes off."""
        strip.tabBar().insertion = None
        return True

    def shift(self, strip, session, x: int) -> None:
        """The dragged tab to the place the pointer names (#1203):
        the bar moves it among the shell tabs, the others making
        room, and the tab stays current through the moves."""
        bar = strip.tabBar()
        current = strip.indexOf(session.widget)
        target = drop_index(bar, x, current, strip.shell_count())
        if target != current:
            bar.moveTab(current, target)
            strip.setCurrentWidget(session.widget)

    def drop(self, strip, event) -> bool:
        """The release over a bar (#1203): the drop is taken — the
        strip's own reorder, or the shell seated here at the marked
        place — and applied once the drag's loop ended."""
        session = self.shell_under(strip, event)
        if session is None:
            event.ignore()
            return True
        event.acceptProposedAction()
        bar = strip.tabBar()
        bar.insertion = None
        if strip is self.source:
            self.pending = partial(strip.stand_order, session)
            return True
        index = drop_index(
            bar, event.position().toPoint().x(), None, strip.shell_count()
        )
        self.pending = partial(strip.dock, session.no, index)
        return True

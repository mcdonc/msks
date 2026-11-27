"""The dictate pill (#1214): the small floating control overlaid
at the top-right corner of every window the GUI displays — the
main window on every tab, and each torn-off shell window (#1203)
— one widget, identical everywhere, in no layout. Dialogs carry
none: the create and edit forms, the progress boxes, the pickers,
and the message boxes are short-lived and modal, and a recording
toggle on a form that is itself answering a question reads as a
control of that form. :class:`PillHost` installs one on each
main window the application polishes; the pill anchors itself on
its window's resize and reads the one :class:`.dictation.Dictation`
every pill shares.

The states and their faces (#1214):

- idle: the mic glyph in the chrome's own text color.
- recording: the glyph in the record red (the error tint) with a
  running timer, ``0:12``, and a small ``x`` beside it. A click
  finishes; ``x`` discards.
- transcribing: an ellipsis in place of the timer. A click
  abandons and starts again; ``x`` discards the pending
  transcript.
- done: ``copied`` for two seconds. failed: the reason for four
  seconds, the desktop notification carrying it too.
- no model yet: the download glyph; a click opens the settings
  page.

The pill keeps one width in every state — its recording shape —
so a recording's timer never moves the pill under the pointer
and the terminal-only corner (#1152) parks beside a fixed edge.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .dictation import (
    DONE,
    FAILED,
    IDLE,
    NOMODEL,
    RECORDING,
    TRANSCRIBING,
)
from .kinds import ERROR, WARN
from .tints import semantic

#: The pill's object name (#1214): how the terminal-only corner
#: and the suite find it on a window.
PILL_NAME = "dictate-pill"

#: The corner's inset (#1214): the gap between the pill and the
#: window's top and right edges, and the gap the terminal-only
#: corner keeps from it.
MARGIN = 8

#: The glyph's box and the discard button's, pixels.
GLYPH_W, GLYPH_H = 12, 16
CLOSE_W = 16
PAD_X = 7
GAP = 4
HEIGHT = 26

#: The recording timer's widest text (#1214): the cap is three
#: minutes, so no count is wider than this.
WIDEST_TIMER = "● 2:59"


MIC = "mic"
DOWNLOAD = "download"


def pill_room(font: QtGui.QFont) -> int:
    """The room a page keeps clear of the pill (#1214): its width
    plus the margin, for a page whose own controls run under the
    top-right corner — the settings form's picker arrow, for
    one."""
    return DictationPill.widest(font) + MARGIN


def pill_in(window: QtWidgets.QWidget) -> DictationPill | None:
    """The pill a window carries (#1214), or None when the window
    shows none — the terminal-only corner asks before it parks.
    A direct child alone: a dialog's pill is the dialog's own,
    and a parent window's recursive walk would find that one
    under a child dialog and answer it as the window's."""
    for child in window.children():
        if isinstance(child, DictationPill):
            return child
    return None


class Glyph(QtWidgets.QWidget):
    """The mic or the download arrow (#1214), painted from the
    palette so it follows a scheme switch: the mic in the chrome's
    text color, or in a state's tint; the download arrow in
    idle's color."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(GLYPH_W, GLYPH_H)
        self.shape = MIC
        self.kind = None

    def paintEvent(self, _event) -> None:  # noqa: N802
        color = semantic(self.kind, self.palette())
        if color is None:
            color = self.palette().color(QtGui.QPalette.ColorRole.WindowText)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(color, 1.4)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.shape == MIC:
            self.draw_mic(painter)
        else:
            self.draw_download(painter)

    def draw_mic(self, painter: QtGui.QPainter) -> None:
        """A capsule above a U stand on a short stem."""
        body = QtCore.QRectF(3.5, 1.0, 5.0, 8.0)
        painter.drawRoundedRect(body, 2.5, 2.5)
        painter.drawArc(QtCore.QRectF(1.5, 4.5, 9.0, 8.5), 180 * 16, 180 * 16)
        painter.drawLine(QtCore.QPointF(6.0, 11.5), QtCore.QPointF(6.0, 14.0))
        painter.drawLine(QtCore.QPointF(4.0, 14.0), QtCore.QPointF(8.0, 14.0))

    def draw_download(self, painter: QtGui.QPainter) -> None:
        """An arrow pointing down at a base line."""
        painter.drawLine(QtCore.QPointF(6.0, 1.5), QtCore.QPointF(6.0, 9.0))
        painter.drawLine(QtCore.QPointF(3.0, 6.0), QtCore.QPointF(6.0, 9.5))
        painter.drawLine(QtCore.QPointF(9.0, 6.0), QtCore.QPointF(6.0, 9.5))
        painter.drawLine(QtCore.QPointF(2.0, 13.0), QtCore.QPointF(10.0, 13.0))


class DictationPill(QtWidgets.QFrame):
    """The floating pill on one window (#1214): a rounded panel
    in the palette's base color, the glyph, the text, and the
    discard button. A click on the panel toggles the recording
    through the shared controller: once to start, once to finish.
    ``x`` discards. The pill takes no focus and joins no tab chain
    (mouse only)."""

    def __init__(self, dictation, window: QtWidgets.QWidget) -> None:
        super().__init__(window)
        self.setObjectName(PILL_NAME)
        self.dictation = dictation
        self.setFixedHeight(HEIGHT)
        self.setFixedWidth(self.widest(self.font()))
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.glyph = Glyph(self)
        self.text = QtWidgets.QLabel(self)
        # The glyph and the text pass their pointer events through
        # (#1214): a click anywhere on the panel is the pill's own
        # toggle, and only the discard button handles its own.
        for child in (self.glyph, self.text):
            child.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
        self.discard_button = QtWidgets.QToolButton(self)
        self.discard_button.setText("×")
        self.discard_button.setFixedSize(CLOSE_W, CLOSE_W)
        self.discard_button.setToolTip("Discard the recording")
        self.discard_button.setAutoRaise(True)
        self.discard_button.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.discard_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.discard_button.clicked.connect(self.dictation.discard)
        self.layout_pill()
        self.pressed = False
        window.installEventFilter(self)
        dictation.changed.connect(self.refresh)
        self.refresh(dictation.state, dictation.elapsed, dictation.note)
        # Shown at once (#1214): a child built under a window
        # already on screen stays hidden until it is shown by
        # hand — the host's polish pass runs before the window
        # maps, but a pill a test or a re-parenting seats later
        # must still appear.
        self.show()
        self.anchor()

    @staticmethod
    def widest(font: QtGui.QFont) -> int:
        """The one width (#1214): the glyph, the widest timer, and
        the discard button, with their gaps and padding."""
        metrics = QtGui.QFontMetrics(font)
        return (
            PAD_X
            + GLYPH_W
            + GAP
            + metrics.horizontalAdvance(WIDEST_TIMER)
            + GAP
            + CLOSE_W
            + PAD_X
        )

    def layout_pill(self) -> None:
        """The glyph, the text, and the button in one row: the
        button holds its place in every state, shown or hidden,
        so the text never moves."""
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(PAD_X, 0, PAD_X - 3, 0)
        row.setSpacing(GAP)
        row.addWidget(self.glyph)
        row.addWidget(self.text)
        row.addWidget(self.discard_button)

    # --- the anchor -----------------------------------------------------

    def eventFilter(self, watched, event) -> bool:
        """The window's resizes and shows re-anchor the pill
        (#1214): the corner is fixed relative to the window, and
        the pill stays above the content."""
        if watched is self.parentWidget() and event.type() in (
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.Show,
        ):
            self.anchor()
        return False

    def anchor(self) -> None:
        """The top-right corner (#1214): ``MARGIN`` in from the top
        and the right, raised over the content."""
        window = self.parentWidget()
        if window is None:
            return
        self.move(
            window.width() - self.width() - MARGIN,
            MARGIN,
        )
        self.raise_()

    # --- the faces ------------------------------------------------------

    @QtCore.Slot(str, float, str)
    def refresh(self, state: str, elapsed: float, note: str) -> None:
        """Wear the state (#1214): the glyph's shape and tint, the
        text, the discard button, and the tooltip."""
        self.glyph.shape = DOWNLOAD if state == NOMODEL else MIC
        self.glyph.kind = {
            RECORDING: ERROR,
            TRANSCRIBING: WARN,
        }.get(state)
        self.text.setText(self.words(state, elapsed, note))
        self.discard_button.setVisible(state in (RECORDING, TRANSCRIBING))
        self.setToolTip(self.tip(state))
        self.glyph.update()
        self.update()

    @staticmethod
    def words(state: str, elapsed: float, note: str) -> str:
        """The text beside the glyph (#1214): the count while
        recording, an ellipsis while transcribing, the outcome's
        word after."""
        if state == RECORDING:
            seconds = int(elapsed)
            return f"● {seconds // 60}:{seconds % 60:02d}"
        return {
            TRANSCRIBING: "…",
            DONE: note or "copied",
            FAILED: note or "failed",
        }.get(state, "")

    @staticmethod
    def tip(state: str) -> str:
        """The tooltip (#1214): what a click does in the state,
        and where the no-model click goes."""
        if state == NOMODEL:
            return "Download the speech model on the settings page"
        return {
            IDLE: "Dictate: click to start recording",
            RECORDING: "Click to finish recording",
            TRANSCRIBING: "Click to start again",
        }.get(state, "")

    # --- the click ------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.pressed = True
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.pressed and self.rect().contains(event.position().toPoint()):
            self.dictation.toggle()
        self.pressed = False
        event.accept()

    def paintEvent(self, _event) -> None:  # noqa: N802
        """The rounded panel (#1214): the palette's base — white
        on a light chrome, the dark gray on a dark one — under a
        hairline border, so it reads over every page and follows
        a scheme switch."""
        palette = self.palette()
        radius = HEIGHT / 2
        panel = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(panel, radius, radius)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillPath(path, palette.base())
        painter.setPen(
            QtGui.QPen(palette.color(QtGui.QPalette.ColorRole.Mid), 1.0)
        )
        painter.drawPath(path)


class PillHost(QtCore.QObject):
    """One pill on every main window the application shows
    (#1214): an application event filter that equips each
    ``QMainWindow`` when it is polished or shown — the main window
    itself, and a shell window torn off the strip. Dialogs, popups,
    and tooltips stay bare: a dialog is a question or a form in
    flight, and the recording toggle belongs to the window the
    operator works in. The host comes off the application at the
    controller's shutdown (:meth:`remove`)."""

    def __init__(self, dictation, app) -> None:
        super().__init__(dictation)
        self.dictation = dictation
        self.app = app
        app.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() in (
            QtCore.QEvent.Type.Polish,
            QtCore.QEvent.Type.Show,
        ) and self.equips(watched):
            DictationPill(self.dictation, watched)
        return False

    @staticmethod
    def equips(watched) -> bool:
        """Whether a window takes a pill (#1214): a top-level
        ``QMainWindow`` carrying none yet. A dialog never is one,
        whatever its parent."""
        return (
            isinstance(watched, QtWidgets.QMainWindow)
            and watched.isWindow()
            and pill_in(watched) is None
        )

    def remove(self) -> None:
        """The filter off the application (#1214): the controller's
        shutdown calls it, so a later window in the same process
        (the suite's) is asked for nothing."""
        self.app.removeEventFilter(self)

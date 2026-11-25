"""The workspace page's terminal-only mode (#1152): the page's
toggle hides the window's chrome and leaves the shell tabs and
the terminal in the layout of a tabbed terminal app — the tab
bar on top, the terminal filling the rest. A corner over the
shell tabs holds the way back: the workspace's name, painted
muted, and the restore button. The desktop's own title bar and
the macOS menu bar stay.

The mode belongs to the page. It ends at every hide the page
takes (the listing and the other top-level tabs must never show
under a hidden strip), it closes no shell, and the window's own
top-level strip hides through :attr:`TerminalOnly.chrome_hidden`
— the page forwards it, and the window's composition wires it to
the strip's bar. The page's status line keeps flashing in the
mode: the flash writes to whichever label the page calls its
``status``, and the mode swaps that label for the corner's own
while it stands, carrying the standing text across. The
terminal keeps every key for the program inside it (#1066), so
the mode's own way back by keyboard is its toggle shortcut,
Ctrl+Shift+M — the key Konsole hides its menubar with — which
answers wherever the page holds the keyboard, a terminal
included. The toggle and the restore button wear the style's
own maximize and restore glyphs, repainted in the palette's
button text (:class:`GlyphButton`): the fixed grays the style
ships them in read as a disabled control on the dark chrome.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time.
"""

from PySide6 import QtCore, QtGui, QtWidgets

#: The toggle's tooltip (#1152): the words its glyph alone
#: cannot carry — the maximize glyph stands for the terminal
#: taking the window.
TERMINAL_ONLY_TIP = "Show only the shell tabs and the terminal"

#: The mode's toggle key (#1152): Ctrl+Shift+M, scoped to the
#: page and its children, so a terminal holding the keyboard
#: answers it and the listing never does.
TOGGLE_KEYS = "Ctrl+Shift+M"

#: The restore button's tooltip (#1152): the glyph is the
#: platform's own "restore down" one, and the key stands in
#: for the button wherever the page holds the keyboard.
RESTORE_TIP = f"Show the workspace page's controls again ({TOGGLE_KEYS})"

#: The workspace name's longest paint, pixels (#1152): a long
#: name elides in the middle rather than widening the corner
#: past the tab bar, and the whole name then reads in the
#: label's tooltip.
NAME_MAX_PX = 240

#: The status line's longest paint in the corner, pixels
#: (#1152): a flash or an outage text is the daemon's own
#: wording and unbounded — the corner's size hint feeds the tab
#: widget's minimum width, and an uncapped label would widen
#: the window or squeeze the tab bar out of its row. The
#: chrome row's label clips in place at the row's mercy; the
#: corner's elides at this cap.
STATUS_MAX_PX = 320

#: The palette events the glyph buttons answer (#1152): the
#: ones the desktop's scheme switch delivers (#1006), whichever
#: object it hands them to — the pair the tab marks watch too.
PALETTE_EVENTS = (
    QtCore.QEvent.Type.PaletteChange,
    QtCore.QEvent.Type.ApplicationPaletteChange,
)


class CornerLabel(QtWidgets.QLabel):
    """One of the corner's labels (#1152): its text elides at the
    width the row gives it, never past a cap — in the middle for
    the name, at the right for the status — with the whole text
    in the tooltip only when the paint cut it. The layout hears
    of the whole text, bounded by the cap, and never of the
    paint: a label squeezed narrow keeps asking for its whole
    width, so a later flash does not shrink it for good. The
    label elides again when its font or its width changes (the
    whole-client zoom, #976), and a text change re-asks the
    shell strip for the row — :class:`QTabWidget` sizes its
    corner on its own layout request alone, so without the ask a
    flash would paint into the width the corner froze at the
    enter."""

    def __init__(self, mode: QtCore.Qt.TextElideMode, parent) -> None:
        super().__init__("", parent)
        self.mode = mode
        self.whole = ""

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        """The whole text's width at the cap (#1152): what the
        row should give the label, whatever it paints now."""
        hint = super().sizeHint()
        hint.setWidth(
            min(
                self.fontMetrics().horizontalAdvance(self.whole),
                self.maximumWidth(),
            )
        )
        return hint

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        """The label asks for its height alone (#1152): its
        width is a preference the layout meets when the row has
        room, never a floor — the corner's size hint feeds the
        tab widget's minimum width, and a flash must not move
        the window under the operator."""
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def setText(self, text: str) -> None:  # noqa: N802
        """Stand the whole text, paint its elision, and ask the
        shell strip for the row again (#1152): the corner's
        width belongs to the strip's layout, and a longer text
        is a wider corner."""
        self.whole = text
        self.paint_elided()
        corner = self.parentWidget()
        strip = corner.parentWidget() if corner is not None else None
        if strip is not None:
            QtWidgets.QApplication.postEvent(
                strip, QtCore.QEvent(QtCore.QEvent.Type.LayoutRequest)
            )

    def paint_elided(self) -> None:
        """The whole text elided at the width the label has —
        its cap before a layout sized it, or while the corner
        hides and no layout will — and the tooltip for the cut
        alone: a tooltip repeating the visible text is noise."""
        laid = self.isVisible() and self.width() > 0
        room = self.width() if laid else self.maximumWidth()
        paint = self.fontMetrics().elidedText(
            self.whole, self.mode, min(room, self.maximumWidth())
        )
        super().setText(paint)
        self.setToolTip(self.whole if paint != self.whole else "")

    def changeEvent(self, event) -> None:  # noqa: N802
        """A font change re-elides (#1152): the cap is in pixels,
        and the same text cuts at a different place under a
        bigger font."""
        super().changeEvent(event)
        if event.type() == QtCore.QEvent.Type.FontChange and self.whole:
            self.paint_elided()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """A width is the paint's own bound (#1152): the row gave
        the label a new width — the re-flow a text change asked
        for, or a squeeze — and the paint cuts there. A resize
        is Qt's own word for it; ``changeEvent`` never hears of
        one."""
        super().resizeEvent(event)
        if self.whole and self.width() > 0:
            self.paint_elided()


class Corner(QtWidgets.QWidget):
    """The corner over the shell tabs (#1152): its size hint is
    its row's own — the labels' whole texts and the restore
    button — capped at what the strip's row has to give beside
    the tab bar's own minimum. The tab widget hands the corner
    its whole hint before the bar gets the rest of the row, so
    an uncapped corner in a narrow window would squeeze the
    tabs; capped, a long flash or a long name narrows the
    labels — they elide at the width they get — and never the
    tabs. The cap follows the strip's width at every layout,
    which the strip runs on its own resizes. The corner is born
    inside its strip and reads it back from the reference, not
    the parent chain: its parent is the strip for its whole
    life."""

    def __init__(self, strip: QtWidgets.QTabWidget) -> None:
        super().__init__(strip)
        self.strip = strip

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        """The row's hint, bounded by the row's room beside the
        bar's minimum."""
        hint = super().sizeHint()
        room = (
            self.strip.width() - self.strip.tabBar().minimumSizeHint().width()
        )
        hint.setWidth(max(0, min(hint.width(), room)))
        return hint


def glyph_in_text(icon: QtGui.QIcon, palette: QtGui.QPalette) -> QtGui.QIcon:
    """The glyph's own shape in the palette's button text
    (#1152): the style ships its maximize and restore glyphs as
    pictures in fixed grays — the maximize one a dark gray cut
    for a light title bar, a shade off the dark chrome's own
    button — and the dark palette shows them dim, the way a
    disabled control reads. Every size the glyph carries takes
    the button text, the disabled sizes the disabled group's
    own, and the shape's alpha stays."""
    modes = (
        (QtGui.QIcon.Mode.Normal, palette.buttonText()),
        (
            QtGui.QIcon.Mode.Disabled,
            palette.color(
                QtGui.QPalette.ColorGroup.Disabled,
                QtGui.QPalette.ColorRole.ButtonText,
            ),
        ),
    )
    out = QtGui.QIcon()
    sizes = icon.availableSizes() or [
        QtCore.QSize(px, px) for px in (16, 24, 32, 48)
    ]
    for size in sizes:
        shape = (
            icon.pixmap(size)
            .toImage()
            .convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        )
        for mode, color in modes:
            image = shape.copy()
            painter = QtGui.QPainter(image)
            painter.setCompositionMode(
                QtGui.QPainter.CompositionMode.CompositionMode_SourceIn
            )
            painter.fillRect(image.rect(), color)
            painter.end()
            out.addPixmap(QtGui.QPixmap.fromImage(image), mode)
    return out


class GlyphButton(QtWidgets.QToolButton):
    """A button over one of the style's title-bar glyphs (#1152),
    painted in the button text of the application's palette
    (:func:`glyph_in_text`) — the one live source the tab marks
    read too (#1012). The glyph repaints when the palette
    changes under the window — the desktop's scheme switch
    (#1006) — through :class:`GlyphFollower`, and a disabled
    button wears the disabled group's own text color."""

    def __init__(self, glyph: QtWidgets.QStyle.StandardPixmap, parent) -> None:
        super().__init__(parent)
        self.glyph = glyph
        self.repaint_glyph()
        self.follower = GlyphFollower(self)

    def repaint_glyph(self) -> None:
        """The glyph in the application's palette as it stands
        now."""
        self.setIcon(
            glyph_in_text(
                self.style().standardIcon(self.glyph),
                QtGui.QGuiApplication.palette(),
            )
        )


class GlyphFollower(QtCore.QObject):
    """A glyph button's palette follower (#1152), the tab marks'
    own shape (#1012): the session hands a palette change to
    whatever objects it chooses — the application, a window, a
    plain widget — and a button's own ``palette()`` keeps its
    resolved colors until an event reaches it, so the follower
    sits on the application, whose filter sees every object's
    palette events, and repaints from the application's palette.
    Its lifetime is the button's (its parent), and a deleted Qt
    object unregisters its own filters."""

    def __init__(self, button: GlyphButton) -> None:
        super().__init__(button)
        self.button = button
        QtCore.QCoreApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() in PALETTE_EVENTS:
            self.button.repaint_glyph()
        return False


class TerminalOnly(QtCore.QObject):
    """The terminal-only mode over one workspace page (#1152):
    the toggle on the page's chrome row and its keyboard
    shortcut, the corner over the shell tabs (the muted name,
    the status line's seat, the restore button), and the switch
    itself. The page hands in its own controls at construction
    — the chrome row's widgets, the footer, the Escape shortcut
    — and the mode shows and hides them whole, then pads the
    page's margins so the restore button takes the toggle's
    place on the screen."""

    #: True while the mode stands (#1152): the window's own
    #: top-level strip hides through it.
    chrome_hidden = QtCore.Signal(bool)

    def __init__(self, page) -> None:
        super().__init__(page)
        self.page = page
        self.on = False
        #: The page's layout margins before the mode padded them
        #: (#1152), put back at the leave.
        self.margins: QtCore.QMargins | None = None
        #: Where the toggle stood in its window when the mode
        #: entered (#1152): its center's height and its right
        #: edge's distance from the window's right edge — the
        #: two numbers a resize of the window leaves true. None
        #: while the mode is off, or entered on a hidden page.
        self.spot: tuple[int, int] | None = None
        #: The chrome row's status label, the one the flash
        #: writes while the mode is off: :attr:`status` stands
        #: in its place while the mode is on.
        self.chrome_status = page.status
        self.corner = Corner(page.shells)
        self.name = CornerLabel(
            QtCore.Qt.TextElideMode.ElideMiddle, self.corner
        )
        self.name.setMaximumWidth(NAME_MAX_PX)
        # Muted (#1152): the placeholder's gray, the role both
        # the light and the dark palette set — the name reads
        # at a glance beside the restore button without
        # competing with the tabs.
        self.name.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        self.status = CornerLabel(
            QtCore.Qt.TextElideMode.ElideRight, self.corner
        )
        self.status.setMaximumWidth(STATUS_MAX_PX)
        self.restore = GlyphButton(
            QtWidgets.QStyle.StandardPixmap.SP_TitleBarNormalButton,
            self.corner,
        )
        self.restore.setToolTip(RESTORE_TIP)
        self.restore.setAccessibleName("Show the page controls")
        self.restore.clicked.connect(self.leave)
        corner_row = QtWidgets.QHBoxLayout(self.corner)
        corner_row.setContentsMargins(0, 0, 0, 0)
        corner_row.addWidget(self.name)
        # The status takes the row's slack and its squeeze alike
        # (#1152): a stretch of one hands it the spare width
        # first and takes the deficit first, so the name keeps
        # its width while the row has any for it.
        corner_row.addWidget(self.status, 1)
        corner_row.addWidget(self.restore)
        # The corner takes the tab row's right end (#1152) —
        # where a tabbed terminal app keeps its own controls —
        # and stands hidden until the mode asks for it: a
        # hidden corner leaves the bar the whole row.
        page.shells.setCornerWidget(
            self.corner, QtCore.Qt.Corner.TopRightCorner
        )
        self.corner.hide()
        self.toggle = GlyphButton(
            QtWidgets.QStyle.StandardPixmap.SP_TitleBarMaxButton, page
        )
        self.toggle.setToolTip(f"{TERMINAL_ONLY_TIP} ({TOGGLE_KEYS})")
        self.toggle.setAccessibleName("Terminal only")
        self.toggle.clicked.connect(self.enter)
        # The toggle after the external-terminal button, the
        # row's right end (#1152). The toggle hides with the row
        # too — the corner's restore button stands in its place —
        # so the row leaves no gap above the terminals.
        page.chrome.addWidget(self.toggle)
        #: The chrome row's widgets and the toggle: what the
        #: mode hides and shows, whole.
        self.row = (
            page.back_button,
            page.bar,
            self.chrome_status,
            page.external_button,
            self.toggle,
        )
        # The toggle's key (#1152): scoped to the page and its
        # children, so it answers from a terminal — whose own
        # keys stay with the program inside (#1066) — and never
        # from the listing beside the page.
        self.keys = QtGui.QShortcut(QtGui.QKeySequence(TOGGLE_KEYS), page)
        self.keys.setContext(
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        # A held key must not flip the mode back and forth with
        # the keyboard's repeat (#1152): one press, one flip.
        self.keys.setAutoRepeat(False)
        self.keys.activated.connect(self.flip)
        # The page's, the shell strip's, the corner's, and the
        # restore button's own moves and resizes re-park it
        # (#1152) while the mode stands: the strip hides move
        # the page, a relayout of the page moves the shell
        # strip, a late layout of the tab row moves the corner,
        # and the corner's own row settling moves the button
        # inside a corner that stood still.
        for watched in (page, page.shells, self.corner, self.restore):
            watched.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """The page, the shell strip, or the corner moved or
        resized under the mode (#1152): the strip's bar hide
        lands as both on the page, and a platform may deliver
        either a pass after the click — so the parked spot is
        checked again on the loop's next pass, from outside the
        event that moved the widget."""
        if self.on and event.type() in (
            QtCore.QEvent.Type.Move,
            QtCore.QEvent.Type.Resize,
        ):
            QtCore.QTimer.singleShot(0, self, self.park_restore)
        return False

    def set_name(self, name: str) -> None:
        """The workspace's name on the corner's label (#1152):
        the page's own label, standing in from the list until
        the row's own answer refines it."""
        self.name.setText(name)

    @QtCore.Slot()
    def flip(self) -> None:
        """The toggle's key (#1152): the mode flips — on when
        off, off when on — from wherever the page holds the
        keyboard."""
        if self.on:
            self.leave()
        else:
            self.enter()

    @QtCore.Slot()
    def enter(self) -> None:
        """The toggle's action (#1152): a rename edit in place
        cancels, the Shells tab shows — the mode has room for
        the terminals alone — then the chrome row, the footer,
        and the Back shortcut hide, the corner stands up with
        the status line's seat, the terminal takes the keyboard,
        the window's strip hears the ask, and the restore button
        is parked where the toggle stood. A second ask while the
        mode stands changes nothing."""
        if self.on:
            return
        self.on = True
        page = self.page
        self.spot = self.toggle_spot()
        if self.spot is not None:
            self.margins = QtCore.QMargins(page.layout().contentsMargins())
        page.shells.cancel_rename()
        page.bar.setCurrentIndex(page.stack.indexOf(page.shells_tab))
        for widget in self.row:
            widget.hide()
        page.footer.hide()
        page.escape.setEnabled(False)
        # The status line's seat moves (#1152): the standing
        # text — a flash mid-span, an outage — comes along, and
        # the flash's own timeout clears whichever label stands
        # when it fires.
        self.status.setText(self.chrome_status.text())
        page.status = self.status
        self.corner.show()
        page.shells_tab.focus_shell()
        self.chrome_hidden.emit(True)
        self.park_restore()
        QtCore.QTimer.singleShot(0, self, self.park_restore)

    def toggle_spot(self) -> tuple[int, int] | None:
        """Where the toggle stands in its window before its row
        hides (#1152): its center's height and its right edge's
        distance from the window's right edge, or None while the
        page does not show — a hidden page's geometry says
        nothing about the pointer."""
        if not self.toggle.isVisible():
            return None
        window = self.toggle.window()
        rect = QtCore.QRect(
            self.toggle.mapTo(window, QtCore.QPoint(0, 0)), self.toggle.size()
        )
        return rect.center().y(), window.rect().right() - rect.right()

    @QtCore.Slot()
    def park_restore(self) -> None:
        """Pad the page's margins so the restore button takes the
        toggle's place (#1152): the pointer that clicked the one
        rests over the other, and the second click needs no
        move. The hides above unsettled two layouts — the
        page's own and the strip's, whose bar the ask took
        away — so they settle first, and the padding is the
        distance between where the toggle stood and where the
        restore button landed: down for the rows that left
        above the tab row, right for the frame the shell strip
        keeps inside the page. The padding always builds on the
        margins the page wore before the mode, so a re-park
        never compounds. The measurement assumes a window that
        stands still through the settle: a window its own
        minimum width grows under (a page alone with a very long
        text in its chrome row) would move the page under the
        measure, and the strip's composition never does — its
        minimum ignores the page's."""
        if self.spot is None or self.margins is None:
            return
        page = self.page
        window = page.window()
        QtWidgets.QApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.LayoutRequest
        )
        landed = QtCore.QRect(
            self.restore.mapTo(window, QtCore.QPoint(0, 0)),
            self.restore.size(),
        )
        center_y, from_right = self.spot
        # The padding stands already on a re-park: the distance
        # measured is the correction to it, added to what the
        # page wears now.
        current = page.layout().contentsMargins()
        down = (
            center_y
            - landed.center().y()
            + (current.top() - self.margins.top())
        )
        right = (
            window.rect().right()
            - from_right
            - landed.right()
            + (self.margins.right() - current.right())
        )
        margins = self.margins
        target = QtCore.QMargins(
            max(0, margins.left()),
            max(0, margins.top() + down),
            max(0, margins.right() - right),
            max(0, margins.bottom()),
        )
        # A page already parked writes nothing: an unchanged
        # margin would relayout the row for no move, and the
        # corner's own resize would ask for the check again.
        if target != current:
            page.layout().setContentsMargins(target)

    @QtCore.Slot()
    def leave(self) -> None:
        """The restore button's action, and the page's hide's
        (#1152): the corner goes, every hidden control shows
        again, the status text returns to the chrome row's
        label, the page's margins come back, the Back shortcut
        arms, and the window's strip hears the mode ended. The
        current tabs — the page's, the shell's — and every
        session stand as they were. An ask while the mode is off
        changes nothing."""
        if not self.on:
            return
        self.on = False
        page = self.page
        # The whole text crosses back (#1152): the corner may have
        # elided a long one, and the chrome row's label clips it
        # at its own width.
        self.chrome_status.setText(self.status.whole)
        page.status = self.chrome_status
        if self.margins is not None:
            page.layout().setContentsMargins(self.margins)
        self.margins = None
        self.spot = None
        self.corner.hide()
        for widget in self.row:
            widget.show()
        page.footer.show()
        page.escape.setEnabled(True)
        self.chrome_hidden.emit(False)

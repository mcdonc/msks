"""The settings page (#1214, #1226): the top-level tab that edits
every setting the client's ``msks.yaml`` accepts, partitioned into
subtabs — Connection (the address keys, the active alias, and the
``daemons:`` editor, :mod:`.connection`), Files (:mod:`.files`),
Terminal and ssh (:mod:`.shell`), Screenshot (:mod:`.screenshot`),
and Dictation (:mod:`.dictate`, the model download with it). The
subtabs share one **Apply** / **Revert** pair and one status line;
a subtab with unsaved edits wears a marker on its tab. A launch
with no file to write (``--config=none``) stands the four
file-backed subtabs dark under a note: their keys were read at
launch, and only the dictation settings, which the running window
re-resolves, apply for the session.

The page shows the *effective* settings and seeds its fields from
them: a key the environment pins reads as the variable's value and
its field stands read-only with the variable marked beside it, the way every
client key behaves; the other keys read the merged document (the
user's file over the system's, #1224). Apply writes the edits — the
fields whose value moved from the seed — into the user's
``msks.yaml`` through :func:`msks.client.config.save_keys`
(:func:`msks.configio.set_scalars` for the scalar keys,
:func:`msks.configio.set_daemon` for the aliases): every other byte
of the file stays as the operator wrote it, and the system file
under it is never written. A cleared field unsets its key, so a
value the system file holds applies again. A result the loader
refuses flashes its message and writes nothing. A launch under
``--config=none`` has no file to write: the four file-backed subtabs
stand dark under a note, and the Dictation subtab's Apply applies
its settings for the session and says so.

The subtab strip follows the spatial-navigation rule the main strip
carries (#962): Left and Right move between the subtabs, Down
enters the panel below at its first tab stop, and Up from a
panel's first field returns to the strip.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ...config import (
    NO_CONFIG,
    ClientConfig,
    floor_daemons,
    own_doc,
    reload_doc,
    save_keys,
)
from ...dictate import download_model, list_sources
from ..dictatekeys import DictateSettings, resolve
from ..dictatepill import seat_pill
from ..focuswalk import first_tab_stop
from ..statusflash import StatusFlash
from ..tabstyle import mark_active
from ..tintedlabel import TintedLabel
from .connection import ConnectionPanel
from .dictate import DictatePanel
from .fields import Panel
from .files import FilesPanel
from .screenshot import ScreenshotPanel
from .shell import ShellPanel
from .vocabulary import (
    MODIFIED,
    SUBTAB_LABELS,
    VARIABLES,
    pinned_keys,
)
from .vocabulary import (
    effective as resolve_key,
)

__all__ = [
    "SESSION_ONLY",
    "SettingsPage",
    "session_document",
]

#: What Apply says with no file to write (#1224, #1226): a launch
#: under ``--config=none`` applies the dictation settings for the
#: session alone.
SESSION_ONLY = (
    "the dictation settings are applied for this session "
    "(--config=none reads and writes no file)"
)

#: The note the file-backed subtabs stand under with no file to write
#: (#1226): their keys are read at launch and no file holds an edit,
#: so their fields stand dark and only the Dictation subtab — whose
#: settings the running window re-resolves — applies for the session.
NO_FILE_NOTE = (
    "--config=none reads and writes no file: these settings were read "
    "at launch and stand as they are; the Dictation subtab alone "
    "applies for this session."
)

#: What Apply says over fields that stand as saved (#1226): the dirty
#: flag lit on a change changed back, nothing to write.
NOTHING_TO_SAVE = "nothing to save: the fields stand as saved"


def session_document(operator: dict, values: dict) -> dict:
    """The document a session-only apply holds (#1224, #1226):
    *values* over the standing document the way a save would land
    them — a blank is the unset form and deletes the key, so
    clearing a field clears it for the session too."""
    doc = {**operator, **values}
    return {key: value for key, value in doc.items() if value}


class SettingsPage(QtWidgets.QWidget, StatusFlash):
    """The settings tab (#1214, #1226). ``settings_saved`` carries the
    freshly resolved dictation settings to whoever wants them; the
    controller takes them back through :meth:`apply` itself. The
    seams: the source lister and the downloader, the suite's
    fakes."""

    settings_saved = QtCore.Signal(object)

    def __init__(
        self,
        dictation,
        conf: ClientConfig | None = None,
        parent=None,
        *,
        sources: Callable[[], list] = list_sources,
        downloader=download_model,
    ) -> None:
        super().__init__(parent)
        self.dictation = dictation
        #: The launch's resolved config (#1224): the user file Apply
        #: writes (``config_path``) and the files a save re-reads
        #: (``config_paths``); None when the window runs with no
        #: config at all, the suite's form.
        self.conf = conf
        #: The merged document the settings resolve over (#1214,
        #: #1224): the user's keys over the system's, read again
        #: after every save.
        self.operator = {} if conf is None else copy.deepcopy(conf.doc)
        #: The keys a variable pins (#1226), read at every load.
        self.pinned: frozenset = pinned_keys(os.environ)
        self.status = TintedLabel("", self)
        self.flash_seq = 0
        self.daemon_down = False
        self.outage_text = ""
        self.dirty = False
        # The subtabs in the strip's order (#1226), each under its
        # own name for the tests and the docs chapter.
        self.connection = ConnectionPanel(self)
        self.files = FilesPanel(self)
        self.shell = ShellPanel(self)
        self.screenshot = ScreenshotPanel(self)
        self.dictate = DictatePanel(
            self, sources=sources, downloader=downloader
        )
        self.panels: list[Panel] = [
            self.connection,
            self.files,
            self.shell,
            self.screenshot,
            self.dictate,
        ]
        self.build_strip()
        self.build_buttons()
        self.layout_page()
        for panel in self.panels:
            panel.touched.connect(self.panel_touched)
        self.load(dictation.settings)

    def darken_without_file(self) -> None:
        """A launch with no file to write (#1226): the file-backed
        subtabs stand dark under a note above them — their keys were
        read at launch and no file holds an edit — and the Dictation
        subtab alone takes the session's apply. Run after every seed,
        which clears the panels' notes."""
        if self.writable():
            return
        for panel in self.panels:
            if panel is self.dictate:
                continue
            # The group boxes go dark and the note above them stays
            # black: every field and button sits in a group box.
            for box in panel.findChildren(QtWidgets.QGroupBox):
                box.setEnabled(False)
            panel.note.setText(NO_FILE_NOTE)
            panel.note.setVisible(True)

    # --- the page -------------------------------------------------------

    @property
    def doc(self) -> dict:
        """The merged document the panels seed from (#1226): the
        page's ``operator``."""
        return self.operator

    def effective(self, key: str) -> tuple[str, bool]:
        """One key's value for its field (#1226): the variable's
        text when it pins, else the merged document's."""
        return resolve_key(key, os.environ, self.operator)

    @staticmethod
    def variable_of(key: str) -> str:
        """The variable that overrides *key* (#1226)."""
        return VARIABLES[key]

    def build_strip(self) -> None:
        """The subtab strip (#1226): a bar over a stack, one panel
        per tab, the active tab's mark the main strip carries
        (#1012). Down leaves the bar for the panel below; Up from
        a panel's first field comes back to the bar."""
        self.bar = QtWidgets.QTabBar(self)
        self.bar.setDrawBase(False)
        self.bar.setExpanding(False)  # the tabs hug the left, the
        # detail page's own strip's shape, never spreading over the
        # page's width
        for label in SUBTAB_LABELS:
            self.bar.addTab(label)
        self.bar.setFocusPolicy(QtCore.Qt.FocusPolicy.TabFocus)
        mark_active(self.bar)
        self.stack = QtWidgets.QStackedWidget(self)
        for panel in self.panels:
            # Each panel scrolls (#1226): a form taller than the
            # page scrolls inside the window's default size instead
            # of growing the window's minimum under it.
            scroll = QtWidgets.QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            scroll.setWidget(panel)
            self.stack.addWidget(scroll)
        self.bar.currentChanged.connect(self.stack.setCurrentIndex)
        self.bar.installEventFilter(self)
        for panel in self.panels:
            UpToBar(panel, self.bar)

    def build_buttons(self) -> None:
        """The page's one Apply and Revert (#1214, #1226)."""
        self.apply_button = QtWidgets.QPushButton("Apply", self)
        self.apply_button.clicked.connect(self.apply)
        self.reload_button = QtWidgets.QPushButton("Revert", self)
        self.reload_button.clicked.connect(self.revert)

    def layout_page(self) -> None:
        """The page's rows (#1226): the strip, the panel, the
        buttons, and the status line with the dictate pill."""
        page = QtWidgets.QVBoxLayout(self)
        page.addWidget(self.bar)
        page.addWidget(self.stack, stretch=1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.reload_button)
        buttons.addStretch(1)
        page.addLayout(buttons)
        # The status line shares its row with the dictate pill
        # (#1214), pinned right.
        page.addWidget(seat_pill(self, self.dictation, self.status))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """The strip's Down (#962, #1226): the bar hands the focus to
        the panel below. The other keys pass on."""
        if (
            obj is self.bar
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Down
        ):
            self.enter_panel()
            return True
        return super().eventFilter(obj, event)

    def show_dictation(self) -> None:
        """Open the strip on the Dictation subtab (#1226): the no-model
        click's answer, the Download button's own page."""
        self.bar.setCurrentIndex(self.panels.index(self.dictate))

    def enter_panel(self) -> None:
        """Focus the current panel's first tab stop (#1226): the
        focus chain's next focusable widget under it, the panel
        itself when nothing under it takes the keyboard — explicit
        focus stands even where the panel's own policy would refuse
        a Tab."""
        page = self.stack.currentWidget()
        target = first_tab_stop(page) or page
        target.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    # --- the dirty flag -------------------------------------------------

    @QtCore.Slot()
    def panel_touched(self) -> None:
        """A field changed under the operator (#1214, #1226): Apply
        and Revert light up, and the panel's tab wears the marker."""
        self.dirty = True
        self.apply_button.setEnabled(True)
        self.reload_button.setEnabled(True)
        index = self.panels.index(self.sender())  # a panel alone connects
        self.bar.setTabText(index, MODIFIED + SUBTAB_LABELS[index])

    def settled(self) -> None:
        """The fields stand as saved (#1214): a seed, a save, or a
        revert just ran; the buttons darken until the next change
        and the markers clear."""
        self.dirty = False
        self.apply_button.setEnabled(False)
        self.reload_button.setEnabled(False)
        for index, label in enumerate(SUBTAB_LABELS):
            self.bar.setTabText(index, label)

    # --- reading and writing the settings -------------------------------

    def load(self, settings: DictateSettings | None = None) -> None:
        """Seed every panel (#1214, #1226): the pins read again from
        the environment, the general panels from the merged
        document, the dictation panel from its record; the seed's
        own signals are not the operator's."""
        self.pinned = pinned_keys(os.environ)
        for panel in self.panels:
            panel.load(settings)
        self.settled()
        self.darken_without_file()

    def file_edits(self) -> tuple[dict, dict]:
        """What Apply writes (#1226): the scalar edits from every
        panel, and the ``daemons:`` edits — pinned keys left out,
        cleared fields as the unset form."""
        scalars: dict = {}
        daemons: dict = {}
        for panel in self.panels:
            scalars.update(panel.edits())
            daemons.update(panel.daemon_edits())
        return scalars, daemons

    def own(self) -> dict:
        """The user's own document (#1226): the file Apply writes,
        validated alone, the keys a save may rewrite without touching
        a value the system file holds. Empty with no config."""
        return {} if self.conf is None else own_doc(self.conf)

    def floor(self) -> dict:
        """The system file's ``daemons:`` aliases (#1226): the ones a
        save checks an entry against and the user's file cannot
        remove. Empty with no config and under ``--config``."""
        return {} if self.conf is None else floor_daemons(self.conf)

    def writable(self) -> bool:
        """Whether the launch has a file Apply writes (#1224):
        false under ``--config=none`` and when the window runs
        with no config at all."""
        return self.conf is not None and self.conf.config_path != NO_CONFIG

    def file_name(self) -> str:
        """The file Apply writes, by name, for the flashes (#1224):
        the launch's own file. Called only on the writable path — a
        session-only save flashes its own fixed line."""
        return Path(self.conf.config_path).name

    def save(self, values: dict, daemons: dict | None = None) -> None:
        """Write the edits into the user's file (#1224, #1226) — or,
        with no file to write, hold the dictation values in the
        document for the session alone (the file-backed subtabs stand
        dark then and carry no edit). The re-read is :meth:`reread`'s,
        so a write that refuses and a re-read that refuses are two
        different flashes."""
        if not self.writable():
            self.operator = session_document(self.operator, values)
        else:
            save_keys(self.conf, values, daemons)

    def reread(self) -> None:
        """The merged document read again after a save (#1224); a
        session-only save holds its document already."""
        if self.writable():
            self.operator = reload_doc(self.conf)

    def write_and_reread(
        self, values: dict, daemons: dict | None = None
    ) -> DictateSettings | None:
        """Apply's and the download's save (#1224): the write, then
        the re-read, each flashing its own refusal — a write that
        refuses leaves the file as it was, a re-read that refuses
        comes after a write that landed and says so — and answering
        None; else the settings as the layers resolve them after
        both."""
        try:
            self.save(values, daemons)
        except (OSError, ValueError) as exc:
            self.flash(f"cannot save {self.file_name()}: {exc}", error=True)
            return None
        try:
            self.reread()
            return self.resolve_settings()
        except (OSError, ValueError) as exc:
            self.flash(
                f"saved to {self.file_name()}, but the re-read refused: {exc}",
                error=True,
            )
            return None

    def resolve_settings(self) -> DictateSettings:
        """The dictation settings as the layers resolve them right
        now (#1214, #1224): the environment and the merged
        document. A file that will not read is the caller's to
        show."""
        return resolve(os.environ, self.operator)

    @QtCore.Slot()
    def apply(self) -> None:
        """Save the edits into the user's file (#1214, #1224, #1226)
        — a pinned key is left out, the variable wins it anyway —
        and hand the freshly resolved settings to the controller. A
        file the tree cannot read or write flashes the reason; a
        launch with no file applies for the session and says so."""
        scalars, daemons = self.file_edits()
        if not scalars and not daemons:
            # Changed and changed back (#1226): nothing differs from
            # the seed, and no write is made.
            self.flash(NOTHING_TO_SAVE)
            self.settled()
            return
        settings = self.write_and_reread(scalars, daemons)
        if settings is None:
            return
        self.dictation.reload(settings)
        # The saved line before the reseed (#1226): a panel's note
        # on the reseed — an alias the system file keeps standing —
        # is the message worth reading, and it lands last.
        self.flash(
            f"saved to {self.file_name()}" if self.writable() else SESSION_ONLY
        )
        self.load(settings)
        self.settings_saved.emit(settings)

    @QtCore.Slot()
    def revert(self) -> None:
        """Back to the effective settings (#1214): the layers read
        again — the environment and the files may have moved
        under the page — and the fields reseed; nothing is
        written, and the controller takes the re-resolved
        settings. A file that will not read flashes the reason
        and the fields reseed from the standing settings."""
        try:
            if self.writable():
                self.operator = reload_doc(self.conf)
            settings = self.resolve_settings()
        except ValueError as exc:
            self.flash(str(exc), error=True)
            self.load(self.dictation.settings)
            return
        self.dictation.reload(settings)
        self.load(settings)

    def matches_file(self) -> bool:
        """Whether the fields stand as the file holds them (#1224):
        every panel's fields and aliases equal the merged
        document's values, a blank field matching an absent key."""
        return all(panel.stands_as_file() for panel in self.panels)


class UpToBar(QtCore.QObject):
    """A panel's first field's Up (#1226): the key returns the focus
    to the subtab strip above, the spatial-navigation rule's own
    (#962), where a line edit has no use for the key. The filter
    stands on every field of the panel and answers only the first
    enabled one, read at the key: a pinned field is dark and no tab
    stop, so the row after it is the first. An editable combo is the
    window's focus object even though its inner line edit holds the
    cursor, so the key reaches the combo, and the combo's own Up
    (cycling its items) gives way there."""

    def __init__(self, panel: Panel, bar: QtWidgets.QTabBar) -> None:
        super().__init__(panel)
        self.panel = panel
        self.bar = bar
        for field in panel.fields.values():
            field.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Up
            and obj is self.panel.first_field()
        ):
            self.bar.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
            return True
        return False

"""The settings page (#1214): the top-level tab that edits the
dictate pill's settings — the model, the device, and the prompt —
saves them into the client's ``msks.yaml`` by merging the three
keys into the file in place (:func:`msks.client.config.save_keys`,
#1224), and carries the model download with its progress.

The page shows the *effective* settings (:mod:`msks.client.gui.dictatekeys`)
and seeds its fields from them. Apply writes every field into the
user's ``msks.yaml`` — every other byte of the file stays as the
operator wrote it, and the system file under it is never written —
and hands the controller the freshly resolved settings. A key the
environment pins reads as the variable's value and its field
stands read-only with the variable named, the way every other
client key behaves. A launch under ``--config=none`` has no file
to write: Apply applies for the session and says so.

The download fetches a *named* model on a worker thread with the
progress bar under it; a path setting (a NixOS store path)
downloads nothing and the page says where it points. The device
list comes from ``pw-dump`` when the host has it; the field stays
editable for a serial or a name the list does not carry.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ..config import NO_CONFIG, ClientConfig, reload_doc, save_keys
from ..config_keys import DICTATE_KEYS, GLOBAL_ENV_VARS
from ..dictate import (
    DEFAULT_MODEL,
    MODEL_SIZES,
    DictateFailure,
    download_model,
    list_sources,
    model_path,
)
from .dictatekeys import DictateSettings, resolve
from .dictatepill import seat_pill
from .statusflash import StatusFlash
from .tintedlabel import TintedLabel

#: The model's size column beside the name (#1214).
MODEL_LABEL = "Model"
#: The download bar's width, pixels (#1214): a third of the
#: default window, beside the two buttons.
BAR_WIDTH = 360
DEVICE_LABEL = "Device"
DEFAULT_DEVICE = "the default input"

#: What Apply says with no file to write (#1224): a launch under
#: ``--config=none`` applies the fields for the session alone.
SESSION_ONLY = (
    "applied for this session (--config=none reads and writes no file)"
)


def session_document(operator: dict, values: dict) -> dict:
    """The document a session-only apply holds (#1224): *values*
    over the standing document the way a save would land them — a
    blank is the unset form and deletes the key, so clearing a
    field clears it for the session too."""
    return {
        key: value for key, value in {**operator, **values}.items() if value
    }


#: The device row's Refresh button (#1214).
REFRESH_TIP = (
    "Read the desktop's source list again: a microphone plugged in or "
    "a source renamed since the page opened joins the picker."
)

#: Each setting's long description (#1214): the field's tooltip,
#: the same words for the page and for the docs chapter. A key the
#: environment pins appends its variable's notice to its field's.
TIPS = {
    "dictate_model": (
        "The whisper.cpp speech model. Pick a known name and Download "
        "fetches it into ~/.cache/msks/whisper/; a larger "
        "model transcribes more accurately and loads more slowly at "
        "each clip (about a second with small, two to three with "
        "large-v3-turbo). A path to a .bin file uses that file instead "
        "and downloads nothing: a NixOS host pins a model as a "
        "derivation and types its store path here."
    ),
    "dictate_device": (
        "The microphone the recording captures from: a PipeWire "
        "source. The picker lists the sources the desktop serves "
        "(Refresh reads the list again); the default input follows "
        "the desktop's own choice. A node name or serial typed free "
        "selects one the list does not carry."
    ),
    "dictate_prompt": (
        "The vocabulary whisper-cli biases its spelling toward: the "
        "names it would otherwise misspell, comma-separated. Leave "
        "it empty for plain dictation."
    ),
}


class SettingsPage(QtWidgets.QWidget, StatusFlash):
    """The settings tab (#1214). ``settings_saved`` carries the
    freshly resolved settings to whoever wants them; the
    controller takes them back through :meth:`apply` itself. The
    seams: the source lister and the downloader, the suite's
    fakes."""

    settings_saved = QtCore.Signal(object)
    progressed = QtCore.Signal(int, int)
    downloaded = QtCore.Signal(str, str, str)

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
        self.cache_root = dictation.cache_root
        #: The launch's resolved config (#1224): the user file Apply
        #: writes (``config_path``) and the files a save re-reads
        #: (``config_paths``); None when the window runs with no
        #: config at all, the suite's form.
        self.conf = conf
        #: The merged document the settings resolve over (#1214,
        #: #1224): the user's keys over the system's, read again
        #: after every save.
        self.operator = {} if conf is None else conf.doc
        self.sources = sources
        self.downloader = downloader
        self.status = TintedLabel("", self)
        self.flash_seq = 0
        self.daemon_down = False
        self.outage_text = ""
        #: The download in flight (#1214): its cancel flag, and the
        #: thread it runs on.
        self.cancel = threading.Event()
        self.flight: threading.Thread | None = None
        self.fields: dict[str, QtWidgets.QWidget] = {}
        self.build_form()
        self.build_download()
        self.layout_page()
        self.progressed.connect(self.progress)
        self.downloaded.connect(self.landed)
        self.load(dictation.settings)
        self.refresh_model()

    # --- the form -------------------------------------------------------

    def build_form(self) -> None:
        """The three fields (#1214): the model as an editable picker
        over the known names (a path typed free), the device as an
        editable picker over the listed sources, and the prompt. The
        picker drives the model line and the Download button as it
        changes, so a pick shows what a download would fetch before
        anything is saved."""
        self.model = QtWidgets.QComboBox(self)
        self.model.setEditable(True)
        self.model.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        for name, size in MODEL_SIZES.items():
            self.model.addItem(f"{name} ({size})", name)
        self.model.currentIndexChanged.connect(self.refresh_model)
        self.model.editTextChanged.connect(self.refresh_model)
        self.fields["dictate_model"] = self.model
        self.device = QtWidgets.QComboBox(self)
        self.device.setEditable(True)
        self.device.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.device.addItem(DEFAULT_DEVICE, "")
        self.fields["dictate_device"] = self.device
        self.prompt = QtWidgets.QLineEdit(self)
        # No placeholder (#1214): the gray example text read as a
        # value already in effect. The tooltip carries the examples,
        # and an empty prompt is visibly empty.
        self.prompt.setToolTip(TIPS["dictate_prompt"])
        self.fields["dictate_prompt"] = self.prompt
        self.refresh_sources = QtWidgets.QPushButton("Refresh", self)
        self.refresh_sources.clicked.connect(self.list_devices)
        self.apply_button = QtWidgets.QPushButton("Apply", self)
        self.apply_button.clicked.connect(self.apply)
        self.reload_button = QtWidgets.QPushButton("Revert", self)
        self.reload_button.clicked.connect(self.revert)
        # The dirty flag (#1214): Apply and Revert stand dark until a
        # field changes, and a save or a revert darkens them again —
        # a lit Apply over saved fields invites a save that changes
        # nothing, and a dark one says the fields stand as saved.
        self.dirty = False
        self.apply_button.setEnabled(False)
        self.reload_button.setEnabled(False)
        for field in self.fields.values():
            self.listen(field)
        self.pinned_note = QtWidgets.QLabel("", self)
        self.pinned_note.setWordWrap(True)

    def listen(self, field) -> None:
        """One field's change signals to the dirty flag (#1214):
        whichever the widget kind answers — a pick, a typed edit, a
        spin, a text change."""
        signals = (
            getattr(field, "currentIndexChanged", None),
            getattr(field, "editTextChanged", None),
            getattr(field, "valueChanged", None),
            getattr(field, "textChanged", None),
        )
        for signal in signals:
            if signal is not None:
                signal.connect(self.touched)

    @QtCore.Slot()
    @QtCore.Slot(int)
    @QtCore.Slot(float)
    @QtCore.Slot(str)
    def touched(self, *_args) -> None:
        """A field changed under the operator (#1214): Apply and
        Revert light up."""
        self.dirty = True
        self.apply_button.setEnabled(True)
        self.reload_button.setEnabled(True)

    def settled(self) -> None:
        """The fields stand as saved (#1214): a seed, a save, or a
        revert just ran; the buttons darken until the next change."""
        self.dirty = False
        self.apply_button.setEnabled(False)
        self.reload_button.setEnabled(False)

    def label(self, key: str, text: str) -> QtWidgets.QLabel:
        """A form row's label (#1214): the setting's name, carrying
        the field's tooltip, kept under ``self.labels`` so the seed
        re-tips it beside the field."""
        label = QtWidgets.QLabel(text, self)
        label.setToolTip(TIPS[key])
        self.labels[key] = label
        return label

    def build_download(self) -> None:
        """The model download (#1214): where the model points and
        whether it stands, the download button, the bar, and the
        cancel beside it."""
        self.model_state = QtWidgets.QLabel("", self)
        self.model_state.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.model_state.setWordWrap(True)
        self.download_button = QtWidgets.QPushButton("Download", self)
        self.download_button.clicked.connect(self.download)
        self.bar = QtWidgets.QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setFixedWidth(BAR_WIDTH)
        self.bar.hide()
        self.cancel_button = QtWidgets.QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.abandon_download)
        self.cancel_button.hide()

    def layout_page(self) -> None:
        """The page's rows (#1214): the settings group over the
        download group, the buttons, and the status line."""
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        # Each label is a widget of its own (#1214) so it carries its
        # field's tooltip: a reader hovers the setting's name, not
        # only its box. The seed keeps a pinned key's notice on both.
        self.labels: dict[str, QtWidgets.QLabel] = {}
        form.addRow(self.label("dictate_model", MODEL_LABEL), self.model)
        device_row = QtWidgets.QHBoxLayout()
        device_row.addWidget(self.device)
        self.refresh_sources.setToolTip(REFRESH_TIP)
        device_row.addWidget(self.refresh_sources)
        form.addRow(self.label("dictate_device", DEVICE_LABEL), device_row)
        form.addRow(
            self.label("dictate_prompt", "Vocabulary prompt"), self.prompt
        )
        form.addRow(self.pinned_note)
        settings_box = QtWidgets.QGroupBox("Dictation", self)
        rows = QtWidgets.QVBoxLayout(settings_box)
        rows.addLayout(form)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.reload_button)
        buttons.addStretch(1)
        rows.addLayout(buttons)
        download_box = QtWidgets.QGroupBox("Speech model", self)
        down = QtWidgets.QVBoxLayout(download_box)
        down.addWidget(self.model_state)
        # The two buttons at their own widths on the left, the bar
        # at a fixed width beside them, and the spare after: a
        # hidden bar leaves the row to the buttons and the
        # spacer.
        bar_row = QtWidgets.QHBoxLayout()
        for button in (self.download_button, self.cancel_button):
            button.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            bar_row.addWidget(button)
        bar_row.addWidget(self.bar)
        bar_row.addStretch(1)
        down.addLayout(bar_row)
        page = QtWidgets.QVBoxLayout(self)
        page.addWidget(settings_box)
        page.addWidget(download_box)
        page.addStretch(1)
        # The status line shares its row with the dictate pill
        # (#1214), pinned right.
        page.addWidget(seat_pill(self, self.dictation, self.status))

    # --- reading and writing the settings -------------------------------

    def seed(self, key: str, settings: DictateSettings) -> None:
        """One field's seed (#1214): the effective value in the
        field's own shape, and the field read-only with the
        variable named when the environment pins the key."""
        field = self.fields[key]
        if key == "dictate_model":
            self.pick_model(settings.model)
        elif key == "dictate_device":
            self.pick_device(settings.device)
        else:
            field.setText(str(settings.value(key)))
        pinned = key in settings.pinned
        field.setEnabled(not pinned)
        tip = self.tip(key, pinned)
        field.setToolTip(tip)
        self.labels[key].setToolTip(tip)

    @staticmethod
    def tip(key: str, pinned: bool) -> str:
        """A field's tooltip (#1214): the setting's description, and
        for a key the environment pins, the variable's notice after
        it."""
        text = TIPS[key]
        if pinned:
            notice = (
                f"{GLOBAL_ENV_VARS[key]} pins this setting; the page "
                "cannot change it."
            )
            return f"{text}\n\n{notice}"
        return text

    def notes(self, settings: DictateSettings) -> str:
        """The note beside the form (#1214): the variables that
        pin a key."""
        return "\n".join(
            f"{GLOBAL_ENV_VARS[key]} pins this setting"
            for key in DICTATE_KEYS
            if key in settings.pinned
        )

    def load(self, settings: DictateSettings) -> None:
        """Seed the fields from the effective settings (#1214): a
        pinned key's field reads as the variable's value and
        stands read-only, the variable named beside."""
        for key in DICTATE_KEYS:
            self.seed(key, settings)
        note = self.notes(settings)
        self.pinned_note.setText(note)
        self.pinned_note.setVisible(bool(note))
        self.settled()  # the seed's own signals are not the operator's

    def pick_model(self, model: str) -> None:
        """The model picker's seed (#1214): the listed item whose
        name is the setting, so a known model shows its label with
        the size; a path or an unknown name typed free."""
        index = self.model.findData(model)
        if index >= 0:
            self.model.setCurrentIndex(index)
        else:
            self.model.setEditText(model)

    def pick_device(self, device: str) -> None:
        """The device field's seed (#1214): the listed source whose
        name is the setting, else the setting typed free."""
        index = self.device.findData(device)
        if index >= 0 and device:
            self.device.setCurrentIndex(index)
        elif not device:
            self.device.setCurrentIndex(0)
        else:
            self.device.setEditText(device)

    def device_value(self) -> str:
        """The device field's setting (#1214): the picked item's
        node name when the text is a listed item's label, else the
        typed text — a serial or a name the list does not carry."""
        index = self.device.currentIndex()
        if (
            index >= 0
            and self.device.itemText(index) == self.device.currentText()
        ):
            return self.device.itemData(index) or ""
        return self.device.currentText().strip()

    def values(self) -> dict:
        """The fields' values by key (#1214): the model's text (a
        name or a path), the device's picked name or typed text,
        and the prompt."""
        return {
            "dictate_model": self.model_value(),
            "dictate_device": self.device_value(),
            "dictate_prompt": self.prompt.text().strip(),
        }

    def file_values(self) -> dict:
        """What Apply writes (#1214): the fields' values, a pinned
        key left out (the variable wins it anyway)."""
        return {
            key: value
            for key, value in self.values().items()
            if key not in self.dictation.settings.pinned
        }

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

    def save(self, values: dict) -> None:
        """Write *values* into the user's file (#1224) — or, with no
        file to write, hold them in the document for the session
        alone. The re-read is :meth:`reread`'s, so a write that
        refuses and a re-read that refuses are two different
        flashes."""
        if not self.writable():
            self.operator = session_document(self.operator, values)
        else:
            save_keys(self.conf, values)

    def reread(self) -> None:
        """The merged document read again after a save (#1224); a
        session-only save holds its document already."""
        if self.writable():
            self.operator = reload_doc(self.conf)

    def write_and_reread(self, values: dict) -> DictateSettings | None:
        """Apply's and the download's save (#1224): the write, then
        the re-read, each flashing its own refusal — a write that
        refuses leaves the file as it was, a re-read that refuses
        comes after a write that landed and says so — and answering
        None; else the settings as the layers resolve them after
        both."""
        try:
            self.save(values)
        except (OSError, ValueError) as exc:
            self.flash(f"cannot save {self.file_name()}: {exc}", error=True)
            return None
        try:
            self.reread()
            return self.effective()
        except (OSError, ValueError) as exc:
            self.flash(
                f"saved to {self.file_name()}, but the re-read refused: {exc}",
                error=True,
            )
            return None

    def effective(self) -> DictateSettings:
        """The settings as the layers resolve them right now
        (#1214, #1224): the environment and the merged config
        document."""
        return resolve(os.environ, self.operator)

    @QtCore.Slot()
    def apply(self) -> None:
        """Save into the user's file (#1214, #1224) — a pinned key is
        left out, the variable wins it anyway — and hand the freshly
        resolved settings to the controller. A file the tree cannot
        read or write flashes the reason; a launch with no file
        applies for the session and says so."""
        settings = self.write_and_reread(self.file_values())
        if settings is None:
            return
        self.dictation.reload(settings)
        self.load(settings)
        self.refresh_model()
        self.flash(
            f"saved to {self.file_name()}" if self.writable() else SESSION_ONLY
        )
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
            settings = self.effective()
        except ValueError as exc:
            self.flash(str(exc), error=True)
            self.load(self.dictation.settings)
            return
        self.dictation.reload(settings)
        self.load(settings)
        self.refresh_model()

    @QtCore.Slot()
    def list_devices(self) -> None:
        """The source list (#1214): ``pw-dump``'s sources as the
        picker's items beside the default, the standing pick kept;
        a host without the tool keeps the default and notes it."""
        try:
            sources = self.sources()
        except DictateFailure as exc:
            self.flash(str(exc), error=True)
            return
        picked = self.device_value()
        self.device.clear()
        self.device.addItem(DEFAULT_DEVICE, "")
        for name, description in sources:
            self.device.addItem(f"{description}  ·  {name}", name)
        self.pick_device(picked)
        self.flash(f"{len(sources)} input(s) found")

    # --- the model and its download -------------------------------------

    def model_value(self) -> str:
        """The picker's setting (#1214): the listed item's model
        name when the text is one of the labels (typed or picked;
        the label carries the size in parentheses), else the typed
        text — a path, or a name the list does not carry."""
        text = self.model.currentText()
        index = self.model.findText(text)
        if index >= 0:
            return str(self.model.itemData(index))
        return text.strip() or DEFAULT_MODEL

    def model_name(self) -> str | None:
        """The picker's model as a downloadable name (#1214): None
        when it points at a path."""
        text = self.model_value()
        if os.sep in text or text.endswith(".bin"):
            return None
        return text

    def model_words(self, path: Path, name: str | None) -> str:
        """The model line's words (#1214): where the model points
        and whether it stands, with its size or the download's."""
        if name is None:
            return f"{path} — a path; nothing to download"
        if path.exists():
            return f"{path} — present ({path.stat().st_size >> 20} MB)"
        size = MODEL_SIZES.get(name, "size unknown")
        return f"{path} — missing ({size})"

    def refresh_model(self) -> None:
        """The model line and the Download button follow the picker
        (#1214): a picked name points at the cache's file for that
        name, a typed path at itself, and the button answers to
        whether that file stands — so a pick changes what a
        download would fetch, before anything is saved."""
        name = self.model_name()
        path = model_path(self.model_value(), self.cache_root)
        self.model_state.setText(self.model_words(path, name))
        self.model_state.setToolTip(str(path))
        self.download_button.setEnabled(
            name is not None
            and not path.exists()
            and self.flight is None
            and "dictate_model" not in self.dictation.settings.pinned
        )

    @QtCore.Slot()
    def download(self) -> None:
        """Fetch the named model (#1214) on a worker thread: the
        bar follows the chunks, Cancel abandons, and the landing
        refreshes the state and the controller."""
        name = self.model_name()
        if name is None or self.flight is not None:
            return
        self.cancel.clear()
        self.bar.setValue(0)
        self.bar.show()
        self.cancel_button.show()
        self.download_button.setEnabled(False)
        self.flash(f"downloading {name}…")
        self.flight = threading.Thread(
            target=self.fetch, args=(name,), daemon=True
        )
        self.flight.start()

    def fetch(self, name: str) -> None:
        """The worker thread's body (#1214): the leaf's download
        with this page's progress and cancel, the answer back
        over a signal — the requested ``name`` beside the path, so
        the landing saves the model that was fetched, not whatever
        the picker holds by then."""
        try:
            path = self.downloader(
                name,
                self.cache_root,
                progress=self.progressed.emit,
                cancel=self.cancel.is_set,
            )
        except DictateFailure as exc:
            self.downloaded.emit(name, "", str(exc))
        except Exception as exc:  # a best-effort worker: the page
            # must not stay with its bar up and its button dark
            # because the thread died — the failure is shown.
            self.downloaded.emit(name, "", f"{type(exc).__name__}: {exc}")
        else:
            self.downloaded.emit(name, str(path), "")

    @QtCore.Slot(int, int)
    def progress(self, done: int, total: int) -> None:
        """The bar under the download (#1214): the percent when the
        server names the total, a busy bar otherwise."""
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(done * 100 // total)
        else:
            self.bar.setRange(0, 0)

    @QtCore.Slot(str, str, str)
    def landed(self, name: str, path: str, error: str) -> None:
        """The download's end (#1214): the bar goes, and a failure
        or a cancel flashes its reason and leaves the fields as the
        operator left them — a pick stands for a retry. A landed
        download makes the fetched ``name`` the configured one —
        not whatever the picker holds by then (#1216 review) — and
        says so."""
        self.flight = None
        self.bar.hide()
        self.cancel_button.hide()
        self.bar.setRange(0, 100)
        if error:
            self.flash(error, error=True)
            self.refresh_model()
            return
        if self.save_model(name):
            self.flash(f"saved {Path(path).name}")

    def save_model(self, model: str) -> bool:
        """Make ``model`` the configured one (#1214): the file takes
        the model key over its standing values — the other fields
        stay as the operator left them, applied or not — and the
        layers resolve again for the controller. A model the
        environment pins is the pin's: the file is left as it
        stands (the pin wins at resolve, and a write would discard
        the operator's own saved row behind the pin's back), and
        the page says the pin still decides. False then, and when
        the file will not write or read, the reason flashed."""
        if "dictate_model" in self.dictation.settings.pinned:
            self.flash(
                f"{GLOBAL_ENV_VARS['dictate_model']} pins the model; "
                "the download stands on disk for when the pin goes"
            )
            return False
        settings = self.write_and_reread({"dictate_model": model})
        if settings is None:
            return False
        self.dictation.reload(settings)
        # The fields stand as the operator left them (#1216 review):
        # only the model line follows the save. A field that differs
        # from the file still asks for Apply; the model pick that
        # started the download is now the file's, so it does not.
        self.refresh_model()
        if self.dirty and self.matches_file():
            self.settled()
        return True

    def matches_file(self) -> bool:
        """Whether the fields stand as the file holds them (#1224):
        every key Apply would write equals the merged document's
        value, a blank field matching an absent key."""
        return all(
            (self.operator.get(key) or "") == value
            for key, value in self.file_values().items()
        )

    @QtCore.Slot()
    def abandon_download(self) -> None:
        """Cancel (#1214): the worker sees the flag before its
        next chunk and raises; the landing hides the bar."""
        if self.flight is not None:
            self.cancel.set()

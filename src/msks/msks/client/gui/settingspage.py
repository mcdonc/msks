"""The settings page (#1214): the top-level tab that edits the
dictate pill's settings — the model, the device, the gate's three
numbers, and the prompt — saves them into ``gui.yaml`` through
:mod:`msks.client.gui.guiprefs`, and carries the model download
with its progress.

The page shows the *effective* settings (:mod:`msks.client.gui.dictatekeys`)
and seeds its fields from them. Apply writes every field into
``gui.yaml`` and hands the controller the freshly resolved
settings; the operator's config file is never touched. A key the
environment pins reads as the variable's value and its field
stands read-only with the variable named, the way every other
client key behaves; a value that refused anywhere is shown with
its message and reads as the default.

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

from ..config_keys import DICTATE_KEYS, GLOBAL_ENV_VARS
from ..dictate import (
    DEFAULT_MODEL,
    MODEL_SIZES,
    DictateFailure,
    download_model,
    list_sources,
)
from ..validate import DICTATE_RANGES
from .dictatekeys import DictateSettings, resolve
from .dictatepill import pill_room
from .guiprefs import load_prefs, save_prefs
from .statusflash import StatusFlash
from .tintedlabel import TintedLabel

#: The model's size column beside the name (#1214).
MODEL_LABEL = "Model"
#: The download bar's width, pixels (#1214): a third of the
#: default window, beside the two buttons.
BAR_WIDTH = 360
DEVICE_LABEL = "Device"
DEFAULT_DEVICE = "the default input"


class SettingsPage(QtWidgets.QWidget, StatusFlash):
    """The settings tab (#1214). ``settings_saved`` carries the
    freshly resolved settings to whoever wants them; the
    controller takes them back through :meth:`apply` itself. The
    seams: the source lister and the downloader, the suite's
    fakes."""

    settings_saved = QtCore.Signal(object)
    progressed = QtCore.Signal(int, int)
    downloaded = QtCore.Signal(str, str)

    def __init__(
        self,
        dictation,
        operator: dict | None = None,
        parent=None,
        *,
        sources: Callable[[], list] = list_sources,
        downloader=download_model,
    ) -> None:
        super().__init__(parent)
        self.dictation = dictation
        self.cache_root = dictation.cache_root
        self.operator = {} if operator is None else operator
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
        """The six fields (#1214): the model as an editable picker
        over the known names (a path typed free), the device as an
        editable picker over the listed sources, the gate's three
        numbers in their validated ranges, and the prompt."""
        self.model = QtWidgets.QComboBox(self)
        self.model.setEditable(True)
        self.model.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        for name, size in MODEL_SIZES.items():
            self.model.addItem(f"{name} ({size})", name)
        self.fields["dictate_model"] = self.model
        self.device = QtWidgets.QComboBox(self)
        self.device.setEditable(True)
        self.device.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.device.addItem(DEFAULT_DEVICE, "")
        self.fields["dictate_device"] = self.device
        low, high = DICTATE_RANGES["dictate_threshold"]
        self.threshold = self.number(low, high, " dBFS")
        self.fields["dictate_threshold"] = self.threshold
        low, high = DICTATE_RANGES["dictate_margin"]
        self.margin = self.number(low, high, " dB")
        self.fields["dictate_margin"] = self.margin
        low, high = DICTATE_RANGES["dictate_silence"]
        self.silence = self.number(low, high, " s", step=0.5, floor=0.1)
        self.fields["dictate_silence"] = self.silence
        self.prompt = QtWidgets.QLineEdit(self)
        self.prompt.setPlaceholderText("msks, devenv, NixOS, pi")
        self.fields["dictate_prompt"] = self.prompt
        self.refresh_sources = QtWidgets.QPushButton("Refresh", self)
        self.refresh_sources.clicked.connect(self.list_devices)
        self.apply_button = QtWidgets.QPushButton("Apply", self)
        self.apply_button.clicked.connect(self.apply)
        self.reload_button = QtWidgets.QPushButton("Revert", self)
        self.reload_button.clicked.connect(self.revert)
        self.pinned_note = QtWidgets.QLabel("", self)
        self.pinned_note.setWordWrap(True)

    def number(
        self,
        low: float,
        high: float,
        suffix: str,
        step: float = 0.5,
        floor: float | None = None,
    ) -> QtWidgets.QDoubleSpinBox:
        """One of the gate's numbers in its validated range, one
        decimal."""
        box = QtWidgets.QDoubleSpinBox(self)
        box.setRange(floor if floor is not None else low, high)
        box.setSingleStep(step)
        box.setDecimals(1)
        box.setSuffix(suffix)
        return box

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
        form.addRow(MODEL_LABEL, self.model)
        device_row = QtWidgets.QHBoxLayout()
        device_row.addWidget(self.device)
        device_row.addWidget(self.refresh_sources)
        form.addRow(DEVICE_LABEL, device_row)
        form.addRow("Speech floor", self.threshold)
        form.addRow("Speech margin", self.margin)
        form.addRow("Silence that ends a clip", self.silence)
        form.addRow("Vocabulary prompt", self.prompt)
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
        # The page's own frame (#1214): the group boxes stop short
        # of the dictate pill's corner. The pill floats over the
        # window's top-right, and without the room the first
        # render put the Model picker's arrow under it; the other
        # tabs' tables run under the pill's band with their last
        # column's header sort arrow, which the pill covers
        # cleanly, while a form's control there would be
        # unclickable.
        page = QtWidgets.QVBoxLayout(self)
        page.setContentsMargins(0, 0, pill_room(self.font()), 0)
        page.addWidget(settings_box)
        page.addWidget(download_box)
        page.addStretch(1)
        page.addWidget(self.status)

    # --- reading and writing the settings -------------------------------

    def seed(self, key: str, settings: DictateSettings) -> None:
        """One field's seed (#1214): the effective value in the
        field's own shape, and the field read-only with the
        variable named when the environment pins the key."""
        field = self.fields[key]
        if key == "dictate_model":
            self.model.setEditText(settings.model)
        elif key == "dictate_device":
            self.pick_device(settings.device)
        elif isinstance(field, QtWidgets.QDoubleSpinBox):
            field.setValue(float(settings.value(key)))
        else:
            field.setText(str(settings.value(key)))
        pinned = key in settings.pinned
        field.setEnabled(not pinned)
        field.setToolTip(
            f"{GLOBAL_ENV_VARS[key]} pins this key; the page cannot change it"
            if pinned
            else ""
        )

    def notes(self, settings: DictateSettings) -> str:
        """The note beside the form (#1214): the variables that
        pin a key, then every refusal with its source."""
        lines = [
            f"{GLOBAL_ENV_VARS[key]} pins this setting"
            for key in DICTATE_KEYS
            if key in settings.pinned
        ]
        lines += [message for _key, message in settings.problems]
        return "\n".join(lines)

    def load(self, settings: DictateSettings) -> None:
        """Seed the fields from the effective settings (#1214): a
        pinned key's field reads as the variable's value and
        stands read-only, the variable named beside; a refused
        value's message stands beside too."""
        for key in DICTATE_KEYS:
            self.seed(key, settings)
        note = self.notes(settings)
        self.pinned_note.setText(note)
        self.pinned_note.setVisible(bool(note))

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
        the three numbers, and the prompt."""
        return {
            "dictate_model": self.model.currentText().strip(),
            "dictate_device": self.device_value(),
            "dictate_threshold": self.threshold.value(),
            "dictate_margin": self.margin.value(),
            "dictate_silence": self.silence.value(),
            "dictate_prompt": self.prompt.text().strip(),
        }

    def effective(self) -> DictateSettings:
        """The settings as the layers resolve them right now
        (#1214): the environment, ``gui.yaml``, and the operator's
        file. A file that will not read is the caller's to show."""
        return resolve(os.environ, load_prefs(), self.operator)

    @QtCore.Slot()
    def apply(self) -> None:
        """Save into ``gui.yaml`` (#1214) — a pinned key is left
        out, the variable wins it anyway — and hand the freshly
        resolved settings to the controller. A file the tree
        cannot write flashes the reason."""
        values = {
            key: value
            for key, value in self.values().items()
            if key not in self.dictation.settings.pinned
        }
        try:
            save_prefs(values)
            settings = self.effective()
        except OSError as exc:
            self.flash(f"cannot save gui.yaml: {exc}", error=True)
            return
        except ValueError as exc:
            self.flash(str(exc), error=True)
            return
        self.dictation.reload(settings)
        self.load(settings)
        self.refresh_model()
        self.flash("saved to gui.yaml")
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

    def model_name(self) -> str | None:
        """The model field as a downloadable name (#1214): None
        when it points at a path."""
        text = self.model.currentText().strip() or DEFAULT_MODEL
        if os.sep in text or text.endswith(".bin"):
            return None
        return text

    def model_words(self, path: Path, name: str | None) -> str:
        """The model state's line (#1214): where the model points
        and whether it stands, with its size or the download's."""
        if name is None:
            return f"{path} — a path; nothing to download"
        if path.exists():
            return f"{path} — present ({path.stat().st_size >> 20} MB)"
        size = MODEL_SIZES.get(name, "size unknown")
        return f"{path} — missing ({size})"

    def refresh_model(self) -> None:
        """Where the model points and whether it stands (#1214):
        the download button answers to it."""
        path = self.dictation.settings.model_file(self.cache_root)
        name = self.model_name()
        self.model_state.setText(self.model_words(path, name))
        self.model_state.setToolTip(str(path))
        self.download_button.setEnabled(name is not None and not path.exists())

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
        over a signal."""
        try:
            path = self.downloader(
                name,
                self.cache_root,
                progress=self.progressed.emit,
                cancel=self.cancel.is_set,
            )
        except DictateFailure as exc:
            self.downloaded.emit("", str(exc))
        except Exception as exc:  # a best-effort worker: the page
            # must not stay with its bar up and its button dark
            # because the thread died — the failure is shown.
            self.downloaded.emit("", f"{type(exc).__name__}: {exc}")
        else:
            self.downloaded.emit(str(path), "")

    @QtCore.Slot(int, int)
    def progress(self, done: int, total: int) -> None:
        """The bar under the download (#1214): the percent when the
        server names the total, a busy bar otherwise."""
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(done * 100 // total)
        else:
            self.bar.setRange(0, 0)

    @QtCore.Slot(str, str)
    def landed(self, path: str, error: str) -> None:
        """The download's end (#1214): the bar goes, the model
        state reads again, and the controller re-checks its
        model; a failure or a cancel flashes its reason."""
        self.flight = None
        self.bar.hide()
        self.cancel_button.hide()
        self.bar.setRange(0, 100)
        if error:
            self.flash(error, error=True)
            self.reload_layers()
            return
        # The model was downloaded to be used: the field's values
        # are saved as Apply would save them, so the controller's
        # re-read takes the downloaded model instead of the one
        # the files still name.
        self.apply()
        self.flash(f"saved {Path(path).name}")

    def reload_layers(self) -> None:
        """The layers read again after a download's end (#1214),
        the controller and the model state following; a file that
        will not read flashes its reason."""
        try:
            settings = self.effective()
        except ValueError as exc:
            self.flash(str(exc), error=True)
            return
        self.dictation.reload(settings)
        self.load(settings)
        self.refresh_model()

    @QtCore.Slot()
    def abandon_download(self) -> None:
        """Cancel (#1214): the worker sees the flag before its
        next chunk and raises; the landing hides the bar."""
        if self.flight is not None:
            self.cancel.set()

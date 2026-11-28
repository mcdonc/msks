"""The Dictation subtab (#1214, #1226): the dictate pill's settings
— the model, the device, and the prompt — over the speech model's
download with its progress. The panel moved here whole when the
settings page grew its subtabs; the page keeps the save, the
Apply/Revert pair, and the status line.

The form shows the *effective* settings
(:mod:`msks.client.gui.dictatekeys`) and seeds its fields from them.
A key the environment pins reads as the variable's value and its
field stands read-only with the variable named. The download
fetches a *named* model on a worker thread with the progress bar
under it; a path setting (a NixOS store path) downloads nothing and
the panel says where it points. The device list comes from
``pw-dump`` when the host has it; the field stays editable for a
serial or a name the list does not carry.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ...config_keys import DICTATE_KEYS, GLOBAL_ENV_VARS
from ...dictate import (
    DEFAULT_MODEL,
    MODEL_SIZES,
    DictateFailure,
    download_model,
    list_sources,
    model_path,
)
from ..dictatekeys import DictateSettings
from .fields import Panel
from .vocabulary import DICTATION_LABEL, REFRESH_TIP

#: The download bar's width, pixels (#1214): a third of the
#: default window, beside the two buttons.
BAR_WIDTH = 360

#: The device picker's first item (#1214): the desktop's own
#: choice, its data the blank that selects no source.
DEFAULT_DEVICE = "the default input"


class DictatePanel(Panel):
    """The Dictation subtab (#1214, #1226). The seams: the source
    lister and the downloader, the suite's fakes."""

    progressed = QtCore.Signal(int, int)
    downloaded = QtCore.Signal(str, str, str)

    def __init__(
        self,
        page,
        parent=None,
        *,
        sources: Callable[[], list] = list_sources,
        downloader=download_model,
    ) -> None:
        super().__init__(page, DICTATION_LABEL, parent)
        self.dictation = page.dictation
        self.cache_root = self.dictation.cache_root
        self.sources = sources
        self.downloader = downloader
        #: The download in flight (#1214): its cancel flag, and the
        #: thread it runs on.
        self.cancel = threading.Event()
        self.flight: threading.Thread | None = None
        self.build_form()
        self.build_download()
        self.progressed.connect(self.progress)
        self.downloaded.connect(self.landed)
        self.load(self.dictation.settings)

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
        self.row("dictate_model", self.model)
        self.device = QtWidgets.QComboBox(self)
        self.device.setEditable(True)
        self.device.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.device.addItem(DEFAULT_DEVICE, "")
        self.refresh_sources = QtWidgets.QPushButton("Refresh", self)
        self.refresh_sources.clicked.connect(self.list_devices)
        self.refresh_sources.setToolTip(REFRESH_TIP)
        self.row("dictate_device", self.device, self.refresh_sources)
        self.prompt = QtWidgets.QLineEdit(self)
        # No placeholder (#1214): the gray example text read as a
        # value already in effect. The tooltip carries the examples,
        # and an empty prompt is visibly empty.
        self.row("dictate_prompt", self.prompt)

    def build_download(self) -> None:
        """The model download (#1214): where the model points and
        whether it stands, the download button, the bar, and the
        cancel beside it — in a group of their own under the
        form."""
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
        box = QtWidgets.QGroupBox("Speech model", self)
        down = QtWidgets.QVBoxLayout(box)
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
        self.body.insertWidget(self.body.count() - 2, box)

    # --- reading the settings -------------------------------------------

    def write(self, key: str, value: str) -> None:
        """One field's seed in the field's own shape (#1214): the
        model picker shows a known name's label, the device picker
        a listed source's, else the value typed free."""
        if key == "dictate_model":
            self.pick_model(value)
        elif key == "dictate_device":
            self.pick_device(value)
        else:
            self.fields[key].setText(str(value))

    def load(self, settings: DictateSettings | None = None) -> None:
        """Seed the fields from the effective settings (#1214): a
        pinned key's field reads as the variable's value and
        stands read-only, the variable named beside."""
        settings = self.dictation.settings if settings is None else settings
        for key in DICTATE_KEYS:
            self.seed(key, str(settings.value(key)), key in settings.pinned)
        self.refresh_model()

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

    def edits(self) -> dict[str, str | None]:
        """What Apply writes (#1214, #1226): the fields whose value
        moved from the seed, a pinned key left out (the variable
        wins it anyway)."""
        return {
            key: value or None
            for key, value in self.values().items()
            if key not in self.page.pinned and value != self.seeded.get(key)
        }

    def stands_as_file(self) -> bool:
        """Whether the fields stand as the file holds them (#1224):
        every key Apply would write equals the merged document's
        value, a blank field matching an absent key."""
        return all(
            (self.page.doc.get(key) or "") == value
            for key, value in self.values().items()
            if key not in self.page.pinned
        )

    @QtCore.Slot()
    def list_devices(self) -> None:
        """The source list (#1214): ``pw-dump``'s sources as the
        picker's items beside the default, the standing pick kept;
        a host without the tool keeps the default and notes it."""
        try:
            sources = self.sources()
        except DictateFailure as exc:
            self.page.flash(str(exc), error=True)
            return
        picked = self.device_value()
        # The refill is no edit (#1226): the standing pick returns
        # and the dirty flag stays as the operator left it.
        self.filling = True
        self.device.clear()
        self.device.addItem(DEFAULT_DEVICE, "")
        for name, description in sources:
            self.device.addItem(f"{description}  ·  {name}", name)
        self.pick_device(picked)
        self.filling = False
        self.page.flash(f"{len(sources)} input(s) found")

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
            and "dictate_model" not in self.page.pinned
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
        self.page.flash(f"downloading {name}…")
        self.flight = threading.Thread(
            target=self.fetch, args=(name,), daemon=True
        )
        self.flight.start()

    def fetch(self, name: str) -> None:
        """The worker thread's body (#1214): the leaf's download
        with this panel's progress and cancel, the answer back
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
            self.page.flash(error, error=True)
            self.refresh_model()
            return
        if self.save_model(name):
            self.page.flash(f"saved {Path(path).name}")

    def save_model(self, model: str) -> bool:
        """Make ``model`` the configured one (#1214): the file takes
        the model key over its standing values — the other fields
        stay as the operator left them, applied or not — and the
        layers resolve again for the controller. A model the
        environment pins is the pin's: the file is left as it
        stands (the pin wins at resolve, and a write would discard
        the operator's own saved row behind the pin's back), and
        the panel says the pin still decides. False then, and when
        the file will not write or read, the reason flashed."""
        if "dictate_model" in self.page.pinned:
            self.page.flash(
                f"{GLOBAL_ENV_VARS['dictate_model']} pins the model; "
                "the download stands on disk for when the pin goes"
            )
            return False
        settings = self.page.write_and_reread({"dictate_model": model})
        if settings is None:
            return False
        self.dictation.reload(settings)
        # The fields stand as the operator left them (#1216 review):
        # only the model line follows the save. A field that differs
        # from the file still asks for Apply; the model pick that
        # started the download is now the file's, so it does not.
        self.refresh_model()
        if self.page.dirty and self.page.matches_file():
            self.page.settled()
        return True

    @QtCore.Slot()
    def abandon_download(self) -> None:
        """Cancel (#1214): the worker sees the flag before its
        next chunk and raises; the landing hides the bar."""
        if self.flight is not None:
            self.cancel.set()

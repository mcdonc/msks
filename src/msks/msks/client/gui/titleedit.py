"""The in-place title edit the shell tabs know, on the page's strip
and on a torn-off window's (#954, #1203): a line edit that opens
over the tab it renames, with the standing text selected. Return stands the
edit on the hub through :attr:`TitleEdit.done`, Escape cancels it,
and both end the edit; the owner forgets the editor on the same
signal.

The edit keys on the session's number, never on the place the
editor covers (#954): tabs close and shift under an open editor,
and a positional key would land the name on another session.

The editor claims its Escape before the shortcut map does (#1099):
a page stands its Back shortcut on the bare key, Qt asks the
focused widget first through a ``ShortcutOverride`` event, and the
accept yields the shortcut while keeping the press for the cancel.
A torn-off window stands no shortcut on the key; the claim is
harmless there.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time.
"""

from PySide6 import QtCore, QtWidgets


class TitleEdit(QtWidgets.QLineEdit):
    """One in-place title edit (#954, #1203): the session's number
    and workspace ride along, so the commit lands on the session
    the edit named whatever moved under the editor meanwhile."""

    #: The edit ended — committed or cancelled (#1203): the owner
    #: takes the editor down. The commit's rename lands before
    #: the signal, so a handler reading the hub sees the name.
    done = QtCore.Signal()

    def __init__(
        self, hub, workspace_id: str, no: int, text: str, parent
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace_id = workspace_id
        self.no = no
        self.setText(text)
        self.selectAll()
        self.returnPressed.connect(self.commit)

    def commit(self) -> None:
        """Return stands the edit (#954): the hub takes the text
        — it strips the name, and an empty edit reads as the
        cancel, changing nothing — on the session the edit named
        while one still stands."""
        session = self.hub.find(self.workspace_id, self.no)
        if session is not None:
            self.hub.rename(self.workspace_id, self.no, self.text())
        self.done.emit()

    def cancel(self) -> None:
        """Escape takes the edit down as a cancel (#954): the
        standing title keeps."""
        self.done.emit()

    def event(self, event) -> bool:  # noqa: N802
        """The two events the editor keeps for itself (#954,
        #1099): the Escape press cancels, and its override
        question — the one a shortcut on the bare key asks the
        focused widget before the dispatch — is accepted, so the
        press reaches the cancel and not the page's Back. Every
        other event is the line edit's own."""
        kind = event.type()
        if kind == QtCore.QEvent.Type.ShortcutOverride and (
            event.key() == QtCore.Qt.Key.Key_Escape
        ):
            event.accept()
            return True
        if kind == QtCore.QEvent.Type.KeyPress and (
            event.key() == QtCore.Qt.Key.Key_Escape
        ):
            self.cancel()
            return True
        return super().event(event)

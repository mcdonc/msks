"""The settings page's suite (#1226): the subtab strip over every
client ``msks.yaml`` key — the partition, the seed from the merged
document, the edits Apply writes (a field the operator leaves alone
stays out of the file, a cleared field unsets its key, a pinned key
is left to the variable), the Connection subtab's ``daemons:``
editor over the in-place alias merge, the session-only apply, the
arrows across the strip, and the vocabulary's resolution. The
dictation subtab's own suite stays in ``test_gui_dictatepill``.
Offscreen, over the controller's recorded seams, with the client's
variables blanked so no shell export pins a field."""

from __future__ import annotations

import textwrap

import pytest
from msks.client.gui.settingspage.vocabulary import (
    SUBTAB_LABELS,
    TIPS,
    display_value,
    effective,
    pinned_keys,
)
from test_client_config import CLIENT_VARS
from test_gui_dictatepill import (
    controller,
    file_doc,
    launched,
    spin,
    user_file,
)
from test_gui_dictatepill import (
    qt as qt,
)
from test_gui_tabs import press

USER_TEXT = textwrap.dedent(
    """\
    # mine
    cache_dir: /user-cache
    # the launcher
    terminal_open_cmd:
      - konsole
      - -e
    ssh_options: -A -o ServerAliveInterval=30
    screenshot_port: 8662
    """
)

SYSTEM = textwrap.dedent(
    """\
    # the host's file
    cafile: /etc/msks/hv-1-ca.pem
    cache_dir: /sys-cache
    daemons:
      host:
        url: https://host:8660
      lab:
        url: https://lab:8660
    """
)

ALIASES_TEXT = textwrap.dedent(
    """\
    url: https://127.0.0.1:8660

    # my daemons
    daemons:
      # the lab
      lab:
        url: https://my-lab:8660
        token_file: ~/lab.token
      dev:
        socket: /run/user/1000/msks/msksd.sock
    """
)


@pytest.fixture(autouse=True)
def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every client variable blanked: a devenv shell exports
    ``MSKSC_CACHE_DIR`` and ``MSKSC_IDENTITY_FILE``, and a pinned
    field would make the suite read the host's shell."""
    for name in CLIENT_VARS:
        monkeypatch.setenv(name, "")


def system_file(qt, text: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The system file under the temporary tree, pointed at by the
    relocation variable; the next launch reads it under the user
    file."""
    path = qt.tmp / "system" / "msks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    monkeypatch.setenv("MSKSC_SYSTEM_CONFIG", str(path))


def window_over(qt, page):
    """The page shown in a real offscreen window, kept alive for the
    test's span — the shape a focus test reads."""
    from PySide6 import QtWidgets

    window = QtWidgets.QMainWindow()
    window.setCentralWidget(page)
    window.show()
    qt.keep.append(window)
    return window


def page_over(qt, conf=None, **seams):
    """A settings page over a real controller and a resolved
    launch, with the seams passed through."""
    from msks.client.gui.settingspage import SettingsPage

    conf = launched(qt) if conf is None else conf
    held, _fake = controller(qt, conf.doc)
    return SettingsPage(held, conf, **seams), held


def field(page, panel: str, key: str):
    """One panel's field by key."""
    return getattr(page, panel).fields[key]


def test_every_key_has_a_field_and_the_strip_partitions_them(qt) -> None:
    """The partition (#1226): the strip carries the five subtabs in
    order, every scalar key the file accepts sits on exactly one
    panel, the groups are the chapter's, and every field and label
    carries its description."""
    from msks.client.config_keys import GLOBAL_ENV_VARS

    page, _held = page_over(qt)
    assert [page.bar.tabText(i) for i in range(page.bar.count())] == list(
        SUBTAB_LABELS
    )
    assert set(page.connection.fields) == {
        "url",
        "socket",
        "token_file",
        "cafile",
        "expected_image",
        "active_daemon",
    }
    assert set(page.files.fields) == {
        "cache_dir",
        "state_dir",
        "identity_file",
        "pubkey_file",
        "image_manifest_url",
    }
    assert set(page.shell.fields) == {
        "terminal_open_cmd",
        "ssh_options",
    }
    assert set(page.screenshot.fields) == {
        "screenshot_command",
        "screenshot_port",
    }
    assert set(page.dictate.fields) == {
        "dictate_model",
        "dictate_device",
        "dictate_prompt",
    }
    every = set()
    for panel in page.panels:
        for key, widget in panel.fields.items():
            every.add(key)
            assert widget.toolTip() == TIPS[key]
            assert len(TIPS[key]) > 60  # a description, not a label
            assert panel.labels[key].toolTip() == TIPS[key]
    assert every == {
        *GLOBAL_ENV_VARS,
        "terminal_open_cmd",
        "ssh_options",
        "active_daemon",
    }


def test_the_panels_seed_from_the_document_and_write_the_edits_alone(
    qt,
) -> None:
    """The seed and the save (#1226): a list-form command shows its
    string form, a port its digits; Apply writes the fields that
    moved and nothing else — the list form the operator left alone
    stays a list, byte for byte, and the comments stay."""
    page, _held = page_over(qt, launched(qt, text=USER_TEXT))
    assert field(page, "files", "cache_dir").text() == "/user-cache"
    assert field(page, "shell", "terminal_open_cmd").text() == "konsole -e"
    assert field(page, "shell", "ssh_options").text() == (
        "-A -o ServerAliveInterval=30"
    )
    assert field(page, "screenshot", "screenshot_port").text() == "8662"
    assert field(page, "screenshot", "screenshot_command").text() == ""
    assert not page.apply_button.isEnabled()
    field(page, "files", "cache_dir").setText("/elsewhere")
    field(page, "screenshot", "screenshot_port").setText("8700")
    assert page.file_edits() == (
        {"cache_dir": "/elsewhere", "screenshot_port": 8700},
        {},
    )
    page.apply()
    assert page.status.text() == "saved to msks.yaml"
    text = user_file(qt).read_text()
    assert text == textwrap.dedent(
        """\
        # mine
        cache_dir: /elsewhere
        # the launcher
        terminal_open_cmd:
          - konsole
          - -e
        ssh_options: -A -o ServerAliveInterval=30
        screenshot_port: 8700
        """
    )
    assert file_doc(qt)["terminal_open_cmd"] == ["konsole", "-e"]
    assert not page.dirty and not page.apply_button.isEnabled()


def test_a_cleared_field_unsets_its_key_and_the_system_file_applies_again(
    qt, monkeypatch
) -> None:
    """The unset path (#1226): the user file's value clears, Apply
    deletes the key, and the system file's value under it shows and
    applies again. A key only the system file carries shows its
    value and stays out of the user file: an inherited value stays
    inherited through a save of other fields."""
    system_file(qt, SYSTEM, monkeypatch)
    page, _held = page_over(qt, launched(qt, text="cache_dir: /user-cache\n"))
    assert field(page, "files", "cache_dir").text() == "/user-cache"
    assert (
        field(page, "connection", "cafile").text() == "/etc/msks/hv-1-ca.pem"
    )
    field(page, "files", "cache_dir").setText("")
    field(page, "files", "state_dir").setText("/state")
    assert page.file_edits() == (
        {"cache_dir": None, "state_dir": "/state"},
        {},
    )
    page.apply()
    assert file_doc(qt) == {"state_dir": "/state"}
    assert page.operator["cache_dir"] == "/sys-cache"
    assert field(page, "files", "cache_dir").text() == "/sys-cache"
    assert "cafile" not in file_doc(qt)
    # Clearing the inherited value is no edit in the file: the key is
    # absent already, and the system file's value stands.
    field(page, "files", "cache_dir").setText("")
    page.apply()
    assert file_doc(qt) == {"state_dir": "/state"}
    assert field(page, "files", "cache_dir").text() == "/sys-cache"


def test_a_pinned_key_reads_read_only_on_its_subtab_and_apply_leaves_it(
    qt, monkeypatch
) -> None:
    """A key a variable pins (#1214, #1226): its field shows the
    variable's value, stands read-only with the notice on the field
    and its label, the panel's note names the variable, and Apply
    leaves the key out of the file — on every subtab alike."""
    monkeypatch.setenv("MSKSC_CACHE_DIR", "/env-cache")
    monkeypatch.setenv("MSKSC_SSH_OPTIONS", "-o Pin=yes")
    monkeypatch.setenv("MSKSC_SCREENSHOT_PORT", "9999")
    monkeypatch.setenv("MSKSC_SCREENSHOT_COMMAND", "  ")  # blank: unset
    page, _held = page_over(qt, launched(qt))
    for panel, key, value in (
        ("files", "cache_dir", "/env-cache"),
        ("shell", "ssh_options", "-o Pin=yes"),
        ("screenshot", "screenshot_port", "9999"),
    ):
        widget = field(page, panel, key)
        assert widget.text() == value
        assert widget.isEnabled() is False
        notice = (
            f"{page.variable_of(key)} pins this setting; "
            "the page cannot change it."
        )
        assert widget.toolTip().endswith(notice)
        assert widget.toolTip().startswith(TIPS[key])
        assert getattr(page, panel).labels[key].toolTip() == widget.toolTip()
    assert page.files.pinned_note.text() == "MSKSC_CACHE_DIR pins this setting"
    assert (
        page.shell.pinned_note.text() == "MSKSC_SSH_OPTIONS pins this setting"
    )
    assert page.screenshot.pinned_note.text() == (
        "MSKSC_SCREENSHOT_PORT pins this setting"
    )
    assert field(page, "screenshot", "screenshot_command").isEnabled() is True
    assert page.connection.pinned_note.isHidden()
    field(page, "files", "cache_dir").setText("/typed-anyway")
    field(page, "shell", "terminal_open_cmd").setText("xterm -e")
    page.apply()
    doc = file_doc(qt)
    assert doc == {"terminal_open_cmd": ["xterm", "-e"]}
    assert field(page, "files", "cache_dir").text() == "/env-cache"


def test_the_dirty_flag_marks_the_edited_subtab(qt) -> None:
    """The marker (#1226): the subtab holding a changed field wears
    the marker from the first change to the settle, the other tabs
    stay plain, and Apply clears every marker with the dirty flag."""
    page, _held = page_over(qt, launched(qt))
    assert [page.bar.tabText(i) for i in range(page.bar.count())] == list(
        SUBTAB_LABELS
    )
    field(page, "shell", "terminal_open_cmd").setText("xterm -e")
    assert page.dirty and page.apply_button.isEnabled()
    assert page.bar.tabText(2) == "* Terminal and ssh"
    assert page.bar.tabText(0) == "Connection"
    field(page, "screenshot", "screenshot_command").setText(
        "import -window root"
    )
    assert page.bar.tabText(3) == "* Screenshot"
    page.revert()
    assert not page.dirty
    assert [page.bar.tabText(i) for i in range(page.bar.count())] == list(
        SUBTAB_LABELS
    )


def test_the_strip_crosses_by_arrows(qt, monkeypatch) -> None:
    """The spatial-navigation rule (#962, #1226): Down on the strip
    enters the panel below at its first tab stop, Up from a panel's
    first field returns to the strip, and the editable combo's Up
    on the Dictation subtab leaves the same way instead of cycling
    the models."""
    from msks.client.gui.settingspage import SettingsPage

    conf = launched(qt)
    held, _fake = controller(qt, conf.doc)
    page = SettingsPage(held, conf)
    window = window_over(qt, page)
    spin(qt, 20)
    page.bar.setFocus()
    press(qt, page.bar, qt.QtCore.Qt.Key.Key_Down)
    assert window.focusWidget() is field(page, "connection", "url")
    press(qt, window.focusWidget(), qt.QtCore.Qt.Key.Key_Up)
    assert window.focusWidget() is page.bar
    page.bar.setCurrentIndex(1)
    press(qt, page.bar, qt.QtCore.Qt.Key.Key_Down)
    assert window.focusWidget() is field(page, "files", "cache_dir")
    page.bar.setCurrentIndex(4)
    press(qt, page.bar, qt.QtCore.Qt.Key.Key_Down)
    assert window.focusWidget() is page.dictate.model
    assert page.dictate.model.lineEdit().hasFocus()  # the cursor sits there
    standing = page.dictate.model.currentIndex()
    press(qt, window.focusWidget(), qt.QtCore.Qt.Key.Key_Up)
    assert window.focusWidget() is page.bar
    assert page.dictate.model.currentIndex() == standing  # no cycling
    # A pinned first field is no tab stop: Down lands on the row
    # after it, and that row's Up returns to the strip the same way.
    page.bar.setCurrentIndex(1)
    monkeypatch.setenv("MSKSC_CACHE_DIR", "/env-cache")
    page.load()
    assert field(page, "files", "cache_dir").isEnabled() is False
    press(qt, page.bar, qt.QtCore.Qt.Key.Key_Down)
    assert window.focusWidget() is field(page, "files", "state_dir")
    press(qt, window.focusWidget(), qt.QtCore.Qt.Key.Key_Up)
    assert window.focusWidget() is page.bar


def test_browse_buttons_land_the_pick_in_the_field(qt, monkeypatch) -> None:
    """A **Browse…** button (#1226) opens the picker over the
    standing path and lands the pick in its field; a cancelled
    dialog changes nothing."""
    from msks.client.gui.settingspage import fields as fields_mod

    picks: list[tuple[str, str | None]] = []

    def fake_file(parent, start):
        picks.append(("file", start))
        return "/picked/key"

    def fake_dir(parent, start):
        picks.append(("dir", start))
        return ""

    monkeypatch.setattr(fields_mod, "pick_file", fake_file)
    monkeypatch.setattr(fields_mod, "pick_directory", fake_dir)
    page, _held = page_over(qt)
    from PySide6 import QtWidgets

    page.findChild(QtWidgets.QPushButton, "browse-identity_file").click()
    assert field(page, "files", "identity_file").text() == "/picked/key"
    field(page, "files", "cache_dir").setText("/standing")
    page.findChild(QtWidgets.QPushButton, "browse-cache_dir").click()
    assert field(page, "files", "cache_dir").text() == "/standing"
    assert picks == [("file", None), ("dir", "/standing")]


def test_the_connection_panel_edits_the_aliases_in_place(qt) -> None:
    """The ``daemons:`` editor (#1226): the list and the active
    picker seed from the merged document, the selected entry fills
    the form (a socket entry's credential fields dark), an edit, an
    add, a rename, and a remove land through Apply as in-place
    block edits — the comments stay, the edited alias is rewritten,
    the renamed one moves, the removed one goes, the added one
    joins — and the active alias saves as a scalar."""
    from msks.client.gui.settingspage import connection as conn_mod

    page, _held = page_over(qt, launched(qt, text=ALIASES_TEXT))
    panel = page.connection
    assert panel.daemons == {
        "lab": {"url": "https://my-lab:8660", "token_file": "~/lab.token"},
        "dev": {"socket": "/run/user/1000/msks/msksd.sock"},
    }
    # Moving the selection is no edit: the form fills, the entry
    # stands, and the dirty flag stays down.
    panel.aliases.setCurrentRow(1)
    panel.select("lab")
    assert not page.dirty and panel.daemon_edits() == {}
    assert [panel.aliases.item(i).text() for i in range(2)] == [
        "lab — https://my-lab:8660",
        "dev — /run/user/1000/msks/msksd.sock",
    ]
    assert [panel.active.itemData(i) for i in range(panel.active.count())] == [
        "",
        "lab",
        "dev",
    ]
    assert panel.active.currentData() == "" and panel.selected() == "lab"
    # A socket entry: its kind, its credential fields dark.
    panel.aliases.setCurrentRow(1)
    assert panel.kind.currentData() == "socket"
    assert panel.address.text() == "/run/user/1000/msks/msksd.sock"
    assert panel.token.isEnabled() is False and panel.ca.isEnabled() is False
    # Edit the lab entry: the address moves, the image joins.
    panel.select("lab")
    assert (
        panel.kind.currentData() == "url" and panel.token.isEnabled() is True
    )
    assert panel.token.text() == "~/lab.token"
    panel.address.setText("https://lab-2:8660")
    panel.image.setText("msks/debian13:1.1")
    assert panel.daemons["lab"] == {
        "url": "https://lab-2:8660",
        "token_file": "~/lab.token",
        "expected_image": "msks/debian13:1.1",
    }
    assert panel.aliases.item(0).text() == "lab — https://lab-2:8660"
    assert page.bar.tabText(0) == "* Connection"
    # Add one through the prompt seam, give it a url.
    asked: list[tuple] = []
    panel.ask = lambda parent, title, label, current="": (
        asked.append((title, label, current)) or "farm"
    )
    panel.add_alias()
    assert asked == [(conn_mod.ADD_TITLE, conn_mod.ADD_LABEL, "")]
    assert panel.selected() == "farm" and panel.daemons["farm"] == {}
    assert panel.kind.currentData() == "url"
    panel.address.setText("https://farm:8660")
    # Rename dev with the active alias naming it; the prompt opens
    # with the standing name, and the active pick follows.
    panel.ask = lambda parent, title, label, current="": (
        asked.append((title, label, current)) or "local"
    )
    panel.active.setCurrentIndex(panel.active.findData("dev"))
    panel.select("dev")
    panel.rename_alias()
    assert asked[-1] == (conn_mod.RENAME_TITLE, "New name for dev:", "dev")
    assert set(panel.daemons) == {"lab", "farm", "local"}
    assert panel.selected() == "local"
    assert panel.active.currentData() == "local"
    # Remove it: the active pick clears, nothing names a gone alias.
    panel.remove_alias()
    assert set(panel.daemons) == {"lab", "farm"}
    assert panel.active.count() == 3  # none, lab, farm
    assert panel.active.currentData() == ""
    panel.active.setCurrentIndex(panel.active.findData("farm"))
    scalars, daemons = page.file_edits()
    assert scalars == {"active_daemon": "farm"}
    assert panel.removed == {"dev"}  # the seed's name, the one written
    assert daemons == {
        "lab": {
            "url": "https://lab-2:8660",
            "token_file": "~/lab.token",
            "expected_image": "msks/debian13:1.1",
        },
        "dev": None,
        "farm": {"url": "https://farm:8660"},
    }
    page.apply()
    assert page.status.text() == "saved to msks.yaml"
    assert user_file(qt).read_text() == textwrap.dedent(
        """\
        url: https://127.0.0.1:8660

        # my daemons
        daemons:
          # the lab
          lab:
            url: https://lab-2:8660
            token_file: ~/lab.token
            expected_image: msks/debian13:1.1
          farm:
            url: https://farm:8660
        active_daemon: farm
        """
    )
    assert file_doc(qt)["daemons"] == {
        "lab": {
            "url": "https://lab-2:8660",
            "token_file": "~/lab.token",
            "expected_image": "msks/debian13:1.1",
        },
        "farm": {"url": "https://farm:8660"},
    }
    assert panel.daemons == panel.seeded_daemons == file_doc(qt)["daemons"]
    assert panel.removed == set()
    assert not page.dirty and page.bar.tabText(0) == "Connection"


def test_an_entry_without_an_address_is_refused_and_nothing_is_written(
    qt,
) -> None:
    """The loader's one-address rule through the editor (#1226): an
    entry with no address flashes its refusal, the file stands as it
    was, the fields keep the edit, and the marker stays."""
    page, _held = page_over(qt, launched(qt, text=ALIASES_TEXT))
    panel = page.connection
    panel.ask = lambda *args, **kwargs: "blank"
    panel.add_alias()
    before = user_file(qt).read_text()
    page.apply()
    assert "daemon 'blank' needs a url or a socket" in page.status.text()
    assert str(user_file(qt)) in page.status.text()
    assert user_file(qt).read_text() == before
    assert page.dirty and page.bar.tabText(0) == "* Connection"
    assert "blank" in panel.daemons


def test_the_one_address_rule_runs_on_the_merged_entry(
    qt, monkeypatch
) -> None:
    """An alias the system file gives a url (#1224, #1226): the
    editor shows the merged entry, and switching its kind to a
    socket is refused on the save — the entry merged with the
    floor's carries both — with nothing written."""
    system_file(
        qt,
        "daemons:\n  lab:\n    url: https://lab:8660\n    token_file: /tok\n",
        monkeypatch,
    )
    page, _held = page_over(qt, launched(qt, text="daemons:\n  lab: {}\n"))
    panel = page.connection
    assert panel.daemons == {
        "lab": {"url": "https://lab:8660", "token_file": "/tok"}
    }
    panel.kind.setCurrentIndex(panel.kind.findData("socket"))
    panel.address.setText("/run/lab.sock")
    before = user_file(qt).read_text()
    page.apply()
    assert "carries both a url and a socket" in page.status.text()
    assert user_file(qt).read_text() == before


def test_an_alias_the_system_file_defines_comes_back_after_a_remove(
    qt, monkeypatch
) -> None:
    """A removed alias the system file defines (#1226): the user
    file loses nothing it held, the alias stands in the merged
    document after the save, and the panel says where it comes
    from."""
    system_file(qt, SYSTEM, monkeypatch)
    page, _held = page_over(qt, launched(qt))
    panel = page.connection
    panel.select("host")
    panel.remove_alias()
    page.apply()
    assert page.status.text() == (
        "host still stands: the system file defines it"
    )
    assert set(panel.daemons) == {"host", "lab"}
    assert file_doc(qt) == {}


def test_a_port_out_of_range_is_refused(qt) -> None:
    """The loader's range check through the field (#1226): a port
    out of range flashes the refusal with the path, and the file
    stands as it was."""
    page, _held = page_over(qt, launched(qt))
    field(page, "screenshot", "screenshot_port").setText("99999")
    page.apply()
    assert "screenshot_port 99999 is out of range" in page.status.text()
    assert str(user_file(qt)) in page.status.text()
    assert file_doc(qt) == {}
    assert page.dirty


def test_a_launch_without_a_file_stands_the_file_backed_subtabs_dark(
    qt,
) -> None:
    """A launch under ``--config=none`` (#1224, #1226): no file to
    write, and the file-backed keys were read at launch — so the
    four file-backed subtabs stand dark under a note, and only the
    Dictation subtab's Apply applies for the session and says so.
    Nothing is written."""
    from msks.client import config as client_config
    from msks.client.gui.settingspage import NO_FILE_NOTE, SESSION_ONLY

    conf = client_config.resolve(config="none")
    page, _held = page_over(qt, conf)
    for panel in (page.connection, page.files, page.shell, page.screenshot):
        assert panel.isEnabled() is False
        assert panel.pinned_note.text() == NO_FILE_NOTE
        assert panel.pinned_note.isHidden() is False
        assert page.file_edits() == ({}, {})  # nothing to write from them
    assert page.dictate.isEnabled() and page.dictate.pinned_note.isHidden()
    page.dictate.device.setEditText("mic-session")
    page.apply()
    assert page.status.text() == SESSION_ONLY
    assert _held.settings.device == "mic-session"
    assert not user_file(qt).exists()
    # The note survives the reseed the apply ran.
    assert page.files.pinned_note.text() == NO_FILE_NOTE
    # A window built with no config at all stands the same way.
    from msks.client.gui.settingspage import SettingsPage

    bare = SettingsPage(_held, None)
    assert bare.files.isEnabled() is False
    assert bare.files.pinned_note.text() == NO_FILE_NOTE


def test_session_document_lands_the_values_and_drops_blanks() -> None:
    """The session document's merge (#1224, #1226): a value lands
    over the standing one, a blank deletes its key, and the standing
    document is left as it was."""
    from msks.client.gui.settingspage import session_document

    operator = {"url": "x", "cafile": "c"}
    assert session_document(operator, {"url": "", "token_file": "t"}) == {
        "cafile": "c",
        "token_file": "t",
    }
    assert session_document(operator, {}) == operator
    assert operator == {"url": "x", "cafile": "c"}


def test_a_panel_without_aliases_has_no_daemon_edits(qt) -> None:
    """The base panel's ``daemons:`` edits (#1226): none — only the
    Connection panel's editor answers any."""
    page, _held = page_over(qt, launched(qt))
    for panel in (page.files, page.shell, page.screenshot, page.dictate):
        assert panel.daemon_edits() == {}
    assert page.connection.daemon_edits() == {}


def test_the_vocabulary_reads_the_effective_value_and_the_pins() -> None:
    """The vocabulary's resolution (#1226): a non-blank variable pins
    its key with its stripped text, a blank one does not, a key with
    no variable never pins, and a list-form command or a port shows
    in its display form."""
    assert effective("url", {"MSKSC_URL": " https:env "}, {"url": "file"}) == (
        "https:env",
        True,
    )
    assert effective("url", {"MSKSC_URL": " "}, {"url": "file"}) == (
        "file",
        False,
    )
    assert effective("active_daemon", {"MSKSC_URL": "x"}, None) == ("", False)
    assert effective("active_daemon", {}, {"active_daemon": "lab"}) == (
        "lab",
        False,
    )
    assert effective(
        "ssh_options", {}, {"ssh_options": ["-A", "-o", "X=1"]}
    ) == (
        "-A -o X=1",
        False,
    )
    assert effective("screenshot_port", {}, {"screenshot_port": 8662}) == (
        "8662",
        False,
    )
    assert pinned_keys({"MSKSC_URL": " ", "MSKSC_SOCKET": "s"}) == frozenset(
        {"socket"}
    )
    assert pinned_keys({}) == frozenset()
    assert display_value(None) == ""
    assert display_value("  /x  ") == "/x"
    assert (
        display_value(["import", "-window", "root"]) == "import -window root"
    )


def test_the_no_model_click_opens_the_dictation_subtab(qt) -> None:
    """The no-model click's answer (#1214, #1226): the settings page
    comes forward on the main strip and opens on its Dictation
    subtab, the Download button's own page, not the Connection
    subtab the strip starts on."""
    from msks.client.gui import app as gui_app
    from msks.client.gui.settingspage import SettingsPage

    conf = launched(qt)
    held, _fake = controller(qt, conf.doc)
    page = SettingsPage(held, conf)
    assert page.bar.currentIndex() == 0
    shown: list = []

    class Win:
        def centralWidget(self) -> object:  # noqa: N802
            return self

        def setCurrentWidget(self, widget) -> None:  # noqa: N802
            shown.append(widget)

        def showNormal(self) -> None:  # noqa: N802
            pass

        def raise_(self) -> None:
            pass

        def activateWindow(self) -> None:  # noqa: N802
            pass

    gui_app.show_settings_page(Win(), {"settings": page})
    assert shown == [page]
    assert page.bar.currentIndex() == 4
    assert page.stack.currentIndex() == 4
    assert page.stack.currentWidget().widget() is page.dictate


def test_the_aliases_list_leaves_upward_from_its_first_row(qt) -> None:
    """The aliases' list (#962, #1226): Up on the first row hands the
    keyboard to the previous tab stop, the way every table in the
    window does; Up on a later row moves within the list."""
    page, _held = page_over(qt, launched(qt, text=ALIASES_TEXT))
    panel = page.connection
    window = window_over(qt, page)
    spin(qt, 20)
    panel.aliases.setCurrentRow(1)
    panel.aliases.setFocus()
    spin(qt, 10)
    assert window.focusWidget() is panel.aliases
    press(qt, panel.aliases, qt.QtCore.Qt.Key.Key_Up)
    assert panel.aliases.currentRow() == 0
    assert window.focusWidget() is panel.aliases
    press(qt, panel.aliases, qt.QtCore.Qt.Key.Key_Up)
    assert panel.aliases.currentRow() == 0
    assert window.focusWidget() is panel.active


def test_the_alias_prompts_refuse_blank_and_standing_names(qt) -> None:
    """The add and rename prompts (#1226): a blank answer changes
    nothing, a name that already stands is refused with a flash, a
    rename to the same name is no edit, and the buttons with no
    selection do nothing."""
    page, _held = page_over(qt, launched(qt, text=ALIASES_TEXT))
    panel = page.connection
    answers = iter(["", "lab", "lab"])
    panel.ask = lambda *args, **kwargs: next(answers)
    panel.add_alias()
    assert set(panel.daemons) == {"lab", "dev"} and not page.dirty
    panel.add_alias()
    assert page.status.text() == "daemon 'lab' already stands"
    assert set(panel.daemons) == {"lab", "dev"}
    # A rename answered with the standing name is no edit.
    panel.select("dev")
    panel.rename_alias()
    assert set(panel.daemons) == {"lab", "dev"} and not page.dirty
    # A rename to a fresh name with no active pick naming the old
    # one: the entry moves alone, the pick stands blank.
    panel.ask = lambda *args, **kwargs: "lab-2"
    panel.select("lab")
    panel.rename_alias()
    assert set(panel.daemons) == {"lab-2", "dev"} and page.dirty
    assert panel.active.currentData() == ""
    page.revert()
    # No selection: the buttons answer nothing.
    panel.aliases.setCurrentRow(-1)
    assert panel.selected() is None
    panel.rename_alias()
    panel.remove_alias()
    panel.select("no-such-alias")  # a row that is not there: nothing
    panel.address.setText("typed into a dark form")  # no alias to land in
    assert set(panel.daemons) == {"lab", "dev"} and not page.dirty
    assert (
        panel.kind.isEnabled() is False and panel.address.isEnabled() is False
    )


def test_the_device_refresh_is_no_edit(qt) -> None:
    """The Dictation subtab's Refresh (#1226): the picker refills and
    the standing pick returns, and the refill lights no Apply — a
    read of the desktop's sources is no edit. A standing edit stays
    lit through it."""
    page, _held = page_over(qt)
    page.dictate.list_devices()
    assert not page.dirty and not page.apply_button.isEnabled()
    page.dictate.prompt.setText("msks")
    page.dictate.list_devices()
    assert page.dirty and page.apply_button.isEnabled()


def test_removing_the_last_alias_drops_the_block_and_the_active_pick(
    qt,
) -> None:
    """The last alias removed (#1226): the ``daemons:`` key goes with
    it — a bare key reads as null and the loader refuses it — the
    active alias that named it clears in the same save, and Apply
    lands."""
    page, _held = page_over(
        qt,
        launched(
            qt,
            text="daemons:\n  dev:\n    socket: /run/s.sock\n"
            "active_daemon: dev\n",
        ),
    )
    panel = page.connection
    assert panel.active.currentData() == "dev"
    panel.remove_alias()
    assert page.file_edits() == ({"active_daemon": None}, {"dev": None})
    page.apply()
    assert page.status.text() == "saved to msks.yaml"
    assert file_doc(qt) == {}
    assert panel.daemons == {} and panel.active.currentData() == ""
    # The entry form stands dark and blank over the empty list.
    assert panel.kind.isEnabled() is False and panel.address.text() == ""


def test_a_revert_after_a_remove_keeps_the_files_active_pick(qt) -> None:
    """Remove the active alias, then Revert (#1226 review): the
    picker's items rebuild from the file and the pick shows the
    file's own alias again — not the blank the stale items left —
    so a later Apply does not drop ``active_daemon`` from the
    file."""
    page, _held = page_over(
        qt,
        launched(
            qt,
            text=ALIASES_TEXT.replace(
                "url: https://127.0.0.1:8660",
                "url: https://127.0.0.1:8660\nactive_daemon: dev",
            ),
        ),
    )
    panel = page.connection
    assert panel.active.currentData() == "dev"
    panel.select("dev")
    panel.remove_alias()
    assert panel.active.currentData() == ""
    page.revert()
    assert panel.active.currentData() == "dev"
    assert panel.daemons == panel.seeded_daemons
    field(page, "connection", "url").setText("https://other:8660")
    page.apply()
    assert file_doc(qt)["active_daemon"] == "dev"
    assert file_doc(qt)["daemons"]["dev"]["socket"] == (
        "/run/user/1000/msks/msksd.sock"
    )


def test_the_still_stands_note_names_the_system_file_alone(
    qt, monkeypatch
) -> None:
    """The note after a remove (#1226 review): an alias the operator
    removed and added back is no removal, an alias the user's own
    file defines and a refused save left standing is no system
    alias, and only an alias the system file defines draws the
    note."""
    page, _held = page_over(qt, launched(qt, text=ALIASES_TEXT))
    panel = page.connection
    panel.select("dev")
    panel.remove_alias()
    panel.ask = lambda *args, **kwargs: "dev"
    panel.add_alias()
    panel.kind.setCurrentIndex(panel.kind.findData("socket"))
    panel.address.setText("/run/user/1000/msks/msksd.sock")
    page.apply()
    # The alias stands as it was seeded: no edit, no write, no note.
    assert page.status.text() == "nothing to save: the fields stand as saved"
    assert panel.removed == set()
    assert file_doc(qt)["daemons"]["dev"]["socket"] == (
        "/run/user/1000/msks/msksd.sock"
    )
    # A refused save leaves the removal standing under a Revert: the
    # user's own alias, not the system's.
    panel.select("lab")
    panel.address.setText("")
    panel.remove_alias()
    panel.ask = lambda *args, **kwargs: "blank"
    panel.add_alias()  # an entry with no address: the save refuses
    page.apply()
    assert "needs a url or a socket" in page.status.text()
    page.revert()
    assert page.status.text() != "lab still stands: the system file defines it"
    # The system file's alias, removed: the note names it.
    system_file(qt, SYSTEM, monkeypatch)
    conf = launched(qt)
    page, _held = page_over(qt, conf)
    panel = page.connection
    panel.select("host")
    panel.remove_alias()
    page.apply()
    assert page.status.text() == (
        "host still stands: the system file defines it"
    )


def test_a_pinned_token_file_shows_the_variable_not_the_token(
    qt, monkeypatch
) -> None:
    """``MSKSC_TOKEN`` carries the token itself (#1226 review): the
    pinned Token file field names the variable and keeps the secret
    off the screen; the tooltip still carries the notice."""
    from msks.client.gui.settingspage.vocabulary import TOKEN_PINNED

    monkeypatch.setenv("MSKSC_TOKEN", "SEKRIT")
    page, _held = page_over(qt, launched(qt))
    widget = field(page, "connection", "token_file")
    assert widget.text() == TOKEN_PINNED
    assert "SEKRIT" not in widget.text()
    assert page.effective("token_file") == (TOKEN_PINNED, True)
    assert widget.isEnabled() is False
    assert widget.toolTip().endswith(
        "MSKSC_TOKEN pins this setting; the page cannot change it."
    )
    assert (
        page.connection.pinned_note.text() == "MSKSC_TOKEN pins this setting"
    )
    assert TOKEN_PINNED == "(the token, set through MSKSC_TOKEN)"


def test_an_edited_system_alias_writes_its_own_changes_alone(
    qt, monkeypatch
) -> None:
    """An alias the system file completes (#1226 review): the editor
    shows the merged entry, and an edit writes the user's own keys
    and the changed ones — a value inherited from the system file
    and left alone stays out, so the system's keeps reaching this
    user. A new alias writes its whole entry; a cleared inherited
    key writes nothing and the system's value stands."""
    system_file(
        qt,
        "daemons:\n  lab:\n    url: https://sys-lab:8660\n"
        "    token_file: /etc/msks/token\n    expected_image: msks/sys-img\n",
        monkeypatch,
    )
    page, _held = page_over(
        qt,
        launched(qt, text="daemons:\n  lab:\n    token_file: ~/mine.token\n"),
    )
    panel = page.connection
    assert panel.daemons["lab"] == {
        "url": "https://sys-lab:8660",
        "token_file": "~/mine.token",  # the user's own wins
        "expected_image": "msks/sys-img",
    }
    panel.select("lab")
    panel.address.setText("https://my-lab:8660")
    panel.image.setText("")  # clearing an inherited key
    assert panel.daemon_edits() == {
        "lab": {"token_file": "~/mine.token", "url": "https://my-lab:8660"}
    }
    page.apply()
    assert user_file(qt).read_text() == (
        "daemons:\n"
        "  lab:\n"
        "    url: https://my-lab:8660\n"
        "    token_file: ~/mine.token\n"
    )
    assert page.operator["daemons"]["lab"]["expected_image"] == "msks/sys-img"
    # A new alias is the user's whole entry.
    panel.ask = lambda *args, **kwargs: "farm"
    panel.add_alias()
    panel.address.setText("https://farm:8660")
    assert panel.daemon_edits() == {"farm": {"url": "https://farm:8660"}}
    page.apply()
    assert file_doc(qt)["daemons"]["farm"] == {"url": "https://farm:8660"}


def test_an_apply_over_fields_changed_back_writes_nothing(qt) -> None:
    """Changed and changed back (#1226): Apply stays lit, and the
    click says there is nothing to save, writes nothing, and
    darkens the buttons — no write of an unchanged file."""
    page, _held = page_over(qt, launched(qt, text="url: https://u\n"))
    before = user_file(qt).read_text()
    field(page, "connection", "url").setText("https://other")
    field(page, "connection", "url").setText("https://u")
    assert page.apply_button.isEnabled() and page.dirty
    page.apply()
    assert page.status.text() == "nothing to save: the fields stand as saved"
    assert user_file(qt).read_text() == before
    assert not page.dirty and not page.apply_button.isEnabled()

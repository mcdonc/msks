"""The workspace-shell window title tests (#445).

The template resolution and the OSC emission are pinned here for
both paths that share them — the tmux consent launcher and the
plain ssh window the TUI marks at spawn — with a stdout that is a
terminal only where a window would be.
"""

import io

from msks.client import wintitle as wt


class Tty(io.StringIO):
    """A stdout the title writer accepts — a pipe answers
    isatty() False and stays clean."""

    def isatty(self) -> bool:
        return True


def test_configured_title_resolves_the_workspace(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks — {workspace}")
    assert wt.configured_title("a1b2c3d4e5") == "msks — a1b2c3d4e5"
    # A child naming no workspace takes the shell spelling.
    assert wt.configured_title(None) == "msks — shell"
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks shell")
    assert wt.configured_title("a1b2c3d4e5") == "msks shell"
    # Unset and blank are the unset form: the emulator's own
    # title stays.
    monkeypatch.delenv("MSKSC_TERMINAL_TITLE")
    assert wt.configured_title("a1b2c3d4e5") is None
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "  ")
    assert wt.configured_title("a1b2c3d4e5") is None


def test_set_window_title_writes_the_osc(monkeypatch) -> None:
    out = Tty()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.set_window_title("msks — a1b2c3d4e5")
    assert out.getvalue() == "\x1b]0;msks — a1b2c3d4e5\x07"


def test_set_window_title_leaves_a_pipe_clean(monkeypatch) -> None:
    # A piped hand-run has no window title to set, so the escape
    # never lands in the stream.
    out = io.StringIO()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.set_window_title("msks — ws")
    assert out.getvalue() == ""


def test_set_window_title_drops_control_characters(monkeypatch) -> None:
    # A BEL in the title would end the OSC early and an ESC would
    # start a live sequence — the title carries neither through;
    # the printable characters around them stay.
    out = Tty()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.set_window_title("a\x07b\x1b[2mc\nd\x7f")
    assert out.getvalue() == "\x1b]0;ab[2mcd\x07"


def test_a_marked_window_takes_the_title(monkeypatch) -> None:
    # The TUI's spawn marker is the handoff: the ssh session it
    # spawned names the window before the session starts. A stale
    # $TMUX (the operator runs the TUI inside tmux) decides
    # nothing — the marker is the one live signal.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv(wt.TITLE_MARKER, "1")
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks — {workspace}")
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1,sess")
    out = Tty()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.title_spawned_window("a1b2c3d4e5")
    assert out.getvalue() == "\x1b]0;msks — a1b2c3d4e5\x07"


def test_a_typed_invocation_keeps_its_terminal_title(
    monkeypatch,
) -> None:
    # No marker — the operator typed the command in their own
    # terminal, and OSC cannot read a title back to restore it.
    monkeypatch.delenv(wt.TITLE_MARKER, raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks — {workspace}")
    out = Tty()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.title_spawned_window("a1b2c3d4e5")
    assert out.getvalue() == ""


def test_a_marked_window_without_a_template_writes_nothing(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv(wt.TITLE_MARKER, "1")
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "")
    out = Tty()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.title_spawned_window("a1b2c3d4e5")
    assert out.getvalue() == ""


def test_a_marked_window_on_a_pipe_writes_nothing(monkeypatch) -> None:
    # The spawn's stdio is the window's tty in the real path; a
    # captured stdout (a piped hand-run with the marker set by
    # hand) stays clean through the whole gate, not just the
    # writer.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv(wt.TITLE_MARKER, "1")
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks — {workspace}")
    out = io.StringIO()
    monkeypatch.setattr(wt.sys, "stdout", out)
    wt.title_spawned_window("a1b2c3d4e5")
    assert out.getvalue() == ""

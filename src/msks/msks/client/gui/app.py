"""The GUI entry: what the ``msks gui`` command runs.

The Qt import lives inside :func:`run` — the CLI imports this
module at load time (``wscommands.py``), so a module-scope
PySide6 import would break every command on a base install. The
window shell is the phase-1 surface (#928) carrying the
workspace list screen (#939, phase 3 of the plan): a main
window over the daemon's REST API whose central widget renders
the presenter's row models and refreshes them through the
bridge.
"""

import math
import os
import subprocess
import sys
from importlib.util import find_spec
from types import ModuleType

from .data import GuiData

# What a base install hears when it asks for the window (#928):
# one stderr line, the client's error convention, naming the
# install that brings the Qt binding.
HINT = "msks: the desktop GUI needs the gui extra: pip install 'msks[gui]'"

# The binding is installed but will not load (a broken wheel set
# or missing system libraries): the reinstall that fixes one
# shape; the caught error's own text names the other (the exact
# missing soname).
BROKEN = (
    "msks: the PySide6 install does not load; reinstall it with "
    "pip install --force-reinstall 'msks[gui]'"
)

# A Linux session with no display and no platform override: Qt's
# own failure here is an abort (qFatal, a multi-line dump and exit
# 134), past anything Python can catch — the pre-flight keeps the
# client's one-line convention instead. macOS is exempt: a stock
# session sets none of the three, and cocoa, its default platform,
# needs none of them.
NO_DISPLAY = (
    "msks: the GUI needs a display (set DISPLAY or "
    "WAYLAND_DISPLAY, or QT_QPA_PLATFORM=offscreen)"
)

# The shell window's title, until the screens give it a
# workspace to name (#924 phases 3 and 4).
WINDOW_TITLE = "msks"

# The style the window asks Qt for by name (#937). A desktop
# session can hand the wheel's Qt a style engine plugin built
# for the host's Qt (the union style: its css subplugin then
# loads a rust parser that resolves against the host's
# libstdc++ and fails, and its breeze fallback is nowhere in
# the wheel). Fusion is compiled into QtWidgets — no plugin,
# no session lookup — and ships identical on every platform,
# so the window looks the same wherever it opens.
FUSION_STYLE = "Fusion"

#: The window's font scale (#939): the platform's default point
#: size reads small beside a terminal's own face, so the window
#: runs its fonts at one and a half times the default — one
#: scale for the whole window, applied to the application font
#: before the first widget reads it.
FONT_SCALE = 1.5

#: The window's opening height scale (#939): a main window
#: shrinks to its central widget's size hint when it shows, and
#: the empty listing's hint alone leaves the window short. The
#: window opens at the layout's own height doubled, its width
#: the layout's own.
HEIGHT_SCALE = 2.0

#: The environment knob for the font scale (#939): an operator
#: whose display or taste reads the default wrong sets it. The
#: same knob shape for the height scale below — presentation
#: defaults, each overridable per launch in the style pin's own
#: spirit (#937: a preset choice is the user's own).
FONT_SCALE_ENV = "MSKSC_GUI_FONT_SCALE"
HEIGHT_SCALE_ENV = "MSKSC_GUI_HEIGHT_SCALE"


def scale_from_env(name: str, default: float) -> float:
    """The scale the environment names, else the default
    (#939): a presentation knob, not a config gate — a value
    that does not parse as a number reads as the default, and
    so does one that parses but is not usable (``nan``, an
    infinity, anything at or under zero), so the window opens
    rather than refusing."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isfinite(value) and value > 0:
        return value
    return default


def gui_session_present() -> bool:
    """Whether this macOS user owns a GUI login session: the
    launchd probe the host-setup workflow uses for the same
    distinction. A desktop login answers true however the
    process was started (the session exists while an ssh login
    rides on it); a headless mac — a CI runner, a server — has
    no WindowServer for cocoa to talk to, and Qt's event loop
    there never returns, so the absence answer matters as much
    as the presence one. A launchd that will not answer (absent,
    slow) reads as headless: the safe side is the notice, not
    the hang."""
    try:
        probed = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}"],
            capture_output=True,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return probed.returncode == 0


def display_present() -> bool:
    """Whether Qt has a display or a platform override to build on.

    The Linux pre-flight for the window: Qt's own failure at the
    QApplication constructor is an abort (qFatal, a multi-line
    dump and exit 134) past anything Python can catch. macOS
    asks launchd instead of the environment: a stock desktop
    session sets none of the three variables, and cocoa, its
    default platform, needs none of them — but a headless mac
    must hear the same one-line notice a headless Linux box
    does, or the event loop never comes back (#934: the macOS
    runner hung exactly there).
    """
    if sys.platform == "darwin":
        return gui_session_present()
    return any(
        os.environ.get(var)
        for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")
    )


def run() -> int:
    """Open the GUI window; the guards keep the parse path
    Qt-free and answer every failure with one stderr line."""
    if find_spec("PySide6") is None:
        print(HINT, file=sys.stderr)
        return 1
    try:
        # allow-deferred-import — the optional gui extra; the
        # CLI loads this module at parse time on every command,
        # so the binding imports only when the window is asked
        # for (the guard above already answered absence).
        from PySide6 import QtWidgets
    except ImportError as exc:
        print(f"{BROKEN} ({exc})", file=sys.stderr)
        return 1
    # The gate sits between the import and the window: the
    # import needs no display; the constructor does.
    if not display_present():
        print(NO_DISPLAY, file=sys.stderr)
        return 1
    return open_window(QtWidgets)


def open_window(QtWidgets: ModuleType) -> int:
    """Show the main-window shell and run the event loop.

    The instance check reuses an existing ``QApplication`` — the
    offscreen window test enters through :func:`run` and makes
    the first one; the reuse arc is pinned by the stub test. Qt
    wants one application per process. The window owns the
    bridge (phase 2 of #924): started with the window, stopped
    on close — ``aboutToQuit`` first, then the return path, both
    through the idempotent stop — and opens on the workspace
    list (#939): the screen is the central widget the bridge's
    answers land on.
    """
    # allow-deferred-import — the bridge brings PySide6 with it
    # (module scope there); this module stays Qt-free at parse
    # time, so the import lands only on the window path.
    from .bridge import Bridge

    # The style pin sits in the environment, before the
    # constructor: QApplication's own theme setup resolves the
    # session's style name (a Plasma portal answers "union"
    # there) and loads the named engine right there — a
    # setStyle after construction comes one plugin load too
    # late. A preset override is the user's own choice: both
    # this write and the setStyle below leave it standing.
    mine = os.environ.get("QT_STYLE_OVERRIDE") is None
    if mine:
        os.environ["QT_STYLE_OVERRIDE"] = FUSION_STYLE
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        sys.argv[:1]
    )
    if mine:
        app.setStyle(FUSION_STYLE)
    # The font scale sits on the application before the first
    # widget builds (#939): every screen the window opens — the
    # table, the button, the status line — inherits the scaled
    # size without a per-widget hand-tune. A preset knob is the
    # operator's own choice and wins over the default.
    font = app.font()
    font.setPointSizeF(
        font.pointSizeF() * scale_from_env(FONT_SCALE_ENV, FONT_SCALE)
    )
    app.setFont(font)
    bridge = Bridge(GuiData())
    bridge.start()
    app.aboutToQuit.connect(bridge.stop)
    window = QtWidgets.QMainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    # allow-deferred-import — the screen module brings PySide6
    # with it (module scope there, like the bridge); the window
    # path loads it after the guards — the same reachability
    # the gate counts, and the stub tests patch the name off
    # the module.
    from .workspaces import WorkspaceList

    window.setCentralWidget(WorkspaceList(bridge))
    # The opening size (#939): ``adjustSize`` pins the central
    # widget's size hint as the window's own geometry before
    # the show shrinks to it, and the resize scales the height —
    # the listing's hint alone opens short. A preset knob is
    # the operator's own choice and wins over the default.
    window.adjustSize()
    size = window.size()
    window.resize(
        size.width(),
        round(size.height() * scale_from_env(HEIGHT_SCALE_ENV, HEIGHT_SCALE)),
    )
    window.show()
    code = app.exec()
    bridge.stop()
    return code

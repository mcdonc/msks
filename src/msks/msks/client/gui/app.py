"""The GUI entry: what the ``msks gui`` command runs.

The Qt import lives inside :func:`run` — the CLI imports this
module at load time (``wscommands.py``), so a module-scope
PySide6 import would break every command on a base install. The
window shell is the phase-1 surface (#928) carrying the
workspace list screen (#939, phase 3 of the plan): a main
window over the daemon's REST API whose central tab strip
(#962) hosts the presenter's row models and refreshes them
through the bridge.
"""

import json
import math
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType

from ..sshsessions import state_dir
from .data import GuiData
from .rows import WINDOW_TITLE
from .shortcut import DESKTOP_ID, ICON_FILE, icon_dir

# What a stripped or partial install hears when it asks for
# the window (#928) — and what a nix-built install hears by
# design: the nix package is the daemon's closure and strips
# the binding (#1023), so the line names the pip install for
# both shapes. One stderr line, the client's error convention.
HINT = (
    "msks: the desktop GUI needs PySide6, a base dependency of "
    "the wheel: pip install --force-reinstall msks (a nix-built "
    "install: pip install msks)"
)

# The binding is installed but will not load (a broken wheel set
# or missing system libraries): the reinstall that fixes one
# shape; the caught error's own text names the other (the exact
# missing soname).
BROKEN = (
    "msks: the PySide6 install does not load; reinstall it with "
    "pip install --force-reinstall msks"
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

#: The window's opening size (#939), the shape no saved
#: geometry answers: 1280 by 720 — a listing five columns wide
#: reads comfortably, and a 16:9 desktop shows it whole.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

#: The environment knob for the font scale (#939): an operator
#: whose display or taste reads the default wrong sets it. The
#: window's size needs no knob — it answers to the saved
#: geometry or the fixed default above.
FONT_SCALE_ENV = "MSKSC_GUI_FONT_SCALE"

#: The saved window geometry's file name, under the client
#: state root (#939): the window opens where it last closed,
#: and the fixed default applies only until the first close
#: remembers a geometry.
GEOMETRY_FILE = "gui-window.json"


def apply_identity(app) -> None:
    """Name the window for the desktop (#1021): the desktop
    file name a compositor matches — the app id a Wayland
    taskbar resolves the icon by, the class an X11 one — plus
    the icon itself, which the surfaces that read the window
    rather than the entry show (macOS's dock reads this one
    alone). The entry and its icon land on the desktop through
    :func:`msks.client.gui.shortcut.run`."""
    # allow-deferred-import — the icon type comes from the
    # binding the window path already loaded (run's guards
    # answered absence); this module stays Qt-free at parse
    # time so the CLI's every command parses without it.
    from PySide6 import QtGui

    app.setDesktopFileName(DESKTOP_ID)
    app.setWindowIcon(QtGui.QIcon(str(icon_dir() / ICON_FILE)))


def geometry_path() -> Path:
    """The saved-geometry file under the client state root
    (#939) — ``MSKSC_STATE_DIR`` when set, else the XDG state
    home's msks subtree, the sessions' own root."""
    return state_dir() / GEOMETRY_FILE


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


def read_geometry() -> tuple[int, int, int, int] | None:
    """The saved window geometry (#939) as a plain rect — x, y,
    width, height — or ``None`` when nothing usable is saved: no
    file, text that does not parse, fields that are missing or
    not whole numbers, or a rect with no area."""
    try:
        doc = json.loads(geometry_path().read_text())
        rect = (
            int(doc["x"]),
            int(doc["y"]),
            int(doc["width"]),
            int(doc["height"]),
        )
    except OSError, ValueError, TypeError, KeyError:
        return None
    x, y, width, height = rect
    if width <= 0 or height <= 0:
        return None
    return rect


def save_geometry(window) -> None:
    """The window's last geometry written (#939): the rect the
    operator left — place and size together — for the next open
    to take back. The write is best-effort: a state root it
    cannot write (read-only, unwritable) says so on stderr, the
    client's one-line convention, and the window still closes."""
    box = window.geometry()
    try:
        root = geometry_path().parent
        root.mkdir(parents=True, exist_ok=True)
        geometry_path().write_text(
            json.dumps(
                {
                    "x": box.x(),
                    "y": box.y(),
                    "width": box.width(),
                    "height": box.height(),
                }
            )
        )
    except OSError as exc:
        print(f"msks: cannot save the window geometry: {exc}", file=sys.stderr)


def restore_geometry(window) -> bool:
    """The saved geometry applied to the window (#939): the rect
    the last close wrote, when a connected screen still shows
    some of it — a monitor change or an unplugged display leaves
    the old rect off the desktop, and a window opened where
    nothing can see it never comes back. Nothing usable saved
    answers False, and the caller opens on the scaled
    defaults."""
    # allow-deferred-import — the binding loads only on the
    # window path (run's guards answered absence already).
    from PySide6 import QtCore, QtGui

    rect = read_geometry()
    if rect is None:
        return False
    x, y, width, height = rect
    box = QtCore.QRect(x, y, width, height)
    if not any(
        screen.availableGeometry().intersects(box)
        for screen in QtGui.QGuiApplication.screens()
    ):
        return False
    window.setGeometry(box)
    return True


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


def run(conf=None) -> int:
    """Open the GUI window; the guards keep the parse path
    Qt-free and answer every failure with one stderr line.
    ``conf`` is the invocation's resolved client config
    (#954) — the external-terminal action reads the configured
    launcher (``terminal_open_cmd``) off it, the TUI's own
    read."""
    if find_spec("PySide6") is None:
        print(HINT, file=sys.stderr)
        return 1
    try:
        # allow-deferred-import — the base install's GUI
        # dependency (#1023); the CLI loads this module at parse
        # time on every command, so the binding imports only
        # when the window is asked for (the guard above already
        # answered absence).
        from PySide6 import QtWidgets
    except ImportError as exc:
        print(f"{BROKEN} ({exc})", file=sys.stderr)
        return 1
    # The gate sits between the import and the window: the
    # import needs no display; the constructor does.
    if not display_present():
        print(NO_DISPLAY, file=sys.stderr)
        return 1
    return open_window(QtWidgets, conf)


def terminal_command(conf) -> list[str] | None:
    """The configured terminal launcher (#954, the TUI's own
    read in its app shell): the list the config resolved — a
    string form arrives shell-split already — or None when no
    bootstrap resolved one."""
    if conf is None or not conf.terminal_open_cmd:
        return None
    return list(conf.terminal_open_cmd)


def open_window(QtWidgets: ModuleType, conf=None) -> int:
    """Show the main-window shell and run the event loop.

    The instance check reuses an existing ``QApplication`` — the
    offscreen window test enters through :func:`run` and makes
    the first one; the reuse arc is pinned by the stub test. Qt
    wants one application per process. The window owns the
    bridge (phase 2 of #924): started with the window, stopped
    on close — ``aboutToQuit`` first, then the return path, both
    through the idempotent stop — and opens on the top-level
    tab strip (#962): the workspace screens (#939, #945) — the
    shell, the list and the detail page over one bridge — live
    under the Workspaces tab, the first of the strip the images
    and secrets tabs of phase 4.6 join. The bridge's answers
    land on the shell wherever the strip shows it, and the
    window carries the whole-client font zoom (#976) and the
    desktop-scheme pairing (#1006): Ctrl+Plus/Minus and
    Ctrl+0 step the chrome's font and the shell hub's
    terminals together, wherever the focus stands, and the
    desktop's color scheme paints the chrome's palette and
    the terminal grids together — light with light, dark with
    dark, switched live when the desktop switches.
    """
    # allow-deferred-import — the bridge brings PySide6 with it
    # (module scope there); this module stays Qt-free at parse
    # time, so the import lands only on the window path.
    from .bridge import Bridge

    # allow-deferred-import — the feed is Qt-free but rides the
    # same window path: nothing parses it before the guards.
    from .feed import EventsFeed

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
    # operator's own choice and wins over the default. The
    # scale multiplies what the application already carries:
    # one window per process is the entry's own contract, and a
    # second open in one process would compound it (the
    # offscreen suite unscales between its opens).
    font = app.font()
    font.setPointSizeF(
        font.pointSizeF() * scale_from_env(FONT_SCALE_ENV, FONT_SCALE)
    )
    app.setFont(font)
    apply_identity(app)
    window = QtWidgets.QMainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    # allow-deferred-import — the macOS menu bar (#1007) carries
    # the screens' own contract: PySide6 at module scope there,
    # loaded only on the window path after the guards.
    from .menubar import install_bar

    # The macOS menu bar (#1007): the platform's own bar, with
    # the two standard roles; the gate inside the module leaves
    # every other platform's window the shape it has.
    install_bar(window)
    # allow-deferred-import — the picker's presenter (#1009):
    # Qt-free, plain data over the client's own config layer.
    from .connection import Connections

    # allow-deferred-import — the Images tab's cells (#964):
    # the channel URL, plain data beside the Qt-bearing tab.
    from .images.cells import channel_url

    # allow-deferred-import — the Images tab itself (#964)
    # carries the screens' contract: PySide6 at module scope,
    # loaded only on the window path — the package's init
    # presents it lazily, and the import takes the defining
    # submodule, not the package's presentation (the
    # names-from-definers rule).
    from .images.tab import ImagesTab

    # allow-deferred-import — the Secrets tab itself (#966)
    # carries the screens' contract: PySide6 at module scope,
    # loaded only on the window path — the images package's
    # own presentation.
    from .secrets.tab import SecretsTab

    # allow-deferred-import — the strip carries the screens'
    # own shape (#962): the screens land under the Workspaces
    # tab, the strip the phase 4.6 pages join.
    from .tabs import IMAGES_LABEL, SECRETS_LABEL, MainTabs

    # allow-deferred-import — the screen module brings PySide6
    # with it (module scope there, like the bridge); the window
    # path loads it after the guards — the same reachability
    # the gate counts, and the stub tests patch the name off
    # the module.
    from .workspace import WorkspaceShell

    # The connection picker and its re-dial (#1009, phase 5):
    # the picker rides the listing's status row below the flash
    # line (the presenter feeds it), never a pane of its own,
    # and a pick rebuilds the window's whole content tree over
    # the picked values — the shell, the tabs, the bridge
    # behind them. The first build lands the same path a pick
    # re-runs, so the standing tree and every re-dial share one
    # construction.
    connections = Connections(conf)
    tree: dict = {}

    def land() -> WorkspaceShell:
        bridge = Bridge(GuiData(), feed_factory=EventsFeed)
        bridge.start()
        app.aboutToQuit.connect(bridge.stop)
        shell = WorkspaceShell(bridge, terminal_cmd=terminal_command(conf))
        tabs = MainTabs(shell)
        tabs.addTab(ImagesTab(bridge, channel_url(conf)), IMAGES_LABEL)
        tabs.addTab(SecretsTab(bridge), SECRETS_LABEL)
        window.setCentralWidget(tabs)
        # The picker's menu hosts from the presenter's rows
        # (#1009): the fresh listing's own button, fed here so
        # the standing connection names itself on every build.
        shell.listing.host_presets(connections.presets())
        shell.listing.connection_picked.connect(reconnect)
        tree["bridge"] = bridge
        tree["shell"] = shell
        return shell

    def reconnect(preset) -> None:
        """One pick's re-dial (#1009): the preset applies to the
        environment through the config layer's own resolution,
        the standing tree closes — its bridge stopped, its
        shell hub's terminals gone with the connection they
        belonged to — and a fresh tree lands over the picked
        values, the zoom carrying its steps to the new hub. A
        pick the presenter refused (the file changed under the
        menu) re-dials nothing — the picker re-hosts on the
        standing truth instead."""
        if not connections.pick(preset):
            # The refusal shows where every flash shows (#1009):
            # the listing's own status line, the menu re-hosted
            # on the standing truth beside it.
            tree["shell"].listing.flash(
                f"the connection pick was refused — {preset.name} "
                "did not resolve"
            )
            tree["shell"].listing.host_presets(connections.presets())
            return
        tree["bridge"].stop()
        tree["shell"].hub.shutdown()
        shell = land()
        zoom.retarget(shell.hub)
        scheme.retarget(shell.hub)

    shell = land()
    # allow-deferred-import — the whole-client zoom (#976)
    # carries the screen modules' own contract: PySide6 at
    # module scope there, loaded only on the window path.
    from .zoom import Zoom

    # The whole-client font zoom (#976): Ctrl+Plus/Minus and
    # Ctrl+0 as application shortcuts, stepping the chrome's
    # application font and every live terminal the shell's hub
    # owns — the keys answer wherever the focus stands. The
    # zoom stands past the strip's swaps (#1009): its retarget
    # carries the operator's steps to each re-dialed tree's
    # hub.
    zoom = Zoom(shell.hub, window)
    # allow-deferred-import — the desktop-scheme pairing
    # (#1006) carries the screens' own contract: PySide6 at
    # module scope there, loaded only on the window path.
    from .scheme import Scheme

    # The desktop's scheme (#1006): the chrome's palette and
    # the terminal grids paint together from the desktop's
    # color scheme — one set for a light desktop, one for a
    # dark one — and a switch mid-session lands live. The
    # pairing stands past the strip's swaps (#1009): its
    # retarget carries the standing scheme to each re-dialed
    # tree's hub.
    scheme = Scheme(shell.hub, app)
    # The opening geometry (#939): the last-closed rect wins —
    # a saved geometry the screens still show — and the fixed
    # 1280 by 720 is the first-open shape until a close
    # remembers one.
    if not restore_geometry(window):
        window.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)
    window.show()
    # allow-deferred-import — the Ctrl-C wiring (#1037) carries
    # PySide6 at module scope there; the window path loads it
    # after the guards, the screens' own contract.
    from .interrupt import arm_sigint

    # The starting terminal's Ctrl-C (#1037): the wiring arms
    # before the loop and settles right after it, so a second
    # Ctrl-C in the cleanup window takes the interpreter's own
    # answer again.
    settle = arm_sigint(app)
    code = app.exec()
    code = settle(code)
    tree["bridge"].stop()
    save_geometry(window)
    return code

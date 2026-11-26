#!/usr/bin/env python
"""Capture the GUI docs chapter's screenshots (#1008).

Runs the real GUI surfaces under the offscreen Qt platform over
stub bridges with fixed data — the capture the shells-consent
reference images used (#1001), widened to every surface the
chapter documents. Each screenshot lands in ``docs/screens/``
as a PNG beside the TUI chapter's SVGs. No daemon, no network,
no desktop window: the widgets paint offscreen and
``QWidget.grab()`` reads the pixels.

Run from the devenv shell (the project's Python carries
PySide6, and the shell exports the font directory below):

    devenv --quiet shell -- python scripts/gui_screens.py

The pixels do not vary with the host's fonts (#1172). On Linux
Qt resolves every family through fontconfig, so the run pins
``FONTCONFIG_FILE`` to a file it writes: one directory,
``GUI_SCREENS_FONT_DIR``, with the generic families aliased —
sans-serif to DejaVu Sans, monospace to DejaVu Sans Mono. DejaVu
Sans is the chrome's face: the one face with every marker the GUI
paints (``✗``, ``↻``, ``·``) in a single file, where Noto Sans and
Liberation Sans lack some and fall back to whatever the host
holds. ``devenv.nix`` exports the directory as the DejaVu package
the devenv lock pins; a Linux host needs no system-level step, the
font is in the shell, and without the variable the script refuses
— an empty pin hands the chrome to Qt's box engine, every glyph a
hollow rectangle, the shape the first Linux render came out in.
The session's platform theme, style engine, scaling, and plugin
path stay out of it too (SCRUBBED_ENV dropped, Fusion forced), and
once the application stands the run checks that the database holds
the DejaVu faces alone and that the chrome resolved to DejaVu
Sans: a directory without the face, or a fontconfig file that did
not parse (fontconfig then falls back to the host's own config),
resolves somewhere else — the host's fonts in play again.
The run stops on macOS there: Qt reads CoreText, not fontconfig.
The terminal's face is the wheel's own bundled JetBrains Mono,
registered as an application font, so it reads no fontconfig. The
chrome's size is the window's own anchor, the terminal face's 15
logical pixels through the screen's dpi (#939, #1056): the capture
shows the window at the size the operator sees on the platform,
where a bare scale of the offscreen platform's own default (9pt)
left the chrome a fifth smaller than the window it documents.

The fixed clock pins every relative label (``2d ago``, the
TTL countdown) to one moment, so re-running the script
reproduces the same pixels except where the live clock or the
terminal child paints: the events tab stamps its rows from
the local clock at capture time, and the terminal shows the
child's own output.
"""

import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screens"

#: The directory the pinned fontconfig lists (#1172): devenv.nix
#: exports the DejaVu package's font directory the devenv lock
#: pins, and the chrome resolves to CHROME_FAMILY from it.
FONT_DIR_ENV = "GUI_SCREENS_FONT_DIR"

#: The chrome's face under the pin (#1172): one file with every
#: marker the GUI paints — ✗, ↻, · — where the other Linux
#: defaults (Noto Sans, Liberation Sans) lack some.
CHROME_FAMILY = "DejaVu Sans"

#: The monospace generic's face under the pin (#1172): no chrome
#: widget asks for the generic (the terminal names JetBrains Mono),
#: but an ask must land on a fixed-pitch face, not the first face
#: in the sort.
MONO_FAMILY = "DejaVu Sans Mono"

#: The family name every face in the pinned directory carries
#: (#1172): the database holds those alone when the pin took.
PINNED_PREFIX = "DejaVu"

#: The generic family names Qt lists whatever the fontconfig
#: holds (#1172): the names the offscreen theme asks with, beside
#: the pinned directory's faces.
GENERIC_FAMILIES = frozenset({"Sans Serif", "Serif", "Monospace"})

#: The host's session variables the capture drops before Qt loads
#: (#1172): a desktop session's platform theme (gtk3, kde) hands
#: Qt its own font family and palette; a preset scale factor or
#: font dpi resizes every capture (QT_SCALE_FACTOR=2 doubles
#: them) while the face check still passes; the host's Qt plugin
#: path would let its own platform plugin stand in for the wheel's.
SCRUBBED_ENV = (
    "QT_QPA_PLATFORMTHEME",
    "QT_SCALE_FACTOR",
    "QT_SCALE_FACTOR_ROUNDING_POLICY",
    "QT_SCREEN_SCALE_FACTORS",
    "QT_FONT_DPI",
    "QT_AUTO_SCREEN_SCALE_FACTOR",
    "QT_ENABLE_HIGHDPI_SCALING",
    "QT_PLUGIN_PATH",
)

#: The moment every relative label reads (#1008's
#: captures): an aware local datetime, the shape the shared
#: leaf's own clock returns — a naive pin would read as local
#: time against the daemon's UTC stamps and skew every label
#: beside it. The created stamps and expiry below are written
#: as UTC around this moment, the daemon's own convention.
PINNED = datetime(2026, 1, 4, 12, 0, 0).astimezone()
PINNED_UTC = PINNED.astimezone(UTC)


def pinned_fontconfig() -> str:
    """Write the fontconfig the run pins (#1172) and return its
    path: one directory, ``GUI_SCREENS_FONT_DIR``, with the
    generic families aliased — sans-serif to CHROME_FAMILY,
    monospace to DejaVu Sans Mono. Without the alias fontconfig
    hands a generic to the first face in its sort, DejaVu Math
    TeX Gyre over DejaVu Sans with the DejaVu directory listed,
    and a proportional face for a monospace ask. An unset or
    missing directory stops the run with one line: the capture
    belongs in the devenv shell, and a directory fontconfig cannot
    find renders the same boxes as no font at all. The path is
    XML-escaped: a file that does not parse drops fontconfig back
    to the host's own config, every host font in play again, and
    only the resolved-face check in capture() would name it."""
    font_dir = os.environ.get(FONT_DIR_ENV)
    if not font_dir:
        sys.exit(
            f"gui_screens: {FONT_DIR_ENV} is unset — run from the "
            "devenv shell (docs/development.md)"
        )
    if not Path(font_dir).is_dir():
        sys.exit(f"gui_screens: {FONT_DIR_ENV} names no directory: {font_dir}")
    fonts = tempfile.NamedTemporaryFile(suffix=".conf", delete=False, mode="w")
    fonts.write(
        "<?xml version='1.0'?>\n"
        "<!DOCTYPE fontconfig SYSTEM 'fonts.dtd'>\n"
        "<fontconfig>\n"
        f"  <dir>{font_dir.replace('&', '&amp;').replace('<', '&lt;')}</dir>\n"
        "  <alias><family>sans-serif</family><prefer>"
        f"<family>{CHROME_FAMILY}</family></prefer></alias>\n"
        "  <alias><family>Sans Serif</family><prefer>"
        f"<family>{CHROME_FAMILY}</family></prefer></alias>\n"
        "  <alias><family>monospace</family><prefer>"
        f"<family>{MONO_FAMILY}</family></prefer></alias>\n"
        "  <alias><family>Monospace</family><prefer>"
        f"<family>{MONO_FAMILY}</family></prefer></alias>\n"
        "</fontconfig>\n"
    )
    fonts.close()
    return fonts.name


def utc_stamp(offset: timedelta) -> str:
    """One naive UTC stamp at PINNED plus the offset — the
    daemon's own stamp shape."""
    return (PINNED_UTC + offset).isoformat()


#: The terminal child the shells screenshot runs: a prompt and
#: a command's output, then a wait — the #1001 capture's own
#: shape, no ssh and no network behind it. The child sets the
#: terminal's title first (OSC 2), as a prompt's PS1 does, so the
#: first shell's tab shows a program's title (#1163) beside the
#: renamed second one.
PROMPT_CHILD = (
    "import sys, time\n"
    "sys.stdout.write('\\x1b]2;user@alpha: ~\\x07')\n"
    "sys.stdout.write('user@alpha:~$ uname -a\\n"
    "Linux alpha 6.12.4-msks #1 SMP x86_64 GNU/Linux\\n"
    "user@alpha:~$ ')\n"
    "sys.stdout.flush()\n"
    "time.sleep(120)\n"
)


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, the suite's own
    stub shape."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def disconnect(self, slot) -> None:
        self.slots.remove(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the captures: the signals the
    screens read and the verbs they call, every call recorded —
    no worker, no network. The suite's own stub."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()
        self.landed = FakeSignal()
        self.feed_served = FakeSignal()
        self.feed_dropped = FakeSignal()
        self.feed_reconnected = FakeSignal()
        self.decider_landed = FakeSignal()
        self.decider_rejected = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))

    def start_decider(self, workspace_id: str, controller=None) -> None:
        pass

    def stop_decider(self) -> None:
        pass


class FakePresenter:
    """The consent presenter's surface for the shells capture:
    fixed rows, no controller — the consent-panel suite's own
    stub."""

    def __init__(self, rows) -> None:
        self.models = list(rows)
        self.decide_keys: set = set()

    def rows(self):
        return self.models

    def decide(self, *args) -> None:
        pass

    def retire(self, key) -> None:
        pass


def daemon_rows() -> list[dict]:
    """The listing the workspaces screenshot reads: three
    workspaces — alpha with an unresolved mount, beta with changes
    waiting for its next start, gamma running unmarked — so the
    list, newest first on screen (alpha, gamma, beta), shows the
    healthy row's green beside the marked rows' amber (#1160,
    #1172): alpha is the row the list holds selected, and a
    selected row keeps the highlight's own text color."""
    return [
        {
            "id": "ws-a",
            "name": "alpha",
            "status": "running",
            "image_ref": "msks/fedora:41",
            "created_at": utc_stamp(timedelta(hours=-2)),
            "cpus": 4,
            "mem_mib": 8192,
            "egress_mode": "interactive",
            "mounts": [
                {"host": "/srv/ok", "guest": "/mnt/ok", "mode": "rw"},
                {"host": "/srv/gone", "guest": "/mnt/gone"},
            ],
            "mount_issues": [
                {"host": "/srv/gone", "guest": "/mnt/gone", "kind": "stopped"}
            ],
            "mounts_pending": False,
            "sizes_pending": False,
        },
        {
            "id": "ws-b",
            "name": "beta",
            "status": "stopped",
            "image_ref": "msks/debian:13",
            "created_at": utc_stamp(timedelta(days=-2)),
            "cpus": 2,
            "mem_mib": 4096,
            "egress_mode": "static",
            "mounts": [
                {"host": "/srv/refs", "guest": "/mnt/refs", "mode": "ro"}
            ],
            "mounts_pending": True,
            "sizes_pending": True,
        },
        {
            "id": "ws-c",
            "name": "gamma",
            "status": "running",
            "image_ref": "msks/debian:13",
            "created_at": utc_stamp(timedelta(days=-1, hours=-6)),
            "cpus": 2,
            "mem_mib": 2048,
            "egress_mode": "static",
            "mounts_pending": False,
            "sizes_pending": False,
        },
    ]


def capacity_report() -> dict:
    """The capacity report the strips read: one cost per
    workspace."""
    return {
        "workspaces": [
            {"id": "ws-a", "root_bytes": 3 * 2**30, "home_bytes": 2**30},
            {"id": "ws-b", "root_bytes": 2 * 2**30, "home_bytes": 2**29},
            {"id": "ws-c", "root_bytes": 2**30, "home_bytes": 2**29},
        ]
    }


def detail_row() -> dict:
    """The row the detail page reads: two mounts, one skipped,
    with a pending mounts change waiting."""
    return daemon_rows()[0] | {
        "created_at": utc_stamp(timedelta(days=-2)),
        "mounts_pending": True,
    }


def settle(app, rounds: int = 12) -> None:
    """Let the widgets paint and the terminal child write: a
    few processEvents rounds with a breath between them."""
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.05)


def live_rises(app) -> list:
    """The row rises still running under the application's
    windows. A stagger on an empty table waits for rows the stub
    data never lands and paints nothing, so it is no rise to
    wait for."""
    from msks.client.gui.arrival import RowStagger

    live = []
    for window in app.topLevelWidgets():
        for stagger in window.findChildren(RowStagger):
            if stagger.live and stagger.rows:
                live.append(stagger)
    return live


def risen(app) -> None:
    """Let every row rise under the application land before a
    shot (#1177): the rise starts at a table's first paint and
    runs its whole clock after the settle rounds, and a grab
    mid-rise bakes a faint row into the page."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        app.processEvents()
        if not live_rises(app):
            return
        time.sleep(0.02)
    sys.exit("msks-gui-screens: a row rise never landed")


def shot(widget, name: str) -> None:
    """One surface to ``docs/screens`` as a PNG, its rows at
    rest."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        risen(app)
    path = OUT / name
    widget.grab().save(str(path))
    print(f"captured {path.relative_to(ROOT)}")


def unselect(*tables) -> None:
    """Clear each table's selection and current cell (#1172): a
    fresh page holds its first rows selected, and a selected
    row keeps the highlight's own text color (#1160) — cleared,
    the table's tints show in their own colors. A click on the
    empty area below the rows is the operator's own gesture."""
    for table in tables:
        table.clearSelection()
        table.selectionModel().clearCurrentIndex()


def foreign_families(families) -> list[str]:
    """The families outside the pin (#1172), sorted: the pinned
    directory's faces carry PINNED_PREFIX and Qt lists the
    generic names on its own, so anything else came from the
    host's own fontconfig — the file did not parse, or the
    variable named another directory."""
    return sorted(
        family
        for family in families
        if family not in GENERIC_FAMILIES
        and not family.startswith(PINNED_PREFIX)
    )


def require_chrome_face(app) -> None:
    """The pin's proof (#1172): the database holds the pinned
    directory's faces alone and the chrome resolved to the pinned
    face, or the run stops with one line. A fontconfig file that
    did not parse drops fontconfig back to the host's own config,
    and the host's families show in the database — the one host
    that passes then holds nothing but DejaVu faces, where only
    the hinting could differ. A directory without the face
    resolves the chrome somewhere else, and macOS resolves
    through CoreText, where the pin reads nothing: the captures
    render on a Linux host."""
    from PySide6 import QtGui

    foreign = foreign_families(QtGui.QFontDatabase.families())
    if foreign:
        sys.exit(
            "gui_screens: the font database holds families outside the "
            f"pin ({', '.join(foreign[:5])}) — FONTCONFIG_FILE did not "
            "take; the captures render on a Linux host from the devenv "
            "shell (docs/development.md)"
        )
    resolved = QtGui.QFontInfo(app.font()).family()
    if resolved != CHROME_FAMILY:
        sys.exit(
            f"gui_screens: the chrome resolved to {resolved!r}, not "
            f"{CHROME_FAMILY!r} — the captures render on a Linux host "
            "from the devenv shell (docs/development.md)"
        )


def preset_rows():
    """The connection picker's menu rows: two aliases with the
    first standing, the dev daemon's dial."""
    from msks.client.gui.connection import ConnectionPreset

    return [
        ConnectionPreset(
            "farm", "alias", "https://10.0.0.4:8443", active=True
        ),
        ConnectionPreset("lab", "alias", "https://10.0.0.9:8443"),
        ConnectionPreset("dev daemon", "dev", "https://127.0.0.1:8449"),
    ]


def main() -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # None of the host's session in the capture (#1172): the theme,
    # scaling, and plugin variables go before Qt loads, and the style
    # is the window's own Fusion, not a setdefault the host could
    # pre-empt.
    for name in SCRUBBED_ENV:
        os.environ.pop(name, None)
    os.environ["QT_STYLE_OVERRIDE"] = "Fusion"
    os.environ["FONTCONFIG_FILE"] = pinned_fontconfig()
    try:
        capture()
    finally:
        # A capture that failed before its own shutdown leaves
        # the hub's terminal children alive: the finally shuts
        # every hub that still holds sessions (an emptied
        # registry leaves its workspace keys behind, so the
        # values are what count). The catch is belt and braces:
        # the capture's own shutdown runs while its widgets
        # live, and whatever a defensive shutdown still raises
        # must not cancel the next hub or the temp fontconfig
        # file's unlink — the failure prints one line instead.
        for hub in HUBS:
            if any(hub.sessions.values()):
                try:
                    hub.shutdown()
                except Exception as exc:
                    print(f"msks-screens: {exc}", file=sys.stderr)
        os.unlink(os.environ["FONTCONFIG_FILE"])
    return 0


#: The capture's hubs (#1008): every hub the run builds, each
#: shut down on the way out whatever path the capture took.
HUBS: list = []


def capture() -> None:

    from msks.client import ages as ages_mod
    from msks.client.gui import consent as consent_mod
    from msks.client.gui import secretrows as secretrows_mod
    from msks.client.gui import workspace as workspace_mod
    from msks.client.gui import workspaces as workspaces_mod
    from msks.client.gui.app import (
        FONT_SCALE,
        FUSION_STYLE,
        chrome_pt,
        screen_dpi,
    )
    from msks.client.gui.images import tab as images_mod
    from msks.client.gui.secrets import tab as secrets_mod
    from msks.client.gui.terminal.fonts import DEFAULT_PT
    from PySide6 import QtWidgets

    ages_mod.clock_now = lambda: PINNED
    secretrows_mod.clock_now = lambda: PINNED

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-screens"]
    )
    app.setStyle(FUSION_STYLE)
    # The window's own chrome anchor (open_window's write, #939,
    # #1056): the terminal face's 15 logical pixels converted
    # through the screen's dpi, so the capture shows the chrome
    # at the size the operator's window opens at — the
    # offscreen platform's own default (9pt) is no desktop
    # session's font, and a bare scale of it left the chrome a
    # fifth smaller than the window it documents. The operator's
    # MSKSC_GUI_FONT_SCALE knob stays out of a docs render.
    font = app.font()
    font.setPointSizeF(chrome_pt(DEFAULT_PT * FONT_SCALE, screen_dpi(app)))
    app.setFont(font)
    require_chrome_face(app)

    list_bridge = StubBridge()

    # --- the workspaces list ---------------------------------------
    listing = workspaces_mod.WorkspaceList(list_bridge)
    window = QtWidgets.QMainWindow()
    window.setWindowTitle("msks")
    window.setCentralWidget(listing)
    window.resize(1280, 720)
    window.show()
    listing.host_presets(preset_rows())
    list_bridge.succeeded.emit(workspaces_mod.LIST_KEY, daemon_rows())
    list_bridge.succeeded.emit(workspaces_mod.STORAGE_KEY, capacity_report())
    settle(app)
    shot(window, "gui-list.png")

    # --- the connection menu ---------------------------------------
    listing.connection.menu().popup(
        listing.connection.mapToGlobal(listing.connection.rect().bottomLeft())
    )
    settle(app, rounds=4)
    shot(listing.connection.menu(), "gui-connection.png")
    listing.connection.menu().close()

    # --- the create dialog -----------------------------------------
    from msks.client.gui.create import (
        DEFAULTS_KEY,
        IMAGES_KEY,
        CreateDialog,
    )

    create_bridge = StubBridge()
    dialog = CreateDialog(create_bridge, parent=window)
    dialog.show()
    create_bridge.succeeded.emit(
        IMAGES_KEY,
        [
            {
                "name": "msks/fedora",
                "version": "41",
                "hash": "f" * 64,
                "default": True,
            },
            {"name": "msks/debian", "version": "13", "hash": "d" * 64},
        ],
    )
    create_bridge.succeeded.emit(
        DEFAULTS_KEY, {"root_mib": 4096, "home_mib": 8192}
    )
    dialog.fields["name"].setText("delta")
    settle(app)
    shot(dialog, "gui-create.png")
    dialog.close()

    # --- the detail page: shells, mounts, proxies, events ----------
    hub = workspace_mod.ShellHub(
        lambda ws: [sys.executable, "-c", PROMPT_CHILD]
    )
    HUBS.append(hub)
    detail_bridge = StubBridge()
    detail = workspace_mod.WorkspaceDetail(detail_bridge, hub)
    detail_window = QtWidgets.QMainWindow()
    detail_window.setWindowTitle("msks — alpha")
    detail_window.setCentralWidget(detail)
    detail_window.resize(1280, 720)
    detail_window.show()
    detail.open("ws-a")
    detail_bridge.succeeded.emit(workspace_mod.ROW_KEY, [detail_row()])
    detail_bridge.succeeded.emit(workspace_mod.STORAGE_KEY, capacity_report())
    # A second shell tab, renamed, beside the open one.
    hub.open_shell("ws-a")
    hub.rename("ws-a", 2, "build")
    # The consent panel's held requests over the shells.
    detail.shells_tab.consent_panel.host(
        FakePresenter(
            [
                consent_mod.ConsentRow(
                    "r1", "api.github.com", 105.0, "1m 45s"
                ),
                consent_mod.ConsentRow(
                    "r2", "registry.npmjs.org", 880.0, "14m 40s"
                ),
            ]
        )
    )
    detail.shells_tab.consent_panel.refresh()
    settle(app, rounds=20)
    shot(detail_window, "gui-shells.png")

    # The terminal-only mode (#1152): the chrome hidden, the
    # shell tabs and the terminal alone, the workspace's name
    # and the restore button at the tab row's right end. The
    # consent panel is empty for this capture — its rows show
    # in the shells shot above.
    detail.shells_tab.consent_panel.host(FakePresenter([]))
    detail.shells_tab.consent_panel.refresh()
    detail.terminal_only.toggle.click()
    settle(app, rounds=20)
    shot(detail_window, "gui-terminal-only.png")
    detail.terminal_only.restore.click()

    detail.bar.setCurrentIndex(1)
    settle(app, rounds=4)
    shot(detail_window, "gui-mounts.png")

    detail.bar.setCurrentIndex(2)
    detail_bridge.succeeded.emit(
        detail.proxies_tab.list_key,
        [
            {
                "id": 7,
                "direction": "forward",
                "listen_host": "127.0.0.1",
                "listen_port": 8080,
                "target_host": "127.0.0.1",
                "target_port": 80,
                "label": "web",
                "status": "listening",
            },
            {
                "id": 9,
                "direction": "remote",
                "listen_host": "127.0.0.1",
                "listen_port": 2222,
                "target_host": "127.0.0.1",
                "target_port": 22,
                "label": None,
                "status": "failed",
                "reason": "bind refused",
            },
        ],
    )
    settle(app, rounds=4)
    shot(detail_window, "gui-proxies.png")

    detail.bar.setCurrentIndex(3)
    for frame in (
        {
            "event": "workspace.status",
            "data": {"id": "ws-a", "status": "running"},
        },
        {
            "event": "egress.request",
            "data": {
                "workspace_id": "ws-a",
                "request": {
                    "id": "req-1",
                    "workspace_id": "ws-a",
                    "dest_host": "api.github.com",
                    "dest_port": 443,
                },
            },
        },
        {
            "event": "workspace.status",
            "data": {"id": "ws-a", "status": "stopping"},
        },
    ):
        detail.events_tab.push(frame)
    settle(app, rounds=4)
    shot(detail_window, "gui-events.png")

    # The hub shuts down here, while its widgets still live
    # (#1008): the capture's tail destroys the top-level widget
    # tree and flushes Qt's deferred deletions, and a shutdown
    # after that reaches deleted C++ objects.
    for hub in HUBS:
        hub.shutdown()
    detail_window.close()

    # --- the images tab --------------------------------------------
    images_bridge = StubBridge()
    images = images_mod.ImagesTab(
        images_bridge, "https://ch.example/manifest.json"
    )
    images_window = QtWidgets.QMainWindow()
    images_window.setCentralWidget(images)
    images_window.resize(1280, 720)
    images_window.show()
    images_bridge.succeeded.emit(
        images_mod.PAGE_KEY,
        {
            "catalog": [
                {
                    "name": "msks/debian",
                    "version": "13.6",
                    "hash": "a" * 64,
                    "default": True,
                    "imported": utc_stamp(timedelta(days=-2, hours=-9)),
                },
                {
                    "name": "msks/fedora",
                    "version": "41",
                    "hash": "b" * 64,
                    "default": False,
                    "imported": utc_stamp(timedelta(days=-1, hours=-9)),
                },
            ],
            "published": [
                {
                    "ref": "msks/debian:13.6",
                    "url": "https://ch.example/debian.tar.zst",
                    "hash": "a" * 64,
                    "description": "Debian 13 trixie",
                    "size": 104_857_600,
                    "built_at": "2026-01-01T00:00:00Z",
                    "arch": "x86_64",
                },
                {
                    "ref": "msks/alpine:3.21",
                    "url": "https://ch.example/alpine.tar.zst",
                    "hash": "c" * 64,
                    "size": 52_428_800,
                },
            ],
        },
    )
    settle(app, rounds=4)
    shot(images_window, "gui-images.png")
    images_window.close()

    # --- the secrets tab -------------------------------------------
    secrets_bridge = StubBridge()
    secrets = secrets_mod.SecretsTab(secrets_bridge)
    secrets_window = QtWidgets.QMainWindow()
    secrets_window.setCentralWidget(secrets)
    secrets_window.resize(1280, 720)
    secrets_window.show()
    secrets_bridge.succeeded.emit(
        secrets_mod.PAGE_KEY,
        {
            "secrets": [
                {
                    "id": 1,
                    "workspaces": [],
                    "name": "github_api",
                    "dests": ["api.github.com"],
                    "created_at": utc_stamp(timedelta(days=-1)),
                    "expires_at": None,
                    "locked": False,
                },
                {
                    "id": 2,
                    "workspaces": ["ws-a", "ws-b"],
                    "name": "deploy_key",
                    "dests": [],
                    "created_at": utc_stamp(timedelta(days=-2)),
                    "expires_at": utc_stamp(timedelta(hours=2)),
                    "locked": True,
                },
            ],
            "audit": [
                {
                    "id": 3,
                    "kind": "mint",
                    "workspaces": [],
                    "name": "github_api",
                    "dests": [],
                    "created_at": utc_stamp(timedelta(minutes=-60)),
                },
                {
                    "id": 2,
                    "kind": "revoke",
                    "workspaces": ["ws-a"],
                    "name": "old_key",
                    "dests": [],
                    "created_at": utc_stamp(timedelta(minutes=-90)),
                },
            ],
        },
    )
    settle(app, rounds=4)
    # The listing keeps its held first row (#1165): the figure
    # shows the bar's Renew… and Revoke… lit beside Mint… and
    # Unlock…, the buttons' state on a page the operator has
    # clicked into, while the locked row's amber still shows on
    # the unselected row beneath it. The audit group carries no
    # tint to show, so its hold clears.
    unselect(secrets.audit_table)
    settle(app, rounds=4)
    shot(secrets_window, "gui-secrets.png")
    secrets_window.close()

    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    from PySide6 import QtCore

    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())

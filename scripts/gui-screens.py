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
PySide6):

    devenv --quiet shell -- python scripts/gui-screens.py

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

#: The pinned moment every relative label reads (#1008's
#: captures): an aware local datetime, the shape the shared
#: leaf's own clock returns — a naive pin would read as local
#: time against the daemon's UTC stamps and skew every label
#: beside it. The created stamps and expiry below are written
#: as UTC around this moment, the daemon's own convention.
PINNED = datetime(2026, 1, 4, 12, 0, 0).astimezone()
PINNED_UTC = PINNED.astimezone(UTC)


def utc_stamp(offset: timedelta) -> str:
    """One naive UTC stamp at PINNED plus the offset — the
    daemon's own stamp shape."""
    return (PINNED_UTC + offset).isoformat()


#: The terminal child the shells screenshot runs: a prompt and
#: a command's output, then a wait — the #1001 capture's own
#: shape, no ssh and no network behind it.
PROMPT_CHILD = (
    "import sys, time\n"
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
    workspaces, one carrying both markers, newest first."""
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
            "status": "created",
            "image_ref": "msks/debian:13",
            "created_at": utc_stamp(timedelta(days=-1, hours=-6)),
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


def shot(widget, name: str) -> None:
    """One surface to ``docs/screens`` as a PNG."""
    path = OUT / name
    widget.grab().save(str(path))
    print(f"captured {path.relative_to(ROOT)}")


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
    fonts = tempfile.NamedTemporaryFile(suffix=".conf", delete=False, mode="w")
    fonts.write("<fontconfig/>\n")
    fonts.close()
    os.environ["FONTCONFIG_FILE"] = fonts.name
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    try:
        capture()
    finally:
        # A capture that failed before its own shutdown leaves
        # the hub's terminal children alive: the finally shuts
        # every hub that still holds sessions. A hub whose
        # widgets are already gone fails its own shutdown; the
        # failure prints one line and the next step still runs,
        # the temp fontconfig file's unlink included.
        for hub in HUBS:
            if hub.sessions:
                try:
                    hub.shutdown()
                except RuntimeError as exc:
                    print(f"msks-screens: {exc}", file=sys.stderr)
        os.unlink(fonts.name)
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
    from msks.client.gui.app import FONT_SCALE, FUSION_STYLE
    from msks.client.gui.images import tab as images_mod
    from msks.client.gui.secrets import tab as secrets_mod
    from PySide6 import QtWidgets

    ages_mod.clock_now = lambda: PINNED
    secretrows_mod.clock_now = lambda: PINNED

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-screens"]
    )
    app.setStyle(FUSION_STYLE)
    font = app.font()
    font.setPointSizeF(font.pointSizeF() * FONT_SCALE)
    app.setFont(font)

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

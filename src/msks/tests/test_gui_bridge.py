"""The GUI's asyncio bridge (#924 phase 2): one loop on a worker
thread, answers over Qt signals. The data stub stands in for
:class:`~msks.client.gui.data.GuiData` (the data layer has its
own tests); the delivery assertions run the real offscreen
``QApplication`` — a receiver ``QObject`` in this (GUI) thread
takes the worker's emits queued and pumps them through
``processEvents``.

The Qt imports stay inside the fixtures: this module is imported
at collection time by the test runner's controller process too,
and a controller (xdist's, or any plain run) has no business
loading the binding — #933's window tests set the pattern, and a
module-scope import here stalled the macOS controller's run
three runs running.
"""

import asyncio
import importlib.util
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client import data as clientdata  # noqa: E402

#: The poll cadence for queued signal delivery, seconds: the
#: worker emits within a network call's time; the test pumps
#: Qt's queue until the answer lands.
TICK = 0.01


class StubData:
    """The data layer's shape for the bridge's tests: answers on
    schedule, refuses on demand, and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def workspaces(self) -> list[dict]:
        self.calls.append(("workspaces",))
        return [{"id": "ws-1", "status": "running"}]

    async def stop(self, workspace_id: str) -> dict:
        self.calls.append(("stop", workspace_id))
        raise clientdata.RestFailure("409: workspace is running", reach=False)

    async def bug(self) -> dict:
        self.calls.append(("bug",))
        raise TypeError("data-layer bug")

    async def refused_environment(self) -> dict:
        """The exit contract raised past a guard: the shape a
        create's environment read (the default user, the
        identity key) raises — SystemExit, past every Exception
        arm."""
        self.calls.append(("refused_environment",))
        raise SystemExit("msks: cannot determine the invoking user")

    async def slow(self) -> dict:
        self.calls.append(("slow",))
        await asyncio.sleep(30)
        return {}


@pytest.fixture()
def qapp_setup(tmp_path: Path):
    """The offscreen ``QApplication`` the queued delivery needs,
    with the silent fontconfig the window test uses: Qt's
    shutdown-time re-scan runs after fixture teardown, so the
    variables are set on the environment directly (the os module,
    not monkeypatch — undo would hand the re-scan the host
    /etc/fonts). The Qt names (the module, the bridge, the
    receiver class) import here too — inside the fixture, never
    at collection time."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )

    class Receiver(QtCore.QObject):
        """A screen-shaped receiver: slots in the GUI thread, the
        answers appended for the assertions."""

        def __init__(self, bridge) -> None:
            super().__init__()
            self.okay: list[tuple[str, object]] = []
            self.bad: list[tuple[str, object]] = []
            bridge.succeeded.connect(self.on_okay)
            bridge.failed.connect(self.on_bad)

        @QtCore.Slot(str, object)
        def on_okay(self, key: str, result: object) -> None:
            self.okay.append((key, result))

        @QtCore.Slot(str, object)
        def on_bad(self, key: str, failure: object) -> None:
            self.bad.append((key, failure))

    from msks.client.gui.bridge import Bridge

    return SimpleNamespace(
        app=app, QtCore=QtCore, Bridge=Bridge, Receiver=Receiver
    )


def pump(qt, receiver, *, okay: int = 0, bad: int = 0) -> None:
    """Spin Qt's queue until the expected answers landed (or the
    deadline fails the test): the worker's emits sit queued for
    this thread, and ``processEvents`` is the loop's turn."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qt.app.processEvents()
        if len(receiver.okay) >= okay and len(receiver.bad) >= bad:
            return
        time.sleep(TICK)
    pytest.fail("the bridge's answer never landed in the GUI thread")


def test_results_and_failures_cross_to_the_gui_thread(
    qapp_setup, tmp_path: Path
) -> None:
    """The one contract (#924): a call runs on the worker loop,
    and its answer — result or ``RestFailure``, the reach flag
    intact — returns to the GUI thread over the signal. A bug in
    the data layer itself takes the same visible shape, the exit
    contract a coroutine raised past its guard arrives carried
    with its reach flag, and the stopped bridge refuses a late
    call rather than touching a dead loop."""
    qt = qapp_setup
    data = StubData()
    bridge = qt.Bridge(data)
    assert not bridge.running
    bridge.start()
    assert bridge.running
    loop = bridge._loop
    bridge.start()  # idempotent: the same loop, no second thread
    assert bridge._loop is loop
    receiver = qt.Receiver(bridge)
    bridge.call("list", "workspaces")
    bridge.call("halt", "stop", "ws-1")
    bridge.call("oops", "bug")
    bridge.call("env", "refused_environment")
    pump(qt, receiver, okay=1, bad=3)
    assert data.calls == [
        ("workspaces",),
        ("stop", "ws-1"),
        ("bug",),
        ("refused_environment",),
    ]
    assert receiver.okay == [("list", [{"id": "ws-1", "status": "running"}])]
    halted = receiver.bad[0][1]
    assert isinstance(halted, clientdata.RestFailure)
    assert "409" in str(halted)
    assert not halted.reach
    bugged = receiver.bad[1][1]
    assert isinstance(bugged, clientdata.RestFailure)
    assert "data-layer bug" in str(bugged)
    assert not bugged.reach
    refused = receiver.bad[2][1]
    assert isinstance(refused, clientdata.RestFailure)
    assert "invoking user" in str(refused)
    assert refused.reach  # the environment refused, not the daemon
    # A cancelled flight is the screen closing, not a failure:
    # nothing is delivered for it — the wrong arm would show a
    # failure line the operator never caused.
    quiet = len(receiver.okay), len(receiver.bad)
    flight = bridge.call("slow", "slow")
    assert flight is not None and flight.cancel()
    time.sleep(0.2)
    qt.app.processEvents()
    assert (len(receiver.okay), len(receiver.bad)) == quiet
    # Stop is idempotent (the window quits and closes), and a
    # call after it answers failed at once — on the caller's
    # thread, no queue needed.
    bridge.stop()
    bridge.stop()
    assert not bridge.running
    before = len(receiver.bad)
    bridge.call("late", "workspaces")
    assert len(receiver.bad) == before + 1
    assert receiver.bad[-1][0] == "late"
    assert not receiver.bad[-1][1].reach

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
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import data as clientdata  # noqa: E402
from msks.client.tui.consent import ConsentController  # noqa: E402

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
    # The three failures read by key, not by arrival index: a
    # coroutine the worker finishes before the GUI thread arms
    # its done callback delivers at once on the caller's thread
    # (a direct emit) while its earlier siblings' answers still
    # sit queued from the worker — arrival order is scheduling's
    # own, and the screens dispatch by key the same way. CI hit
    # the window once: the env refusal landed before the queued
    # 409.
    by_key = dict(receiver.bad)
    halted = by_key["halt"]
    assert isinstance(halted, clientdata.RestFailure)
    assert "409" in str(halted)
    assert not halted.reach
    bugged = by_key["oops"]
    assert isinstance(bugged, clientdata.RestFailure)
    assert "data-layer bug" in str(bugged)
    assert not bugged.reach
    refused = by_key["env"]
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


def test_a_feed_done_before_stop_stops_clean(qapp_setup) -> None:
    """A feed task that crashed on its own (#946): the stop's
    cancel is a no-op on a done task, and the completion
    callback retrieves the task's exception — never left
    un-retrieved — and stops the loop all the same."""

    class QuickFeed:
        """A feed whose run ends at once: the done-before-stop
        shape."""

        def __init__(self) -> None:
            self.on_frame = None
            self.on_drop = None
            self.on_reconnect = None

        async def run(self) -> None:
            raise RuntimeError("the feed crashed on its own")

    qt = qapp_setup
    feed = QuickFeed()
    bridge = qt.Bridge(StubData(), feed_factory=lambda: feed)
    bridge.start()
    # Let the task land done before the stop takes the bridge.
    deadline = time.monotonic() + 10.0
    task = None
    while time.monotonic() < deadline:
        task = bridge._feed_task
        if task is not None and task.done():
            break
        time.sleep(TICK)
    assert task is not None and task.done()
    bridge.stop()
    assert not bridge.running


def test_the_feed_rides_the_loop_and_its_frames_cross(
    qapp_setup,
) -> None:
    """The live events feed (#946): a feed factory's task rides
    the bridge's loop for the bridge's whole life, its three
    callbacks cross to the GUI thread as the bridge's signals,
    and stop cancels the task with the loop — no task outlives
    the window."""
    qt = qapp_setup

    class StubFeed:
        """The feed's shape: one frame, one drop, one recovery
        told at once, then a serve the stop must cancel."""

        def __init__(self) -> None:
            self.on_frame = None
            self.on_drop = None
            self.on_reconnect = None
            self.on_serve = None
            self.cancelled = False

        async def run(self) -> None:
            if self.on_frame is not None:
                self.on_frame(
                    {"event": "workspace.status", "data": {"id": "ws-1"}}
                )
            if self.on_drop is not None:
                self.on_drop("the socket broke")
            if self.on_reconnect is not None:
                self.on_reconnect()
            if self.on_serve is not None:
                self.on_serve()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    from PySide6 import QtCore

    class FeedReceiver(QtCore.QObject):
        """The shell-shaped receiver: the feed's three signals,
        the deliveries appended for the asserts."""

        def __init__(self, bridge) -> None:
            super().__init__()
            self.frames: list[dict] = []
            self.drops: list[str] = []
            self.backs = 0
            self.serves = 0
            bridge.landed.connect(self.on_frame)
            bridge.feed_dropped.connect(self.on_drop)
            bridge.feed_reconnected.connect(self.on_back)
            bridge.feed_served.connect(self.on_serve)

        @QtCore.Slot(object)
        def on_frame(self, frame: dict) -> None:
            self.frames.append(frame)

        @QtCore.Slot(str)
        def on_drop(self, reason: str) -> None:
            self.drops.append(reason)

        @QtCore.Slot()
        def on_back(self) -> None:
            self.backs += 1

        @QtCore.Slot()
        def on_serve(self) -> None:
            self.serves += 1

    feed = StubFeed()
    bridge = qt.Bridge(StubData(), feed_factory=lambda: feed)
    # The connections go up before the loop does: start_feed wires
    # the stub's callbacks to the signals the moment the loop
    # turns, and the stub fires its three deliveries at once —
    # a receiver that connects after start() can lose the race
    # to the worker's first emission (the stub fires exactly
    # once, so the deadline pump cannot recover the frames; CI
    # hit it as a bare `frames == []`).
    receiver = FeedReceiver(bridge)
    bridge.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        qt.app.processEvents()
        if receiver.frames and receiver.drops and receiver.backs:
            break
        time.sleep(TICK)
    assert receiver.frames == [
        {"event": "workspace.status", "data": {"id": "ws-1"}}
    ]
    assert receiver.drops == ["the socket broke"]
    assert receiver.backs == 1
    assert receiver.serves == 1  # the serve poke crossed too (#1019)
    assert not feed.cancelled
    bridge.stop()
    assert not bridge.running
    assert feed.cancelled
    bridge.stop()  # idempotent with the feed in place


class ParkingLink:
    """The link's shape for the registration's lifetime tests:
    parked, the retire's cancel its own end, the controller it
    was built around recorded."""

    def __init__(self, workspace_id: str, controller=None) -> None:
        self.workspace_id = workspace_id
        self.controller = controller
        self.on_frame = None
        self.on_rejected = None
        self.cancelled = False

    async def run(self) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class StubLink:
    """The decider link's shape for the bridge's tests: the
    presence told first — one serve and one drop (#1052) — then
    one frame outcome and the registration's refusal, then a
    serve the stop must cancel."""

    def __init__(self, workspace_id: str, controller=None) -> None:
        self.workspace_id = workspace_id
        self.controller = controller
        self.on_frame = None
        self.on_rejected = None
        self.on_serve = None
        self.on_drop = None
        self.cancelled = False

    async def run(self) -> None:
        if self.on_serve is not None:
            self.on_serve()
        if self.on_drop is not None:
            self.on_drop("the socket broke")
        if self.on_frame is not None:
            self.on_frame("added", f"hold-for-{self.workspace_id}")
        if self.on_rejected is not None:
            self.on_rejected("unknown workspace")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_the_decider_registration_rides_the_loop_with_the_page(
    qapp_setup,
) -> None:
    """The page's decider registration (#999): the link's task
    rides the bridge's loop, its callbacks cross to the GUI
    thread as the bridge's signals, the ask is idempotent per
    workspace (the page's open and show both ask), a different
    workspace retires the standing registration first, and the
    stop paths — the page's own retire, the window's close —
    cancel the task with the loop. An ask after the stop lands
    nowhere: the window is closing, and no page outlives it."""
    qt = qapp_setup

    class DeciderReceiver(qt.Receiver):
        """The shell-shaped receiver: the link's four signals,
        the deliveries appended for the asserts."""

        def __init__(self, bridge) -> None:
            super().__init__(bridge)
            self.landed: list[tuple[str, object]] = []
            self.refusals: list[str] = []
            self.serves = 0
            self.drops: list[str] = []
            bridge.decider_landed.connect(self.on_landed)
            bridge.decider_rejected.connect(self.on_refused)
            bridge.decider_served.connect(self.on_serve_told)
            bridge.decider_dropped.connect(self.on_drop_told)

        def on_landed(self, outcome: str, payload: object) -> None:
            self.landed.append((outcome, payload))

        def on_refused(self, reason: str) -> None:
            self.refusals.append(reason)

        def on_serve_told(self) -> None:
            self.serves += 1

        def on_drop_told(self, reason: str) -> None:
            self.drops.append(reason)

    links: list[StubLink] = []
    bridge = qt.Bridge(
        StubData(),
        decider_factory=lambda workspace_id, controller=None: (
            links.append(StubLink(workspace_id)) or links[-1]
        ),
    )
    bridge.start()
    receiver = DeciderReceiver(bridge)
    bridge.start_decider("ws-1")
    pump_until = time.monotonic() + 10.0
    while time.monotonic() < pump_until:
        qt.app.processEvents()
        if receiver.landed and receiver.refusals:
            break
        time.sleep(TICK)
    assert receiver.landed == [("added", "hold-for-ws-1")]
    assert receiver.refusals == ["unknown workspace"]
    # The presence tells (#1052) cross the same way: the serve
    # and the drop land on the GUI thread beside the frames.
    assert receiver.serves == 1
    assert receiver.drops == ["the socket broke"]
    # The re-ask for the standing workspace is a no-op: the
    # page's open and its show both ask, and one link serves.
    bridge.start_decider("ws-1")
    time.sleep(0.1)
    qt.app.processEvents()
    assert len(links) == 1
    assert not links[0].cancelled
    # A different workspace — the page re-hosted — retires the
    # old registration and claims the new.
    bridge.start_decider("ws-2")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not links[0].cancelled:
        qt.app.processEvents()
        time.sleep(TICK)
    assert links[0].cancelled
    assert links[1].workspace_id == "ws-2"
    # The page's retire cancels the standing link — the socket's
    # close the daemon's own deregistration — and a retire with
    # nothing standing is a plain no-op.
    bridge.stop_decider()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not links[1].cancelled:
        qt.app.processEvents()
        time.sleep(TICK)
    bridge.stop_decider()
    assert links[1].cancelled
    # The window's close takes the loop down with whatever tasks
    # stand — none here but the retiring unwind — and an ask
    # after it lands nowhere.
    bridge.stop()
    assert not bridge.running
    bridge.start_decider("ws-3")
    time.sleep(0.05)
    assert len(links) == 2
    # The retire on a stopped bridge is the same quiet no-op.
    bridge.stop_decider()


def test_a_decider_task_that_dies_on_its_own_stops_clean(
    qapp_setup,
) -> None:
    """A decider task that crashed on its own (#999): the reap
    retrieves the task's exception the moment it lands — never
    left un-retrieved on a task the bridge made — and the stop
    still takes the loop down clean."""

    class DyingLink:
        """A link whose run ends at once: the died-on-its-own
        shape."""

        def __init__(self, workspace_id: str, controller=None) -> None:
            self.workspace_id = workspace_id
            self.controller = controller
            self.on_frame = None
            self.on_rejected = None

        async def run(self) -> None:
            raise RuntimeError("the link died on its own")

    qt = qapp_setup
    bridge = qt.Bridge(StubData(), decider_factory=DyingLink)
    bridge.start()
    bridge.start_decider("ws-1")
    deadline = time.monotonic() + 10.0
    task = None
    while time.monotonic() < deadline:
        qt.app.processEvents()
        task = bridge._decider_task
        if task is not None and task.done():
            break
        time.sleep(TICK)
    assert task is not None and task.done()
    bridge.stop()
    assert not bridge.running


def test_the_stop_retrieves_a_task_that_dies_mid_unwind(
    qapp_setup,
) -> None:
    """The loop's shutdown with tasks still standing (#999): a
    decider link whose unwind itself fails — the cancel caught
    and a bug raised in its place — lands as an ended task at
    stop time, and the shutdown's completion callback retrieves
    the exception and still stops the loop only after the last
    task lands."""

    class ParkingFeed:
        """The feed's shape: parked, the stop's cancel its own
        end."""

        def __init__(self) -> None:
            self.on_frame = None
            self.on_drop = None
            self.on_reconnect = None

        async def run(self) -> None:
            await asyncio.sleep(3600)

    class UnwindingLink:
        """A link that answers its cancel with a bug: the
        died-mid-unwind shape."""

        def __init__(self, workspace_id: str, controller=None) -> None:
            self.workspace_id = workspace_id
            self.controller = controller
            self.on_frame = None
            self.on_rejected = None

        async def run(self) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise RuntimeError("the unwind broke") from None

    qt = qapp_setup
    feed = ParkingFeed()
    bridge = qt.Bridge(
        StubData(),
        feed_factory=lambda: feed,
        decider_factory=UnwindingLink,
    )
    bridge.start()
    bridge.start_decider("ws-1")
    time.sleep(0.2)  # both tasks parked on their serves
    qt.app.processEvents()
    bridge.stop()
    assert not bridge.running


def test_the_registration_lands_on_the_controller_the_ask_carried(
    qapp_setup,
) -> None:
    """The decider state seam (#1000): the controller the page's
    consent presenter owns crosses to the link the factory
    builds — the frames land on the state the presenter's rows
    build from — the same-controller re-ask stays the no-op the
    page's open-and-show pair asks, and a fresh controller (a
    new open's presenter) retires the standing registration
    first."""

    qt = qapp_setup
    links: list[ParkingLink] = []
    bridge = qt.Bridge(
        StubData(),
        decider_factory=lambda workspace_id, controller=None: (
            links.append(ParkingLink(workspace_id, controller)) or links[-1]
        ),
    )
    bridge.start()
    first = ConsentController(workspace_id="ws-1")
    bridge.start_decider("ws-1", first)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not links:
        qt.app.processEvents()
        time.sleep(TICK)
    assert links
    assert links[0].controller is first
    # The re-ask under the same controller — the page's show
    # after its open — keeps the one link standing.
    bridge.start_decider("ws-1", first)
    time.sleep(0.1)
    qt.app.processEvents()
    assert len(links) == 1
    assert not links[0].cancelled
    # A fresh presenter's controller (a new open of the same
    # workspace) retires the standing registration first: the
    # holds land on the state the new presenter reads.
    second = ConsentController(workspace_id="ws-1")
    bridge.start_decider("ws-1", second)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and (
        not links[0].cancelled or len(links) < 2
    ):
        qt.app.processEvents()
        time.sleep(TICK)
    assert links[0].cancelled
    assert links[1].controller is second
    bridge.stop()
    assert not bridge.running


def test_the_controller_arms_of_the_idempotence_rule(qapp_setup) -> None:
    """The idempotence key's cross arms (#1000): a controller-less
    re-ask over a standing controller-registration keeps the
    standing link — the ask without a controller says keep, not
    rebuild — and a controller-bearing ask over a standing
    controller-less registration retires it and relaunches
    around the controller the ask carried, the presenter's state
    the frames must land on."""

    def spun(count: int, cancelled: int | None = None) -> bool:
        return len(links) >= count and (
            cancelled is None or links[cancelled].cancelled
        )

    qt = qapp_setup
    links: list[ParkingLink] = []
    bridge = qt.Bridge(
        StubData(),
        decider_factory=lambda workspace_id, controller=None: (
            links.append(ParkingLink(workspace_id, controller)) or links[-1]
        ),
    )
    bridge.start()
    held = ConsentController(workspace_id="ws-1")
    bridge.start_decider("ws-1", held)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not spun(1):
        qt.app.processEvents()
        time.sleep(TICK)
    assert spun(1)
    assert links[0].controller is held
    # The controller-less re-ask keeps the controller-bearing
    # registration standing.
    bridge.start_decider("ws-1")
    time.sleep(0.1)
    qt.app.processEvents()
    assert len(links) == 1
    assert not links[0].cancelled
    # The retire, then a controller-less ask: the next ask under
    # a controller retires the plain registration and relaunches
    # around it.
    bridge.stop_decider()
    bridge.start_decider("ws-1")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not spun(2):
        qt.app.processEvents()
        time.sleep(TICK)
    assert spun(2)
    assert links[1].controller is None
    fresh = ConsentController(workspace_id="ws-1")
    bridge.start_decider("ws-1", fresh)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not spun(3, cancelled=1):
        qt.app.processEvents()
        time.sleep(TICK)
    assert spun(3, cancelled=1)
    assert links[2].controller is fresh
    bridge.stop()
    assert not bridge.running

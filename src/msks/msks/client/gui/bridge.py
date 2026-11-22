"""The GUI's asyncio worker thread (phase 2 of #924): one event
loop on one worker thread, started with the window and stopped on
close.

The screens' REST calls run as :class:`GuiData` coroutines on
that loop — Qt's GUI thread never awaits the network — and each
call's result or failure comes back over a Qt signal: the signal
is emitted from the worker thread, and a receiver
:class:`~PySide6.QtCore.QObject` living in the GUI thread takes
it queued (Qt's auto connection resolves by the receiver's
thread), so the slot paints without a thread hop of its own.

This module imports PySide6 at module scope. That is safe
because nothing imports it at CLI parse time: the package init
pins only the Qt-free ``.app`` and ``.data`` (#927), and
``.app``'s window path imports this module lazily, after its
guards — that import is the reachability the graph gate counts.

Receivers must be ``QObject`` instances created in the GUI
thread (the queued delivery above depends on the receiver's
thread affinity) — a plain function connected to these signals
runs on the worker thread instead. Phase 3's screens build
their receivers that way; the bridge's own tests show the
shape.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError

from PySide6 import QtCore

from ..data import RestFailure
from .data import GuiData
from .decider import DeciderLink

#: How long stop() waits for the worker to land its loop's
#: shutdown, in seconds: a stuck network call inside the loop
#: must not hang the window's close on it.
JOIN_TIMEOUT_S = 5.0


def stop_on_last(
    tasks: set[asyncio.Task], loop: asyncio.AbstractEventLoop
) -> None:
    """The loop's stop wired to the last task's completion
    (#946, #999): each ended task's exception, if it died of
    one, retrieved the moment it lands, and the loop stopped
    only when the last one has. A task's unwind suspends inside
    its socket's close, so a stop on a timer can outrun it and
    leave the task destroyed-but-pending; the completion
    callback cannot."""

    def finished(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # retrieved, whatever it was
        tasks.discard(task)
        if not tasks:
            loop.stop()

    for task in tasks:
        task.add_done_callback(finished)


class Bridge(QtCore.QObject):
    """One asyncio loop on a worker thread, owned by the window.

    ``succeeded(key, result)`` and ``failed(key, RestFailure)``
    answer each :meth:`call` on the GUI thread (queued through a
    receiver in that thread); ``key`` is the caller's own handle
    for matching the answer to the call. The live events feed
    (#946) rides the same loop when a feed factory is given:
    ``landed(frame)`` delivers each pushed frame to the GUI
    thread, ``feed_dropped(reason)`` and ``feed_reconnected()``
    carry the subscription's outage and recovery — the screens
    flash the one and refresh on the other — and
    ``feed_served()`` tells every connection's first frame
    (#1019): the poke the screens re-read on, first connection
    included, because frames published while no connection
    stood never landed. The workspace
    page's decider registration (#999) rides the same loop as
    its own task: ``start_decider(workspace)`` claims a
    workspace's holds for the page's lifetime —
    ``stop_decider()`` retires it, the socket's close the
    daemon's own retire — and the link's landings cross as
    ``decider_landed(outcome, payload)`` with the daemon's
    refusal of the registration as
    ``decider_rejected(workspace, reason)``, its presence as
    ``decider_served(workspace)`` and
    ``decider_dropped(workspace, reason)`` (#1052 — the Egress
    tab's indicator reads them live, each tell carrying its
    own link's workspace: a retired link's drop cannot paint
    the workspace the page moved to).
    The registration's state lands on the controller the ask
    carries (#1000): the page's consent presenter owns one, and
    the rows the panel renders read the same map the frames
    land on. The controller rides opaquely — the bridge never
    reads it, and its typed seam is the presenter's and the
    link's (``msks.client.tui.consent`` sits at the import
    fan-in ceiling, so the bridge does not become another
    importer for an annotation).
    """

    succeeded = QtCore.Signal(str, object)
    failed = QtCore.Signal(str, object)
    landed = QtCore.Signal(object)
    feed_dropped = QtCore.Signal(str)
    feed_reconnected = QtCore.Signal()
    feed_served = QtCore.Signal()
    decider_landed = QtCore.Signal(str, object)
    decider_rejected = QtCore.Signal(str, str)
    decider_served = QtCore.Signal(str)
    decider_dropped = QtCore.Signal(str, str)

    def __init__(
        self,
        data: GuiData,
        *,
        thread_name: str = "msks-gui",
        feed_factory=None,
        decider_factory=DeciderLink,
    ) -> None:
        super().__init__()
        self._data = data
        self._thread_name = thread_name
        self._feed_factory = feed_factory
        self._decider_factory = decider_factory
        self._feed_task: asyncio.Task | None = None
        self._decider_task: asyncio.Task | None = None
        self._decider_link = None
        #: The standing registration's workspace (#999) — the
        #: idempotence key a re-ask for the page's own workspace
        #: reads.
        self._decider_workspace = ""
        #: The standing registration's controller (#1000) —
        #: the other half of the idempotence key: a re-ask for
        #: the same workspace under a fresh presenter (a new
        #: page open) retires the old registration first.
        self._decider_controller = None
        #: The cancelled-but-unwound decider tasks (#999): a
        #: retire cancels and moves on, and the loop's shutdown
        #: still owes each one its completion — the unwind
        #: suspends inside the socket's close.
        self._retiring: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Whether the worker loop is up and taking calls."""
        return self._loop is not None

    def start(self) -> None:
        """Bring the loop up: the worker thread runs it, and
        start() returns once it is turning — a call handed over
        right after start never races the loop's first turn."""
        if self._loop is not None:
            return
        started = threading.Event()
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread = threading.Thread(
            target=self.serve,
            args=(loop, started),
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()
        started.wait()
        if self._feed_factory is not None:
            loop.call_soon_threadsafe(self.start_feed, loop)

    def serve(
        self, loop: asyncio.AbstractEventLoop, started: threading.Event
    ) -> None:
        """The worker's whole body: run the loop until stop()
        asks it to quit, then close it (run_forever never
        returns otherwise, and the loop's own close is safest on
        its own thread). The loop comes in as an argument, not
        off self: stop() clears self before the worker lands.
        An escaped exit contract — a coroutine that raised
        SystemExit past its guard — tears out of run_forever
        after the flight's failure is already delivered, so the
        worker catches it and turns the loop again: one escaped
        refusal must not take the worker every later call
        needs."""
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        try:
            while True:
                try:
                    loop.run_forever()
                except SystemExit:
                    continue
                break
        finally:
            loop.close()

    def start_feed(self, loop: asyncio.AbstractEventLoop) -> None:
        """The events feed task on the loop (#946), its
        callbacks the bridge's signals — the feed runs for the
        bridge's whole life, cancelled with it. Runs on the loop
        thread (a ``call_soon_threadsafe`` hand-off from
        :meth:`start`), so the loop reference comes in as an
        argument: a racing stop may already have cleared the
        one on self."""
        feed = self._feed_factory()
        feed.on_frame = self.landed.emit
        feed.on_drop = self.feed_dropped.emit
        feed.on_reconnect = self.feed_reconnected.emit
        feed.on_serve = self.feed_served.emit
        self._feed_task = loop.create_task(feed.run())

    def stop_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop's shutdown (#946, #999): the standing tasks
        — the feed, the page's decider link, and any decider
        link still unwinding from a cancel — cancelled, and the
        stop wired to the last completion. No task standing
        means a plain stop."""
        tasks = self.standing_tasks()
        if not tasks:
            loop.stop()
            return
        for task in tasks:
            task.cancel()
        stop_on_last(tasks, loop)

    def standing_tasks(self) -> set[asyncio.Task]:
        """The tasks the loop's shutdown owes a completion to
        (#946, #999): the feed, the standing decider link, and
        the retired ones still unwinding their socket's close —
        done tasks excluded, their completions already
        landed."""
        return {
            task
            for task in (self._feed_task, self._decider_task, *self._retiring)
            if task is not None and not task.done()
        }

    def start_decider(
        self, workspace_id: str, controller: object | None = None
    ) -> None:
        """The workspace page's registration (#999): claim
        ``workspace_id``'s holds on the loop, the frames landing
        on ``controller`` when the ask carries one — the page's
        consent presenter's (#1000). Idempotent per workspace —
        the page's open and its show both ask, and the second
        ask for the standing workspace changes nothing — while
        a different workspace (the page re-hosted) retires the
        old registration first. A stopped bridge ignores the
        ask: the window is closing, and the page's hide tells
        the retire anyway."""
        if self._loop is None:
            return
        loop = self._loop
        loop.call_soon_threadsafe(
            self.launch_decider, loop, workspace_id, controller
        )

    def launch_decider(
        self,
        loop: asyncio.AbstractEventLoop,
        workspace_id: str,
        controller: object | None = None,
    ) -> None:
        """The registration's own start, on the loop thread: the
        link built around the controller the ask carried, its
        callbacks the bridge's signals, and its task reaped when
        it ends — an exception never left un-retrieved on a task
        the bridge made."""
        if self.standing_answers(workspace_id, controller):
            return
        self.retire_decider()
        link = self._decider_factory(workspace_id, controller)
        link.on_frame = self.decider_landed.emit
        # The presence and refusal tells carry their own link's
        # workspace (#1052 review): the closure holds the launch's
        # id, not the standing slot a retire clears — a drop off
        # a link the page already retired lands with the retired
        # workspace's name, and the indicator it no longer hosts
        # ignores it.
        link.on_rejected = lambda reason: self.decider_rejected.emit(
            workspace_id, reason
        )
        link.on_serve = lambda: self.decider_served.emit(workspace_id)
        link.on_drop = lambda reason: self.decider_dropped.emit(
            workspace_id, reason
        )
        self._decider_link = link
        self._decider_workspace = workspace_id
        self._decider_controller = controller
        task = loop.create_task(link.run())
        task.add_done_callback(self.reap_decider)
        self._decider_task = task

    def standing_answers(
        self, workspace_id: str, controller: object | None
    ) -> bool:
        """Whether the standing registration answers the ask
        (#999, #1000): the same workspace under no controller
        change — a controller-less re-ask keeps the standing
        state, and the same controller's re-ask (the page's
        show after its open) keeps the link — while a fresh
        controller (a new open's presenter) retires the old
        registration first."""
        task = self._decider_task
        return (
            task is not None
            and not task.done()
            and workspace_id == self._decider_workspace
            and (controller is None or controller is self._decider_controller)
        )

    def stop_decider(self) -> None:
        """The page's retire (#999): cancel the standing
        registration — the socket's close is the daemon's own
        deregistration — from any thread, through the loop's
        threadsafe door."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.retire_decider)

    def retire_decider(self) -> None:
        """The retire on the loop thread: the standing link's
        own drop tell silenced before the cancel — the unwind
        can outlast a whole re-registration (its close handshake
        suspends up to the close bound), and a drop that lands
        after the new link's serve would paint a healthy
        registration down (#1052 review) — the retire's drop
        told here instead, synchronously with the retire, its
        workspace beside it: the indicator reads down while the
        page is away, and the link that takes over tells the
        rest. The slot cleared, the task cancelled and held in
        the retiring set until its unwind lands — the loop's
        shutdown waits for it there. A task that already ended
        (the daemon's refusal) tells nothing: its own refusal
        stands."""
        task = self._decider_task
        link = self._decider_link
        workspace = self._decider_workspace
        self._decider_task = None
        self._decider_link = None
        self._decider_workspace = ""
        self._decider_controller = None
        if task is None or task.done():
            return
        link.on_drop = None
        self.decider_dropped.emit(workspace, "the registration was retired")
        task.cancel()
        self._retiring.add(task)

    def reap_decider(self, task: asyncio.Task) -> None:
        """One decider task's end: its exception, if it died of
        one, retrieved the moment it lands — and its retiring
        bookkeeping cleared with it."""
        self._retiring.discard(task)
        if not task.cancelled():
            task.exception()

    def call(self, key: str, method: str, *args: object):
        """Run one :class:`GuiData` coroutine by name — the
        screens' one verb for the data layer. The answer arrives as
        ``succeeded``/``failed`` with the same key; a bridge that
        is not running answers ``failed`` at once rather than
        handing the call to a loop that is gone. Returns the
        flight's future — a screen closing mid-call cancels
        through it — or None on the answered-at-once arm."""
        if self._loop is None:
            self.failed.emit(
                key, RestFailure("the GUI's bridge is stopped", reach=False)
            )
            return None
        coro = getattr(self._data, method)(*args)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(lambda done: self.deliver(key, done))
        return future

    @staticmethod
    def as_failure(exc: BaseException) -> RestFailure:
        """One raised outcome as the failure the screens hear.
        The exit contract keeps its reach flag (an environment
        refused — the daemon was never asked); a bug in the data
        layer shows its own message without it."""
        return RestFailure(str(exc), reach=isinstance(exc, SystemExit))

    def deliver(self, key: str, future: asyncio.Future) -> None:
        """Turn one finished future into a signal — the
        fire-and-forget contract: a raising body must not reach
        the event loop, so every arm ends in an emit or a named
        silence. The cancellation arm is that named silence: a
        flight cancelled through its handle (the screen that
        asked closed) surfaces here as the concurrent future's
        own CancelledError — an Exception subclass, not
        asyncio's — and nothing the screens need shown. The
        combined arm carries both the exit contract a coroutine
        raised past its guard (SystemExit, BaseException-derived;
        uncaught, a screen would wait forever) and a data-layer
        bug, each as the same visible shape (a failed call)."""
        try:
            result = future.result()
        except RestFailure as exc:
            self.failed.emit(key, exc)
        except CancelledError:
            pass
        except (SystemExit, Exception) as exc:
            self.failed.emit(key, self.as_failure(exc))
        else:
            self.succeeded.emit(key, result)

    def stop(self) -> None:
        """Take the loop down and land the worker: idempotent (a
        window that both quits and closes calls it twice), and
        safe from any thread — stop() never touches the loop
        except through its own threadsafe door."""
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        self._loop = None
        self._thread = None
        loop.call_soon_threadsafe(self.stop_tasks, loop)
        thread.join(JOIN_TIMEOUT_S)

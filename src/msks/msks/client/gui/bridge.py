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

#: How long stop() waits for the worker to land its loop's
#: shutdown, in seconds: a stuck network call inside the loop
#: must not hang the window's close on it.
JOIN_TIMEOUT_S = 5.0


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
    flash the one and refresh on the other.
    """

    succeeded = QtCore.Signal(str, object)
    failed = QtCore.Signal(str, object)
    landed = QtCore.Signal(object)
    feed_dropped = QtCore.Signal(str)
    feed_reconnected = QtCore.Signal()

    def __init__(
        self,
        data: GuiData,
        *,
        thread_name: str = "msks-gui",
        feed_factory=None,
    ) -> None:
        super().__init__()
        self._data = data
        self._thread_name = thread_name
        self._feed_factory = feed_factory
        self._feed_task: asyncio.Task | None = None
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
        self._feed_task = loop.create_task(feed.run())

    def stop_feed(self, loop: asyncio.AbstractEventLoop) -> None:
        """One loop-turn shutdown (#946): the feed task cancelled
        first, then the stop behind a zero-delay timer — the
        cancel queues the task's step, and the timer fires only
        after the step lands, so the cancellation reaches the
        task's own ``except CancelledError`` (a socket's close
        among them) before the loop exits. A stop scheduled
        straight after the cancel would win the race and leave
        the task destroyed-but-pending."""
        if self._feed_task is not None:
            self._feed_task.cancel()
        loop.call_later(0, loop.stop)

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
        loop.call_soon_threadsafe(self.stop_feed, loop)
        thread.join(JOIN_TIMEOUT_S)

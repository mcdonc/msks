"""The dictate pill's controller (#1214): one state machine over
one recording — the capture stream, the speech gate, the
transcription, and the delivery to the clipboard — with every
pill on every window reading the same state.

The states are ``idle``, ``recording``, ``transcribing``, ``done``,
``failed``, and ``nomodel``; the machine the issue draws is
``idle -> recording -> transcribing -> done``. The pill is a
toggle: one click starts a recording, a second click finishes it.
Nothing ends a recording on the operator's behalf — the gate's
quiet stop is not used, because a pause inside a sentence is not
the end of one — except the cap, the safety net that ends a clip
nobody stopped. A click in transcribing abandons the pending
transcript and starts again; Discard cancels from recording or
transcribing. ``done`` shows ``copied`` for two seconds and
``failed`` shows the failure for four, each then back to idle;
``nomodel`` stands until the model downloads.

Capture is the spike's own (#1212): ``pw-record`` writes raw 16 kHz
mono s16 to a pipe, here a QProcess whose stdout feeds the
:mod:`msks.client.dictate` gate frame by frame. The gate still finds
where speech starts — the half second before it is kept, so the
first word survives — and whether anything was said at all. The
transcription runs once per clip on a worker thread, exactly as the
spike ran ``whisper-cli``, and its answer lands on the GUI thread
through a signal. The tones, the notification, the clipboard, the
transcription, and the capture command are constructor seams: the
offscreen suite drives the machine over a synthetic source and a
canned transcript, with no microphone, model, or desktop.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui

from ..dictate import (
    DONE_TONE_HZ,
    START_TONE_HZ,
    DictateFailure,
    Framer,
    capture_command,
    frame_db,
    notify,
    play,
    tone_file,
    transcribe,
    write_wav,
)
from .dictatekeys import DictateSettings

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"
DONE = "done"
FAILED = "failed"
NOMODEL = "nomodel"

#: How long ``done`` and ``failed`` stand before idle (#1214).
COPIED_MS = 2000
FAILED_MS = 4000
#: The recording timer's tick: the display counts whole seconds.
TICK_MS = 500

WORK_PREFIX = "msks-dictate-"


def copy_both(text: str) -> None:
    """The transcript onto the clipboard and the primary selection
    (#1210): the clipboard always, the selection where the
    platform keeps one (X11 and the Wayland compositors that serve
    the primary-selection protocols). The user pastes."""
    board = QtGui.QGuiApplication.clipboard()
    board.setText(text, QtGui.QClipboard.Mode.Clipboard)
    if board.supportsSelection():
        board.setText(text, QtGui.QClipboard.Mode.Selection)


class CaptureStream(QtCore.QObject):
    """``pw-record`` under a QProcess (#1214): the raw s16 stream
    on the child's stdout, re-emitted in reads of any size for
    the controller's :class:`Framer`. ``gone`` carries why the
    child ended when the controller did not stop it — a missing
    binary, a nonzero exit — and never fires on a stop."""

    #: Bytes read off the pipe per pass.
    READ = 1 << 16

    frames = QtCore.Signal(bytes)
    gone = QtCore.Signal(str)

    def __init__(self, command: list[str], parent=None) -> None:
        super().__init__(parent)
        self.command = command
        self.proc = QtCore.QProcess(self)
        self.proc.readyRead.connect(self.pump)
        self.proc.finished.connect(self.ended)
        self.proc.errorOccurred.connect(self.refused)
        self.stopping = False
        self.reported = False

    def start(self) -> None:
        self.proc.start(self.command[0], self.command[1:])

    def pump(self) -> None:
        """The pipe's bytes out (#1214): read while any stand, and
        hand each read to the controller. A stopping stream reads
        nothing — its controller has moved on, and the bytes still
        in the pipe are the child's own to flush until it exits."""
        while not self.stopping and self.proc.bytesAvailable():
            self.frames.emit(bytes(self.proc.read(self.READ)))

    def stop(self) -> None:
        """End the child (#1212's own stop): SIGINT, the way the
        spike stopped ``pw-record``, then a kill if it lingers.
        The end is the controller's, so ``gone`` stays silent.
        Never called from inside a ``readyRead`` slot: the wait
        blocks the loop under the notifier that is dispatching,
        and the child's exit tears that notifier down mid-flight
        (a segfault under the offscreen suite). The controller
        defers its finish one loop iteration for exactly that;
        this method stays the blocking stop for every other
        caller."""
        self.stopping = True
        if self.proc.state() == QtCore.QProcess.ProcessState.NotRunning:
            return
        self.proc.terminate()
        if not self.proc.waitForFinished(3000):
            self.proc.kill()
            self.proc.waitForFinished(1000)

    def ended(self, code, _status) -> None:
        if self.stopping or self.reported:
            return
        self.reported = True
        detail = (
            bytes(self.proc.readAllStandardError())
            .decode("utf-8", "replace")
            .strip()[-300:]
        )
        self.gone.emit(detail or f"{self.command[0]} exited {code}")

    def refused(self, error) -> None:
        if self.stopping or self.reported:
            return
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.reported = True
            self.gone.emit(f"{self.command[0]} not found")


class Dictation(QtCore.QObject):
    """The one recording (#1214). ``changed`` names the state,
    the recording's elapsed seconds, and the note the transient
    states carry; ``settings_requested`` asks the window for the
    settings page — the no-model click's answer. The seams take
    the suite's fakes: the capture command, the transcriber, the
    tone player, the notifier, and the clipboard."""

    changed = QtCore.Signal(str, float, str)
    settings_requested = QtCore.Signal()
    landed = QtCore.Signal(int, str, str)

    def __init__(
        self,
        settings: DictateSettings,
        cache_root: Path,
        parent=None,
        *,
        command_for: Callable[[str], list[str]] = capture_command,
        transcriber: Callable[[Path, DictateSettings], str] | None = None,
        tone: Callable[[int], None] | None = None,
        notifier: Callable[[str], None] = notify,
        clipboard: Callable[[str], None] = copy_both,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.cache_root = cache_root
        self.command_for = command_for
        self.transcriber = transcriber or self.whisper
        #: The start and done tones (#1210): the spike's own WAVs
        #: through ``pw-cat``; a host without it hears nothing.
        self.tone = tone or (lambda hz: play(tone_file(hz, self.cache_root)))
        self.notifier = notifier
        self.clipboard = clipboard
        self.state = IDLE
        self.note = ""
        self.elapsed = 0.0
        self.stream: CaptureStream | None = None
        self.detector = None
        self.framer = Framer()
        #: True from the gate's stop to the deferred finish
        #: (#1214): no frame feeds the gate twice past its stop.
        self.finishing = False
        #: Counts recordings (#1214): a transcription's answer
        #: lands only while its count still stands — a click in
        #: transcribing moves on and the old answer is dropped.
        self.generation = 0
        self.tick = QtCore.QTimer(self)
        self.tick.setInterval(TICK_MS)
        self.tick.timeout.connect(self.tocked)
        self.settle = QtCore.QTimer(self)
        self.settle.setSingleShot(True)
        self.settle.timeout.connect(self.settled)
        self.landed.connect(self.delivered)
        self.check_model()

    # --- the state ------------------------------------------------------

    def whisper(self, wav: Path, settings: DictateSettings) -> str:
        """The default transcriber: whisper-cli once, over the
        settings' model and prompt."""
        return transcribe(
            wav,
            settings.model_file(self.cache_root),
            settings.lang,
            settings.prompt,
        )

    def model_present(self) -> bool:
        return self.settings.model_file(self.cache_root).exists()

    def check_model(self) -> None:
        """The no-model state (#1214): the pill wears the download
        glyph until the model stands — at the open, after the
        settings change, and after a download."""
        if self.state in (RECORDING, TRANSCRIBING):
            return
        self.show(NOMODEL if not self.model_present() else IDLE)

    def reload(self, settings: DictateSettings) -> None:
        """New settings from the page (#1214): the next recording
        takes them, and the model's presence is read again."""
        self.settings = settings
        self.check_model()

    def show(self, state: str, note: str = "") -> None:
        """Move to ``state`` and tell every pill."""
        self.state = state
        self.note = note
        self.changed.emit(state, self.elapsed, note)

    def toggle(self) -> None:
        """The click (#1214): the toggle — start in idle and in
        the transient states, finish in recording — and in
        transcribing abandon the pending transcript and start
        again; in nomodel open the settings."""
        if self.state == RECORDING:
            self.finish()
        elif self.state == TRANSCRIBING:
            self.abandon()
            self.start()
        elif self.state == NOMODEL:
            self.settings_requested.emit()
        else:
            self.start()

    def start(self) -> None:
        """Begin a recording (#1214): the start tone, a fresh
        gate, and the capture child; the timer runs from zero. The
        model was checked by the caller's state."""
        self.settle.stop()
        self.finishing = False
        if self.stream is not None:
            self.stream.stop()
        self.elapsed = 0.0
        self.detector = self.settings.gate.detector()
        self.framer = Framer()
        try:
            self.tone(START_TONE_HZ)
        except OSError:
            pass  # a cache the tones cannot write into stays silent
        self.stream = CaptureStream(self.command_for(self.settings.device))
        self.stream.frames.connect(self.heard)
        self.stream.gone.connect(self.died)
        self.tick.start()
        self.show(RECORDING)
        self.stream.start()

    @QtCore.Slot(bytes)
    def heard(self, raw: bytes) -> None:
        """The pipe's bytes through the gate (#1214): each frame
        fed, so the gate finds where speech starts and whether
        anything was said. The gate's quiet stop ends nothing — the
        operator's click does — but its cap still does, as the
        safety net, and that stop is taken one loop iteration
        later, never inside the read slot: the finish stops the
        child, and the blocking stop under the notifier that
        delivered these bytes takes the process down mid-dispatch.
        The stream reads nothing more meanwhile
        (:meth:`CaptureStream.pump`)."""
        if self.detector is None or self.finishing:
            return
        for frame in self.framer.feed(raw):
            if self.capped_after(frame):
                self.defer_finish()
                return

    def capped_after(self, frame) -> bool:
        """Feed one frame (#1214) and answer whether the cap ended
        the clip: the gate's quiet stop and its wait's timeout end
        nothing here — only the cap does."""
        decision = self.detector.feed(frame, frame_db(frame))
        return decision == "stop" and self.detector.capped()

    def defer_finish(self) -> None:
        """The cap's finish, one loop iteration later (#1214): the
        gate is marked finishing so no frame feeds it again, the
        stream reads nothing more, and the zero timer lands the
        finish outside this read slot."""
        self.finishing = True
        self.stream.stopping = True
        QtCore.QTimer.singleShot(0, self, self.finish)

    @QtCore.Slot()
    def tocked(self) -> None:
        """The recording timer (#1214): the pill's count follows
        the gate's own clock, the frames fed."""
        if self.detector is not None:
            self.elapsed = self.detector.elapsed
        self.changed.emit(self.state, self.elapsed, self.note)

    @QtCore.Slot(str)
    def died(self, reason: str) -> None:
        """The capture child's own end (#1214): a recording with
        speech in it finishes on what it has; one with nothing
        fails with the reason — the missing binary, the exit."""
        if self.state != RECORDING:
            return
        if self.detector is not None and self.detector.said_something():
            self.finish()
        else:
            self.tick.stop()
            self.fail(reason)

    def finish(self) -> None:
        """The end of a recording (#1214): the second click, the
        cap, or the capture child's own end. The child stops, and
        a clip with speech in it goes to the transcription;
        nothing said returns to idle with no tone. The cap
        reaches here through a zero timer (:meth:`heard`); the
        click and the child's end call it directly."""
        self.tick.stop()
        self.finishing = False
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
        if self.detector is None or not self.detector.said_something():
            self.show(IDLE)
            return
        work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX))
        wav = work / "clip.wav"
        write_wav(wav, self.detector.frames())
        self.generation += 1
        self.show(TRANSCRIBING)
        threading.Thread(
            target=self.transcribe_in_thread,
            args=(self.generation, wav, work),
            daemon=True,
        ).start()

    def transcribe_in_thread(
        self, generation: int, wav: Path, work: Path
    ) -> None:
        """The worker thread's body (#1214): whisper-cli once, the
        answer back over a signal the GUI thread receives."""
        try:
            text = self.transcriber(wav, self.settings)
        except DictateFailure as exc:
            self.landed.emit(generation, "", str(exc))
        else:
            self.landed.emit(generation, text, "")
        shutil.rmtree(work, ignore_errors=True)

    @QtCore.Slot(int, str, str)
    def delivered(self, generation: int, text: str, error: str) -> None:
        """A transcription's answer on the GUI thread (#1214): a
        stale one — the recording it belonged to was abandoned —
        changes nothing; a failure stands four seconds with the
        reason; a transcript is delivered."""
        if generation != self.generation:
            return
        if error or not text:
            self.fail(error or "no transcript")
            return
        self.deliver(text)

    def deliver(self, text: str) -> None:
        """A transcript's delivery (#1214): the clipboard and the
        selection take it, the done tone plays, the notification
        carries the first line, and ``copied`` stands two
        seconds."""
        self.clipboard(text)
        try:
            self.tone(DONE_TONE_HZ)
        except OSError:
            pass
        self.notifier(text.splitlines()[0][:120])
        self.show(DONE, "copied")
        self.settle.start(COPIED_MS)

    def fail(self, reason: str) -> None:
        """A failure (#1214): the notification carries the reason
        the pill then shows for four seconds."""
        self.notifier(f"dictation failed: {reason}")
        self.show(FAILED, reason)
        self.settle.start(FAILED_MS)

    def settled(self) -> None:
        """The transient states' end (#1214): back to idle, or to
        the no-model glyph when the model is gone."""
        self.check_model()

    def abandon(self) -> None:
        """Drop a pending transcript (#1214): its answer lands on
        a generation that no longer stands."""
        self.generation += 1

    def discard(self) -> None:
        """The pill's ``x`` (#1214): a recording stops and its
        clip drops with no tone; a pending transcription drops."""
        self.settle.stop()
        self.tick.stop()
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
        self.abandon()
        self.show(IDLE)

    def shutdown(self) -> None:
        """The window's close (#1214): the capture child ends and
        a pending transcription is dropped, with nothing shown —
        the pills are going down with the window. Idempotent, the
        way the bridge's stop is: ``aboutToQuit`` and the return
        path both reach it."""
        self.settle.stop()
        self.tick.stop()
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
        self.abandon()

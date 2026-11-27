"""The dictation's suite (#1214): the state machine offscreen over
a synthetic capture source — a ``pw-record`` stand-in on a private
PATH writing raw frames to its stdout — with the transcriber, the
tones, the notifier, and the clipboard as recorded seams. No
microphone, model, or desktop: the GUI suite's own
``QT_QPA_PLATFORM=offscreen`` pattern (test_gui_zoom.py's fixture
shape), the detector's own checks in test_client_dictate.py."""

import array
import os
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.client import dictate
from msks.client.gui.dictatekeys import DictateSettings, resolve

QUIET = array.array("h", [0] * dictate.FRAME_SAMPLES)
LOUD = array.array("h", [8000] * dictate.FRAME_SAMPLES)

#: The ``pw-record`` stand-in (#1212's synthetic source): frames
#: per spec — ``q10 l40 q*`` is ten quiet, forty loud, then quiet
#: forever — written one frame every 30 ms, the microphone's pace,
#: so the controller sees a real stream over a real pipe.
SOURCE = textwrap.dedent(
    """
    import sys, time
    frames = {
        "q": bytes(%(frame_bytes)d),
        "l": (8000).to_bytes(2, "little") * %(samples)d,
    }
    spec = open(sys.argv[sys.argv.index("--target") + 1]).read()
    try:
        for item in spec.split():
            kind, count = item[0], item[1:]
            n = 0
            while count == "*" or n < int(count):
                sys.stdout.buffer.write(frames[kind])
                sys.stdout.buffer.flush()
                n += 1
                time.sleep(%(seconds)r)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    """
)


@pytest.fixture()
def qt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The offscreen ``QApplication`` with a private PATH head
    (the capture stand-in lands there) and the client's cache
    root pointed at the temporary tree."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for key in ("MSKSC_CACHE_DIR", "MSKSC_DICTATE_MODEL"):
        monkeypatch.delenv(key, raising=False)
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        tmp=tmp_path,
        bin=source_on_path(tmp_path, monkeypatch),
    )
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def source_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The ``pw-record`` stand-in on a private PATH head (#1214):
    its last argument names a spec file, so each test writes the
    stream it wants."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "pw-record"
    script.write_text(
        f"#!{sys.executable}\n"
        + SOURCE
        % dict(
            frame_bytes=dictate.FRAME_BYTES,
            samples=dictate.FRAME_SAMPLES,
            seconds=dictate.FRAME_SECONDS,
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def spec(qt, text: str) -> str:
    """A spec file the stand-in reads; the capture command's
    ``--target`` carries its path."""
    path = qt.tmp / "clip.spec"
    path.write_text(text)
    return str(path)


def spin(qt, ms: int, until=None) -> None:
    """Turn the event loop for ``ms`` (in 10 ms slices), stopping
    early once ``until()`` answers True."""
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        qt.app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.01)
    qt.app.processEvents()


class Fake:
    """The controller's recorded seams (#1214): the capture command
    (a stand-in over the spec the test names), the transcriber (a
    canned answer, held back until the test releases it), the
    tones, the notifications, and the clipboard."""

    def __init__(self, spec_path: str) -> None:
        self.spec_path = spec_path
        self.tones: list[int] = []
        self.notes: list[str] = []
        self.copied: list[str] = []
        self.transcribed: list[Path] = []
        self.answer = "Hello world."
        self.error = ""
        self.gate = threading.Event()
        self.started = threading.Event()

    def command(self, device: str) -> list[str]:
        return ["pw-record", "--target", self.spec_path, "-"]

    def transcribe(self, wav: Path, settings: DictateSettings) -> str:
        self.transcribed.append(wav)
        self.started.set()
        self.gate.wait(5)
        if self.error:
            raise dictate.DictateFailure(self.error)
        return self.answer

    def tone(self, hz: int) -> None:
        self.tones.append(hz)

    def notify(self, text: str) -> None:
        self.notes.append(text)

    def clipboard(self, text: str) -> None:
        self.copied.append(text)


def dictation(qt, spec_text: str = "q10 l40 q*", **settings) -> tuple:
    """A controller over a present model and the recorded seams."""
    from msks.client.gui.dictation import Dictation

    model = qt.tmp / "cache" / "msks" / "whisper" / "ggml-small.bin"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"ggml")
    fake = Fake(spec(qt, spec_text))
    controller = Dictation(
        resolve(settings and {"dictate_model": "small"} or {}),
        qt.tmp / "cache" / "msks",
        command_for=fake.command,
        transcriber=fake.transcribe,
        tone=fake.tone,
        notifier=fake.notify,
        clipboard=fake.clipboard,
    )
    return controller, fake


def states(controller) -> list:
    """The state *transitions* ``changed`` has named, live from
    here on: the tick re-emits the standing state every half
    second for the timer, and those repeats are not moves."""
    seen: list = []
    last = [controller.state]

    def note(state, _elapsed, _note) -> None:
        if state != last[0]:
            last[0] = state
            seen.append(state)

    controller.changed.connect(note)
    return seen


def record_speech(qt, controller, fake, quiet_tail: bool = False) -> None:
    """Start a recording and let speech arrive, then finish it
    with the toggle's second click (#1214): the clip holds the
    speech and, with ``quiet_tail``, the quiet after it. The
    transcription starts and its answer waits on the fake's
    gate."""
    controller.toggle()
    spin(
        qt,
        3000,
        until=lambda: (
            controller.detector is not None
            and controller.detector.state == "speaking"
            and controller.detector.elapsed >= 1.0
        ),
    )
    if quiet_tail:
        spin(qt, 1200)
    controller.toggle()
    spin(qt, 3000, until=lambda: fake.started.is_set())
    assert controller.state == "transcribing"


# --- the idle and no-model states ----------------------------------------


def test_a_missing_model_wears_the_download_glyph_and_opens_settings(qt):
    """No model on disk (#1214): the controller stands in nomodel,
    a click asks for the settings page instead of recording, and
    the model's arrival — a download's end — moves it to idle."""
    from msks.client.gui.dictation import IDLE, NOMODEL, Dictation

    asked: list = []
    controller = Dictation(
        DictateSettings(model="small"),
        qt.tmp / "cache" / "msks",
        transcriber=lambda wav, s: "x",
        tone=lambda hz: None,
        notifier=lambda text: None,
        clipboard=lambda text: None,
    )
    controller.settings_requested.connect(lambda: asked.append(True))
    assert controller.state == NOMODEL
    controller.toggle()
    assert asked == [True]
    assert controller.state == NOMODEL
    # The download's landing: the file stands, the state reads again.
    controller.model_present  # the read the page's landing asks
    controller.settings.model_file(controller.cache_root).parent.mkdir(
        parents=True, exist_ok=True
    )
    controller.settings.model_file(controller.cache_root).write_bytes(b"g")
    controller.check_model()
    assert controller.state == IDLE


def test_the_toggle_records_until_the_second_click(qt):
    """The toggle (#1214): a click starts the capture with the
    start tone, and the gate's quiet stop ends nothing — the clip
    that goes quiet on its own keeps recording until the second
    click. Then whisper-cli runs once over the clip, the
    transcript goes to the clipboard, the done tone plays, the
    notification carries the transcript, and ``copied`` stands
    two seconds before idle."""
    controller, fake = dictation(qt, "q10 l40 q*")
    seen = states(controller)
    controller.toggle()
    assert controller.state == "recording"
    assert fake.tones == [dictate.START_TONE_HZ]
    # The stream runs well past the quiet that would have ended
    # the spike's clip (1.5 s of frames, then silence for
    # another 1.5 s): the recording stands.
    spin(qt, 3000)
    assert controller.state == "recording"
    assert seen == ["recording"]
    assert fake.transcribed == []
    # The second click finishes it.
    controller.toggle()
    spin(qt, 3000, until=lambda: fake.started.is_set())
    assert seen == ["recording", "transcribing"]
    fake.gate.set()
    spin(qt, 2000, until=lambda: controller.state == "done")
    assert fake.copied == ["Hello world."]
    assert fake.tones == [dictate.START_TONE_HZ, dictate.DONE_TONE_HZ]
    assert fake.notes == ["Hello world."]
    assert controller.state == "done" and controller.note == "copied"
    assert not fake.transcribed[0].exists()  # swept after the run
    spin(qt, 2600, until=lambda: controller.state == "idle")
    assert controller.state == "idle"


def test_the_second_click_finishes_the_recording(qt):
    """A click while recording finishes at once (#1214): the
    speech so far goes to the transcription."""
    controller, fake = dictation(qt, "q10 l*")
    controller.toggle()
    spin(
        qt,
        3000,
        until=lambda: (
            controller.detector is not None
            and controller.detector.state == "speaking"
        ),
    )
    controller.toggle()  # the second click
    spin(qt, 3000, until=lambda: controller.state == "transcribing")
    assert controller.state == "transcribing"
    fake.gate.set()
    spin(qt, 2000, until=lambda: controller.state == "done")
    assert fake.copied == ["Hello world."]


def test_the_cap_ends_a_recording(qt):
    """The cap (#1214): a clip that runs past the gate's
    ``max_seconds`` ends on its own — the one stop the operator
    does not make — with its speech transcribed. The leaf's own
    cap is 180 s; the detector under test takes a shorter one."""
    controller, fake = dictation(qt, "q10 l*")
    controller.toggle()
    spin(
        qt,
        2500,
        until=lambda: (
            controller.detector is not None
            and controller.detector.elapsed >= 1.0
        ),
    )
    controller.detector.max_seconds = controller.detector.elapsed
    spin(qt, 3000, until=lambda: fake.started.is_set())
    assert controller.state == "transcribing"
    fake.gate.set()
    spin(qt, 2000, until=lambda: controller.state == "done")
    assert fake.copied == ["Hello world."]


def test_nothing_said_returns_to_idle_with_no_tone(qt):
    """Silence throughout (#1214): the gate's wait times out and
    the recording still stands — nothing ends it but the click —
    and the click ends it with nothing said: no transcription, no
    done tone, back to idle."""
    controller, fake = dictation(qt, "q*")
    controller.toggle()
    controller.detector.timeout = 0.3
    spin(qt, 1200)
    assert controller.state == "recording"  # the wait's timeout ended nothing
    controller.toggle()
    assert controller.state == "idle"
    assert fake.transcribed == []
    assert fake.tones == [dictate.START_TONE_HZ]


def test_discard_drops_the_clip(qt):
    """The pill's ``x`` (#1214): a recording in flight stops with
    no transcription and no tone but the start's; a pending
    transcript drops, its late answer landing nowhere."""
    controller, fake = dictation(qt, "q10 l*")
    controller.toggle()
    spin(
        qt,
        2500,
        until=lambda: (
            controller.detector is not None
            and controller.detector.state == "speaking"
        ),
    )
    controller.discard()
    assert controller.state == "idle"
    assert controller.stream is None
    spin(qt, 1500)
    assert fake.transcribed == []  # nothing reached whisper-cli
    # A pending transcript dropped mid-transcription.
    record_speech(qt, controller, fake)
    controller.discard()
    assert controller.state == "idle"
    fake.gate.set()
    spin(qt, 1500)
    assert fake.copied == []  # the stale answer landed nowhere
    assert controller.state == "idle"


def test_a_click_while_transcribing_starts_again(qt):
    """Abandon and start again (#1214): the pending transcript
    drops, a fresh recording begins, and only the new clip's
    answer reaches the clipboard."""
    controller, fake = dictation(qt, "q10 l40 q*")
    record_speech(qt, controller, fake)
    controller.toggle()  # abandon, start again
    assert controller.state == "recording"
    assert fake.tones.count(dictate.START_TONE_HZ) == 2
    spin(qt, 3000, until=lambda: controller.detector.elapsed >= 1.0)
    controller.toggle()
    spin(qt, 3000, until=lambda: len(fake.transcribed) == 2)
    fake.gate.set()
    spin(qt, 3000, until=lambda: controller.state == "done")
    assert fake.copied == ["Hello world."]  # the first answer dropped


def test_a_failing_transcription_fails_with_the_reason(qt):
    """whisper-cli's failure (#1214): ``failed`` stands four
    seconds with the reason on the pill and in the notification,
    nothing is copied, and no done tone plays."""
    controller, fake = dictation(qt, "q10 l40 q*")
    fake.error = "whisper-cli exited 1: no model"
    record_speech(qt, controller, fake)
    fake.gate.set()
    spin(qt, 2000, until=lambda: controller.state == "failed")
    assert controller.note == "whisper-cli exited 1: no model"
    assert fake.notes == ["dictation failed: whisper-cli exited 1: no model"]
    assert fake.copied == []
    assert dictate.DONE_TONE_HZ not in fake.tones
    spin(qt, 5000, until=lambda: controller.state == "idle")
    assert controller.state == "idle"


def test_a_missing_capture_binary_fails_at_once(qt, monkeypatch):
    """No ``pw-record`` on the host (#1214): the child fails to
    start, the recording fails with the binary named, and the
    notification carries it."""
    from msks.client.gui.dictation import Dictation

    fake = Fake("unused")
    controller = Dictation(
        DictateSettings(),
        qt.tmp / "cache" / "msks",
        command_for=lambda device: ["msks-no-such-record", "-"],
        transcriber=fake.transcribe,
        tone=fake.tone,
        notifier=fake.notify,
        clipboard=fake.clipboard,
    )
    controller.model_present = lambda: True  # a model stands
    controller.check_model()
    controller.toggle()
    spin(qt, 3000, until=lambda: controller.state == "failed")
    assert controller.note == "msks-no-such-record not found"
    assert fake.notes == ["dictation failed: msks-no-such-record not found"]


def test_settings_reload_rechecks_the_model(qt):
    """New settings from the page (#1214): the next recording takes
    them, and a model that went missing moves the pill to the
    download glyph."""
    controller, _fake = dictation(qt)
    assert controller.state == "idle"
    controller.reload(DictateSettings(model="large-v3-turbo"))
    assert controller.settings.model == "large-v3-turbo"
    assert controller.state == "nomodel"  # not on disk
    controller.reload(DictateSettings(model="small"))
    assert controller.state == "idle"


def test_shutdown_stops_a_recording_and_drops_a_transcript(qt):
    """The window's close (#1214): the capture child ends, a
    pending transcription drops, nothing further shows, and the
    second call is a no-op."""
    controller, fake = dictation(qt, "q10 l40 q*")
    record_speech(qt, controller, fake)
    controller.shutdown()
    assert controller.stream is None
    fake.gate.set()
    spin(qt, 1500)
    assert fake.copied == []
    controller.shutdown()  # idempotent
    assert controller.settle.isActive() is False


# --- the branches the first pass left dark -------------------------------


def test_copy_both_takes_the_clipboard_and_the_selection(qt):
    """The transcript's delivery (#1210): the clipboard always,
    the primary selection where the platform keeps one — the
    offscreen platform answers whether it does, and both arms
    run."""
    from msks.client.gui.dictation import copy_both

    copy_both("hello")
    from PySide6 import QtGui

    board = qt.app.clipboard()
    assert board.text() == "hello"
    if board.supportsSelection():
        assert board.text(QtGui.QClipboard.Mode.Selection) == "hello"


def test_the_default_transcriber_runs_whisper_cli(qt, tmp_path, monkeypatch):
    """The default transcriber (#1214): whisper-cli once, over
    the settings' model file, language, and prompt — the leaf's
    own call, with its binary pinned by PATH."""
    import stat as stat_mod

    from msks.client.gui.dictation import Dictation

    bin_dir = tmp_path / "whisper-bin"
    bin_dir.mkdir()
    script = bin_dir / "whisper-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "open(args[args.index('-f') + 1]).close()\n"
        "sys.stdout.write(args[args.index('-m') + 1] + ' '\n"
        " + args[args.index('--prompt') + 1] + '\\n')\n"
    )
    script.chmod(script.stat().st_mode | stat_mod.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    model = tmp_path / "m.bin"
    model.write_bytes(b"g")
    held = Dictation(
        DictateSettings(model=str(model), prompt="msks"),
        tmp_path,
        transcriber=None,
        tone=lambda hz: None,
        notifier=lambda text: None,
        clipboard=lambda text: None,
    )
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    assert held.transcriber == held.whisper
    assert held.whisper(wav, held.settings) == f"{model} msks"


def test_a_second_start_while_recording_restarts_the_stream(qt):
    """``start`` under a standing stream (#1214): the old child
    stops before the new one begins, and the finishing flag
    clears."""
    controller, fake = dictation(qt, "q10 l*")
    controller.toggle()
    spin(qt, 600, until=lambda: controller.stream is not None)
    old = controller.stream
    controller.finishing = True
    controller.start()
    assert controller.stream is not old
    assert controller.finishing is False
    assert old.stopping is True
    controller.discard()


def test_the_tone_guards_a_cache_that_cannot_be_written(qt, tmp_path):
    """A tone seam that raises OSError (#1214) stays silent and
    the recording, the start and the delivery, go on."""
    from msks.client.gui.dictation import Dictation

    def torn(hz: int) -> None:
        raise OSError("read-only cache")

    held = Dictation(
        DictateSettings(),
        tmp_path,
        command_for=lambda device: ["msks-no-such-record", "-"],
        transcriber=lambda wav, s: "text",
        tone=torn,
        notifier=lambda text: None,
        clipboard=lambda text: None,
    )
    held.model_present = lambda: True
    held.check_model()
    held.start()
    assert held.state == "recording"
    held.show("transcribing")
    held.delivered(held.generation, "text", "")
    assert held.state == "done"  # the done tone's failure changed nothing


def test_the_childs_own_end_finishes_or_fails(qt):
    """``died`` (#1214): speech in the clip finishes it; nothing
    said fails with the reason; a state past recording ignores
    the news."""
    controller, fake = dictation(qt, "q10 l*")
    controller.detector = controller.settings.gate.detector()
    controller.show("recording")
    controller.died("pw-record exited 2")  # nothing said yet
    assert controller.state == "failed"
    assert controller.note == "pw-record exited 2"
    controller.show("done")
    controller.died("late")
    assert controller.state == "done"
    # Speech in the clip: the end finishes it into the transcription.
    controller.show("idle")
    controller.detector = controller.settings.gate.detector()
    quiet = dictate.frame_db(QUIET)
    loud = dictate.frame_db(LOUD)
    for _ in range(10):  # the floor calibrates on silence
        controller.detector.feed(QUIET, quiet)
    for _ in range(20):  # then speech arrives above it
        controller.detector.feed(LOUD, loud)
    assert controller.detector.said_something()
    controller.show("recording")
    controller.tick.start()
    controller.died("pw-record exited 0")
    spin(qt, 3000, until=lambda: controller.state == "transcribing")
    assert controller.state == "transcribing"
    controller.discard()


def test_shutdown_stops_a_live_capture(qt):
    """``shutdown`` with a child still running (#1214): the child
    ends and the stream clears, nothing shown."""
    controller, fake = dictation(qt, "q10 l*")
    controller.toggle()
    spin(qt, 600, until=lambda: controller.stream is not None)
    stream = controller.stream
    controller.shutdown()
    assert controller.stream is None
    assert stream.stopping is True
    assert controller.state == "recording"  # shown nothing: the pills go too
    controller.discard()


def test_discard_while_transcribing_drops_the_pending_answer(qt):
    """A stale answer after a discard (#1214): the generation moved,
    so the old transcript lands nowhere."""
    controller, fake = dictation(qt, "q10 l40 q*")
    record_speech(qt, controller, fake)
    stale = controller.generation
    controller.discard()
    fake.gate.set()
    spin(qt, 1500)
    controller.delivered(stale, "late", "")
    assert fake.copied == [] and controller.state == "idle"
    # An empty transcript fails with its own words.
    controller.delivered(controller.generation, "", "")
    assert controller.state == "failed" and controller.note == "no transcript"
    controller.discard()


def test_heard_ignores_bytes_with_no_detector_or_a_finishing_gate(qt):
    """``heard`` (#1214): no detector yet, or a gate that already
    stopped — the bytes feed nothing."""
    controller, _fake = dictation(qt)
    controller.heard(LOUD.tobytes())  # no detector
    controller.detector = controller.settings.gate.detector()
    controller.finishing = True
    controller.heard(LOUD.tobytes())
    assert controller.detector.elapsed == 0.0


def test_check_model_holds_while_busy(qt):
    """``check_model`` under a recording or a transcription
    (#1214) changes nothing."""
    controller, _fake = dictation(qt)
    controller.settings.model_file(controller.cache_root).unlink()
    controller.show("recording")
    controller.check_model()
    assert controller.state == "recording"
    controller.show("transcribing")
    controller.check_model()
    assert controller.state == "transcribing"


def test_copy_both_fills_the_selection_where_the_platform_keeps_one(
    qt, monkeypatch
):
    """The selection arm (#1210): a platform whose clipboard keeps
    a primary selection gets the transcript there too."""
    from PySide6 import QtGui

    calls: list = []
    monkeypatch.setattr(
        QtGui.QGuiApplication,
        "clipboard",
        staticmethod(
            lambda: SimpleNamespace(
                setText=lambda text, mode: calls.append((text, mode)),
                supportsSelection=lambda: True,
            )
        ),
    )
    from msks.client.gui.dictation import copy_both

    copy_both("hello")
    assert calls == [
        ("hello", QtGui.QClipboard.Mode.Clipboard),
        ("hello", QtGui.QClipboard.Mode.Selection),
    ]


def test_stop_on_a_dead_child_and_a_stubborn_one(qt, tmp_path, monkeypatch):
    """``CaptureStream.stop`` (#1214): a child already gone stops
    at once; one that ignores the terminate is killed after the
    wait."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    from msks.client.gui.dictation import CaptureStream

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    dead = CaptureStream(["msks-no-such-record", "-"])
    dead.start()
    end = time.monotonic() + 5
    while dead.proc.state() != dead.proc.ProcessState.NotRunning and (
        time.monotonic() < end
    ):
        app.processEvents()
        time.sleep(0.01)
    dead.stop()  # already not running: returns before the terminate
    assert dead.stopping

    # A child that traps SIGTERM: the kill ends it.
    import stat as stat_mod

    bin_dir = tmp_path / "stubborn"
    bin_dir.mkdir()
    script = bin_dir / "pw-record"
    script.write_text(
        f"#!{sys.executable}\n"
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    script.chmod(script.stat().st_mode | stat_mod.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    import msks.client.gui.dictation as mod

    monkeypatch.setattr(
        mod.QtCore.QProcess, "waitForFinished", lambda self, ms: False
    )
    stubborn = CaptureStream(dictate.capture_command(None))
    killed: list = []
    monkeypatch.setattr(
        mod.QtCore.QProcess, "kill", lambda self: killed.append(True)
    )
    stubborn.start()
    stubborn.stop()
    assert killed == [True]


# --- the review's pins (#1216) --------------------------------------------


def test_a_discard_between_the_cap_and_its_deferred_finish_cancels_it(qt):
    """The cap's finish lands one loop iteration after the gate's
    stop; a discard in that iteration owns the recording, and the
    deferred call ends nothing: no transcript, no clipboard."""
    controller, fake = dictation(qt, "q10 l*")
    controller.toggle()
    spin(qt, 2500, until=lambda: controller.detector.state == "speaking")
    # The cap's stop, deferred — then the discard before the timer.
    # The gate is fed the frame its own cap lands on.
    controller.detector.max_seconds = controller.detector.elapsed
    assert controller.capped_after(LOUD) is True
    assert controller.detector.capped()
    controller.defer_finish()
    assert controller.finishing is True
    controller.discard()
    assert controller.finishing is False
    controller.capped_finish()  # the timer's slot, after the discard
    assert controller.state == "idle"
    assert fake.transcribed == [] and fake.copied == []
    # The same for a shutdown.
    controller.toggle()
    spin(qt, 2500, until=lambda: controller.detector.state == "speaking")
    controller.detector.max_seconds = controller.detector.elapsed
    controller.capped_after(LOUD)
    controller.defer_finish()
    controller.shutdown()
    assert controller.finishing is False
    controller.capped_finish()
    assert fake.transcribed == []
    controller.discard()


def test_a_dying_capture_leaves_no_stream_behind(qt):
    """``died`` with nothing said (#1214): the failed recording
    holds no stream — the dead child is nobody's."""
    controller, fake = dictation(qt, "q*")
    controller.toggle()
    spin(qt, 600)
    assert controller.stream is not None
    controller.detector.timeout = 3600  # the gate's wait holds
    controller.stream.gone.emit("pw-record exited 2")
    spin(qt, 100)
    assert controller.state == "failed"
    assert controller.stream is None


def test_a_transcriber_that_raises_anything_fails_the_recording(qt):
    """The worker's catch (#1214): a transcriber raising anything
    at all fails the recording with the error named, and the clip
    directory is swept — the pill never stays in transcribing."""
    controller, fake = dictation(qt, "q10 l*")

    def explode(wav: Path, settings: DictateSettings) -> str:
        fake.transcribed.append(wav)  # the clip this recording wrote
        raise RuntimeError("whisper-cli hung up")

    controller.transcriber = explode
    controller.toggle()
    spin(qt, 2500, until=lambda: controller.detector.state == "speaking")
    controller.toggle()
    spin(qt, 3000, until=lambda: controller.state == "failed")
    assert controller.note == "RuntimeError: whisper-cli hung up"
    controller.discard()
    # The sweep follows the answer on the worker thread: this
    # recording's own directory goes, whatever else stands.
    work = fake.transcribed[0].parent
    spin(qt, 2000, until=lambda: not work.exists())
    assert not work.exists()

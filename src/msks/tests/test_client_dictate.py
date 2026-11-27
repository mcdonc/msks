"""The dictation leaf's suite (#1214, ported from the #1212
spike's smoke checks): the silence gate, the framer, the WAV
writer, the command builders, the whisper-cli call, the model
download, and the feedback tools. No microphone, no model, no
clipboard: every external tool is a child script on a private
PATH or a recorded call."""

import array
import io
import os
import stat
import sys
import time
import wave
from pathlib import Path

import pytest
from msks.client import dictate

QUIET = array.array("h", [0] * dictate.FRAME_SAMPLES)
LOUD = array.array("h", [8000] * dictate.FRAME_SAMPLES)


def detector(**overrides) -> dictate.Detector:
    """A detector over the spike's default gate."""
    return (
        dict(
            threshold=-60.0,
            margin=14.0,
            silence=1.0,
            timeout=5.0,
            max_seconds=60.0,
            preroll=0.5,
            pad=0.3,
        )
        | overrides
    )


def build(**overrides) -> dictate.Detector:
    return dictate.Detector(**detector(**overrides))


def tone(db: float) -> array.array:
    """A constant-amplitude frame whose RMS is the given dBFS."""
    return array.array(
        "h", [int(32768 * 10 ** (db / 20))] * dictate.FRAME_SAMPLES
    )


def feed_until_stop(det: dictate.Detector, frames: list) -> list:
    decisions = []
    for frame in frames:
        decisions.append(det.feed(frame, dictate.frame_db(frame)))
        if decisions[-1] == "stop":
            break
    return decisions


def tool(tmp_path: Path, name: str, body: str) -> Path:
    """A child script on PATH under ``name``, running ``body``
    through the test interpreter."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture()
def private_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private PATH head: a tool the suite writes there is the
    one the leaf finds, and one it does not write is missing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


# --- the gate ------------------------------------------------------------


def test_frame_db():
    assert dictate.frame_db(QUIET) == -99.0
    assert -13 < dictate.frame_db(LOUD) < -11
    assert dictate.frame_db(array.array("h")) == -99.0


def test_the_detector_carries_the_leaf_constants():
    """The gate's numbers are the leaf's own (#1214): the detector's
    defaults are the spike's findings, and the GUI builds one per
    recording from them."""
    det = dictate.Detector()
    assert det.threshold == dictate.DEFAULT_THRESHOLD
    assert det.margin == dictate.DEFAULT_MARGIN
    assert det.silence == dictate.PAUSE_SECONDS
    assert det.timeout == dictate.WAIT_SECONDS
    assert det.max_seconds == dictate.CAP_SECONDS
    assert det.preroll == dictate.PREROLL_SECONDS
    assert det.pad == dictate.PAD_SECONDS


def test_speech_then_silence():
    det = build()
    decisions = feed_until_stop(
        det, [QUIET] * 10 + [LOUD] * 40 + [QUIET] * 100
    )
    # 7 loud frames in the 10-frame window start speech at frame 17.
    assert decisions[:16] == ["waiting"] * 16, decisions[:17]
    assert decisions[16] == "speaking"
    # The tail holds 33 frames and tolerates three loud ones, so the
    # stop comes at frame 80 with the last loud frames still inside.
    assert decisions[-1] == "stop"
    assert len(decisions) == 80, len(decisions)
    assert abs(det.elapsed - 80 * dictate.FRAME_SECONDS) < 1e-9
    assert det.said_something()
    clip = det.frames()
    # preroll trimmed to 16 frames: 9 quiet + 7 loud, then 33 loud.
    assert len(clip) == det.last_voiced + 1 + 10 == 59, len(clip)
    loud = [i for i, frame in enumerate(clip) if dictate.frame_db(frame) > -40]
    assert loud == list(range(9, 49)), loud


def test_a_short_blip_is_not_speech():
    det = build()
    decisions = feed_until_stop(det, [QUIET] * 5 + [LOUD] * 3 + [QUIET] * 200)
    assert "speaking" not in decisions
    assert decisions[-1] == "stop"  # the timeout, at 5 s
    assert not det.said_something()
    assert det.frames() == []


def test_clicks_inside_the_quiet_window():
    det = build()
    clicks = [LOUD if i % 15 == 0 else QUIET for i in range(1, 100)]
    decisions = feed_until_stop(det, [QUIET] * 10 + [LOUD] * 40 + clicks)
    # Clicks at frames 65 and 80 hold the stop until frame 82.
    assert len(decisions) == 82, len(decisions)
    assert det.said_something()


def test_a_noisy_room_never_starts():
    # The speakerphone's floor in a quiet room: median -33 dBFS,
    # peaks to -19. A fixed threshold at -40 started at once and
    # never stopped; the tracked floor keeps the detector waiting.
    profile = [-43, -33, -33, -22, -33, -33, -19, -33, -28, -33]
    room = [tone(db) for db in profile]
    det = build()
    decisions = feed_until_stop(det, room * 30)
    assert "speaking" not in decisions
    assert decisions[-1] == "stop"  # the timeout, at 5 s
    assert len(decisions) == 167, len(decisions)
    assert -30 <= det.limit <= -27, det.limit


def test_the_timeout_and_the_cap():
    det = build(timeout=1.0)
    decisions = feed_until_stop(det, [QUIET] * 200)
    assert len(decisions) == 34, len(decisions)
    assert not det.said_something()
    capped = build(max_seconds=1.0, silence=10.0)
    decisions = feed_until_stop(capped, [QUIET] * 10 + [LOUD] * 200)
    assert len(decisions) == 34, len(decisions)
    assert capped.said_something()
    assert len(capped.frames()) == 33  # still speaking: the whole clip


def test_a_quiet_setting_shortens_the_stop_window():
    det = build(silence=0.2)
    assert det.stop_window == max(3, round(0.2 / dictate.FRAME_SECONDS))
    assert det.hangover == 1


# --- the framer and the WAV writer ---------------------------------------


def test_the_framer_keeps_a_partial_frame():
    framer = dictate.Framer()
    half = dictate.FRAME_BYTES // 2
    assert framer.feed(QUIET.tobytes()[:half]) == []
    frames = framer.feed(QUIET.tobytes()[half:] + LOUD.tobytes())
    assert len(frames) == 2
    assert frames[0] == QUIET and frames[1] == LOUD
    assert framer.pending == b""


def test_the_framer_drops_a_trailing_sliver_at_the_end():
    framer = dictate.Framer()
    framer.feed(LOUD.tobytes()[:2])  # one sample, not a frame
    # A frame and one byte short of the next: one frame out.
    assert framer.feed(LOUD.tobytes()[2:] + LOUD.tobytes()[:-1]) == [LOUD]
    assert len(framer.pending) == dictate.FRAME_BYTES - 1


def test_wav_roundtrip(tmp_path: Path):
    wav = tmp_path / "clip.wav"
    frames = [LOUD, QUIET, LOUD]
    samples = dictate.write_wav(wav, frames)
    assert samples == 3 * dictate.FRAME_SAMPLES
    with wave.open(str(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == dictate.RATE
        assert handle.getnframes() == samples
    assert dictate.read_frames(wav) == frames


def test_frames_from_drops_a_partial_frame():
    raw = LOUD.tobytes() + QUIET.tobytes()[: dictate.FRAME_BYTES - 2]
    assert dictate.frames_from(raw) == [LOUD]


# --- the model -----------------------------------------------------------


def test_model_path(tmp_path: Path):
    cache = tmp_path / "cache"
    assert dictate.model_path("small", cache) == (
        cache / "whisper" / "ggml-small.bin"
    )
    assert dictate.model_path(" small ", cache).name == "ggml-small.bin"
    assert dictate.model_path("", cache).name == "ggml-small.bin"
    pinned = tmp_path / "nix-store-whisper" / "ggml-small.bin"
    assert dictate.model_path(str(pinned), cache) == pinned
    assert (
        dictate.model_path("~/models/x.bin", cache)
        == Path("~/models/x.bin").expanduser()
    )


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, length: bool = True) -> None:
        super().__init__(body)
        headers = {"Content-Length": str(len(body))} if length else {}
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_download_model_streams_into_place(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    seen: list[tuple[int, int]] = []
    asked: list[bool] = []
    opened: list[str] = []

    def fake_urlopen(url: str, timeout: int):
        opened.append((url, timeout))
        return FakeResponse(b"x" * (dictate.DOWNLOAD_CHUNK + 7))

    monkeypatch.setattr(dictate.urllib.request, "urlopen", fake_urlopen)
    path = dictate.download_model(
        "small",
        cache,
        progress=lambda done, total: seen.append((done, total)),
        cancel=lambda: asked.append(True) or False,
    )
    assert opened == [(dictate.MODEL_URL.format(name="small"), 60)]
    assert path == cache / "whisper" / "ggml-small.bin"
    assert path.read_bytes() == b"x" * (dictate.DOWNLOAD_CHUNK + 7)
    assert not path.with_suffix(".part").exists()
    assert seen == [
        (dictate.DOWNLOAD_CHUNK, dictate.DOWNLOAD_CHUNK + 7),
        (dictate.DOWNLOAD_CHUNK + 7, dictate.DOWNLOAD_CHUNK + 7),
    ]
    assert asked  # the cancel hook was asked before every chunk
    # A present model downloads nothing.
    opened.clear()
    assert dictate.download_model("small", cache) == path
    assert opened == []


def test_download_model_cancels_and_fails(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        dictate.urllib.request,
        "urlopen",
        lambda url, timeout: FakeResponse(b"x" * 10),
    )
    with pytest.raises(dictate.DictateFailure, match="cancelled"):
        dictate.download_model("small", cache, cancel=lambda: True)
    assert not (cache / "whisper" / "ggml-small.bin").exists()
    assert not (cache / "whisper" / "ggml-small.part").exists()

    def broken(url: str, timeout: int):
        raise OSError("connection reset")

    monkeypatch.setattr(dictate.urllib.request, "urlopen", broken)
    with pytest.raises(dictate.DictateFailure, match="download failed"):
        dictate.download_model("small", cache)
    assert not (cache / "whisper" / "ggml-small.part").exists()


def test_download_model_refuses_a_path(tmp_path: Path):
    with pytest.raises(dictate.DictateFailure, match="not a path"):
        dictate.download_model("/nix/store/x-ggml-small.bin", tmp_path)


# --- the whisper call ----------------------------------------------------


def test_whisper_command_shape():
    base = dict(wav=Path("a.wav"), model=Path("m.bin"), lang="en", threads=8)
    cmd = dictate.whisper_command(prompt="msks", **base)
    assert cmd[0] == "whisper-cli" and "-nt" in cmd and "-np" in cmd
    assert cmd[cmd.index("--prompt") + 1] == "msks"
    assert "--prompt" not in dictate.whisper_command(prompt="", **base)


def test_transcribe_joins_the_segments(tmp_path: Path, private_bin: Path):
    tool(
        private_bin,
        "whisper-cli",
        "import sys\n"
        "args = sys.argv[1:]\n"
        "with open(args[args.index('-f') + 1]) as f:\n"
        "    assert f.read()\n"
        "assert args[args.index('--prompt') + 1] == 'msks'\n"
        "sys.stdout.write('Hello world.\\n\\nSecond line.\\n')\n"
        "sys.stderr.write('whisper_init: loading model\\n')\n",
    )
    wav = tmp_path / "clip.wav"
    wav.write_text("RIFF")
    text = dictate.transcribe(
        wav, tmp_path / "m.bin", prompt="msks", threads=2
    )
    assert text == "Hello world. Second line."


def test_transcribe_names_a_failing_run(tmp_path: Path, private_bin: Path):
    tool(private_bin, "whisper-cli", "import sys\nsys.exit(3)\n")
    wav = tmp_path / "clip.wav"
    with pytest.raises(dictate.DictateFailure, match="exited 3"):
        dictate.transcribe(wav, tmp_path / "m.bin")
    tool(
        private_bin,
        "whisper-cli",
        "import sys\nsys.stderr.write('x' * 500 + 'no model\\n')\n"
        "sys.exit(1)\n",
    )
    with pytest.raises(dictate.DictateFailure, match="no model") as excinfo:
        dictate.transcribe(wav, tmp_path / "m.bin")
    assert str(excinfo.value).endswith("no model")
    assert len(str(excinfo.value)) < 400  # the stderr's tail alone


def test_transcribe_names_the_missing_binary(tmp_path: Path):
    monkey = pytest.MonkeyPatch()
    monkey.setenv("PATH", str(tmp_path / "empty"))
    wav = tmp_path / "clip.wav"
    with pytest.raises(dictate.DictateFailure, match="not found"):
        dictate.transcribe(wav, tmp_path / "m.bin")


# --- capture, sources, feedback ------------------------------------------


def test_capture_command():
    cmd = dictate.capture_command(None)
    assert cmd[0] == "pw-record" and cmd[-1] == "-"
    assert "--raw" in cmd and str(dictate.RATE) in cmd
    assert cmd[cmd.index("--channels") + 1] == "1"
    assert "--target" not in cmd
    assert dictate.capture_command(" scarlett ") == cmd[:-1] + [
        "--target",
        "scarlett",
        "-",
    ]


def test_list_sources_reads_the_graph(private_bin: Path):
    graph = [
        {
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "node.name": "alsa_input.usb-scarlett.mic1",
                    "node.description": "Scarlett Mic1",
                }
            },
        },
        {
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "node.name": "alsa_output",
                }
            },
        },
        {
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source/Virtual",
                }
            },
        },
        {"type": "PipeWire:Interface:Link"},
    ]
    tool(
        private_bin,
        "pw-dump",
        f"import json, sys\nsys.stdout.write(json.dumps({graph!r}))\n",
    )
    assert dictate.list_sources() == [
        ("alsa_input.usb-scarlett.mic1", "Scarlett Mic1")
    ]


def test_list_sources_names_a_missing_or_broken_tool(
    tmp_path: Path, private_bin: Path
):
    monkey = pytest.MonkeyPatch()
    monkey.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(dictate.DictateFailure, match="pw-dump not found"):
        dictate.list_sources()
    monkey.undo()
    tool(private_bin, "pw-dump", "import sys\nsys.exit(2)\n")
    with pytest.raises(dictate.DictateFailure, match="exited 2"):
        dictate.list_sources()
    tool(private_bin, "pw-dump", "import sys\nsys.stdout.write('not json')\n")
    with pytest.raises(dictate.DictateFailure, match="no graph"):
        dictate.list_sources()


def test_the_tone_file_is_written_once(tmp_path: Path):
    path = dictate.tone_file(880, tmp_path)
    assert path == tmp_path / "beep-880.wav"
    assert dictate.tone_file(880, tmp_path) == path
    with wave.open(str(path), "rb") as handle:
        assert handle.getnframes() == dictate.RATE * dictate.TONE_MS // 1000
        assert handle.getframerate() == dictate.RATE
    samples = dictate.tone_samples(880)
    assert samples[0] == 0 and max(abs(s) for s in samples) <= 6000


def test_play_and_notify_use_the_tools_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dictate.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        dictate.subprocess,
        "Popen",
        lambda cmd, **kw: calls.append((cmd, kw)),
    )
    assert dictate.play(tmp_path / "beep.wav") is True
    assert dictate.notify("hello") is True
    assert [cmd for cmd, _kw in calls] == [
        ["/usr/bin/pw-cat", "--playback", str(tmp_path / "beep.wav")],
        ["/usr/bin/notify-send", "-t", "4000", "dictate", "hello"],
    ]
    assert all(
        kw["stdout"] == dictate.subprocess.DEVNULL for _cmd, kw in calls
    )
    monkeypatch.setattr(dictate.shutil, "which", lambda name: None)
    assert dictate.play(tmp_path / "beep.wav") is False
    assert dictate.notify("hello") is False
    assert len(calls) == 2


# --- the branches the first pass left dark -------------------------------


def test_speech_level_holds_the_threshold_until_the_floor_has_frames():
    det = build()
    assert det.speech_level() == det.threshold  # fewer than 5 frames
    for frame in [QUIET] * 5:
        det.feed(frame, dictate.frame_db(frame))
    assert det.speech_level() == det.threshold  # a silent floor


def test_capture_stream_reads_and_stops_under_qt(tmp_path: Path, private_bin):
    """The QProcess seam (#1214): the bytes read off the pipe
    whole, a stopping stream reading nothing more, the child's
    own end named with its stderr, and a failed start named."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    from msks.client.gui.dictation import CaptureStream

    tool(
        private_bin,
        "pw-record",
        "import sys, time\n"
        "sys.stdout.buffer.write(bytes(960))\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(5)\n",
    )
    seen: list[bytes] = []
    gone: list[str] = []
    stream = CaptureStream(dictate.capture_command(None))
    stream.frames.connect(seen.append)
    stream.gone.connect(gone.append)
    stream.start()
    end = time.monotonic() + 5
    while not seen and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert seen and len(seen[0]) == 960
    stream.stopping = True  # a stopping stream reads nothing more
    assert stream.pump() is None and len(seen) == 1
    stream.stopping = False
    stream.stop()
    assert stream.proc.state() == QtCore.QProcess.ProcessState.NotRunning
    assert gone == []  # a stop is the controller's, never "gone"

    # The child's own end: its stderr's tail names it.
    tool(
        private_bin,
        "pw-record",
        "import sys\nsys.stderr.write('x' * 400 + 'no device\\n')\n"
        "sys.exit(2)\n",
    )
    died = CaptureStream(dictate.capture_command(None))
    died.gone.connect(gone.append)
    died.start()
    end = time.monotonic() + 5
    while not gone and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert gone[0].endswith("no device") and len(gone[0]) == 300

    # A bare exit with no stderr names the code.
    tool(private_bin, "pw-record", "import sys\nsys.exit(3)\n")
    quiet = CaptureStream(dictate.capture_command(None))
    quiet.gone.connect(gone.append)
    quiet.start()
    end = time.monotonic() + 5
    while len(gone) < 2 and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert gone[1] == "pw-record exited 3"
    # A failed start names the binary.
    missing = CaptureStream(["msks-no-such-record", "-"])
    missing.gone.connect(gone.append)
    missing.start()
    end = time.monotonic() + 5
    while len(gone) < 3 and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert gone[2] == "msks-no-such-record not found"
    # Another process error while running — a crash — is the
    # child's own end's to name, not the start's: the slot on a
    # fresh, unreported stream names nothing for it.
    fresh = CaptureStream(dictate.capture_command(None))
    fresh.gone.connect(gone.append)
    fresh.refused(QtCore.QProcess.ProcessError.Crashed)
    assert len(gone) == 3


def test_download_model_with_no_progress_hook_and_an_empty_body(
    tmp_path: Path, monkeypatch
):
    """The stream's arcs (#1214): no progress hook, an empty body
    (the loop never runs), and a server naming no total."""
    monkeypatch.setattr(
        dictate.urllib.request,
        "urlopen",
        lambda url, timeout: FakeResponse(b"", length=False),
    )
    seen: list = []
    path = dictate.download_model(
        "small", tmp_path, progress=lambda done, total: seen.append(total)
    )
    assert path.exists() and path.stat().st_size == 0
    assert seen == []  # an empty body tells no progress


def test_download_model_returns_to_the_loop_after_each_chunk(
    tmp_path: Path, monkeypatch
):
    """The stream's loop (#1214): every chunk reports and the
    read comes back for the next until the body ends — two full
    chunks and then EOF."""
    monkeypatch.setattr(
        dictate.urllib.request,
        "urlopen",
        lambda url, timeout: FakeResponse(b"x" * (2 * dictate.DOWNLOAD_CHUNK)),
    )
    seen: list = []
    path = dictate.download_model(
        "small",
        tmp_path,
        progress=lambda done, total: seen.append(done),
        cancel=lambda: False,
    )
    assert path.stat().st_size == 2 * dictate.DOWNLOAD_CHUNK
    assert seen == [dictate.DOWNLOAD_CHUNK, 2 * dictate.DOWNLOAD_CHUNK]


def test_capped_tells_the_cap_from_the_quiet_stop():
    """``capped`` (#1214): the GUI ends a recording on the click
    alone and takes the cap as its one safety net, so the gate
    tells the two stops apart."""
    det = build(max_seconds=1.0, silence=10.0)
    decisions = [
        det.feed(f, dictate.frame_db(f)) for f in [QUIET] * 10 + [LOUD] * 12
    ]
    assert decisions[-1] == "speaking" and det.capped() is False
    decisions = feed_until_stop(det, [LOUD] * 40)
    assert decisions[-1] == "stop" and det.capped() is True
    quiet = build(timeout=0.3)
    feed_until_stop(quiet, [QUIET] * 200)
    assert quiet.state == "waiting" and quiet.capped() is False


def test_a_torn_download_never_installs(tmp_path: Path, monkeypatch):
    """A body shorter than its ``Content-Length`` (#1216 review):
    the download raises, the partial file goes, and nothing stands
    at the model's path — a torn download never reads as a present
    model. A server naming no total is not torn."""

    def torn(url: str, timeout: int):
        response = FakeResponse(b"x" * 10)
        response.headers = {"Content-Length": "1000"}
        return response

    monkeypatch.setattr(dictate.urllib.request, "urlopen", torn)
    with pytest.raises(dictate.DictateFailure, match="torn: 10 of 1000"):
        dictate.download_model("small", tmp_path)
    whisper = tmp_path / "whisper"
    assert sorted(p.name for p in whisper.iterdir()) == []
    monkeypatch.setattr(
        dictate.urllib.request,
        "urlopen",
        lambda url, timeout: FakeResponse(b"x" * 10, length=False),
    )
    assert dictate.download_model("small", tmp_path).read_bytes() == b"x" * 10


def test_a_model_name_that_makes_a_bad_url_fails_named(tmp_path: Path):
    """A model name with a space (#1216 review): the URL is
    malformed and ``urlopen`` raises an ``HTTPException``, which
    the download reports as a named failure — never a dead thread
    and a ``.part`` left behind."""
    with pytest.raises(dictate.DictateFailure, match="download failed"):
        dictate.download_model("my model", tmp_path)
    assert list((tmp_path / "whisper").iterdir()) == []


def test_a_hung_whisper_cli_is_named_and_failed(tmp_path: Path, private_bin):
    """A whisper-cli that never finishes (#1216 review): the call
    times out, the failure names the binary and the bound, and the
    pill never stays in transcribing."""
    tool(private_bin, "whisper-cli", "import time\ntime.sleep(30)\n")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(dictate, "TRANSCRIBE_TIMEOUT_S", 0.3)
    try:
        with pytest.raises(
            dictate.DictateFailure, match="did not finish in 0.3s"
        ):
            dictate.transcribe(tmp_path / "clip.wav", tmp_path / "m.bin")
    finally:
        monkey.undo()

#!/usr/bin/env python3
"""Offscreen checks for dictate.py.

Covers the detector, the WAV writer, the command builders, and the
record loop over a synthetic source (a child process that writes raw
frames to stdout, optionally at the pace of a microphone). No
microphone, no model, no clipboard; the pid file and the cache live in
a temporary directory for the duration of each check.

Usage: smoke.py [name ...]   (no names: run every check)
"""

from __future__ import annotations

import argparse
import array
import contextlib
import io
import os
import re
import signal
import sys
import tempfile
import threading
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dictate  # noqa: E402

QUIET = array.array("h", [0] * dictate.FRAME_SAMPLES)
LOUD = array.array("h", [8000] * dictate.FRAME_SAMPLES)

CHECKS = []


def check(func):
    CHECKS.append(func)
    return func


@contextlib.contextmanager
def sandbox():
    """A private runtime dir and cache; stderr captured as a string."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME")
        }
        os.environ["XDG_RUNTIME_DIR"] = tmp
        os.environ["XDG_CACHE_HOME"] = tmp
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                yield Path(tmp), err
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def detector(**overrides) -> dictate.Detector:
    options = dict(
        threshold=-60.0,
        margin=14.0,
        silence=1.0,
        timeout=5.0,
        max_seconds=60.0,
        preroll=0.5,
        pad=0.3,
    )
    options.update(overrides)
    return dictate.Detector(**options)


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


def synthetic_source(spec: str, paced: bool) -> list[str]:
    """A child that writes frames per spec as raw s16 to stdout.

    spec is like "q10 l40 q*" (a kind, a count or * for forever).
    paced sleeps one frame time between writes, like a microphone.
    """
    script = f"""
import sys, time
frames = {{
    "q": bytes({dictate.FRAME_BYTES}),
    "l": (8000).to_bytes(2, "little") * {dictate.FRAME_SAMPLES},
}}
try:
    for item in {spec!r}.split():
        kind, count = item[0], item[1:]
        n = 0
        while count == "*" or n < int(count):
            sys.stdout.buffer.write(frames[kind])
            sys.stdout.flush()
            n += 1
            if {paced!r}:
                time.sleep({dictate.FRAME_SECONDS!r})
except (KeyboardInterrupt, BrokenPipeError):
    pass  # dictate stops the source with SIGINT, or stops reading
"""
    return [sys.executable, "-c", script]


def record_args(*extra: str) -> argparse.Namespace:
    return dictate.build_parser().parse_args(
        ["record", "--dry-run", "--no-sound", *extra]
    )


# --- the detector -------------------------------------------------------


@check
def test_frame_db():
    assert dictate.frame_db(QUIET) == -99.0
    assert -13 < dictate.frame_db(LOUD) < -11
    assert dictate.frame_db(array.array("h")) == -99.0


@check
def test_speech_then_silence():
    det = detector()
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


@check
def test_short_blip_is_not_speech():
    det = detector()
    decisions = feed_until_stop(det, [QUIET] * 5 + [LOUD] * 3 + [QUIET] * 200)
    assert "speaking" not in decisions
    assert decisions[-1] == "stop"  # the timeout, at 5 s
    assert not det.said_something()
    assert det.frames() == []


@check
def test_clicks_inside_the_quiet_window():
    det = detector()
    clicks = [LOUD if i % 15 == 0 else QUIET for i in range(1, 100)]
    decisions = feed_until_stop(det, [QUIET] * 10 + [LOUD] * 40 + clicks)
    # Clicks at frames 65 and 80 hold the stop until frame 82.
    assert len(decisions) == 82, len(decisions)
    assert det.said_something()


@check
def test_noisy_room_never_starts():
    # The speakerphone's floor in a quiet room: median -33 dBFS,
    # peaks to -19. A fixed threshold at -40 started at once and never
    # stopped; the tracked floor keeps the detector waiting.
    profile = [-43, -33, -33, -22, -33, -33, -19, -33, -28, -33]
    room = [tone(db) for db in profile]
    det = detector()
    decisions = feed_until_stop(det, room * 30)
    assert "speaking" not in decisions
    assert decisions[-1] == "stop"  # the timeout, at 5 s
    assert len(decisions) == 167, len(decisions)
    assert -30 <= det.limit <= -27, det.limit


@check
def test_timeout():
    det = detector(timeout=1.0)
    decisions = feed_until_stop(det, [QUIET] * 200)
    assert len(decisions) == 34, len(decisions)
    assert not det.said_something()


@check
def test_max_seconds():
    det = detector(max_seconds=1.0, silence=10.0)
    decisions = feed_until_stop(det, [QUIET] * 10 + [LOUD] * 200)
    assert len(decisions) == 34, len(decisions)
    assert det.said_something()
    assert len(det.frames()) == 33  # still speaking: the whole clip


# --- files and commands -------------------------------------------------


@check
def test_wav_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "clip.wav"
        frames = [LOUD, QUIET, LOUD]
        samples = dictate.write_wav(wav, frames)
        assert samples == 3 * dictate.FRAME_SAMPLES
        with wave.open(str(wav), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == dictate.RATE
            assert handle.getnframes() == samples
        assert dictate.read_frames(wav) == frames


@check
def test_model_path():
    assert dictate.model_path("small").name == "ggml-small.bin"
    assert dictate.model_path("/tmp/ggml-small.bin") == Path(
        "/tmp/ggml-small.bin"
    )


@check
def test_commands():
    cmd = dictate.capture_command(None)
    assert cmd[0] == "pw-record" and cmd[-1] == "-"
    assert "--raw" in cmd and str(dictate.RATE) in cmd
    assert "--target" not in cmd
    assert dictate.capture_command("scarlett")[-3:-1] == [
        "--target",
        "scarlett",
    ]
    base = dict(wav=Path("a.wav"), model=Path("m.bin"), lang="en", threads=8)
    cmd = dictate.transcribe_command(prompt="msks", **base)
    assert cmd[0] == "whisper-cli" and "-nt" in cmd and "-np" in cmd
    assert cmd[cmd.index("--prompt") + 1] == "msks"
    assert "--prompt" not in dictate.transcribe_command(prompt="", **base)


@check
def test_meter_and_beep():
    assert dictate.meter(-99) == ".........."
    assert dictate.meter(-60) == ".........."
    assert dictate.meter(-20) == "##########"
    with sandbox() as (tmp, _err):
        path = dictate.beep_file(880)
        assert path.is_relative_to(tmp)
        with wave.open(str(path), "rb") as handle:
            assert handle.getnframes() == dictate.RATE * 60 // 1000
        assert dictate.beep_file(880) == path  # generated once


# --- the record loop over a synthetic source ----------------------------


@check
def test_record_dry_run():
    with sandbox() as (tmp, err):
        model = tmp / "ggml-fake.bin"
        model.write_bytes(b"fake")
        saved = dictate.capture_command
        dictate.capture_command = lambda target: synthetic_source(
            "q10 l40 q*", paced=False
        )
        try:
            code = dictate.record(
                record_args("--model", str(model)), dictate.Status(False)
            )
        finally:
            dictate.capture_command = saved
        text = err.getvalue()
        assert code == dictate.EXIT_OK, text
        assert "listening\n" in text and "speaking\n" in text, text
        assert not dictate.pidfile().exists()
        wav = Path(re.search(r"at (\S+clip\.wav)", text).group(1))
        frames = dictate.read_frames(wav)
        loud = [
            i
            for i, frame in enumerate(frames)
            if dictate.frame_db(frame) > -40
        ]
        assert loud == list(range(9, 49)), (len(frames), loud[:3], loud[-3:])


@check
def test_record_nothing_said():
    with sandbox() as (tmp, err):
        model = tmp / "ggml-fake.bin"
        model.write_bytes(b"fake")
        saved = dictate.capture_command
        dictate.capture_command = lambda target: synthetic_source(
            "q*", paced=False
        )
        try:
            code = dictate.record(
                record_args("--model", str(model), "--timeout", "0.5"),
                dictate.Status(False),
            )
        finally:
            dictate.capture_command = saved
        text = err.getvalue()
        assert code == dictate.EXIT_NOTHING_SAID, text
        assert "nothing said" in text, text
        assert not dictate.pidfile().exists()


@check
def test_record_finish_signal():
    with sandbox() as (tmp, err):
        model = tmp / "ggml-fake.bin"
        model.write_bytes(b"fake")
        saved = dictate.capture_command
        dictate.capture_command = lambda target: synthetic_source(
            "q10 l*", paced=True
        )
        timer = threading.Timer(
            1.5, os.kill, args=(os.getpid(), signal.SIGUSR1)
        )
        timer.start()
        try:
            code = dictate.record(
                record_args("--model", str(model), "--timeout", "10"),
                dictate.Status(False),
            )
        finally:
            timer.cancel()
            dictate.capture_command = saved
        text = err.getvalue()
        assert code == dictate.EXIT_OK, text
        assert "stopped after 1." in text, text
        assert re.search(r"kept [01]\.\ds", text), text


@check
def test_record_cancel_signal():
    with sandbox() as (tmp, err):
        model = tmp / "ggml-fake.bin"
        model.write_bytes(b"fake")
        saved = dictate.capture_command
        dictate.capture_command = lambda target: synthetic_source(
            "q10 l*", paced=True
        )
        timer = threading.Timer(
            1.0, os.kill, args=(os.getpid(), signal.SIGTERM)
        )
        timer.start()
        try:
            code = dictate.record(
                record_args("--model", str(model), "--timeout", "10"),
                dictate.Status(False),
            )
        finally:
            timer.cancel()
            dictate.capture_command = saved
        text = err.getvalue()
        assert code == dictate.EXIT_CANCELLED, text
        assert "cancelled" in text, text
        assert not dictate.pidfile().exists()


# --- the control commands -----------------------------------------------


@check
def test_signal_running_without_capture():
    with sandbox():
        assert (
            dictate.signal_running(signal.SIGUSR1, "finishing")
            == dictate.EXIT_ERROR
        )
        assert dict_from_main("stop") == dictate.EXIT_ERROR
        assert dict_from_main("cancel") == dictate.EXIT_ERROR


def dict_from_main(command: str) -> int:
    with contextlib.redirect_stderr(io.StringIO()):
        return dictate.main([command])


# --- runner -------------------------------------------------------------


def main(argv: list[str]) -> int:
    wanted = set(argv)
    failures = 0
    for func in CHECKS:
        if wanted and func.__name__ not in wanted:
            continue
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {func.__name__}: {exc}")
        except Exception as exc:  # the harness flattens every failure
            failures += 1
            print(f"ERROR {func.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {func.__name__}")
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

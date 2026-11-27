#!/usr/bin/env python3
"""dictate: a one-shot speech-to-clipboard spike.

Press a shortcut, talk, pause. About a second of silence ends the clip,
whisper-cli transcribes it, and the text goes to the clipboard and the
primary selection. Nothing is pasted: the user pastes. README.md sets
out the interaction under test.

Standard library only. External tools: pw-record (capture), whisper-cli
from whisper.cpp (transcription), wl-copy or xclip (clipboard), pw-cat
(feedback tones), notify-send (optional toast).
"""

from __future__ import annotations

import argparse
import array
import atexit
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

RATE = 16000
FRAME_MS = 30
FRAME_SECONDS = FRAME_MS / 1000
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

MIN_SPEECH = 0.2  # voiced seconds before the detector calls it speech
START_WINDOW = 10  # frames scanned for the start of speech (0.3 s)
FLOOR_SECONDS = 3.0  # the window that tracks the room's noise floor
FLOOR_PERCENT = 0.10  # its low percentile: speech rarely fills 90% of it

MODEL_URL = (
    "https://huggingface.co/ggml-org/whisper.cpp/resolve/main/ggml-{name}.bin"
)
MODEL_SIZES = {
    "tiny.en": "75 MB",
    "base.en": "142 MB",
    "small.en": "488 MB",
    "small": "466 MB",
    "large-v3-turbo": "1.5 GB",
    "large-v3": "2.9 GB",
}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING_SAID = 3
EXIT_CANCELLED = 130


class Failure(Exception):
    """A tool is missing or a step went wrong; the message is the answer."""


class Cancelled(Exception):
    """SIGTERM or Ctrl-C reached a capture in progress."""


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "dictate"


def pidfile() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "dictate.pid"


def model_path(name_or_path: str) -> Path:
    """A path stays a path; a name maps into the model cache."""
    if os.sep in name_or_path or name_or_path.endswith(".bin"):
        return Path(name_or_path)
    return cache_dir() / "models" / f"ggml-{name_or_path}.bin"


def frame_db(frame: array.array) -> float:
    """RMS level of one s16 frame in dBFS; -99 is digital silence."""
    if not frame:
        return -99.0
    total = 0
    for sample in frame:
        total += sample * sample
    rms = math.sqrt(total / len(frame))
    if rms < 1.0:
        return -99.0
    return 20.0 * math.log10(rms / 32768.0)


def meter(db: float) -> str:
    """Ten cells from -60 dBFS (empty) to -20 dBFS (full)."""
    cells = max(0, min(10, int((db + 60) / 4)))
    return "#" * cells + "." * (10 - cells)


@dataclass
class Detector:
    """A silence-gated clip collector.

    The states are waiting, speaking, and the "stop" decision. The
    preroll keeps the half second before speech so the first word
    survives; the pad keeps a little air after the last voiced frame.

    The level that counts as speech tracks the room: the low
    percentile of the last few seconds plus the margin, never below
    the threshold. A speakerphone's noise floor sits near -33 dBFS,
    so a fixed level would either never start or never stop. The stop
    tolerates a few loud frames (clicks) inside the quiet window.
    """

    threshold: float  # the lowest level that can count as speech, dBFS
    margin: float  # dB above the tracked noise floor
    silence: float
    timeout: float
    max_seconds: float
    preroll: float
    pad: float

    state: str = "waiting"
    elapsed: float = 0.0
    voiced_total: float = 0.0
    last_db: float = -99.0
    limit: float = -60.0  # the level that counts as speech right now
    levels: deque = field(default_factory=deque)
    history: deque = field(default_factory=deque)
    window: deque = field(default_factory=deque)
    tail: deque = field(default_factory=deque)
    clip: list = field(default_factory=list)
    last_voiced: int = -1

    def __post_init__(self) -> None:
        self.stop_window = max(3, round(self.silence / FRAME_SECONDS))
        self.hangover = max(1, round(0.1 * self.stop_window))

    def speech_level(self) -> float:
        """The tracked noise floor plus the margin, at least the threshold."""
        if len(self.levels) < 5:
            return self.threshold
        ranked = sorted(self.levels)
        floor = ranked[int(FLOOR_PERCENT * (len(ranked) - 1))]
        return max(self.threshold, floor + self.margin)

    def feed(self, frame: array.array, db: float) -> str:
        """Take one frame; return "waiting", "speaking", or "stop"."""
        self.elapsed += FRAME_SECONDS
        self.last_db = db
        # The first START_WINDOW frames calibrate the floor; nothing is
        # speech until they are in. Speak after the start tone.
        ready = len(self.levels) >= START_WINDOW
        self.limit = self.speech_level() if ready else self.threshold
        voiced = ready and db >= self.limit
        # The floor learns while waiting and during pauses; speech
        # frames leave it alone, or a long sentence would raise it.
        if self.state == "waiting" or not voiced:
            self.levels.append(db)
            while len(self.levels) * FRAME_SECONDS > FLOOR_SECONDS:
                self.levels.popleft()
        if self.state == "waiting":
            self.history.append(frame)
            while len(self.history) * FRAME_SECONDS > self.preroll:
                self.history.popleft()
            self.window.append(voiced)
            if len(self.window) > START_WINDOW:
                self.window.popleft()
            voiced_frames = sum(self.window)
            if voiced_frames * FRAME_SECONDS >= MIN_SPEECH:
                self.state = "speaking"
                self.clip = list(self.history)
                self.voiced_total = voiced_frames * FRAME_SECONDS
                self.last_voiced = len(self.clip) - 1
            elif self.elapsed >= self.timeout:
                return "stop"
            return self.state
        self.clip.append(frame)
        if voiced:
            self.voiced_total += FRAME_SECONDS
            self.last_voiced = len(self.clip) - 1
        self.tail.append(voiced)
        if len(self.tail) > self.stop_window:
            self.tail.popleft()
        loud_in_tail = sum(self.tail)
        settled = len(self.tail) == self.stop_window
        too_long = self.elapsed >= self.max_seconds
        if (settled and loud_in_tail <= self.hangover) or too_long:
            return "stop"
        return "speaking"

    def frames(self) -> list:
        """The clip up to the last voiced frame plus the pad."""
        if self.last_voiced < 0:
            return []
        keep = self.last_voiced + 1 + round(self.pad / FRAME_SECONDS)
        return self.clip[: min(keep, len(self.clip))]

    def said_something(self) -> bool:
        return self.voiced_total >= MIN_SPEECH


class Status:
    """Progress on stderr.

    On a terminal the live line is overwritten in place. Elsewhere
    (a shortcut launcher has no terminal) only whole lines appear.
    """

    def __init__(self, interactive: bool):
        self.interactive = interactive
        self.width = 0

    def show(self, text: str) -> None:
        if not self.interactive:
            return
        sys.stderr.write("\r" + " " * self.width + "\r" + text)
        sys.stderr.flush()
        self.width = len(text)

    def line(self, text: str) -> None:
        if self.interactive and self.width:
            sys.stderr.write("\r" + " " * self.width + "\r")
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
        self.width = 0


def capture_command(target: str | None) -> list[str]:
    """pw-record writing raw s16 mono at 16 kHz to stdout."""
    cmd = [
        "pw-record",
        "--raw",
        "--rate",
        str(RATE),
        "--channels",
        "1",
        "--format",
        "s16",
    ]
    if target:
        cmd += ["--target", target]
    cmd.append("-")
    return cmd


def transcribe_command(
    wav: Path, model: Path, lang: str, prompt: str, threads: int
) -> list[str]:
    """whisper-cli printing the text alone, one line per segment."""
    cmd = [
        "whisper-cli",
        "-m",
        str(model),
        "-f",
        str(wav),
        "-nt",
        "-np",
        "-l",
        lang,
        "-t",
        str(threads),
    ]
    if prompt:
        cmd += ["--prompt", prompt]
    return cmd


def write_wav(path: Path, frames: list) -> int:
    """Write the frames as a 16 kHz mono s16 WAV; return the samples."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        for frame in frames:
            wav.writeframes(frame.tobytes())
    return sum(len(frame) for frame in frames)


def read_frames(wav_path: Path) -> list:
    """The frames of a WAV dictate wrote (smoke.py reads them back)."""
    with wave.open(str(wav_path), "rb") as wav:
        raw = wav.readframes(wav.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return [
        samples[i : i + FRAME_SAMPLES]
        for i in range(0, len(samples), FRAME_SAMPLES)
    ]


def beep_file(freq: int) -> Path:
    """A 60 ms tone with 5 ms fades, generated once into the cache."""
    path = cache_dir() / f"beep-{freq}.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    count = RATE * 60 // 1000
    fade = RATE * 5 // 1000
    samples = array.array("h")
    for i in range(count):
        envelope = min(1.0, i / fade, (count - i) / fade)
        tone = math.sin(2 * math.pi * freq * i / RATE)
        samples.append(int(6000 * envelope * tone))
    write_wav(path, [samples])
    return path


def play(path: Path) -> None:
    """Play a WAV in the background when pw-cat is present."""
    pw_cat = shutil.which("pw-cat")
    if pw_cat is None:
        return
    subprocess.Popen(
        [pw_cat, "--playback", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def toast(text: str) -> None:
    notify = shutil.which("notify-send")
    if notify is None:
        return
    subprocess.Popen(
        [notify, "-t", "4000", "dictate", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def copy_to_clipboard(text: str) -> str:
    """Copy to the clipboard and, on Wayland, the primary selection.

    Returns the tool used, or "none" when no tool is present.
    """
    wl_copy = shutil.which("wl-copy")
    if wl_copy and os.environ.get("WAYLAND_DISPLAY"):
        subprocess.run([wl_copy], input=text, text=True, check=True)
        subprocess.run([wl_copy, "-p"], input=text, text=True, check=False)
        return "wl-copy"
    xclip = shutil.which("xclip")
    if xclip:
        subprocess.run(
            [xclip, "-selection", "clipboard", "-i"],
            input=text,
            text=True,
            check=True,
        )
        return "xclip"
    return "none"


def running_pid() -> int | None:
    """The pid in the pid file when that process is alive."""
    path = pidfile()
    try:
        pid = int(path.read_text().strip())
    except OSError, ValueError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid
    return pid


def claim_pidfile() -> None:
    pidfile().write_text(f"{os.getpid()}\n")
    atexit.register(release_pidfile)


def release_pidfile() -> None:
    path = pidfile()
    try:
        pid = int(path.read_text().strip())
    except OSError, ValueError:
        return
    if pid == os.getpid():
        path.unlink(missing_ok=True)


def signal_running(sig: int, verb: str) -> int:
    pid = running_pid()
    if pid is None:
        print("dictate: no capture in progress", file=sys.stderr)
        return EXIT_ERROR
    try:
        os.kill(pid, sig)
    except PermissionError:
        print(
            f"dictate: capture {pid} belongs to another user", file=sys.stderr
        )
        return EXIT_ERROR
    print(f"dictate: {verb} capture {pid}", file=sys.stderr)
    return EXIT_OK


def stop_source(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def capture(
    args: argparse.Namespace,
    detector: Detector,
    status: Status,
    finish: dict,
) -> list:
    """Run the source, feed the detector, and return the clip frames."""
    cmd = capture_command(args.target)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    except FileNotFoundError:
        raise Failure(f"{cmd[0]} not found") from None
    status.line(f"capturing with {cmd[0]}")
    source = proc.stdout
    assert source is not None
    announced = detector.state
    try:
        while True:
            chunk = source.read(FRAME_BYTES)
            if len(chunk) < FRAME_BYTES:
                raise Failure(
                    f"{cmd[0]} stopped after {detector.elapsed:.1f}s"
                )
            frame = array.array("h")
            frame.frombytes(chunk)
            db = frame_db(frame)
            decision = detector.feed(frame, db)
            if detector.state != announced:
                announced = detector.state
                status.line(announced)
            live = f"[{detector.elapsed:5.1f}s {detector.state:7}]"
            status.show(
                f"{live} {meter(db)} {db:4.0f} dB  limit {detector.limit:3.0f}"
            )
            if decision == "stop" or finish["now"]:
                return detector.frames()
    finally:
        stop_source(proc)


def transcribe(wav: Path, args: argparse.Namespace, status: Status) -> str:
    cmd = transcribe_command(
        wav, model_path(args.model), args.lang, args.prompt, args.threads
    )
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise Failure(f"{cmd[0]} not found") from None
    if done.returncode != 0:
        detail = done.stderr.strip()[-300:]
        raise Failure(f"{cmd[0]} exited {done.returncode}: {detail}")
    return " ".join(done.stdout.split())


def download_model(name: str, force: bool, status: Status) -> int:
    path = model_path(name)
    if path.exists() and not force:
        size = path.stat().st_size >> 20
        status.line(f"{path} already present ({size} MB)")
        return EXIT_OK
    if path != cache_dir() / "models" / f"ggml-{name}.bin":
        status.line(
            f"dictate: give a model name to download, not a path: {name}"
        )
        return EXIT_ERROR
    url = MODEL_URL.format(name=name)
    status.line(
        f"downloading {name} ({MODEL_SIZES.get(name, 'size unknown')})"
    )
    status.line(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_percent = -1
            with part.open("wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        percent = done * 100 // total
                        if percent != last_percent:
                            last_percent = percent
                            status.show(f"{percent:3d}% of {total >> 20} MB")
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise Failure(f"download failed: {exc}") from exc
    part.replace(path)
    size = path.stat().st_size >> 20
    status.line(f"saved {path} ({size} MB)")
    return EXIT_OK


def record(args: argparse.Namespace, status: Status) -> int:
    model = model_path(args.model)
    if not args.dry_run and not model.exists():
        status.line(f"model {model} is missing")
        status.line(f"fetch it with: dictate model {args.model}")
        return EXIT_ERROR
    other = running_pid()
    if other is not None:
        status.line(f"already recording as pid {other}; use stop or cancel")
        return EXIT_ERROR
    claim_pidfile()
    try:
        return capture_and_deliver(args, status)
    finally:
        release_pidfile()


def capture_and_deliver(args: argparse.Namespace, status: Status) -> int:
    finish = {"now": False}

    def finish_now(signum, frame):  # noqa: ANN001
        finish["now"] = True

    def cancel_now(signum, frame):  # noqa: ANN001
        raise Cancelled

    signal.signal(signal.SIGUSR1, finish_now)
    signal.signal(signal.SIGTERM, cancel_now)
    if not args.no_sound:
        play(beep_file(880))
    toast("listening")
    detector = Detector(
        threshold=args.threshold,
        margin=args.margin,
        silence=args.silence,
        timeout=args.timeout,
        max_seconds=args.max_seconds,
        preroll=args.preroll,
        pad=args.pad,
    )
    status.line("listening")
    started = time.monotonic()
    try:
        frames = capture(args, detector, status, finish)
    except Cancelled:
        status.line("cancelled")
        return EXIT_CANCELLED
    stopped = time.monotonic() - started
    status.line(f"stopped after {stopped:.1f}s")
    if not detector.said_something():
        status.line("nothing said")
        return EXIT_NOTHING_SAID
    work = Path(tempfile.mkdtemp(prefix="dictate-"))
    wav = work / "clip.wav"
    seconds = write_wav(wav, frames) / RATE
    if args.dry_run:
        status.line(f"dry run: kept {seconds:.1f}s at {wav}")
        return EXIT_OK
    status.line(f"transcribing {seconds:.1f}s with {args.model}")
    started = time.monotonic()
    try:
        text = transcribe(wav, args, status)
    except Failure:
        status.line(f"clip kept at {wav}")
        raise
    status.line(f"transcribed in {time.monotonic() - started:.1f}s")
    if not text:
        status.line(f"no transcript; clip kept at {wav}")
        return EXIT_NOTHING_SAID
    print(text)
    if args.keep:
        status.line(f"clip kept at {wav}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    tool = copy_to_clipboard(text)
    if tool == "none":
        status.line("no clipboard tool (wl-copy or xclip); text printed above")
        return EXIT_ERROR
    status.line("copied to the clipboard; paste it where you want it")
    if not args.no_sound:
        play(beep_file(1320))
    toast(text[:120])
    return EXIT_OK


def add_capture_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default="small",
        help="a ggml model name in the cache, or a path to a .bin "
        f"({' | '.join(MODEL_SIZES)}; default: small)",
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="spoken language for whisper-cli, or auto (default: en)",
    )
    parser.add_argument(
        "--prompt",
        default="",
        help="vocabulary hint, e.g. 'msks, devenv, NixOS, pi'",
    )
    parser.add_argument(
        "--silence",
        type=float,
        default=1.0,
        help="seconds of quiet that end a clip; a few loud frames are "
        "tolerated inside it (default: 1.0)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=-60.0,
        help="the lowest dBFS level that can count as speech (default: -60)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=14.0,
        help="dB above the tracked noise floor that counts as speech "
        "(default: 14)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds to wait for speech before giving up (default: 5)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=180.0,
        help="hard cap on one clip (default: 180)",
    )
    parser.add_argument(
        "--preroll",
        type=float,
        default=0.5,
        help="seconds kept before speech starts (default: 0.5)",
    )
    parser.add_argument(
        "--pad",
        type=float,
        default=0.3,
        help="seconds kept after the last voiced frame (default: 0.3)",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="pw-record source node name or serial (default: the default "
        "source; see pw-record --list-targets)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=os.cpu_count() or 4,
        help="whisper-cli threads (default: the cpu count)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="capture and stop, keep the clip, skip whisper and the clipboard",
    )
    parser.add_argument(
        "--no-sound", action="store_true", help="skip the start and done tones"
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the clip wav after transcription",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dictate",
        description=__doc__.split("\n\n")[0],
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    for name, text in (
        ("record", "capture until silence, copy the transcript (default)"),
        ("toggle", "finish a running capture, else start one"),
    ):
        sub.add_parser(name, help=text)
        add_capture_options(sub.choices[name])
    sub.add_parser("stop", help="finish the running capture now")
    sub.add_parser("cancel", help="discard the running capture")
    model = sub.add_parser("model", help="download a ggml whisper model")
    model.add_argument(
        "name",
        nargs="?",
        default="small",
        help=f"one of {' | '.join(MODEL_SIZES)} (default: small)",
    )
    model.add_argument(
        "--force", action="store_true", help="download again over an old copy"
    )
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    argv = list(argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "record")
    args = parser.parse_args(argv)
    status = Status(interactive=sys.stderr.isatty())
    try:
        if args.command == "record":
            return record(args, status)
        if args.command == "toggle":
            if running_pid() is None:
                return record(args, status)
            return signal_running(signal.SIGUSR1, "finishing")
        if args.command == "stop":
            return signal_running(signal.SIGUSR1, "finishing")
        if args.command == "cancel":
            return signal_running(signal.SIGTERM, "cancelling")
        if args.command == "model":
            return download_model(args.name, args.force, status)
    except Failure as exc:
        status.line(f"dictate: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        status.line("cancelled")
        return EXIT_CANCELLED
    parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

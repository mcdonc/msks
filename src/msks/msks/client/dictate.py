"""One-shot dictation's leaf (#1210, #1214): the silence gate,
the WAV writer, the whisper-cli call, the model download, and the
feedback tools — standard library alone, so the GUI's dictate pill
imports a tested leaf and a future ``msks dictate`` command shares
it.

Press, talk, pause: about a second of quiet ends the clip,
whisper-cli transcribes it once per clip (the ggml model loads
each time, about a second with ``small``), and the transcript
goes to the clipboard and the primary selection. The user pastes.

Capture happens on the host because a workspace guest has no
audio device. The stream itself belongs to the caller — the GUI
runs :func:`capture_command` through a QProcess — and the leaf
takes frames: 30 ms of s16 mono at 16 kHz.

The level that counts as speech tracks the room: the 10th
percentile of the last three seconds plus the margin, never below
the threshold. A speakerphone's noise floor sits near -33 dBFS
median in a quiet room, so a fixed level would either never start
or never stop. The first 0.3 s calibrates the floor; speak after
the start tone. The stop tolerates a few loud frames (clicks)
inside the quiet window.

External tools, each looked up at call time and each a named
:class:`DictateFailure` when missing: ``pw-record`` (capture),
``pw-cat`` (feedback tones), ``pw-dump`` (the source list),
whisper.cpp's ``whisper-cli`` (transcription), ``notify-send``
(the desktop notification).
"""

from __future__ import annotations

import array
import http.client
import json
import math
import os
import shutil
import subprocess
import urllib.request
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: The sample rate whisper-cli takes (#1210): 16 kHz mono s16,
#: the one shape the model was trained on.
RATE = 16000
FRAME_MS = 30
FRAME_SECONDS = FRAME_MS / 1000
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

#: Voiced seconds before the gate calls it speech (#1210).
MIN_SPEECH = 0.2
#: Frames scanned for the start of speech (0.3 s): nothing is
#: speech until they are in, which is also the floor's calibration.
START_WINDOW = 10
#: The window that tracks the room's noise floor, and its low
#: percentile: speech rarely fills 90% of it.
FLOOR_SECONDS = 3.0
FLOOR_PERCENT = 0.10

#: The gate's defaults (#1210's findings): the lowest level that
#: can count as speech, the margin over the tracked floor, and the
#: quiet that ends a clip. The three are the operator's settings.
DEFAULT_THRESHOLD = -60.0
DEFAULT_MARGIN = 14.0
DEFAULT_SILENCE = 1.0
#: Seconds to wait for speech before the clip ends with nothing
#: said; the hard cap on one clip; the air kept before speech
#: starts and after the last voiced frame.
WAIT_SECONDS = 5.0
CAP_SECONDS = 180.0
PREROLL_SECONDS = 0.5
PAD_SECONDS = 0.3

#: The model setting's default: a ggml name, downloaded from the
#: settings page.
DEFAULT_MODEL = "small"
DEFAULT_LANG = "en"
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
)
MODEL_SIZES = {
    "tiny.en": "75 MB",
    "base.en": "142 MB",
    "small.en": "488 MB",
    "small": "466 MB",
    "large-v3-turbo": "1.5 GB",
    "large-v3": "2.9 GB",
}

#: The model binaries' home under the client cache root (#1214):
#: ``$XDG_CACHE_HOME/msks/whisper`` on a default host. A NixOS
#: host can pin a model as a derivation and point the model
#: setting at the store path instead.
WHISPER_DIR = "whisper"

WHISPER_CLI = "whisper-cli"
PW_RECORD = "pw-record"
PW_CAT = "pw-cat"
PW_DUMP = "pw-dump"
NOTIFY_SEND = "notify-send"

#: The start and done tones (#1210): 60 ms each, with 5 ms fades.
START_TONE_HZ = 880
DONE_TONE_HZ = 1320
TONE_MS = 60
TONE_FADE_MS = 5

DOWNLOAD_CHUNK = 1 << 20


class DictateFailure(Exception):
    """A tool is missing or a step went wrong; the message is the
    answer the pill and the settings page show."""


def frame_db(frame: array.array) -> float:
    """RMS level of one s16 frame in dBFS; -99 is digital
    silence."""
    if not frame:
        return -99.0
    total = 0
    for sample in frame:
        total += sample * sample
    rms = math.sqrt(total / len(frame))
    if rms < 1.0:
        return -99.0
    return 20.0 * math.log10(rms / 32768.0)


@dataclass(frozen=True)
class Gate:
    """The gate's three numbers (#1214), the settings page's own:
    the lowest level that can count as speech, the dB over the
    tracked floor that counts, and the seconds of quiet that end
    a clip. :meth:`detector` builds one collector per
    recording."""

    threshold: float = DEFAULT_THRESHOLD
    margin: float = DEFAULT_MARGIN
    silence: float = DEFAULT_SILENCE

    def detector(self) -> Detector:
        return Detector(
            threshold=self.threshold,
            margin=self.margin,
            silence=self.silence,
            timeout=WAIT_SECONDS,
            max_seconds=CAP_SECONDS,
            preroll=PREROLL_SECONDS,
            pad=PAD_SECONDS,
        )


@dataclass
class Detector:
    """A silence-gated clip collector (#1210).

    The states are waiting, speaking, and the "stop" decision.
    The preroll keeps the half second before speech so the first
    word survives; the pad keeps a little air after the last
    voiced frame. The level that counts as speech tracks the
    room through :meth:`speech_level`; the stop tolerates a few
    loud frames inside the quiet window.
    """

    threshold: float
    margin: float
    silence: float
    timeout: float
    max_seconds: float
    preroll: float
    pad: float

    state: str = "waiting"
    elapsed: float = 0.0
    voiced_total: float = 0.0
    last_db: float = -99.0
    #: The level that counts as speech right now.
    limit: float = -60.0
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
        """The tracked noise floor plus the margin, at least the
        threshold."""
        if len(self.levels) < 5:
            return self.threshold
        ranked = sorted(self.levels)
        floor = ranked[int(FLOOR_PERCENT * (len(ranked) - 1))]
        return max(self.threshold, floor + self.margin)

    def learn_floor(self, db: float, voiced: bool) -> None:
        """The floor learns while waiting and during pauses; speech
        frames leave it alone, or a long sentence would raise it."""
        if self.state == "waiting" or not voiced:
            self.levels.append(db)
            while len(self.levels) * FRAME_SECONDS > FLOOR_SECONDS:
                self.levels.popleft()

    def voiced_now(self, db: float) -> bool:
        """Whether this frame counts as speech: the first
        START_WINDOW frames calibrate the floor, and nothing is
        speech until they are in — speak after the start tone."""
        ready = len(self.levels) >= START_WINDOW
        self.limit = self.speech_level() if ready else self.threshold
        return ready and db >= self.limit

    def keep_preroll(self, frame: array.array) -> None:
        """The half second before speech (#1210), so the first
        word survives: the history holds the preroll's frames."""
        self.history.append(frame)
        while len(self.history) * FRAME_SECONDS > self.preroll:
            self.history.popleft()

    def wait(self, frame: array.array, voiced: bool) -> str:
        """The waiting state (#1210): speech starts once the start
        window holds MIN_SPEECH of voiced frames — the preroll
        becomes the clip — and the wait's timeout stops with
        nothing said."""
        self.keep_preroll(frame)
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

    def speak(self, frame: array.array, voiced: bool) -> str:
        """The speaking state (#1210): the clip grows, the tail of
        the last quiet window tracks the tolerated loud frames,
        and the stop comes once the window settles quiet or the
        cap is reached."""
        self.clip.append(frame)
        if voiced:
            self.voiced_total += FRAME_SECONDS
            self.last_voiced = len(self.clip) - 1
        self.tail.append(voiced)
        if len(self.tail) > self.stop_window:
            self.tail.popleft()
        if self.quiet_settled() or self.elapsed >= self.max_seconds:
            return "stop"
        return "speaking"

    def quiet_settled(self) -> bool:
        """Whether the quiet window has settled (#1210): the tail
        holds a whole window and at most the tolerated loud
        frames — a few clicks do not hold the clip open."""
        settled = len(self.tail) == self.stop_window
        return settled and sum(self.tail) <= self.hangover

    def feed(self, frame: array.array, db: float) -> str:
        """Take one frame; return "waiting", "speaking", or
        "stop"."""
        self.elapsed += FRAME_SECONDS
        self.last_db = db
        voiced = self.voiced_now(db)
        self.learn_floor(db, voiced)
        if self.state == "waiting":
            return self.wait(frame, voiced)
        return self.speak(frame, voiced)

    def frames(self) -> list:
        """The clip up to the last voiced frame plus the pad."""
        if self.last_voiced < 0:
            return []
        keep = self.last_voiced + 1 + round(self.pad / FRAME_SECONDS)
        return self.clip[: min(keep, len(self.clip))]

    def said_something(self) -> bool:
        return self.voiced_total >= MIN_SPEECH

    def capped(self) -> bool:
        """Whether the last decision was the cap (#1214): the clip
        ran to ``max_seconds``. The GUI ends a recording on the
        operator's click alone and treats the cap as the safety
        net, so it tells the two stops apart."""
        return self.elapsed >= self.max_seconds


def frames_from(raw: bytes) -> list:
    """Whole 30 ms frames out of a raw s16 byte stream (#1214):
    the GUI's process stdout arrives in chunks of any size, and
    the detector takes whole frames. A partial frame at the end
    belongs to the next read; :class:`Framer` keeps it."""
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    return [
        samples[i : i + FRAME_SAMPLES]
        for i in range(0, len(samples) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
    ]


class Framer:
    """Whole frames out of a byte stream (#1214): the bytes the
    last read left short of a frame wait here for the next."""

    def __init__(self) -> None:
        self.pending = b""

    def feed(self, raw: bytes) -> list:
        """The whole frames in ``pending + raw``; a remainder
        under one frame stays pending."""
        buffer = self.pending + raw
        whole = len(buffer) - len(buffer) % FRAME_BYTES
        self.pending = buffer[whole:]
        return frames_from(buffer[:whole])


def write_wav(path: Path, frames: list) -> int:
    """Write the frames as a 16 kHz mono s16 WAV; return the
    samples."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        for frame in frames:
            wav.writeframes(frame.tobytes())
    return sum(len(frame) for frame in frames)


def read_frames(wav_path: Path) -> list:
    """The frames of a WAV :func:`write_wav` wrote (the suite
    reads them back)."""
    with wave.open(str(wav_path), "rb") as wav:
        raw = wav.readframes(wav.getnframes())
    return frames_from(raw)


def model_path(setting: str, cache_root: Path) -> Path:
    """Where the model setting points (#1214): a path stays a
    path — a NixOS store path a derivation pinned, or any
    ``.bin`` on disk — and a name maps into the cache's whisper
    directory as ``ggml-<name>.bin``."""
    setting = setting.strip()
    if not setting:
        setting = DEFAULT_MODEL
    if os.sep in setting or setting.endswith(".bin"):
        return Path(setting).expanduser()
    return cache_root / WHISPER_DIR / f"ggml-{setting}.bin"


def stream_model(response, part: Path, progress, cancel) -> None:
    """The response's body into ``part`` (#1214): 1 MiB chunks,
    the progress told per chunk (the total 0 when the server
    names none), the cancel asked before each. A body that ends
    short of the length the server named is a torn download: it
    raises, and the caller takes the partial file with it."""
    total = int(response.headers.get("Content-Length") or 0)
    report = progress or (lambda done, total: None)
    done = 0
    with part.open("wb") as out:
        while chunk := response.read(DOWNLOAD_CHUNK):
            check_cancel(cancel)
            out.write(chunk)
            done += len(chunk)
            report(done, total)
    check_whole(done, total)


def check_whole(done: int, total: int) -> None:
    """The body's length against the one the server named (#1214):
    a body that ended short is a torn download, refused by name —
    a server that named no total is not checked."""
    if total and done != total:
        raise DictateFailure(f"download torn: {done} of {total} bytes")


def check_cancel(cancel) -> None:
    """The cancel's question before each chunk (#1214): a set
    flag raises, and the caller takes the partial file with it."""
    if cancel is not None and cancel():
        raise DictateFailure("download cancelled")


def pull_model(url: str, part: Path, progress, cancel) -> None:
    """The download (#1214): the stream into ``part`` under the
    ``.part`` name; any failure — the cancel, a transport error —
    takes the partial file with it and names the cause."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            stream_model(response, part, progress, cancel)
    except DictateFailure:
        part.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # URLError and the socket errors are OSError; a model name
        # with a space raises InvalidURL, an HTTPException; a bad
        # header a ValueError.
        part.unlink(missing_ok=True)
        raise DictateFailure(f"download failed: {exc}") from exc


def download_model(
    name: str,
    cache_root: Path,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Fetch the named ggml model into the cache (#1214), one
    1 MiB chunk at a time under a ``.part`` name that the
    finished download renames into place — a torn download never
    reads as a present model. ``progress(done, total)`` hears
    each chunk (the total is 0 when the server names none);
    ``cancel`` answering True abandons the download and raises.
    A model already present downloads nothing. A path where a
    name belongs is refused: the operator places those."""
    path = model_path(name, cache_root)
    if path.exists():
        return path
    if path != cache_root / WHISPER_DIR / f"ggml-{name}.bin":
        raise DictateFailure(
            f"give a model name to download, not a path: {name}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    pull_model(MODEL_URL.format(name=name), part, progress, cancel)
    part.replace(path)
    return path


def whisper_command(
    wav: Path, model: Path, lang: str, prompt: str, threads: int
) -> list[str]:
    """whisper-cli printing the text alone, one line per segment,
    with the prompt as the vocabulary hint when one is set."""
    cmd = [
        WHISPER_CLI,
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


def transcribe(
    wav: Path,
    model: Path,
    lang: str = DEFAULT_LANG,
    prompt: str = "",
    threads: int | None = None,
) -> str:
    """The clip's text through whisper-cli (#1210): the segments
    joined on one line, or a named failure — the binary missing,
    or a run that exited nonzero with its stderr's tail."""
    cmd = whisper_command(
        wav, model, lang, prompt, threads or os.cpu_count() or 4
    )
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise DictateFailure(f"{WHISPER_CLI} not found") from None
    if done.returncode != 0:
        detail = done.stderr.strip()[-300:]
        raise DictateFailure(
            f"{WHISPER_CLI} exited {done.returncode}: {detail}"
        )
    return " ".join(done.stdout.split())


def capture_command(target: str | None) -> list[str]:
    """pw-record writing raw s16 mono at 16 kHz to stdout, from
    the source ``target`` names (a node serial or name) or the
    default source."""
    cmd = [
        PW_RECORD,
        "--raw",
        "--rate",
        str(RATE),
        "--channels",
        "1",
        "--format",
        "s16",
    ]
    if target:
        cmd += ["--target", target.strip()]
    cmd.append("-")
    return cmd


def dump_graph() -> list:
    """PipeWire's graph (#1214) off ``pw-dump``: the parsed
    objects, or a named failure — the tool missing, a run that
    exited nonzero, a graph that is not JSON."""
    try:
        done = subprocess.run(
            [PW_DUMP], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise DictateFailure(f"{PW_DUMP} not found") from None
    if done.returncode != 0:
        raise DictateFailure(f"{PW_DUMP} exited {done.returncode}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise DictateFailure(f"{PW_DUMP} printed no graph") from None


def is_source_node(obj) -> bool:
    """Whether one graph object is an audio source node (#1214):
    PipeWire's ``Audio/Source`` class — the virtual sources'
    subclass included — on a node."""
    props = (obj.get("info") or {}).get("props") or {}
    media_class = str(props.get("media.class", ""))
    return obj.get(
        "type"
    ) == "PipeWire:Interface:Node" and media_class.startswith("Audio/Source")


def source_node(obj) -> tuple[str, str] | None:
    """One graph object as a source (#1214): the ``(node name,
    description)`` pair for an ``Audio/Source`` node carrying a
    name, None for every other object."""
    if not is_source_node(obj):
        return None
    props = obj["info"]["props"]
    name = str(props.get("node.name", ""))
    if not name:
        return None
    return name, str(props.get("node.description", name))


def list_sources() -> list[tuple[str, str]]:
    """The audio sources PipeWire serves (#1214), as ``(node
    name, description)`` pairs — the names are what
    :func:`capture_command` targets, the descriptions what the
    settings page shows. Read off ``pw-dump``'s graph; a missing
    tool or an unreadable graph is a named failure the page
    notes."""
    pairs = (source_node(obj) for obj in dump_graph())
    return [pair for pair in pairs if pair is not None]


def tone_samples(freq: int) -> array.array:
    """A 60 ms tone with 5 ms fades as s16 samples."""
    count = RATE * TONE_MS // 1000
    fade = RATE * TONE_FADE_MS // 1000
    samples = array.array("h")
    for i in range(count):
        envelope = min(1.0, i / fade, (count - i) / fade)
        tone = math.sin(2 * math.pi * freq * i / RATE)
        samples.append(int(6000 * envelope * tone))
    return samples


def tone_file(freq: int, cache_root: Path) -> Path:
    """The tone's WAV, generated once into the cache."""
    path = cache_root / f"beep-{freq}.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    write_wav(path, [tone_samples(freq)])
    return path


def play(path: Path) -> bool:
    """Play a WAV in the background when pw-cat is present; False
    when the host has none, and the tones stay silent."""
    pw_cat = shutil.which(PW_CAT)
    if pw_cat is None:
        return False
    subprocess.Popen(
        [pw_cat, "--playback", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def notify(text: str) -> bool:
    """A desktop notification when notify-send is present; False
    when the host has none."""
    send = shutil.which(NOTIFY_SEND)
    if send is None:
        return False
    subprocess.Popen(
        [send, "-t", "4000", "dictate", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True

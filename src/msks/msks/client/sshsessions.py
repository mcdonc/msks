"""The client's per-user session records (#858).

Every interactive ``msks ssh`` session writes one small record
under the per-user msks state directory — ``MSKSC_STATE_DIR``
when set, else ``XDG_STATE_HOME/msks`` (``~/.local/state/msks``
by default) — one file per session, keyed by the workspace's
immutable id (the one alias every ssh invocation pins under,
#697) and the session process's pid. The session heartbeats its
record (:data:`HEARTBEAT_S`) for its life and unlinks it at exit;
a session that died without cleanup leaves a record whose
heartbeat goes stale, and a reader drops it after
:data:`STALE_S` and sweeps the file away.

The workspaces list counts the fresh records per workspace on its
refresh cadence — the ``»N`` marker beside a row's status word —
so the count names the shells open on the host the TUI runs on:
the daemon is not involved, and a tree restart changes nothing
(the records live on disk and the sessions keep heartbeating). A
passthrough remote command and ``msks rsync`` write nothing: they
are connections, not shells.

Record creation mints a short token drawn against the
workspace's live sessions (best effort; the mint holds no lock —
the token is a join key, not a security boundary); the token
rides the record and the spawned window's default title, and is
the join key a later surface (#856) matches a
session's window by. The state directory is the one the
session-shaped surfaces share: #857's shape files ride it too when
they land.
"""

import hashlib
import json
import os
import random
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

#: The heartbeat interval (#858): the session rewrites its record
#: this often while it lives, so a count reads a live session
#: within one beat of its open and its clean close.
HEARTBEAT_S = 5.0

#: The stale TTL (#858): a record whose heartbeat is older than
#: this reads as a session that died without cleanup — a SIGKILL,
#: a power loss — and the reader drops it and sweeps the file.
#: Three beats: one missed beat is a slow host, two is a stall,
#: three is gone.
STALE_S = 15.0

#: The record token's shape (#858): five characters from an
#: alphabet without the confusables (no ``0``/``o``, no ``1``/
#: ``l``/``i``) — short enough to read in a window title, long
#: enough that two live sessions of one workspace never collide.
TOKEN_LEN = 5
TOKEN_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"

#: The sessions subtree under the state root (#858): one directory
#: per workspace, one file per session inside it.
SESSIONS_DIRNAME = "sessions"


def env_state_dir(variable: str) -> Path | None:
    """One state-root variable's value, normalized or refused.

    A leading ``~`` expands; a relative value names a different
    directory from every working directory, which is exactly what
    a state root must not do, so it is refused with the one-line
    fix rather than honored. An empty value counts as unset — the
    same rule the shell preset applies.
    """
    value = os.environ.get(variable, "")
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise SystemExit(
            f"msks: {variable} must name an absolute path (got "
            f"'{value}'); unset it or pass an absolute path"
        )
    return root


def state_dir() -> Path:
    """The client state root: `MSKSC_STATE_DIR` when set, else
    `XDG_STATE_HOME` or `~/.local/state`, under msks (#858).

    The variable names the root itself — the sessions subtree is
    created below it — so an operator or a checkout takes the
    whole client state out of the shared XDG tree. The sibling
    :func:`msks.client.ssh.cache_dir` answers the cache root the
    same way.
    """
    override = env_state_dir("MSKSC_STATE_DIR")
    if override is not None:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state"
    )
    return Path(base) / "msks"


def sessions_root(base: Path | None = None) -> Path:
    """The sessions subtree: the state root's ``sessions``
    directory, one directory per workspace below it."""
    return (state_dir() if base is None else base) / SESSIONS_DIRNAME


#: The workspace keys that name a directory directly (#858): a
#: first character the path vocabulary cannot carve (never a
#: dot), then word characters, a hyphen, a dot, or an underscore
#: to the end — a daemon id, a plain name. Anything else (a
#: slash, a space, a leading dot) takes the hash form instead.
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def record_key(pin: str) -> str:
    """The workspace's directory name under the sessions root: the
    pin passes through when it reads as one path-safe word (a
    daemon id, a plain name, a hyphenated one), else a short hash
    of it stands in — the records tree never grows a path a typed
    reference carved."""
    if SAFE_KEY.fullmatch(pin):
        return pin
    return hashlib.sha1(pin.encode()).hexdigest()[:12]


def record_dir(pin: str, base: Path | None = None) -> Path:
    """The workspace's session-record directory."""
    return sessions_root(base) / record_key(pin)


def record_path(pin: str, pid: int, base: Path | None = None) -> Path:
    """One session's record file: the workspace's directory, the
    pid's file — one record per live session, and a pid reusing a
    swept record's name starts clean (the write replaces it)."""
    return record_dir(pin, base) / f"{pid}.json"


def clock_now() -> float:
    """The wall clock the records stamp (#858) — the tests' seam
    for a pinned now."""
    return time.time()


def resolve_now(now: float | None) -> float:
    """The clock a reader reads: the injected now, else the wall
    clock — the one seam every reader shares."""
    return clock_now() if now is None else now


def write_record(
    path: Path, workspace: str, pid: int, token: str, when: float
) -> None:
    """Write one record atomically, best effort: a staging file
    per pid replaces into place (a reader never sees a torn
    write), and a tree that refuses the write — a read-only state
    root — costs the count, not the session: the shell keeps
    running untitled-to-the-count."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        staged.write_text(
            json.dumps(
                {
                    "workspace": workspace,
                    "pid": pid,
                    "token": token,
                    "heartbeat": when,
                }
            ),
            encoding="utf-8",
        )
        os.replace(staged, path)
    except OSError:
        pass


def read_record(path: Path) -> dict | None:
    """One record file as a dict, or None when it does not read —
    missing, torn, or not a record's shape (a stray file, a
    staging file caught mid-sweep)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def remove_record(path: Path) -> None:
    """Unlink one record best effort: a record the sweep already
    took (a session outliving a stale sweep, exiting later) is
    gone, which is the outcome the unlink wanted."""
    try:
        path.unlink()
    except OSError:
        pass


def record_is_fresh(record: dict, now: float) -> bool:
    """Whether a record's heartbeat sits inside the TTL (#858): a
    live session rewrites its record every :data:`HEARTBEAT_S`, so
    a stamp older than :data:`STALE_S` names a session that died
    without its cleanup."""
    beat = record.get("heartbeat")
    return isinstance(beat, (int, float)) and now - beat <= STALE_S


def sweep_entry(entry: Path, now: float) -> dict | None:
    """One directory entry's fresh record, or ``None`` with the
    entry swept beside it (#858): the TTL holds the record, and
    everything else — a stale record, a torn write, a stray file
    — goes."""
    record = read_record(entry)
    if record is not None and record_is_fresh(record, now):
        return record
    remove_record(entry)
    return None


def scan_workspace(directory: Path, now: float) -> list[dict]:
    """One workspace directory's fresh records (#858), each stale
    entry swept on the way (:func:`sweep_entry`); a directory left
    empty goes with them. A directory that does not read answers
    no records."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    live = [kept for entry in entries if (kept := sweep_entry(entry, now))]
    if not live:
        drop_directory(directory)
    return live


def drop_directory(directory: Path) -> None:
    """Remove an empty workspace directory best effort: a
    concurrent record landing in it keeps the directory, which is
    the record's to keep."""
    try:
        directory.rmdir()
    except OSError:
        pass


def live_tokens(
    pin: str, now: float | None = None, base: Path | None = None
) -> set[str]:
    """The workspace's live session tokens (#858): the fresh
    records' tokens, swept of the stale ones on the way through —
    a dead session's token is free to mint again."""
    return {
        record["token"]
        for record in scan_workspace(record_dir(pin, base), resolve_now(now))
        if isinstance(record.get("token"), str)
    }


def candidate_token(picker, attempt: int) -> str:
    """One mint attempt's draw (#858): :data:`TOKEN_LEN` fresh
    characters, a numeric suffix riding the retries."""
    token = "".join(picker.choice(TOKEN_CHARS) for _ in range(TOKEN_LEN))
    return token + str(attempt) if attempt else token


def mint_token(
    pin: str,
    now: float | None = None,
    base: Path | None = None,
    rng: random.Random | None = None,
) -> str:
    """A short token drawn against the workspace's live sessions
    (#858): five characters from :data:`TOKEN_CHARS`, redrawn
    until one stands clear of the fresh records' tokens — a draw
    that collides takes a numeric suffix on its next attempt
    (:func:`candidate_token`), so the mint always terminates. The
    uniqueness is best effort: two sessions minting concurrently
    read the same live set and can draw alike (about one pair in
    28 million), and the mint costs no lock because the token is
    a display and join key, not a security boundary.
    """
    picker = rng if rng is not None else random.SystemRandom()
    live = live_tokens(pin, now=now, base=base)
    attempt = 0
    while (token := candidate_token(picker, attempt)) in live:
        attempt += 1
    return token


def beat_loop(
    path: Path,
    workspace: str,
    pid: int,
    token: str,
    stop: threading.Event,
    interval: float,
    clock=clock_now,
) -> None:
    """The record's heartbeat (#858): rewrite the record each
    interval until the session's exit sets ``stop`` — the wait
    wakes on the set, so the loop leaves promptly at cleanup."""
    while not stop.wait(interval):
        write_record(path, workspace, pid, token, clock())


@contextmanager
def record_session(
    pin: str,
    *,
    base: Path | None = None,
    beat: float = HEARTBEAT_S,
    clock=clock_now,
    rng: random.Random | None = None,
):
    """One interactive session's record (#858): mint the token,
    write the record, and hold it — heartbeated on a daemon thread
    beside the ssh child — for the body's life, unlinking at exit
    however the body ends (a clean exit, ssh's own nonzero code,
    an interrupt, a refusal). Yields the token: the join key the
    spawned window's default title carries.

    The write is best effort throughout (:func:`write_record`): a
    state root the tree refuses costs the count and keeps the
    shell — registration never stands between an operator and the
    session.
    """
    token = mint_token(pin, base=base, rng=rng)
    pid = os.getpid()
    path = record_path(pin, pid, base=base)
    write_record(path, pin, pid, token, clock())
    stop = threading.Event()
    thread = threading.Thread(
        target=beat_loop,
        args=(path, pin, pid, token, stop, beat, clock),
        daemon=True,
    )
    thread.start()
    try:
        yield token
    finally:
        stop.set()
        thread.join()
        remove_record(path)


def count_sessions(
    now: float | None = None, base: Path | None = None
) -> dict[str, int]:
    """The host's open shells per workspace (#858): the fresh
    records' count keyed by the workspace's record key, the stale
    ones swept on the way through (:func:`scan_workspace`). A
    state root that does not read answers no counts — the listing
    paints no markers rather than guessing."""
    try:
        directories = list(sessions_root(base).iterdir())
    except OSError:
        return {}
    counts: dict[str, int] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        live = scan_workspace(directory, resolve_now(now))
        if live:
            counts[directory.name] = len(live)
    return counts

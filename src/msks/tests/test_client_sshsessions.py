"""The per-user session records (#858).

The records' arithmetic is pinned here against a throwaway state
root: the state directory's resolution, the token's minting, the
record's write/read/fresh lifecycle, the sweep, and the count —
with the clock and the token picker injected, so no test waits on
a heartbeat or wins a race with one.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from msks.client import sshsessions as rec

# --- the state root ---


def test_state_dir_honors_msksc_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MSKSC_STATE_DIR` names the root itself and wins over the
    XDG fallback — the same rule the cache root carries."""
    monkeypatch.setenv("MSKSC_STATE_DIR", "/tmp/per-checkout-state")
    assert rec.state_dir() == Path("/tmp/per-checkout-state")


def test_state_dir_honors_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MSKSC_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    assert rec.state_dir() == Path("/tmp/xdg-state/msks")


def test_state_dir_defaults_under_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MSKSC_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert rec.state_dir() == Path.home() / ".local" / "state" / "msks"


def test_state_dir_expands_a_home_relative_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_STATE_DIR", "~/state/msks")
    assert rec.state_dir() == Path.home() / "state" / "msks"


def test_state_dir_refuses_a_relative_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_STATE_DIR", "state/msks")
    with pytest.raises(SystemExit, match="absolute path"):
        rec.state_dir()


def test_state_dir_treats_an_empty_override_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_STATE_DIR", "")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    assert rec.state_dir() == Path("/tmp/xdg-state/msks")


# --- the record tree ---


def test_record_key_passes_a_safe_pin_through() -> None:
    """A daemon id, a plain name, and a hyphenated one all name
    their directory directly — the shapes a workspace's pin
    takes."""
    assert rec.record_key("a1b2c3d4e5") == "a1b2c3d4e5"
    assert rec.record_key("my-workspace") == "my-workspace"


def test_record_key_hashes_a_carving_reference() -> None:
    """A typed reference a path could carve (a slash, a space, a
    leading dot) never names a directory: a short hash of it
    stands in — and a trailing newline carves nothing either (the
    hash form is the one spelling a newline cannot sneak past)."""
    for carving in ("../etc", "a b", ".hidden", ".", "abc\n"):
        assert rec.record_key(carving).isalnum()
        assert rec.record_key(carving) != carving


def test_record_path_keys_on_the_workspace_and_pid(tmp_path: Path) -> None:
    path = rec.record_path("a1b2c3d4e5", 4242, base=tmp_path)
    assert path == (tmp_path / "sessions" / "a1b2c3d4e5" / "4242.json")


# --- write, read, freshness ---


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    path = rec.record_path("ws", 10, base=tmp_path)
    rec.write_record(path, "ws", 10, "ab23c", 100.0)
    assert rec.read_record(path) == {
        "workspace": "ws",
        "pid": 10,
        "token": "ab23c",
        "heartbeat": 100.0,
    }


def test_write_record_stages_atomically(tmp_path: Path) -> None:
    """The record lands by an os.replace from a per-pid staging
    name: a reader never sees a torn write, and the staging file
    leaves nothing behind."""
    path = rec.record_path("ws", 10, base=tmp_path)
    rec.write_record(path, "ws", 10, "ab23c", 100.0)
    assert list(path.parent.iterdir()) == [path]


def test_write_record_survives_a_refusing_tree(tmp_path: Path) -> None:
    """A state root the tree refuses — here a file squatting on
    the workspace directory's name — costs the count, not the
    session: the write swallows the refusal."""
    blocker = tmp_path / "sessions" / "ws"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory")
    rec.write_record(blocker / "10.json", "ws", 10, "ab23c", 100.0)


def test_read_record_answers_none_for_the_unreadable(
    tmp_path: Path,
) -> None:
    """A missing file, torn JSON, and a file that is no record's
    shape all read as no record — the sweep's side of the same
    rule."""
    path = rec.record_path("ws", 10, base=tmp_path)
    assert rec.read_record(path) is None
    path.parent.mkdir(parents=True)
    path.write_text("{torn")
    assert rec.read_record(path) is None
    path.write_text("[1, 2]")
    assert rec.read_record(path) is None


def test_record_is_fresh_reads_the_ttl() -> None:
    assert rec.record_is_fresh({"heartbeat": 100.0}, 100.0 + rec.STALE_S)
    assert not rec.record_is_fresh(
        {"heartbeat": 100.0}, 100.0 + rec.STALE_S + 0.01
    )
    # A record without a readable stamp is not fresh: it names a
    # session nobody is heartbeating.
    assert not rec.record_is_fresh({}, 100.0)
    assert not rec.record_is_fresh({"heartbeat": "soon"}, 100.0)


# --- the sweep and the count ---


def seed(base: Path, pin: str, pid: int, token: str, beat: float) -> Path:
    """One record planted directly, as a live session's writer
    would have left it."""
    path = rec.record_path(pin, pid, base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workspace": pin,
                "pid": pid,
                "token": token,
                "heartbeat": beat,
            }
        )
    )
    return path


def test_count_sessions_counts_the_fresh_per_workspace(
    tmp_path: Path,
) -> None:
    """Two spawned windows and one typed session against a
    workspace count as three, and a second workspace's shells
    count separately — keyed by the workspace's record key."""
    for pid, token in ((11, "aa11b"), (12, "cc22d"), (13, "ee33f")):
        seed(tmp_path, "ws-a", pid, token, 100.0)
    seed(tmp_path, "ws-b", 21, "gg44h", 100.0)
    counts = rec.count_sessions(now=101.0, base=tmp_path)
    assert counts == {"ws-a": 3, "ws-b": 1}


def test_count_sessions_sweeps_stale_records(tmp_path: Path) -> None:
    """A session that died without cleanup — a SIGKILL — stops
    counting once its heartbeat passes the TTL, and the sweep
    takes the file and the emptied directory with it."""
    stale = seed(tmp_path, "ws", 10, "aa11b", 100.0)
    assert (
        rec.count_sessions(now=100.0 + rec.STALE_S + 1.0, base=tmp_path) == {}
    )
    assert not stale.exists()
    assert not (tmp_path / "sessions" / "ws").exists()


def test_count_sessions_sweeps_strays_and_keeps_live_dirs(
    tmp_path: Path,
) -> None:
    """A stray file the sessions tree never wrote — and a torn
    record — is swept like a stale one, while a live record keeps
    its workspace's directory standing."""
    root = tmp_path / "sessions" / "ws"
    root.mkdir(parents=True)
    (root / "stray.txt").write_text("not a record")
    torn = root / "30.json"
    torn.write_text("{torn")
    live = seed(tmp_path, "ws", 31, "aa11b", 100.0)
    counts = rec.count_sessions(now=101.0, base=tmp_path)
    assert counts == {"ws": 1}
    assert not (root / "stray.txt").exists()
    assert not torn.exists()
    assert live.exists()
    assert root.exists()


def test_count_sessions_skips_a_stray_at_the_root(
    tmp_path: Path,
) -> None:
    """A stray file directly under the sessions root is no
    workspace directory: the count steps over it, and the sweep
    owns files inside the workspaces' own directories."""
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    (root / "stray.txt").write_text("not a directory")
    assert rec.count_sessions(now=100.0, base=tmp_path) == {}
    assert (root / "stray.txt").exists()


def test_the_sweep_leaves_a_nest_it_cannot_take(tmp_path: Path) -> None:
    """A directory nested inside a workspace's directory survives
    the sweep — neither the record unlink nor the empty-dir rmdir
    touches a directory — so the workspace's directory stays too,
    quietly: the sweep is housekeeping, not enforcement."""
    nested = tmp_path / "sessions" / "ws" / "nested"
    nested.mkdir(parents=True)
    stale = seed(tmp_path, "ws", 10, "aa11b", 100.0)
    assert rec.count_sessions(now=200.0, base=tmp_path) == {}
    assert not stale.exists()
    assert nested.exists()
    assert nested.parent.exists()


def test_count_sessions_answers_nothing_without_a_root(
    tmp_path: Path,
) -> None:
    """A state root that does not exist (nothing ever registered
    on this host) reads as no counts — no markers, not an
    error."""
    assert rec.count_sessions(now=100.0, base=tmp_path / "absent") == {}


def test_a_stale_sweep_leaves_the_tree_ready_for_reuse(
    tmp_path: Path,
) -> None:
    """A tree restart — or any fresh read — counts the records on
    disk: a live session's record survives the read, and a later
    session under the same workspace joins it."""
    seed(tmp_path, "ws", 10, "aa11b", 100.0)
    assert rec.count_sessions(now=101.0, base=tmp_path) == {"ws": 1}
    seed(tmp_path, "ws", 11, "cc22d", 102.0)
    assert rec.count_sessions(now=103.0, base=tmp_path) == {"ws": 2}


# --- the token ---


class FixedPicker:
    """A rigged token picker: every draw answers the same five
    characters, so a collision is arranged, not won by chance."""

    def choice(self, chars: str) -> str:
        return chars[0]


def test_mint_token_draws_from_the_alphabet(tmp_path: Path) -> None:
    """The mint's shape: five characters, the alphabet without the
    confusables, unique among nothing (no live sessions)."""

    class Counting:
        def __init__(self) -> None:
            self.draws = 0

        def choice(self, chars: str) -> str:
            self.draws += 1
            return chars[len(chars) // 2]

    picker = Counting()
    token = rec.mint_token("ws", now=100.0, base=tmp_path, rng=picker)
    assert token == rec.TOKEN_CHARS[len(rec.TOKEN_CHARS) // 2] * rec.TOKEN_LEN
    assert picker.draws == rec.TOKEN_LEN
    assert len(rec.TOKEN_CHARS) == len(set(rec.TOKEN_CHARS))
    assert not {"0", "1", "o", "i", "l"} & set(rec.TOKEN_CHARS)


def test_mint_token_redraws_against_the_live_sessions(
    tmp_path: Path,
) -> None:
    """A draw that collides with a live session's token takes a
    numeric suffix on its next attempt: the mint always answers a
    token unique among the workspace's live sessions."""
    seed(tmp_path, "ws", 10, "aaaaa", 100.0)
    token = rec.mint_token("ws", now=101.0, base=tmp_path, rng=FixedPicker())
    assert token == "aaaaa1"


def test_mint_token_reuses_a_dead_session_s_token(
    tmp_path: Path,
) -> None:
    """A stale record's token is free again: the mint's uniqueness
    check reads the fresh records alone."""
    seed(tmp_path, "ws", 10, "aaaaa", 100.0)
    token = rec.mint_token(
        "ws", now=100.0 + rec.STALE_S + 1.0, base=tmp_path, rng=FixedPicker()
    )
    assert token == "aaaaa"


def test_mint_tokens_ignores_a_record_without_a_token(
    tmp_path: Path,
) -> None:
    """A live record whose token field is not a string constrains
    nothing — the mint draws fresh."""
    root = tmp_path / "sessions" / "ws"
    root.mkdir(parents=True)
    (root / "10.json").write_text(
        json.dumps({"workspace": "ws", "pid": 10, "heartbeat": 100.0})
    )
    token = rec.mint_token("ws", now=101.0, base=tmp_path, rng=FixedPicker())
    assert token == "aaaaa"


def test_mint_token_checks_its_own_workspace_only(
    tmp_path: Path,
) -> None:
    """Uniqueness is among the workspace's live sessions: another
    workspace's token is free to draw."""
    seed(tmp_path, "ws-b", 10, "aaaaa", 100.0)
    token = rec.mint_token("ws-a", now=101.0, base=tmp_path, rng=FixedPicker())
    assert token == "aaaaa"


# --- the session's own registration ---


def test_record_session_registers_heartbeats_and_unlinks(
    tmp_path: Path,
) -> None:
    """One session's whole life: the record exists with the pid
    and token from the first beat, the heartbeat keeps it fresh,
    and the clean close unlinks it at once — the count drops with
    the session, not with the TTL."""
    clock = {"now": 100.0}

    def tick() -> float:
        return clock["now"]

    with rec.record_session(
        "ws", base=tmp_path, beat=0.01, clock=tick, rng=FixedPicker()
    ) as token:
        assert token == "aaaaa"
        path = rec.record_path("ws", os.getpid(), base=tmp_path)
        record = rec.read_record(path)
        assert record is not None
        assert record["pid"] == os.getpid()
        assert record["token"] == "aaaaa"
        clock["now"] += 1.0
        beat = threading.Event()

        def waiter() -> None:
            while not beat.is_set():
                fresh = rec.read_record(path)
                if fresh is not None and fresh["heartbeat"] >= 101.0:
                    beat.set()
                    return
                time.sleep(0.001)

        threading.Thread(target=waiter, daemon=True).start()
        assert beat.wait(5.0)
    assert not path.exists()
    assert rec.count_sessions(now=102.0, base=tmp_path) == {}


def test_record_session_unlinks_on_an_interrupted_body(
    tmp_path: Path,
) -> None:
    """However the body ends — an interrupt carries out of ssh, a
    refusal raises through — the record goes with it."""
    with pytest.raises(KeyboardInterrupt):
        with rec.record_session(
            "ws", base=tmp_path, beat=60.0, clock=lambda: 100.0
        ):
            raise KeyboardInterrupt
    assert rec.count_sessions(now=100.0, base=tmp_path) == {}


def test_record_session_survives_a_refusing_state_tree(
    tmp_path: Path,
) -> None:
    """A state root the tree cannot write registers nothing and
    still yields a token: registration never stands between an
    operator and the session — the shell runs, the count simply
    does not see it."""
    blocker = tmp_path / "sessions" / "ws"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory")
    with rec.record_session(
        "ws", base=tmp_path, beat=0.01, clock=lambda: 100.0
    ) as token:
        assert token
    assert rec.count_sessions(now=100.0, base=tmp_path) == {}


def test_beat_loop_leaves_at_once_when_stopped(tmp_path: Path) -> None:
    """The loop's own exit: a stop set before the first beat
    writes nothing — the record stands as its writer left it."""
    path = seed(tmp_path, "ws", 10, "aa11b", 100.0)
    stop = threading.Event()
    stop.set()
    rec.beat_loop(path, "ws", 10, "aa11b", stop, 0.01, lambda: 200.0)
    assert rec.read_record(path)["heartbeat"] == 100.0


def test_a_swept_record_unlink_stays_quiet(tmp_path: Path) -> None:
    """A session outliving a stale sweep (the host slept; the
    reader reaped its record) exits onto a missing file: the
    unlink wanted the record gone, and it is."""
    rec.remove_record(tmp_path / "nowhere" / "10.json")

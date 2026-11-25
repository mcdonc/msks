"""The GUI secrets presenter (#965): placeholder and
audit fakes in, row models out — the coverage label, the TTL
countdown and the deadline clock beside it (#1165), the TTL
choices, and the audit
rows. The presenter holds no Qt import, so these tests drive it
as plain Python, the same way the coverage gate does.
"""

import ast
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from msks.client import ages as ages_mod
from msks.client.gui import secretrows as gui_secrets
from msks.client.tui import consent as tui_consent
from msks.client.tui.consent_ui import events as tui_events
from msks.client.tui.secrets import audit as tui_audit
from msks.client.tui.secrets import cells as tui_cells
from rich.markup import escape

#: The pinned clock (#965's fakes): an aware local moment the
#: countdowns and the created labels read, so every label under
#: test is deterministic on every host's timezone.
NOW = datetime(2030, 1, 2, 3, 4, 5).astimezone()

#: The pinned stamps' epoch domain (#965's fakes): a naive UTC
#: stamp's epoch, the domain the audit rows sort in.
NAIVE_STAMP = "2030-01-02T03:04:05"
NAIVE_EPOCH = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()


#: The deadline stamps the EXPIRES cell carries beside the
#: countdown (#1165's reads): the host's own local spelling,
#: computed the way the presenter computes it, so the labels
#: land deterministically on every host's timezone.
def clock(expires_ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(expires_ts))


def stamp(expires_ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_ts))


def placeholder(
    id: int = 1,
    name: str = "github_api",
    workspaces: list[str] | None = None,
    dests: list[str] | None = None,
    expires_at: str | None = None,
    created_at: str | None = None,
    locked: bool = False,
) -> dict:
    """One placeholder row as the listing serves it (#965's
    fakes): the fields the presenter reads."""
    row = {
        "id": id,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": list(dests) if dests is not None else ["api.github.com"],
        "expires_at": expires_at,
        "locked": locked,
    }
    if created_at is not None:
        row["created_at"] = created_at
    return row


def recorded(
    id: int,
    kind: str,
    *,
    workspaces: list[str] | None = None,
    name: str = "github_api",
    dests: list[str] | None = None,
    created_at: str | None = NAIVE_STAMP,
) -> dict:
    """One recorded audit row as the listing serves it (#965's
    fakes): the fields the presenter reads, with the dests the
    daemon's every row carries (the mint's alone render)."""
    return {
        "id": id,
        "kind": kind,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": list(dests or []),
        "created_at": created_at,
    }


def test_the_placeholder_rows_build_models() -> None:
    """The listing's row models (#965): one per daemon row, in
    the daemon's own order, each carrying the id the actions key
    on beside the cells the row renders — a fresh row counting
    down, a standing unbounded row, an expired locked one."""
    fresh = placeholder(
        id=7,
        workspaces=["ws2", "ws1"],
        expires_at=(NOW + timedelta(hours=2)).isoformat(),
        created_at=(NOW - timedelta(days=3)).isoformat(),
    )
    standing = placeholder(
        id=8, name="second", dests=[], created_at=NOW.isoformat()
    )
    expired = placeholder(
        id=9,
        workspaces=["ws1"],
        expires_at=(NOW - timedelta(seconds=1)).isoformat(),
        created_at=(NOW - timedelta(days=1)).isoformat(),
        locked=True,
    )
    models = gui_secrets.secret_rows([fresh, standing, expired], NOW)
    assert models == [
        gui_secrets.SecretRow(
            id=7,
            name="github_api",
            coverage="scoped: ws1,ws2",
            dests="api.github.com",
            ttl=f"2h · {clock((NOW + timedelta(hours=2)).timestamp())}",
            expires_ts=(NOW + timedelta(hours=2)).timestamp(),
            created="3d ago",
            created_ts=(NOW - timedelta(days=3)).timestamp(),
            state="open",
        ),
        gui_secrets.SecretRow(
            id=8,
            name="second",
            coverage="daemon-wide",
            dests="-",
            ttl="never",
            expires_ts=None,
            created="today",
            created_ts=NOW.timestamp(),
            state="open",
        ),
        gui_secrets.SecretRow(
            id=9,
            name="github_api",
            coverage="scoped: ws1",
            dests="api.github.com",
            ttl=f"expired · {clock((NOW - timedelta(seconds=1)).timestamp())}",
            expires_ts=(NOW - timedelta(seconds=1)).timestamp(),
            created="yesterday",
            created_ts=(NOW - timedelta(days=1)).timestamp(),
            state="locked",
        ),
    ]
    # The daemon's own order stands — the listing's rule, not
    # the catalog's sort.
    assert [model.id for model in models] == [7, 8, 9]


def test_one_placeholder_model_carries_what_the_widget_renders() -> None:
    """One model's cells (#965): the name's dash floor for a row
    that carries none, the created label's dash beside it, and
    the unparsable expiry's dash — the TUI row's own floors. A
    stamp that does not parse leaves no deadline epoch (the
    frozen dash stands, the tick recomputes nothing), and a
    missing creation floors at zero, below every dated row."""
    model = gui_secrets.secret_row(
        placeholder(name="", expires_at="junk", created_at=None), NOW
    )
    assert model == gui_secrets.SecretRow(
        id=1,
        name="-",
        coverage="daemon-wide",
        dests="api.github.com",
        ttl="-",
        expires_ts=None,
        created="-",
        created_ts=0.0,
        state="open",
    )


def test_the_ttl_cell_counts_the_remaining_lifetime() -> None:
    """The TTL countdown (#390's arithmetic, #965's read, with
    the deadline's clock beside it #1165): a bounded row counts
    down compactly, an unbounded row reads ``never``, a past
    deadline reads ``expired``, a stamp that does not parse
    reads its dash — and the unpinned call reads the wall clock
    the shared leaf owns."""
    assert (
        gui_secrets.ttl_text(
            placeholder(expires_at=(NOW + timedelta(hours=2)).isoformat()),
            NOW,
        )
        == f"2h · {clock((NOW + timedelta(hours=2)).timestamp())}"
    )
    assert gui_secrets.ttl_text(
        placeholder(expires_at=NOW.isoformat()), NOW
    ) == (f"expired · {clock(NOW.timestamp())}")
    assert gui_secrets.ttl_text(placeholder(expires_at=None), NOW) == "never"
    assert gui_secrets.ttl_text(placeholder(expires_at=""), NOW) == "never"
    assert gui_secrets.ttl_text(placeholder(expires_at="junk"), NOW) == "-"
    far = placeholder(
        expires_at=(ages_mod.clock_now() + timedelta(days=2)).isoformat()
    )
    assert gui_secrets.ttl_text(far) == gui_secrets.ttl_text(
        far, ages_mod.clock_now()
    )


def test_the_countdown_reads_the_deadline_epoch() -> None:
    """The tick's own read (#965): the label over the model's
    deadline epoch — the same label the row-dict path renders
    over the same deadline — with the unpinned call reading the
    wall clock the shared leaf owns."""
    deadline = (NOW + timedelta(hours=2)).timestamp()
    assert gui_secrets.countdown(deadline, NOW) == "2h"
    assert gui_secrets.countdown(NOW.timestamp(), NOW) == "expired"
    far = (ages_mod.clock_now() + timedelta(days=2)).timestamp()
    assert gui_secrets.countdown(far) == gui_secrets.countdown(
        far, ages_mod.clock_now()
    )
    for offset in (timedelta(hours=2), timedelta(seconds=45)):
        row = placeholder(expires_at=(NOW + offset).isoformat())
        assert gui_secrets.expires_text(
            gui_secrets.secret_row(row, NOW).expires_ts, NOW
        ) == gui_secrets.ttl_text(row, NOW)


def test_the_expires_cell_stamps_the_deadline_clock() -> None:
    """The EXPIRES cell's stamp (#1165): the countdown, the
    middle dot, the deadline's clock in the operator's own zone
    — a deadline at 14:20 local reads ``2h · 14:20`` two hours
    out and ``expired · 14:20`` past it — the full local date
    and time beside it for the cell's tooltip."""
    deadline = datetime(2030, 1, 2, 14, 20).astimezone()
    assert gui_secrets.deadline_clock(deadline.timestamp()) == "14:20"
    assert (
        gui_secrets.deadline_stamp(deadline.timestamp()) == "2030-01-02 14:20"
    )
    assert (
        gui_secrets.expires_text(
            deadline.timestamp(), deadline - timedelta(hours=2)
        )
        == "2h · 14:20"
    )
    assert (
        gui_secrets.expires_text(
            deadline.timestamp(), deadline + timedelta(seconds=1)
        )
        == "expired · 14:20"
    )
    # A multi-day countdown still reads the deadline's own
    # clock, and the stamp beside it carries the date.
    week = deadline + timedelta(days=7, minutes=5)
    assert (
        gui_secrets.expires_text(week.timestamp(), deadline)
        == f"7d · {clock(week.timestamp())}"
    )
    assert gui_secrets.deadline_stamp(week.timestamp()) == (
        stamp(week.timestamp())
    )


def test_the_duration_ladder_reads_compactly() -> None:
    """The countdown's ladder (#965, the TUI's own): seconds
    under a minute, minutes under an hour, hours under a day,
    days past it."""
    for secs, label in (
        (0, "0s"),
        (45.9, "45s"),
        (59, "59s"),
        (300, "5m"),
        (3599, "59m"),
        (7200, "2h"),
        (86399, "23h"),
        (259200, "3d"),
    ):
        assert gui_secrets.fmt_duration(secs) == label


def test_the_coverage_label_reads_the_row() -> None:
    """The coverage label (#390, the GUI's read, #1165): the
    daemon-wide row reads the mint form's own word; a scoped
    row reads ``scoped:`` and its workspace ids joined —
    sorted, deduplicated, whatever order the row carried — the
    words the form's picker names, so the listing reads back as
    the choice the operator made."""
    assert gui_secrets.coverage_text(placeholder()) == "daemon-wide"
    assert (
        gui_secrets.coverage_text(placeholder(workspaces=[])) == "daemon-wide"
    )
    assert (
        gui_secrets.coverage_text(placeholder(workspaces=["b", "a", "b"]))
        == "scoped: a,b"
    )
    assert gui_secrets.COVERAGE_WIDE == "daemon-wide"
    assert gui_secrets.COVERAGE_SCOPED == "scoped"


def test_the_dests_and_state_cells_read_their_floors() -> None:
    """The destinations and state cells (#965): the destinations
    joined with commas — a dash for a row that holds none — and
    the lock state word the listing surfaces."""
    assert (
        gui_secrets.dests_cell(placeholder(dests=["a.example", "b.example"]))
        == "a.example, b.example"
    )
    assert gui_secrets.dests_cell(placeholder(dests=[])) == "-"
    assert gui_secrets.dests_cell({}) == "-"
    assert gui_secrets.state_text(placeholder(locked=True)) == "locked"
    assert gui_secrets.state_text(placeholder(locked=False)) == "open"
    assert gui_secrets.state_text(placeholder()) == "open"


def test_the_ttl_choices_and_their_seconds() -> None:
    """The mint form's TTL choices (#393, #965's model): the
    unbounded row first — the daemon's default when a mint
    carries no ttl — then the renew picker's span, each choice
    with its seconds; the unbounded pick sends no ``ttl_s``."""
    assert gui_secrets.SECRET_TTLS == ("1h", "6h", "1d", "7d", "30d")
    assert gui_secrets.SECRET_TTL_SECONDS == {
        "1h": 3600,
        "6h": 21600,
        "1d": 86400,
        "7d": 604800,
        "30d": 2592000,
    }
    assert gui_secrets.MINT_TTLS == ("unbounded", *gui_secrets.SECRET_TTLS)
    assert gui_secrets.ttl_seconds("unbounded") is None
    assert gui_secrets.ttl_seconds("1h") == 3600
    assert gui_secrets.ttl_seconds("30d") == 2592000


def test_the_renew_picker_default_reads_the_remaining_lifetime() -> None:
    """The renew picker's highlighted choice (#390, the GUI's
    read): the choice nearest the row's remaining lifetime, and
    the longest choice for a row that parses no deadline."""
    soon = placeholder(
        expires_at=(ages_mod.clock_now() + timedelta(seconds=70)).isoformat()
    )
    half = placeholder(
        expires_at=(ages_mod.clock_now() + timedelta(hours=12)).isoformat()
    )
    assert gui_secrets.ttl_default(soon) == "1h"
    assert gui_secrets.ttl_default(half) == "6h"
    assert gui_secrets.ttl_default(placeholder()) == "30d"
    assert gui_secrets.ttl_default(placeholder(expires_at="junk")) == "30d"


def test_the_audit_rows_build_newest_first() -> None:
    """The audit view's rows (#965): one per recorded row the
    shape accepts, newest first — the stamps order them, so a
    caller that stacks them out of stamp order still reads the
    view's own — and a broken row holds no place."""
    rows = [
        recorded(3, "revoke", created_at="2030-01-02T03:06:07"),
        recorded(2, "sighting", created_at="2030-01-02T03:07:07"),
        recorded(1, "mint", created_at="2030-01-02T03:05:07"),
        "not a mapping",
        recorded(4, "expiry", created_at="junk"),
    ]
    models = gui_secrets.audit_rows(rows)
    assert [model.audit_id for model in models] == [2, 3, 1, 4]


def test_one_audit_row_carries_what_the_widget_renders(
    monkeypatch,
) -> None:
    """One audit model's fields (#965): the sighting's ``!''
    marker beside every other kind's blank, the local wall-clock
    label (blank on a row that carries no parsable stamp), the
    workspace/name identity — ``*`` on the daemon-wide row, the
    first covered id on a scoped one — the mint's allowlist
    after the arrow, and the coverage the filters read. The
    clock is pinned three days past the stamp (#1064), so the
    label's dated spelling lands deterministically on every
    host."""
    monkeypatch.setattr(ages_mod, "clock_now", lambda: NOW + timedelta(days=3))
    mint = gui_secrets.audit_row(
        recorded(
            5,
            "mint",
            workspaces=["ws1"],
            dests=["api.github.com", ".example.com"],
        )
    )
    assert mint == gui_secrets.AuditRow(
        audit_id=5,
        kind="mint",
        marker=" ",
        ts=NAIVE_EPOCH,
        when=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NAIVE_EPOCH)),
        who="ws1/github_api",
        dest=" \u2192 api.github.com, .example.com",
        workspaces=("ws1",),
    )
    sighting = gui_secrets.audit_row(
        recorded(6, "sighting", workspaces=[], created_at="junk")
    )
    assert sighting.marker == "!"
    assert sighting.who == "*/github_api"
    assert sighting.when == ""
    assert sighting.ts == 0.0
    assert sighting.workspaces == ()
    revoke = gui_secrets.audit_row(
        recorded(7, "revoke", dests=["api.github.com"])
    )
    assert revoke.dest == ""  # the mint's alone, the replay rule


def test_the_audit_parsing_refuses_broken_rows() -> None:
    """The audit row's tolerance (#965, the TUI replay's own): a
    row that is not a mapping, or whose kind or name is not a
    string, answers None; the audit id reads as an int with
    bools and junk excluded."""
    for broken in (
        None,
        "row",
        [],
        {},
        {"kind": "mint"},
        {"name": "x"},
        {"kind": 7, "name": "x"},
        {"kind": "mint", "name": 7},
    ):
        assert gui_secrets.audit_row(broken) is None
    row = recorded(5, "mint")
    for id_value, expected in ((5, 5), (5.0, 5), (True, None), ("x", None)):
        row["id"] = id_value
        assert gui_secrets.audit_row(row).audit_id == expected


def test_the_recorded_stamp_reads_as_epoch() -> None:
    """The recorded row's stamp (#390's rule, the GUI's read): a
    naive stamp reads as UTC, an offset stamp converts, and
    anything else reads 0.0 — the row renders undated."""
    assert gui_secrets.audit_stamp_epoch(NAIVE_STAMP) == NAIVE_EPOCH
    assert gui_secrets.audit_stamp_epoch("2030-01-02T03:04:05+02:00") == (
        NAIVE_EPOCH - 2 * 3600
    )
    assert gui_secrets.audit_stamp_epoch("junk") == 0.0
    assert gui_secrets.audit_stamp_epoch(None) == 0.0
    assert gui_secrets.audit_stamp_epoch(2030) == 0.0


def test_the_audit_time_labels_the_epoch() -> None:
    """The audit row's time label (#1064): the local clock
    alone on a row from the viewer's own day, the date beside
    the clock on a row from an earlier day; an undated row
    keeps its blank column. The pinned ``now`` values sit on
    the stamp's own day and three days past it, so both
    spellings land deterministically on every host."""
    assert gui_secrets.audit_time(0.0) == ""
    assert gui_secrets.audit_time(-1.0) == ""
    same_day = datetime.fromtimestamp(NAIVE_EPOCH).astimezone()
    assert gui_secrets.audit_time(NAIVE_EPOCH, now=same_day) == (
        time.strftime("%H:%M:%S", time.localtime(NAIVE_EPOCH))
    )
    later = same_day + timedelta(days=3)
    assert gui_secrets.audit_time(NAIVE_EPOCH, now=later) == (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NAIVE_EPOCH))
    )


def test_the_row_fields_read_their_tuples() -> None:
    """The row-field readers (#965, the frame parsing's own
    tolerance): a list of anything reads as a tuple of strings,
    everything else as (); an int field excludes bools and
    junk."""
    assert gui_secrets.string_tuple(None) == ()
    assert gui_secrets.string_tuple("ab") == ()
    assert gui_secrets.string_tuple(["a", 2]) == ("a", "2")
    assert gui_secrets.int_field(True) is None
    assert gui_secrets.int_field("x") is None
    assert gui_secrets.int_field(7) == 7
    assert gui_secrets.int_field(7.9) == 7


def test_the_placeholder_helpers_match_the_tui_page() -> None:
    """The mirrored placeholder helpers keep behavior parity with
    the TUI's cells (#965): over the same placeholder fakes, both
    clients read the same coverage sets, TTL countdowns,
    destinations cells, renew defaults, and choice vocabulary —
    a formatting change on one side must fail here rather than
    drift the two pages apart. The GUI's two cells carry a
    deliberate addition each (#1165), pinned here: the coverage
    cell spells the TUI's ``*`` as the mint form's ``daemon-wide``
    and prefixes the scoped set, and the EXPIRES cell appends the
    deadline's local clock to the TUI's countdown."""
    rows = [
        placeholder(),
        placeholder(
            workspaces=["b", "a"],
            dests=["x.example", "y.example"],
            expires_at=(NOW + timedelta(hours=2)).isoformat(),
        ),
        placeholder(expires_at=(NOW - timedelta(minutes=90)).isoformat()),
        placeholder(expires_at="junk", locked=True),
    ]
    for row in rows:
        tui_coverage = tui_cells.coverage_text(row)
        gui_coverage = gui_secrets.coverage_text(row)
        if tui_coverage == "*":
            assert gui_coverage == "daemon-wide"
        else:
            assert gui_coverage == f"scoped: {tui_coverage}"
        assert gui_secrets.dests_cell(row) == tui_cells.dests_cell(row)
        tui_ttl = tui_cells.ttl_text(row, NOW)
        stamp = ages_mod.parse_stamp(row.get("expires_at") or "")
        expected = (
            f"{tui_ttl} · {clock(stamp.timestamp())}" if stamp else tui_ttl
        )
        assert gui_secrets.ttl_text(row, NOW) == expected
        assert gui_secrets.ttl_default(row) == tui_cells.ttl_default(row)
    assert gui_secrets.SECRET_TTLS == tui_cells.SECRET_TTLS
    assert gui_secrets.SECRET_TTL_SECONDS == tui_cells.SECRET_TTL_SECONDS
    assert gui_secrets.MINT_TTLS == tui_cells.MINT_TTLS
    assert gui_secrets.AUDIT_KINDS == tui_audit.AUDIT_KINDS
    assert gui_secrets.FILTER_ALL == tui_audit.FILTER_ALL
    assert tui_cells.SECRET_COLUMNS[5][0] == "STATE"
    for row in (placeholder(locked=True), placeholder(locked=False)):
        assert (
            gui_secrets.state_text(row) == tui_cells.secret_row_cells(row)[5]
        )
    for secs in (0.0, 45.9, 300, 3599, 7200, 86399, 259200):
        assert gui_secrets.fmt_duration(secs) == tui_consent.fmt_duration(secs)


def test_the_audit_helpers_match_the_tui_page() -> None:
    """The mirrored audit helpers keep parity with the TUI's
    replay (#965): over the same recorded fakes, both clients
    read the same epochs and the same rendered line — marker,
    time, kind, identity, destination — beside the same
    newest-first order the TUI's view shows."""
    rows = [
        recorded(3, "revoke", created_at="2030-01-02T03:06:07"),
        recorded(2, "sighting", created_at="2030-01-02T03:07:07"),
        recorded(
            1,
            "mint",
            workspaces=["ws1"],
            dests=["api.github.com"],
            created_at="2030-01-02T03:05:07",
        ),
        recorded(4, "expiry", created_at="2030-01-02T03:05:07"),
        recorded(5, "unlock", created_at="junk"),
        "not a mapping",
    ]
    stamps = [
        row.get("created_at") for row in rows if isinstance(row, dict)
    ] + ["junk", None, 2030]
    for iso in stamps:
        assert gui_secrets.audit_stamp_epoch(iso) == (
            tui_consent.audit_stamp_epoch(iso)
        )
    # The TUI's own display pipeline: the seed's events, oldest
    # first, rendered newest first — the GUI's models read the
    # same order and the same lines.
    events = [
        event
        for event in (
            tui_consent.audit_event(seq, row)
            for seq, row in enumerate(reversed(rows), start=1)
        )
        if event is not None
    ]
    display = list(reversed(tui_events.render_order(events)))
    models = gui_secrets.audit_rows(rows)
    assert [model.audit_id for model in models] == [
        event.audit_id for event in display
    ]
    for model, event in zip(models, display):
        assert event.kind == model.kind
        assert tui_events.event_line(event) == (
            f"{model.marker} {model.when:>19}  {model.kind:<8}"
            f"  {escape(model.who)}{escape(model.dest)}"
        )


def test_the_module_imports_nothing_from_pyside6() -> None:
    """The presenter stays Qt-free (#965): the widget module owns
    every Qt import, and this module's plain data keeps the
    coverage gate — and a base install's parse path, through the
    package init's pin — off the binding."""
    tree = ast.parse(Path(gui_secrets.__file__).read_text())
    roots = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots |= {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(root.split(".")[0] == "PySide6" for root in roots)


def test_the_state_and_expiry_kinds_read_the_model() -> None:
    """The tints the presenter names (#1160): an open placeholder
    reads ok and a locked one warn; the expiry cell reads warn
    under the renew floor alone — an unbounded row, a deadline
    further out, and one already past wear the chrome's own
    text."""
    open_row = gui_secrets.secret_row(placeholder(), NOW)
    locked_row = gui_secrets.secret_row(placeholder(locked=True), NOW)
    assert gui_secrets.state_kind(open_row) == "ok"
    assert gui_secrets.state_kind(locked_row) == "warn"
    expired = gui_secrets.secret_row(
        placeholder(expires_at=(NOW - timedelta(hours=1)).isoformat()), NOW
    )
    assert expired.state == "open"  # the word is the lock's alone
    other = gui_secrets.SecretRow(**{**expired.__dict__, "state": "revoked"})
    assert gui_secrets.state_kind(other) is None
    assert gui_secrets.expiry_kind(None, NOW) is None
    soon = (NOW + timedelta(minutes=10)).timestamp()
    later = (NOW + timedelta(hours=2)).timestamp()
    past = (NOW - timedelta(minutes=1)).timestamp()
    assert gui_secrets.expiry_kind(soon, NOW) == "warn"
    assert gui_secrets.expiry_kind(later, NOW) is None
    assert gui_secrets.expiry_kind(past, NOW) is None
    edge = (NOW + timedelta(seconds=gui_secrets.EXPIRY_WARN_S)).timestamp()
    assert gui_secrets.expiry_kind(edge, NOW) is None

"""The secrets row and note builders.

The coverage, ttl, and destination cells a secrets row draws,
the notes a renew or revoke answers with, the paste-split
helpers, and the OSC 52 copy the sentinel panel uses.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime

from rich.markup import escape

from .ui import (
    clip,
    clock_now,
    created_label,
    fmt_duration,
    padded_cells,
    parse_stamp,
)

#: The mint form's ttl choices (#390): the duration picker over
#: TTL-appropriate labels — the hours-to-a-month span a shared
#: credential lives, not the verdict picker's minutes-to-forever.
#: The secrets list's columns with their fixed minimum widths
#: (#510): COVERAGE shows what a placeholder covers, NAME its
#: mint-time name, DESTS the destinations the placeholder admits
#: (the mint's allowlist, `*` a daemon-wide row), EXPIRES and
#: CREATED the ttl clock, STATE the state a revoke leaves. The
#: widths are the fixed minimums the 80-column frame budgets; a
#: wider terminal widens the growing columns past them.
SECRET_COLUMNS = (
    ("COVERAGE", 13),
    ("NAME", 14),
    ("DESTS", 15),
    ("EXPIRES", 8),
    ("CREATED", 8),
    ("STATE", 6),
)

#: The columns the secrets page's spare width flows into when the
#: terminal widens past the 80-column frame.
SECRET_GROWING_LABELS = frozenset({"COVERAGE", "NAME", "DESTS"})

SECRET_TTLS = ("1h", "6h", "1d", "7d", "30d")

#: Each choice's seconds — the ``ttl_s`` the renew endpoint takes.
SECRET_TTL_SECONDS = {
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "7d": 604800,
    "30d": 2592000,
}


def coverage_text(row: dict) -> str:
    """The row's coverage label (#390): ``*`` for the daemon-wide
    row, the covered workspace ids joined for a scoped one — the
    client's own copy of the CLI's label (the client-isolation
    rule keeps the daemon's table out of this process)."""
    workspaces = row.get("workspaces") or []
    return ",".join(sorted(set(workspaces))) if workspaces else "*"


def ttl_text(row: dict, now: datetime | None = None) -> str:
    """The EXPIRES cell (#390): the remaining lifetime's compact
    label — a live countdown the tick repaints — ``never`` for an
    unbounded row, ``expired`` past the deadline, ``-`` for a
    stamp that does not parse."""
    expires = row.get("expires_at")
    if not expires:
        return "never"
    stamp = parse_stamp(expires)
    if stamp is None:
        return "-"
    when = clock_now() if now is None else now
    remaining = (stamp - when).total_seconds()
    return fmt_duration(remaining) if remaining > 0 else "expired"


def dests_cell(row: dict) -> str:
    """The DESTS cell (#390): the row's destinations joined with
    commas — the honest absence for a row that holds none."""
    return ", ".join(row.get("dests") or []) or "-"


def secret_row_cells(
    row: dict, now: datetime | None = None, columns=SECRET_COLUMNS
) -> tuple[str, ...]:
    """The placeholder row's column cells (#390) at the given
    columns' widths, each clipped to its column's width; the
    created cell reads the listing's own relative label (#350)."""
    widths = [width for _label, width in columns]
    return (
        clip(coverage_text(row), widths[0]),
        clip(row.get("name") or "-", widths[1]),
        clip(dests_cell(row), widths[2]),
        clip(ttl_text(row, now), widths[3]),
        clip(created_label(row.get("created_at")), widths[4]),
        clip("locked" if row.get("locked") else "open", widths[5]),
    )


def secret_row_text(
    row: dict, now: datetime | None = None, columns=SECRET_COLUMNS
) -> str:
    """One placeholder listing row's text (#390) at the given
    columns' widths: the padded cells — plain text, never markup,
    so a markup-carrying name cannot shift the columns."""
    return padded_cells(secret_row_cells(row, now, columns), columns).rstrip()


def ttl_default(row: dict) -> str:
    """The renew picker's highlighted choice (#390): the choice
    nearest the row's remaining lifetime — a bounded row renews
    onto something like what it had — the longest choice for an
    unbounded one (the endpoint takes a ttl; the picker offers no
    unbounded renew)."""
    expires = row.get("expires_at")
    stamp = parse_stamp(expires) if expires else None
    if stamp is None:
        return SECRET_TTLS[-1]
    remaining = (stamp - clock_now()).total_seconds()
    return min(
        SECRET_TTLS,
        key=lambda label: abs(SECRET_TTL_SECONDS[label] - remaining),
    )


def revoke_note(row: dict, reply: dict) -> str:
    """The revoke's outcome line (#390): the retired row's label
    (the reply's shape keeps the daemon's id beside it)."""
    del reply
    return f"revoked {escape(coverage_text(row))}/{escape(row['name'])}"


def renew_note(row: dict, reply: dict) -> str:
    """The renew's outcome line (#390): the extended row's label
    with its new remaining lifetime — the page speaks the
    countdown's vocabulary, not the CLI's absolute stamp."""
    label = f"{escape(coverage_text(row))}/{escape(row['name'])}"
    return f"renewed {label} · expires {ttl_text(reply)}"


#: The mint form's daemon-wide choice (#393): one row every
#: accepting workspace honors — the CLI's mint without a
#: ``--workspace`` target, the daemon's own default.
COVERAGE_WIDE = "daemon-wide"

#: The mint form's scoped choice (#393): one row whose coverage
#: set is the workspaces the form's picker holds.
COVERAGE_SCOPED = "scoped"

#: The coverage select's choices (#393): the label names what each
#: pick mints.
COVERAGE_CHOICES = (
    ("daemon-wide — every accepting workspace", COVERAGE_WIDE),
    ("scoped — the picked workspaces", COVERAGE_SCOPED),
)

#: The mint form's TTL choices (#393): the unbounded row first —
#: the daemon's default when a mint carries no ttl — then the
#: renew picker's own span (#390).
MINT_TTLS = ("unbounded", *SECRET_TTLS)

#: The TTL select's choices (#393): the label is the choice.
MINT_TTL_CHOICES = tuple((label, label) for label in MINT_TTLS)

#: The scoped sentinel's prefix (#393): the client's own copy of
#: the daemon's spelling (the client-isolation rule keeps the
#: daemon's secretstore out of this process) — the panel's reach
#: line decodes it.
SCOPED_SENTINEL = "mskssec1_"

#: The daemon-wide sentinel's prefix (#393): every accepting
#: workspace's tap swaps it.
WIDE_SENTINEL = "mskssec2_"

#: The mint form's standing note (#393): its title line, back
#: on the form once a flight ends without a reply — the failure
#: panel owns the refusal (#426), and the note names nothing in
#: the air.
MINT_NOTE = (
    "mint a placeholder — the sentinel shows on the panel;"
    " Enter on a row reopens it"
)

#: The mint name's pattern (#393): the client's own copy of
#: the store's identifier rule — letters, numbers, and underscores,
#: no leading digit.
MINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: One destination entry's pattern (#393): an exact hostname or a
#: label-anchored suffix — the client's own copy of the daemon's
#: rule, so a junk entry is refused on the form where the
#: operator can still edit it.
DEST_ENTRY = re.compile(
    r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)


def split_entries(value: str) -> list[str]:
    """The repeatable entries one comma-separated field carries
    (#393): ``a, b , , c`` becomes ``[a, b, c]`` — the CLI's own
    repeatable, comma-splitting flag shape, held in one input."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def refused_dest(dests: list[str]) -> str | None:
    """The first destination entry the daemon's pattern refuses
    (#393), or None — the check lands here, on the form, so a
    junk entry is named where the operator can still edit it."""
    for entry in dests:
        if not DEST_ENTRY.match(entry.lower().rstrip(".")):
            return entry
    return None


def sentinel_reach(row: dict) -> str:
    """The sentinel's reach line (#393), decoded from its prefix:
    ``mskssec2_`` swaps on every accepting workspace's tap,
    ``mskssec1_`` on the row's own coverage set — the prefix names
    the reach from the string alone (the daemon's own rule,
    duplicated here per the client-isolation rule)."""
    sentinel = row.get("sentinel") or ""
    if sentinel.startswith(SCOPED_SENTINEL):
        return f"the chosen workspaces: {escape(coverage_text(row))}"
    return "every accepting workspace"


def osc52_sequence(text: str) -> str:
    """The OSC 52 clipboard-copy sequence (#393): the payload
    base64-encoded inside the ``52;c;`` selection — a terminal
    that honors OSC 52 answers it by filling its clipboard, over
    ssh included."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def osc52_copy(app, text: str) -> bool:
    """Write the copy sequence through the app's driver (#393):
    the sequence rides beside the frame the driver flushes — a
    terminal that ignores OSC 52 shows nothing and copies
    nothing, and the sentinel stays on the panel. Returns whether
    the sequence left through a driver (a teardown race holds no
    device to write through)."""
    driver = getattr(app, "_driver", None)
    if driver is None:  # a teardown race — nothing to write through
        return False
    driver.write(osc52_sequence(text))
    driver.flush()
    return True


def minted_note(row: dict) -> str:
    """The mint's outcome line (#393): the row's label in the
    page's own vocabulary — the sentinel itself stays on the
    panel above the page."""
    return f"minted {escape(coverage_text(row))}/{escape(row['name'])}"

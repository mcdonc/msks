"""The secrets tab's plain cells (phase 4.6 of #924, #966,
#997): the panes' glue over the presenter's row models — the
column sets and their indexes, the sort-rank items a countdown
or created cell orders by, the standing and flash texts, and
the mint form's local checks (the TUI form's own vocabulary,
mirrored and pinned by parity tests). Plain data and plain
widgets' glue, no tab state in it: the tab module
(:mod:`msks.client.gui.secrets.tab`) owns the flights and the
fills, and this module owns what they render and check.

This module imports PySide6 at module scope — the package's
init imports none of it at parse time (#927), the images
package's own pattern.
"""

import re

from PySide6 import QtCore, QtWidgets

from ..secretrows import (
    MINT_TTLS,
    SecretRow,
    coverage_text,
    ttl_seconds,
    ttl_text,
)

#: The model's place on every row's first cell (#966): the
#: actions read the row's model through it, whatever a rebuild
#: or a sort did to the row slots.
ROW_INDEX_ROLE = QtCore.Qt.ItemDataRole.UserRole

#: The row's own identity on its first cell (#966): the
#: placeholder's id — the renew and revoke key, and the carry
#: key across a rebuild's swap.
ROW_KEY_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

#: A cell's sort rank (#966): the epoch a rank cell orders by —
#: a label's spelling and its order disagree (the countdown
#: across unit boundaries, the created labels across spans).
RANK_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

#: The placeholder table's columns (#966) — the TUI listing's
#: own cells: the coverage (the workspaces the row serves), the
#: mint-time name, the destinations, the live TTL countdown, the
#: created label, and the lock state word.
LIST_COLUMNS = ("COVERAGE", "NAME", "DESTS", "EXPIRES", "CREATED", "STATE")

#: The countdown column (#966): a rank cell — the deadline
#: epoch the presenter lands, the unbounded row last.
EXPIRES_COLUMN = 3

#: The created column (#966): a rank cell — the creation epoch,
#: the undated row last.
CREATED_COLUMN = 4

#: The audit table's columns (#966) — the TUI audit line's own
#: cells: the sighting marker, the local wall-clock label, the
#: kind, the workspace/name identity, and the destination.
AUDIT_COLUMNS = ("!", "TIME", "KIND", "PLACEHOLDER", "DEST")

#: The audit marker column (#966): the ``!`` that marks a
#: sighting — the exfiltration signal, the one row kind the
#: view colors.
MARKER_COLUMN = 0

#: The audit marker header's tooltip (#1064): the ``!``
#: column's meaning in the TUI's own words, and where the
#: marker fires — the daemon's recorded rows are the four
#: lifecycle kinds, so this table's marker column stays blank
#: and the sighting reaches the TUI's audit view live.
MARKER_TIP = (
    "! marks a sighting: a placeholder token reached a host "
    "its mint did not allow — the exfiltration signal. "
    "Sightings stream live to the TUI's audit view; the "
    "recorded rows this table lists are mint, revoke, expiry, "
    "and unlock only."
)


def marker_tip(table: QtWidgets.QTableWidget) -> None:
    """The ``!`` header's tooltip (#1064): the marker's meaning
    on the column's own header, so a hover on the column
    names its reading — the pane carries no standing note
    line, the tooltip is the explanation."""
    item = table.horizontalHeaderItem(MARKER_COLUMN)
    if item is not None:
        item.setToolTip(MARKER_TIP)


#: The first group's label (#966).
LIST_GROUP = "Placeholders"

#: The audit group's label (#966).
AUDIT_GROUP = "Audit"

#: The mint form's daemon-wide choice (#393, the GUI's read):
#: one row every accepting workspace honors.
COVERAGE_WIDE = "daemon-wide"

#: The mint form's scoped choice (#393): one row whose coverage
#: set is the workspaces the form's picker holds.
COVERAGE_SCOPED = "scoped"

#: The coverage picker's choices (#966): the label names what
#: each pick mints — the TUI select's own pair.
COVERAGE_CHOICES = (
    ("daemon-wide — every accepting workspace", COVERAGE_WIDE),
    ("scoped — the picked workspaces", COVERAGE_SCOPED),
)

#: The mint name's pattern (#393, mirrored): letters, numbers,
#: and underscores, no leading digit — the store's identifier
#: rule, checked on the form where the operator can still edit
#: it.
MINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: One destination entry's pattern (#393, mirrored): an exact
#: hostname or a label-anchored suffix — the daemon's own rule,
#: refused on the form.
DEST_ENTRY = re.compile(
    r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)


def split_entries(value: str) -> list[str]:
    """The repeatable entries one comma-separated field carries
    (#393, mirrored): ``a, b , , c`` becomes ``[a, b, c]`` — the
    CLI's own repeatable, comma-splitting flag shape, held in
    one input."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def refused_dest(dests: list[str]) -> str | None:
    """The first destination entry the daemon's pattern refuses
    (#393, mirrored), or None — the check lands on the form, so
    a junk entry is named where the operator can still edit
    it."""
    for entry in dests:
        if not DEST_ENTRY.match(entry.lower().rstrip(".")):
            return entry
    return None


def mint_identity(
    name: str, dests_text: str
) -> tuple[tuple[str, list[str]] | None, str]:
    """The form's name and destinations, checked locally
    (#966 over #393): ``((name, dests), "")`` when both pass,
    ``(None, note)`` with the refusal the form shows — the
    checks are the store's own rules, the notes the form's."""
    if not name:
        return None, "a name is required"
    if not MINT_NAME.match(name):
        return None, "name: letters, numbers, or underscores, no leading digit"
    dests = split_entries(dests_text)
    if not dests:
        return None, "at least one destination is required"
    bad = refused_dest(dests)
    if bad is not None:
        return None, (
            f"dest {bad}: an exact hostname or a suffix like .example.com"
        )
    return (name, dests), ""


def form_value(values: dict, field: str) -> str:
    """One form field's raw value, stripped (#966): blank or
    missing fields read the same — nothing."""
    return (values.get(field) or "").strip()


def scoped_workspaces(values: dict) -> tuple[list | None, str]:
    """The scoped pick's coverage set (#966): the sorted pick,
    or the refusal a scoped pick with nothing in it answers —
    the daemon-wide pick sends no set and no refusal."""
    if values.get("coverage") != COVERAGE_SCOPED:
        return None, ""
    workspaces = sorted(values.get("workspaces") or [])
    if not workspaces:
        return None, "scoped coverage picks at least one workspace"
    return workspaces, ""


def whole_body(
    identity: tuple[str, list[str]],
    workspaces: list | None,
    value: str,
    choice,
) -> dict:
    """The whole body assembled (#966): the identity, the
    scoped pick's set, the value, and the picked ``ttl_s`` —
    the unbounded pick sending none, the daemon's own
    default."""
    body: dict = {"name": identity[0], "dests": identity[1], "value": value}
    if workspaces:
        body["workspaces"] = workspaces
    seconds = ttl_seconds(choice or MINT_TTLS[0])
    if seconds is not None:
        body["ttl_s"] = seconds
    return body


def mint_body(values: dict) -> tuple[dict | None, str]:
    """The mint body from the form's raw values (#966, over
    #393 and #423): the name's shape, at least one destination,
    the scoped coverage's non-empty pick, and a non-empty value
    are checked piecewise — each refusal answers its note and
    lands nothing. The value rides the request and is never
    echoed back."""
    identity, note = mint_identity(
        form_value(values, "name"), form_value(values, "dests")
    )
    if identity is None:
        return None, note
    workspaces, refusal = scoped_workspaces(values)
    if refusal:
        return None, refusal
    value = form_value(values, "value")
    if not value:
        return None, "a value is required"
    return whole_body(identity, workspaces, value, values.get("ttl")), ""


def counts_text(placeholders: list, audit: list) -> str:
    """The standing status after a refresh (#966): the two
    groups' counts, the images tab's own bottom line."""
    return f"{len(placeholders)} placeholders · {len(audit)} audit rows"


def list_empty_text() -> str:
    """The placeholder group's empty state (#966): the TUI
    page's own advice, the Mint action's name in the GUI's
    vocabulary."""
    return "No placeholders — Mint mints one."


def audit_empty_text() -> str:
    """The audit group's empty state (#966): the TUI view's own
    line — what will appear, honestly named."""
    return (
        "No placeholder events yet — mints, revokes, expiries, "
        "and unlocks appear here."
    )


def revoke_question(model: SecretRow) -> str:
    """The revoke's confirmation (#966 over #390): the question
    names the row — its coverage and name — and what a yes does:
    the row retires everywhere at once."""
    return (
        f"revoke {model.coverage}/{model.name}? the row retires "
        "everywhere at once"
    )


def renewed_text(model: SecretRow, reply: dict) -> str:
    """The renew's landing flash (#966): the extended row's
    label with its new remaining lifetime, read off the
    daemon's own reply — the page speaks the countdown's
    vocabulary, not the CLI's absolute stamp, and the label is
    the lifetime the daemon actually wrote."""
    return f"renewed {model.coverage}/{model.name} · expires {ttl_text(reply)}"


def minted_text(row: dict) -> str:
    """The mint's landing flash (#966, #997): the reply row's
    label in the page's own vocabulary — the page carries no
    sentinel display, the CLI's own read owns the token."""
    return f"minted {coverage_text(row)}/{row['name']}"


def revoked_text(model: SecretRow) -> str:
    """The revoke's landing flash (#966): the retired row's
    label."""
    return f"revoked {model.coverage}/{model.name}"


def read_only_item(
    item: QtWidgets.QTableWidgetItem,
) -> QtWidgets.QTableWidgetItem:
    """One cell that renders but never edits (#966, the
    listings' own rule): the groups are views over the daemon's
    state, and an edit would paint a value only the display
    holds."""
    flags = item.flags()
    flags &= ~QtCore.Qt.ItemFlag.ItemIsEditable
    item.setFlags(flags)
    return item


class RankItem(QtWidgets.QTableWidgetItem):
    """One cell that sorts by its carried rank (#966), never by
    its label: the countdown orders by the deadline epoch, the
    created label by the creation epoch — the labels' spelling
    and their order disagree."""

    def __init__(self, text: str, rank) -> None:
        super().__init__(text)
        self.rank = rank

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.rank < other.rank


def ranked_item(cell: str, rank) -> QtWidgets.QTableWidgetItem:
    """One read-only cell, ranked or plain (#966): a rank sorts
    by the carried value, None by the cell's own text — the
    coverage, the name, the destinations, the state, all words
    already."""
    if rank is None:
        item = QtWidgets.QTableWidgetItem(cell)
    else:
        item = RankItem(cell, rank)
    return read_only_item(item)


def list_cells(model: SecretRow) -> tuple[tuple[str, object], ...]:
    """One placeholder model's cells in column order with their
    sort ranks (#966): the countdown by its deadline epoch — the
    unbounded row sorting last beside the unparsable one — the
    created label by its creation epoch, the rest plain
    words."""
    expires_rank = (
        model.expires_ts if model.expires_ts is not None else float("inf")
    )
    return (
        (model.coverage, None),
        (model.name, None),
        (model.dests, None),
        (model.ttl, expires_rank),
        (model.created, model.created_ts),
        (model.state, None),
    )


def audit_cells(model) -> tuple[str, ...]:
    """One audit model's cells in column order (#966): the
    marker, the time label, the kind, the identity, the
    destination — the presenter's own fields, one source for the
    fill."""
    return (model.marker, model.when, model.kind, model.who, model.dest)

"""The secrets tab's presenter (#965, phase 4.6 of #924): the
placeholder listing, the sentinel panel, the mint form's TTL
choices, and the audit view's rows as plain data — daemon rows
and recorded audit rows in, dataclasses out, nothing Qt-bound
in between. A placeholder row carries the id the renew and
revoke actions key on, the mint-time name, the coverage label
(the workspaces the row serves, ``*`` the daemon-wide row —
the workspace the listing shows of it), the destinations cell,
the TTL countdown (the arithmetic the TUI's EXPIRES cell
repaints per tick, mirrored here so the tab's timer reads it
without a daemon call), the created label (the shared
:mod:`msks.client.ages` leaf both listings read), and the lock
state word. The sentinel panel's model masks the fresh or
standing sentinel a row exposes — the prefix names the reach
and holds no bearer power, the body renders one bullet per
character — beside the reach line the prefix decodes. The
helpers the TUI's page owns (the secrets cells, the panel's
mask, and the audit halves of :mod:`msks.client.tui.consent`)
are mirrored here — the GUI imports nothing from the TUI, and
the suite pins the pair's parity — so the module holds no
PySide6 import and the coverage gate lands on it whole.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ..ages import clock_now, created_label, parse_stamp

#: The renew picker's ttl choices (#390): the duration picker
#: over TTL-appropriate labels — the hours-to-a-month span a
#: shared credential lives, not the verdict picker's
#: minutes-to-forever.
SECRET_TTLS = ("1h", "6h", "1d", "7d", "30d")

#: Each choice's seconds — the ``ttl_s`` the renew endpoint
#: takes, and the mint's when its form picks a bound.
SECRET_TTL_SECONDS = {
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "7d": 604800,
    "30d": 2592000,
}

#: The mint form's TTL choices (#393): the unbounded row first —
#: the daemon's default when a mint carries no ttl — then the
#: renew picker's own span (#390).
MINT_TTLS = ("unbounded", *SECRET_TTLS)

#: The scoped sentinel's prefix (#393): the client's own copy of
#: the daemon's spelling (the client-isolation rule keeps the
#: daemon's secretstore out of this process) — the panel's mask
#: and reach line decode it.
SCOPED_SENTINEL = "mskssec1_"

#: The daemon-wide sentinel's prefix (#393): every accepting
#: workspace's tap swaps it.
WIDE_SENTINEL = "mskssec2_"

#: The audit kind filter's choices (#390): every kind a row can
#: carry, in the issue's own order — the vocabulary the view's
#: picker reads beside its no-filter word.
AUDIT_KINDS = ("swap", "sighting", "mint", "revoke", "expiry", "unlock")

#: The audit filter's no-filter sentinel (#510).
FILTER_ALL = "all"


@dataclass(frozen=True)
class SecretRow:
    """One placeholder row as the secrets tab renders it (#965):
    the id the renew and revoke actions key on, the mint-time
    name, the coverage label (the workspaces the row serves),
    the destinations cell, the TTL countdown, the created label,
    and the lock state word — the whole row the widget needs to
    render, as plain data with nothing Qt-bound in it."""

    id: int
    name: str
    coverage: str
    dests: str
    ttl: str
    created: str
    state: str


@dataclass(frozen=True)
class SentinelModel:
    """The sentinel panel's model (#965, over #393 and #440): the
    row's label, the sentinel masked for display (the prefix
    names the reach, the body renders one bullet per character),
    and the reach line the prefix decodes. The real sentinel
    stays off the model — the reveal and the copy read the row
    the panel was opened on, and the model puts no bearer power
    on screen."""

    label: str
    masked: str
    reach: str


@dataclass(frozen=True)
class AuditRow:
    """One recorded audit row as the audit view renders it
    (#965, over #390): the marker (``!`` on a sighting — the
    exfiltration signal, the row kind the view highlights), the
    local wall-clock label beside its epoch (the sort key — an
    undated row keeps a blank column and sorts undated), the
    kind, the workspace/name identity, the destination text,
    and the coverage the filters read. Plain data with nothing
    Qt-bound in it."""

    audit_id: int | None
    kind: str
    marker: str
    ts: float
    when: str
    who: str
    dest: str
    workspaces: tuple[str, ...]


def coverage_text(row: dict) -> str:
    """The row's coverage cell (#965, over #390): ``*`` on the
    daemon-wide row, the covered workspace ids joined on a
    scoped one — sorted and deduplicated, whatever order the
    row carried. The client keeps its own copy of the label
    (the client-isolation rule keeps the daemon's table out of
    this process)."""
    workspaces = row.get("workspaces") or []
    return ",".join(sorted(set(workspaces))) if workspaces else "*"


def ttl_text(row: dict, now: datetime | None = None) -> str:
    """The TTL cell (#965, over #390): the remaining lifetime as
    the countdown's compact label — the tab's timer repaints it
    from the cached row, no daemon call per tick. An unbounded
    row reads ``never``, a past deadline ``expired``, a stamp
    that does not parse its dash."""
    expires = row.get("expires_at")
    if not expires:
        return "never"
    stamp = parse_stamp(expires)
    if stamp is None:
        return "-"
    when = now if now is not None else clock_now()
    remaining = (stamp - when).total_seconds()
    if remaining <= 0:
        return "expired"
    return fmt_duration(remaining)


def dests_cell(row: dict) -> str:
    """The destinations cell (#390): the row's destinations
    joined with commas — the honest absence for a row that
    holds none."""
    return ", ".join(row.get("dests") or []) or "-"


def state_text(row: dict) -> str:
    """The state cell: ``locked`` when the daemon's cache holds
    no plaintext for the row — the flag the listing surfaces so
    a cold row is seen, not discovered at the swap's refusal —
    ``open`` beside it."""
    return "locked" if row.get("locked") else "open"


def fmt_duration(secs: float) -> str:
    """A compact remaining-time label: ``45s``, ``5m``, ``2h``,
    ``3d`` — the TUI's own ladder, mirrored so the two clients
    count a remaining lifetime alike."""
    whole = int(secs)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    if whole < 86400:
        return f"{whole // 3600}h"
    return f"{whole // 86400}d"


def ttl_default(row: dict) -> str:
    """The renew picker's opening choice (#965, over #390): the
    choice nearest the row's remaining lifetime — a bounded row
    renews onto something like what it had. A row that parses
    no deadline takes the longest choice: the endpoint takes a
    ttl, and the picker offers no unbounded renew."""
    stamp = parse_stamp(row.get("expires_at") or "")
    if stamp is None:
        return SECRET_TTLS[-1]
    remaining = (stamp - clock_now()).total_seconds()
    return min(
        SECRET_TTLS,
        key=lambda choice: abs(SECRET_TTL_SECONDS[choice] - remaining),
    )


def ttl_seconds(choice: str) -> int | None:
    """The mint form's choice into the body's ``ttl_s`` (#393):
    the unbounded pick sends nothing — the daemon's own default
    — and every other pick its seconds."""
    return None if choice == MINT_TTLS[0] else SECRET_TTL_SECONDS[choice]


def secret_row(row: dict, now: datetime | None = None) -> SecretRow:
    """One daemon placeholder row as its listing model (#965):
    the cells the row renders, with the id the actions key on —
    the countdown and the created label read the clock the
    caller pins (the shared leaf's own arithmetic, the tab's
    timer supplying none)."""
    return SecretRow(
        id=row["id"],
        name=row.get("name") or "-",
        coverage=coverage_text(row),
        dests=dests_cell(row),
        ttl=ttl_text(row, now),
        created=created_label(row.get("created_at"), now),
        state=state_text(row),
    )


def secret_rows(rows: list[dict], now: datetime | None = None) -> list:
    """The listing's row models (#965) in the daemon's own order
    — the listing's rule, not the catalog's sort; the widget's
    columns order rows when the operator asks."""
    return [secret_row(row, now) for row in rows]


def masked_sentinel(sentinel: str) -> str:
    """The sentinel's masked display (#393): the prefix stays —
    it names the reach, holds no bearer power — and the body
    renders one bullet per character, so the panel opens without
    putting the token on screen. A sentinel under neither prefix
    masks whole."""
    if not sentinel:
        return ""
    if sentinel.startswith(SCOPED_SENTINEL):
        prefix = SCOPED_SENTINEL
    elif sentinel.startswith(WIDE_SENTINEL):
        prefix = WIDE_SENTINEL
    else:
        return "\u2022" * len(sentinel)
    return prefix + "\u2022" * (len(sentinel) - len(prefix))


def sentinel_reach(row: dict) -> str:
    """The sentinel's reach line (#393), decoded from its prefix:
    ``mskssec2_`` swaps on every accepting workspace's tap,
    ``mskssec1_`` on the row's own coverage set — the prefix
    names the reach from the string alone (the daemon's own
    rule, duplicated here per the client-isolation rule)."""
    sentinel = row.get("sentinel") or ""
    if sentinel.startswith(SCOPED_SENTINEL):
        return f"the chosen workspaces: {coverage_text(row)}"
    return "every accepting workspace"


def sentinel_model(row: dict) -> SentinelModel:
    """The panel's model over one row (#965): the label the
    panel heads with, the masked display it opens on, and the
    reach line beneath — the row a mint's reply carried, or the
    on-demand fetch's (#440)."""
    return SentinelModel(
        label=f"{coverage_text(row)}/{row['name']}",
        masked=masked_sentinel(row.get("sentinel") or ""),
        reach=sentinel_reach(row),
    )


def string_tuple(value: object) -> tuple[str, ...]:
    """A row field as a tuple of strings, () for anything else."""
    return (
        tuple(str(entry) for entry in value) if isinstance(value, list) else ()
    )


def int_field(value: object) -> int | None:
    """A row field as an int, with bools and junk excluded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def audit_stamp_epoch(iso: object) -> float:
    """A recorded row's ISO stamp as epoch (#965, over #390) —
    the domain the audit rows sort in. The daemon stores naive
    UTC on the sqlite round-trip; an offset stamp converts, and
    anything else reads 0.0, the undated floor a row that
    renders blank sorts at."""
    if not isinstance(iso, str):
        return 0.0
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def audit_time(ts: float) -> str:
    """The audit row's local wall-clock label; an undated row (a
    stamp that does not parse) keeps a blank column."""
    if ts <= 0.0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def audit_dests(kind: str, row: dict) -> tuple[str, ...]:
    """The recorded row's allowlist — the mint's alone (the
    daemon's own replay rule: revoke and expiry carry the
    identity alone, so a replayed row renders exactly as its
    live delivery)."""
    if kind != "mint":
        return ()
    return string_tuple(row.get("dests"))


def dest_text(dests: tuple[str, ...]) -> str:
    """The audit row's destination text: the allowlist joined
    after the arrow — or nothing, on the kinds that carry the
    identity alone."""
    return f" \u2192 {', '.join(dests)}" if dests else ""


def audit_identity(row: dict) -> tuple[str, str] | None:
    """The row's kind and name — the pair every audit row keys
    on — or None when either side is missing or not a string
    (the tolerance the TUI's replay carries: such a row holds
    nothing to render)."""
    kind = row.get("kind")
    name = row.get("name")
    if isinstance(kind, str) and isinstance(name, str):
        return kind, name
    return None


def audit_row(row: object) -> AuditRow | None:
    """One recorded audit row (``GET /api/v1/secrets/audit``,
    #390) as the view's model, or None on an unusable shape: a
    row that is not a mapping, or whose kind or name is not a
    string, holds nothing to render. The row's coverage reads
    as the live frame's spellings: the first covered id, ``*``
    on the daemon-wide row."""
    if not isinstance(row, dict):
        return None
    identity = audit_identity(row)
    if identity is None:
        return None
    kind, name = identity
    workspaces = string_tuple(row.get("workspaces"))
    ts = audit_stamp_epoch(row.get("created_at"))
    return AuditRow(
        audit_id=int_field(row.get("id")),
        kind=kind,
        marker="!" if kind == "sighting" else " ",
        ts=ts,
        when=audit_time(ts),
        who=f"{workspaces[0] if workspaces else '*'}/{name}",
        dest=dest_text(audit_dests(kind, row)),
        workspaces=workspaces,
    )


def audit_rows(rows: list) -> list[AuditRow]:
    """The audit view's row models (#965) newest first — the
    TUI's own display order: the stamps order the rows (ties in
    recording order), then the view flips them so the newest
    stands at the top. A row the shape refuses holds no place."""
    built = [
        model
        for model in (audit_row(row) for row in reversed(rows))
        if model is not None
    ]
    built.sort(key=lambda model: model.ts)
    built.reverse()
    return built

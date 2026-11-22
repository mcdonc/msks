"""The secrets tab's presenter (#965, phase 4.6 of #924, the
sentinel model gone #997): the placeholder listing, the mint
form's TTL choices, and the audit view's rows as plain data —
daemon rows and recorded audit rows in, dataclasses out,
nothing Qt-bound in between. A placeholder row carries the id
the renew and revoke actions key on, the mint-time name, the
coverage label (the workspaces the row serves, ``*`` the
daemon-wide row — the workspace the listing shows of it), the
destinations cell, the TTL countdown (the arithmetic the TUI's
EXPIRES cell repaints per tick, mirrored here) beside the
deadline epoch the tab's timer repaints it from — no daemon
call per tick — the created label (the shared
:mod:`msks.client.ages` leaf both listings read) beside its
own epoch for the column sort, and the lock state word. The
helpers the TUI's page owns (the secrets cells and the audit
halves of :mod:`msks.client.tui.consent`) are mirrored here —
this module imports nothing from the TUI (the decider link's
own controller share, #999, is the exception the gui package
carries), and the suite pins the pair's parity — so the module
holds no PySide6 import and the coverage gate lands on it
whole.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from ..ages import clock_label, clock_now, created_label, parse_stamp

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

#: The audit kind filter's choices (#390): every kind a row can
#: carry, in the issue's own order. The recorded listing serves
#: the lifecycle kinds alone (mint, revoke, expiry, unlock) —
#: swaps and sightings arrive on the live stream, and the
#: filter takes both deliveries' kinds.
AUDIT_KINDS = ("swap", "sighting", "mint", "revoke", "expiry", "unlock")

#: The audit filter's no-filter sentinel (#510).
FILTER_ALL = "all"


@dataclass(frozen=True)
class SecretRow:
    """One placeholder row as the secrets tab renders it (#965):
    the id the renew and revoke actions key on, the mint-time
    name, the coverage label (the workspaces the row serves),
    the destinations cell, the TTL countdown, the created label,
    and the lock state word — beside the deadline and creation
    epochs the tab's timer and column sorts read. ``expires_ts``
    is None when the row carries no countdown (an unbounded
    row, or a stamp that does not parse — the frozen ``ttl``
    cell holds the distinction, ``never`` against the dash),
    and such a row's cell never recomputes; ``created_ts``
    floors at zero, where a row without a usable stamp sorts
    below every dated one. Plain data, nothing Qt-bound."""

    id: int
    name: str
    coverage: str
    dests: str
    ttl: str
    expires_ts: float | None
    created: str
    created_ts: float
    state: str


@dataclass(frozen=True)
class AuditRow:
    """One recorded audit row as the audit view renders it
    (#965, over #390): the marker (``!`` on a sighting — the
    exfiltration signal, the row kind the view highlights),
    the local wall-clock label beside its epoch (the sort
    key; a row from an earlier day carries its date beside
    the clock (#1064), and an undated row keeps a blank
    column and sorts undated), the kind, the workspace/name
    identity, the destination text, and the coverage the
    filters read. Plain data with nothing Qt-bound in it."""

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
    """The TTL cell over a daemon row (#965, over #390): the
    remaining lifetime as the countdown's compact label. An
    unbounded row reads ``never``, a past deadline ``expired``,
    a stamp that does not parse its dash."""
    expires = row.get("expires_at")
    if not expires:
        return "never"
    stamp = parse_stamp(expires)
    if stamp is None:
        return "-"
    return countdown(stamp.timestamp(), now)


def countdown(expires_ts: float, now: datetime | None = None) -> str:
    """The countdown over the model's deadline epoch (#965): the
    label the tab's timer repaints each tick from the cached
    model, no daemon call and no re-parse — ``expired`` past
    the deadline, the compact countdown between now and it.
    The clock the caller pins reads as an aware moment (the
    shared leaf's clock is); a naive one would read as local
    time."""
    when = now if now is not None else clock_now()
    remaining = expires_ts - when.timestamp()
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


def stamp_epoch(row: dict, field: str) -> float | None:
    """One row's stamp field as epoch (#965) — None when the row
    carries no stamp or the stamp does not parse, the shared
    leaf's own floors."""
    stamp = parse_stamp(row.get(field) or "")
    return stamp.timestamp() if stamp is not None else None


def secret_row(row: dict, now: datetime | None = None) -> SecretRow:
    """One daemon placeholder row as its listing model (#965):
    the cells the row renders, with the id the actions key on —
    the countdown and the created label read the clock the
    caller pins (the shared leaf's own arithmetic, the tab's
    timer supplying none), and the epochs land beside them for
    the tick and the sorts."""
    return SecretRow(
        id=row["id"],
        name=row.get("name") or "-",
        coverage=coverage_text(row),
        dests=dests_cell(row),
        ttl=ttl_text(row, now),
        expires_ts=stamp_epoch(row, "expires_at"),
        created=created_label(row.get("created_at"), now),
        created_ts=stamp_epoch(row, "created_at") or 0.0,
        state=state_text(row),
    )


def secret_rows(
    rows: list[dict], now: datetime | None = None
) -> list[SecretRow]:
    """The listing's row models (#965) in the daemon's own order
    — the listing's rule, not the catalog's sort; the widget's
    columns order rows when the operator asks (the epochs this
    module lands beside the labels are the sort keys)."""
    return [secret_row(row, now) for row in rows]


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


def audit_time(ts: float, now: float | None = None) -> str:
    """The audit row's local wall-clock label (#1064): the
    clock alone on a row from the viewer's own day, the date
    beside the clock on a row from an earlier day — the
    shared leaf's own spelling
    (:func:`msks.client.ages.clock_label`). An undated row (a
    stamp that does not parse) keeps a blank column."""
    return clock_label(ts, now)


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

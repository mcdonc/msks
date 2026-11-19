"""The workspace mounts tab's presenter (phase 5.7 of #924,
#987): the editing list's plain arithmetic — the add, the
replace, and the remove that compose the next whole list —
beside the parse that turns the form's fields into the wire's
mount entry through the CLI's own spec parser
(:func:`msks.client.workspaces.mount_spec`), and the notes the
tab reads: the save's next-start note and the standing words
for a configured set the last start has not attached. Plain
data, no Qt in it, mirroring ``tui/mounts.py``'s own pure
helpers; the rows the table renders are the detail presenter's
own (:func:`msks.client.gui.detail.mount_entries`).
"""

from ..workspaces import mount_spec

#: The form's mode choices (#886's own set): the daemon's two,
#: ``rw`` the default.
MODE_CHOICES = ("rw", "ro")

#: The save note's next-start half (#756's own words, the TUI
#: editor's note): what a saved list waits for.
SAVED_PENDING = "saved — applies at the next start"

#: The save note with nothing waiting.
SAVED = "saved"

#: The standing pending words (#987): the daemon's
#: ``mounts_pending`` flag stands while the configured set
#: differs from the set the last start attached, and the note
#: keys on it until a start attaches the configured set.
PENDING_WORDS = (
    "the configured set differs from the attached one — the "
    "next start attaches it"
)


def mount_entry(host: str, guest: str, mode: str) -> tuple[dict | None, str]:
    """The form's fields as the wire's mount entry (#987, the
    TUI form's own path): the CLI's own parser over the composed
    spec, so a local refusal names itself in the same words the
    command prints and the daemon never sees a half-parsed body.
    The two required fields say themselves first — the parser's
    own refusals name the shape, not the blank. A local refusal
    answers ``(None, note)``; a whole entry ``((entry), "")``."""
    if not host:
        return None, "the host path is the form's one required field"
    if not guest:
        return None, "the guest path is the form's one required field"
    try:
        return mount_spec(f"{host}:{guest}:{mode}", "mount"), ""
    except SystemExit as exc:
        return None, str(exc).replace("msks: ", "refused: ")


def dialog_seeds(entry: dict | None) -> dict:
    """The form's seeded values (#987): the edited mount's own
    fields, blanks for the add — the mode defaulting to ``rw``
    on both paths."""
    seeded = dict(entry or {})
    return {
        "host": seeded.get("host") or "",
        "guest": seeded.get("guest") or "",
        "mode": seeded.get("mode") or "rw",
    }


def added_mount(mounts: list, entry: dict) -> list:
    """The list with one entry joined at its end (#987): the add
    composes the next whole list the save posts."""
    return [*mounts, entry]


def replaced_mount(mounts: list, index: int, entry: dict) -> list:
    """The list with one slot taking the new entry (#987): the
    edit composes the next whole list. An index the list no
    longer carries leaves the list unchanged — the selection
    moved under the edit, and the save decides nothing."""
    if not 0 <= index < len(mounts):
        return list(mounts)
    return [
        entry if slot == index else candidate
        for slot, candidate in enumerate(mounts)
    ]


def removed_mount(mounts: list, index: int) -> list:
    """The list with one slot gone (#987): the remove composes
    the next whole list. An index the list no longer carries
    leaves the list unchanged."""
    if not 0 <= index < len(mounts):
        return list(mounts)
    return [
        candidate for slot, candidate in enumerate(mounts) if slot != index
    ]


def saved_note(row: dict) -> str:
    """The save's note (#987, the TUI editor's own words): the
    next-start half while the daemon's refreshed row says the
    configured set waits for a start, the plain word when the
    two agree."""
    return SAVED_PENDING if row.get("mounts_pending") else SAVED


def pending_words(row: dict) -> str | None:
    """The tab's standing note (#987): the words while the
    configured set differs from the set the last start attached,
    ``None`` when the two agree again."""
    return PENDING_WORDS if row.get("mounts_pending") else None

"""The TUI's mounts surfaces (#620, part #624; #667): the
workspace page's mounts line and the list editor screen.

The editor edits the row's whole mount list — a workspace mounts
several mounts at once — one ``HOST_PATH:GUEST_PATH[:MODE]``
row each (the mode defaults to ``rw``), with add, edit, and remove
per row. The add and edit paths share one form (#886) — the
proxy add form's shape over the mount's own fields, its fields
composed into one spec the CLI's own parser reads. Every change
posts the full list through the daemon's
mounts route (next-start semantics, like resize: a running
workspace keeps its attached devices; the next start attaches the
new set), and the daemon's reply — the refreshed row with its
derived per-mount state — repaints the editor and the page behind
it. The derived state rides every row: ``healthy`` mounts attach
at the next start; a ``skipped`` mount names its reason, and the
reason stands until the config and the row agree again.
"""

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    ListItem,
    ListView,
    Select,
    Static,
)

from ..workspaces import mount_spec
from .consent_ui import flash_safe
from .detail import status_line
from .forms import FormSelect, SeededForm
from .rows import (
    COLUMN_GAP,
    cell_pad,
    clip,
    list_header,
    workspace_label,
)

#: The editor's columns (#886): the spec beside its derived
#: state — the proxies page's own frame, at widths the 80-column
#: terminal holds (the widest spec clips its middle; the state's
#: reason clips its tail).
MOUNT_COLUMNS = (
    ("SPEC", 44),
    ("STATE", 26),
)


def mount_spec_text(mount: dict) -> str:
    """One mount row's spec: ``HOST_PATH:GUEST_PATH:MODE`` — the
    words the CLI's ``--share`` takes, the mode always shown so
    the row reads whole (the wire's default is ``rw``)."""
    mode = mount.get("mode") or "rw"
    return f"{mount.get('host')}:{mount.get('guest')}:{mode}"


def issue_for(mount: dict, issues: list[dict]) -> dict | None:
    """The derived issue naming one mount, if any: matched on the
    host path beside the guest path, the pair a row names."""
    for issue in issues or []:
        if issue_names(issue, mount):
            return issue
    return None


def issue_names(issue: dict, mount: dict) -> bool:
    """Whether an issue frame names one mount: the host path and
    the guest path agree."""
    return issue.get("host") == mount.get("host") and issue.get(
        "guest"
    ) == mount.get("guest")


def mount_cells(mount: dict, issues: list[dict]) -> tuple[str, ...]:
    """One editor row's cells at the columns' widths: the spec
    beside its derived state — ``healthy`` attaches at the next
    start; anything else names the kind and the reason."""
    issue = issue_for(mount, issues)
    state = (
        "healthy" if issue is None else f"{issue['kind']}: {issue['detail']}"
    )
    widths = [width for _label, width in MOUNT_COLUMNS]
    return (clip(mount_spec_text(mount), widths[0]), clip(state, widths[1]))


def mount_row_text(mount: dict, issues: list[dict]) -> str:
    """One editor row's paint: the padded cells, the proxies
    page's own row shape."""
    return COLUMN_GAP.join(
        cell_pad(cell, width)
        for cell, (_label, width) in zip(
            mount_cells(mount, issues), MOUNT_COLUMNS, strict=True
        )
    ).rstrip()


def mounts_line(row: dict, theme_variables: dict | None = None) -> Content:
    """The workspace page's mounts line (#620, part #624, #752,
    #756): one entry per mount joined with the status lines' one
    grammar — the ``mounts`` label muted in the label column, a
    ``·`` between the value's parts — with a skipped mount's
    kind riding beside its spec and the next-start note joining
    only while a change is pending. Empty when the workspace
    mounts nothing."""
    mounts = row.get("mounts") or []
    if not mounts:
        return Content("")
    issues = row.get("mount_issues") or []
    parts = [mount_state_text(mount, issues) for mount in mounts]
    value = " · ".join(parts) + pending_note(row)
    return status_line("mounts", value, theme_variables)


def pending_note(row: dict) -> str:
    """The line's next-start part (#756): the daemon's
    ``mounts_pending`` flag stands while the configured set
    differs from the set the last start attached (an edit the
    next start takes, a mount never yet attached), and the note
    clears when the two agree again."""
    if row.get("mounts_pending"):
        return " · applies at next start"
    return ""


def replaced_mount(mounts: list, mount: dict | None, entry: dict) -> list:
    """The edited list: the focused mount's slot takes the new
    entry. A focus the list no longer carries (a swap window the
    pilot cannot arrange) leaves the list unchanged."""
    if mount is None:  # pragma: no cover
        return mounts
    return [entry if candidate is mount else candidate for candidate in mounts]


def resolved_mount(mounts: list, mount: dict) -> dict | None:
    """The pressed row's mount on the working list, re-resolved
    by its (host, guest) pair — a reply that landed during the
    remove's wait re-created every dict, and the pair is the
    wire's own identity (the one the derived issues match on)."""
    return next(
        (
            candidate
            for candidate in mounts
            if candidate.get("host") == mount.get("host")
            and candidate.get("guest") == mount.get("guest")
        ),
        None,
    )


def clamp_index(focused: int, count: int) -> int | None:
    """The rebuilt list's focused index: the row focus kept, or
    the last row when the list shrank under it; None when the
    list is empty."""
    if not count:
        return None
    return min(focused, count - 1)


def mount_state_text(mount: dict, issues: list[dict]) -> str:
    """One mount's line entry: its spec alone when healthy, its
    kind riding beside with the status line's ``·`` join when the
    daemon derived a skip."""
    issue = issue_for(mount, issues)
    state = "" if issue is None else f" · {issue['kind']}"
    return f"{mount_spec_text(mount)}{state}"


def form_note(verb: str) -> str:
    """The form's standing note (#886): the one grammar the
    CLI's own ``msks mount`` reads — the proxy add form's note
    shape, the verb naming the path that opened the form."""
    return (
        f"{verb} a mount — the same specs `msks mount` reads\n"
        "HOST_PATH:GUEST_PATH[:MODE] · the host path is absolute "
        "or ~-shaped\n"
        "MODE ro or rw · rw is the default"
    )


class MountFormScreen(SeededForm):
    """The mount form (#886): the proxy add form's shape over the
    mount's own fields — the workspace fixed at the page that
    opened it, the host path, the guest path, and the mode. The
    fields compose into one spec that parses with the CLI's own
    parser, so a refusal here names itself on the note in the
    same words the command prints and the daemon never sees a
    half-parsed body. An entry that lands dismisses with it; the
    pushing editor owns the exchange. The walk's keys and the
    edit seed live on the shared :class:`SeededForm` base."""

    def compose(self) -> ComposeResult:
        seeded = self.seed or {}
        with Vertical(id="form"):
            yield Static(
                form_note("edit" if self.seed else "add"),
                id="form-note",
            )
            with Horizontal(classes="form-row"):
                yield Static("workspace", classes="form-label")
                # The workspace is the editor's own, shown as the
                # name it reads — no control, so the walk starts
                # at the host path and never visits a fixed
                # field.
                yield Static(workspace_label(self.scope), id="field-workspace")
            with Horizontal(classes="form-row"):
                yield Static("host path", classes="form-label")
                yield Input(
                    value=seeded.get("host", ""),
                    placeholder="/srv/refs",
                    id="field-host",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("guest path", classes="form-label")
                yield Input(
                    value=seeded.get("guest", ""),
                    placeholder="/mnt/refs",
                    id="field-guest",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("mode", classes="form-label")
                yield FormSelect(
                    [
                        ("read-write (rw)", "rw"),
                        ("read-only (ro)", "ro"),
                    ],
                    value=seeded.get("mode") or "rw",
                    allow_blank=False,
                    id="field-mode",
                    compact=True,
                )
            with Horizontal(id="form-buttons"):
                yield Button(
                    "Save" if self.seed else "Add",
                    id="do-add",
                    variant="primary",
                    compact=True,
                )
                yield Button("Cancel", id="do-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#field-host", Input).focus()

    def note(self, text: str) -> None:
        """The form's note line: its standing rule, or the local
        refusal that keeps a half-parsed body home."""
        self.query_one("#form-note", Static).update(text)

    def submit(self) -> None:
        """Hand the parsed entry to the callback, or keep the form
        on a local refusal — the CLI's own parser and its words
        (the ``msks: `` prefix aside, the note owns the line)."""
        host = self.query_one("#field-host", Input).value.strip()
        if not host:
            self.note("the host path is the form's one required field")
            return
        guest = self.query_one("#field-guest", Input).value.strip()
        if not guest:
            self.note("the guest path is the form's one required field")
            return
        mode = str(self.query_one("#field-mode", Select).value)
        try:
            entry = mount_spec(f"{host}:{guest}:{mode}", "mount")
        except SystemExit as exc:
            # The parser's message embeds the raw spec; a markup
            # tag in it must not crash the note's Static.
            self.note(flash_safe(str(exc).replace("msks: ", "refused: ")))
            return
        self.dismiss_with(entry)


class MountsScreen(ModalScreen[dict | None]):
    """The mounts list editor (#620, part #624): the workspace's
    mounts one row each, add (``a``), edit (Enter),
    remove (``D`` — the workspaces list's own remove letter)
    — every change posts the whole list and the daemon's reply
    repaints the rows with their derived state. Escape closes;
    the page behind takes the refreshed row."""

    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("D", "remove", "Del"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, row: dict, data) -> None:
        super().__init__()
        self.row = row
        self.data = data
        # The exchanges' turnstile: one read-mutate-post at a
        # time, the old exclusive worker's own rule carried to
        # the form's task callbacks — a second change that
        # arrives mid-flight waits, so a late-landing reply can
        # never roll a later change back out of the working list
        # (its snapshot is taken under the same lock).
        self.exchanges = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static(id="mount-status")
        with Vertical(id="mounts-listing"):
            # The empty state rides the panel's top — an empty
            # list still grows to fill the panel, so a message
            # after it lands at the bottom, away from the eye.
            yield Static("", id="mount-empty")
            yield Static(list_header(MOUNT_COLUMNS), id="mount-columns")
            yield ListView(id="mount-rows")
        yield Static("", id="mount-note")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.rebuild_rows)

    def mounts(self) -> list[dict]:
        """The editor's working list — the row's mounts."""
        return list(self.row.get("mounts") or [])

    def rows_widget(self) -> ListView | None:
        """The rows list, or None in a rebuild's swap window."""
        try:
            return self.query_one("#mount-rows", ListView)
        except NoMatches:
            return None

    async def rebuild_rows(self) -> None:
        """Swap in a freshly-built rows list (its mount awaited),
        preserving the focused row by index — the consent
        queue's rebuild rule, carried to the editor: a
        clear-and-append rebuild double-adds rows when two land
        in one refresh window (the clear misses appends still
        queued), and the awaited swap keeps every repaint whole."""
        old = self.rows_widget()
        if old is None:
            return  # compose has not built the editor yet
        focused = old.index if old.index is not None else 0
        fresh = ListView(*self.editor_items(), id="mount-rows")
        listing = self.query_one("#mounts-listing", Vertical)
        await old.remove()  # frees the id before the fresh list mounts
        await listing.mount(fresh)
        fresh.index = clamp_index(focused, len(self.mounts()))
        fresh.focus()
        self.sync_status()

    def sync_status(self) -> None:
        """The editor's status line and empty state (#886) — the
        proxies page's own frame: the mount count beside the
        workspace's name, the columns standing only while rows
        do, and the empty state naming the add key."""
        count = len(self.mounts())
        word = "mount" if count == 1 else "mounts"
        self.query_one("#mount-status", Static).update(
            f" {count} {word} · {workspace_label(self.row)} (next start)"
        )
        self.query_one("#mount-columns", Static).display = bool(count)
        empty = self.query_one("#mount-empty", Static)
        empty.display = not count
        empty.update("No mounts — a adds one.")

    def editor_items(self) -> list[ListItem]:
        """The list's rows: one per mount with its derived state."""
        issues = self.row.get("mount_issues") or []
        return [
            ListItem(Static(mount_row_text(mount, issues)))
            for mount in self.mounts()
        ]

    def note(self, text: str) -> None:
        """The editor's note line: what the last change did, or
        why the daemon refused one."""
        self.query_one("#mount-note", Static).update(text)

    def focused_mount(self) -> dict | None:
        """The row under focus as its mount, None when nothing is
        focused (an empty listing, or a rebuild's swap window)."""
        rows = self.rows_widget()
        mounts = self.mounts()
        if rows is None or rows.index is None or not mounts:
            return None
        if rows.index >= len(mounts):
            # The list shrank under the focus between the rebuild
            # and this read — a swap window the pilot cannot
            # arrange deterministically.
            return None  # pragma: no cover
        return mounts[rows.index]

    async def post(self, before: list | None = None) -> None:
        """Post the working list whole and land the daemon's
        reply: the refreshed row repaints the rows' derived state.
        A refusal names itself on the note line and rolls the
        working list back to *before* — the snapshot the edit
        handed in, taken ahead of its own mutation — the daemon is
        the authority, and a refused edit must not ride a later
        save or the page's line."""
        undo = self.mounts() if before is None else before
        try:
            fresh = await self.data.set_mounts(self.row["id"], self.mounts())
        except (Exception, SystemExit) as exc:
            reason = f"refused: {exc}"
            self.row["mounts"] = undo
            await self.rebuild_rows()
            self.call_after_refresh(lambda: self.note(reason))
            return
        self.row = fresh
        await self.rebuild_rows()
        self.call_after_refresh(
            lambda: self.note(
                "saved — applies at the next start"
                if fresh.get("mounts_pending")
                else "saved"
            )
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row edits it — the list owns the key (the
        binding is the same action for a focus walk that lands
        elsewhere)."""
        self.action_edit()

    def action_add(self) -> None:
        """``a``: the form over the editor, the workspace fixed
        to the page's own — the form's callback runs the exchange
        (async, the proxy form's rule)."""
        self.app.push_screen(MountFormScreen(self.row, self.added))

    async def added(self, entry: dict | None) -> None:
        """The add form's callback: an entry joins the list's
        end, a cancel decides nothing."""
        if entry is None:
            return
        async with self.exchanges:
            before = self.mounts()
            self.row["mounts"] = [*before, entry]
            await self.post(before)

    def action_edit(self) -> None:
        """Enter on a row opens the form seeded with the row's
        mount."""
        mount = self.focused_mount()
        if mount is None:
            return
        self.app.push_screen(
            MountFormScreen(self.row, self.edited, seed=mount)
        )

    async def edited(self, entry: dict | None) -> None:
        """The edit form's callback: the focused row's mount takes
        the new entry."""
        if entry is None:
            return
        async with self.exchanges:
            mount = self.focused_mount()
            before = self.mounts()
            self.row["mounts"] = replaced_mount(before, mount, entry)
            await self.post(before)

    def action_remove(self) -> None:
        """``D``: remove the focused row's mount — the remaining
        list posts whole."""
        mount = self.focused_mount()
        if mount is None:
            return
        # Referenced under its own name (NOT ``_task`` — the
        # DOM's prune hook: a node's ``_task`` is awaited at its
        # removal, and a remove parked on the turnstile there
        # would park the pump with it).
        self._remove_flight = asyncio.create_task(self.removed(mount))

    async def removed(self, mount: dict) -> None:
        """The remove's exchange, through the same turnstile — a
        remove that lands beside an in-flight add waits for it,
        re-resolves its row by pair (:func:`resolved_mount`), and
        a pair that left the list another way decides quietly
        (nothing to remove)."""
        async with self.exchanges:
            before = self.mounts()
            target = resolved_mount(before, mount)
            if target is None:
                return
            self.row["mounts"] = [
                candidate for candidate in before if candidate is not target
            ]
            await self.post(before)

    def action_close(self) -> None:
        """Close the editor; the page behind takes the row."""
        self.dismiss(self.row)

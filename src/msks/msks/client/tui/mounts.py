"""The TUI's mounts surfaces (#620, part #624): the workspace
page's mounts line and the list editor screen.

The editor edits the row's whole mount list — a workspace mounts
several mounts at once — one ``HOST-OR-SHARE:GUEST_PATH[:MODE]``
row each (the mode defaults to ``rw``), with add, edit, and remove
per row. Every change posts the full list through the daemon's
mounts route (next-start semantics, like resize: a running
workspace keeps its attached devices; the next start attaches the
new set), and the daemon's reply — the refreshed row with its
derived per-mount state — repaints the editor and the page behind
it. The derived state rides every row: ``healthy`` mounts attach at
the next start; a ``skipped``/``denied``/``conflict``/``drift``
mount names its reason, and the reason stands until the config and
the row agree again.
"""

from functools import partial

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from ..workspaces import mount_spec
from .rows import clip, workspace_label

#: The editor row's spec column budget: the share, the guest path,
#: and the mode beside the state's word — the guest path is the
#: long half and clips last (its head names the mount point's
#: tree).
SPEC_W = 44


def mount_spec_text(mount: dict) -> str:
    """One mount row's spec: ``HOST-OR-SHARE:GUEST_PATH:MODE`` —
    the words the CLI's ``--mount`` takes, the mode always shown
    so the row reads whole (the wire's default is ``rw``)."""
    mode = mount.get("mode") or "rw"
    served = mount.get("host") or mount.get("share")
    return f"{served}:{mount.get('guest')}:{mode}"


def issue_for(mount: dict, issues: list[dict]) -> dict | None:
    """The derived issue naming one mount, if any: matched on the
    form's naming key — the share name or the host path — beside
    the guest path, the pair a row names."""
    for issue in issues or []:
        if issue_names(issue, mount):
            return issue
    return None


def issue_names(issue: dict, mount: dict) -> bool:
    """Whether an issue frame names one mount: each naming key
    present on either side agrees (``None == None`` included — a
    frame and a row of one form carry no key for the other), and
    the guest path matches."""
    return (
        issue.get("share") == mount.get("share")
        and issue.get("host") == mount.get("host")
        and issue.get("guest") == mount.get("guest")
    )


def mount_row_text(mount: dict, issues: list[dict]) -> str:
    """One editor row's paint: the spec beside its state —
    ``healthy`` attaches at the next start; anything else names
    the kind and the reason."""
    issue = issue_for(mount, issues)
    if issue is None:
        return f"{clip(mount_spec_text(mount), SPEC_W)}  healthy"
    return (
        f"{clip(mount_spec_text(mount), SPEC_W)}  "
        f"{issue['kind']}: {issue['detail']}"
    )


def mounts_line(row: dict) -> str:
    """The workspace page's mounts line (#620, part #624): one
    entry per mount, its state riding beside — healthy mounts read
    their spec alone; a skip or conflict names itself, the marker
    that stands until the config and the row agree again. Empty
    when the workspace mounts nothing."""
    mounts = row.get("mounts") or []
    if not mounts:
        return ""
    issues = row.get("mount_issues") or []
    parts = [mount_state_text(mount, issues) for mount in mounts]
    return "mounts  " + "  ·  ".join(parts) + "  (next start)"


def replaced_mount(mounts: list, mount: dict | None, entry: dict) -> list:
    """The edited list: the focused mount's slot takes the new
    entry. A focus the list no longer carries (a swap window the
    pilot cannot arrange) leaves the list unchanged."""
    if mount is None:  # pragma: no cover
        return mounts
    return [entry if candidate is mount else candidate for candidate in mounts]


def clamp_index(focused: int, count: int) -> int | None:
    """The rebuilt list's focused index: the row focus kept, or
    the last row when the list shrank under it; None when the
    list is empty."""
    if not count:
        return None
    return min(focused, count - 1)


def mount_state_text(mount: dict, issues: list[dict]) -> str:
    """One mount's line entry: its spec alone when healthy, its
    kind riding beside when the daemon derived a skip or
    conflict."""
    issue = issue_for(mount, issues)
    state = "" if issue is None else f" ({issue['kind']})"
    return f"{mount_spec_text(mount)}{state}"


class MountEditScreen(ModalScreen[str | None]):
    """One mount's spec on a single line (#620, part #624): the
    editor's add and edit path share it — the input holds the
    CLI's own ``HOST-OR-SHARE:GUEST_PATH[:MODE]`` grammar, seeded with the
    row being edited. Enter accepts, Escape cancels."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self.title = title
        self.initial = initial

    def compose(self) -> ComposeResult:
        yield Label(self.title)
        yield Input(value=self.initial, id="mount-spec")

    def on_mount(self) -> None:
        self.query_one("#mount-spec", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MountsScreen(ModalScreen[dict | None]):
    """The mounts list editor (#620, part #624): the workspace's
    mounts one row each, add (``a``), edit (Enter), remove (``r``)
    — every change posts the whole list and the daemon's reply
    repaints the rows with their derived state. Escape closes;
    the page behind takes the refreshed row."""

    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("r", "remove", "Remove"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, row: dict, data) -> None:
        super().__init__()
        self.row = row
        self.data = data

    def compose(self) -> ComposeResult:
        yield Label(f"{workspace_label(self.row)} — mounts (next start)")
        yield ListView(id="mount-rows")
        yield Static("", id="mount-note")
        yield Footer()

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#mount-rows", ListView).focus()

    def mounts(self) -> list[dict]:
        """The editor's working list — the row's mounts."""
        return list(self.row.get("mounts") or [])

    def rebuild(self) -> None:
        """Repaint the rows from the row's mounts and their
        derived state, keeping the focused row by index."""
        rows = self.query_one("#mount-rows", ListView)
        focused = rows.index if rows.index is not None else 0
        items = self.editor_items()
        rows.clear()
        for item in items:
            rows.append(item)
        rows.index = clamp_index(focused, len(items))

    def editor_items(self) -> list[ListItem]:
        """The list's rows: one per mount with its derived state,
        or the empty placeholder that names the add key."""
        issues = self.row.get("mount_issues") or []
        items = [
            ListItem(Static(mount_row_text(mount, issues)))
            for mount in self.mounts()
        ]
        if items:
            return items
        prompt = "no mounts — press a to add HOST-OR-SHARE:GUEST_PATH[:MODE]"
        return [ListItem(Static(prompt))]

    def note(self, text: str) -> None:
        """The editor's note line: what the last change did, or
        why the daemon refused one."""
        self.query_one("#mount-note", Static).update(text)

    def focused_mount(self) -> dict | None:
        """The row under focus as its mount, None on the empty
        placeholder or a swap window."""
        rows = self.query_one("#mount-rows", ListView)
        mounts = self.mounts()
        if rows.index is None or not mounts:
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
            self.call_after_refresh(self.rebuild)
            self.call_after_refresh(lambda: self.note(reason))
            return
        self.row = fresh
        self.call_after_refresh(self.rebuild)
        self.call_after_refresh(
            lambda: self.note("saved — applies at the next start")
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row edits it — the list owns the key (the
        binding is the same action for a focus walk that lands
        elsewhere)."""
        self.action_edit()

    def action_add(self) -> None:
        """Add one mount: the spec line opens empty, and a filled
        line joins the list before the focused row's position —
        the new mount lands where the editor stands."""
        self.app.push_screen(
            MountEditScreen("add  HOST-OR-SHARE:GUEST_PATH[:MODE]"),
            self.added,
        )

    def added(self, spec: str | None) -> None:
        """The add dialog's answer: a spec joins the list, a blank
        line or a cancel decides nothing."""
        if not spec:
            return
        entry = self.entry(spec)
        if entry is None:
            return
        before = self.mounts()
        self.row["mounts"] = [*before, entry]
        self.rebuild()
        self.run_worker(partial(self.post, before), exclusive=True)

    def action_edit(self) -> None:
        """Edit the focused row's spec in place."""
        mount = self.focused_mount()
        if mount is None:
            return
        self.app.push_screen(
            MountEditScreen(
                "edit  HOST-OR-SHARE:GUEST_PATH[:MODE]",
                initial=mount_spec_text(mount),
            ),
            self.edited,
        )

    def edited(self, spec: str | None) -> None:
        """The edit dialog's answer: the focused row's mount takes
        the new spec."""
        if not spec:
            return
        entry = self.entry(spec)
        if entry is None:
            return
        mount = self.focused_mount()
        before = self.mounts()
        self.row["mounts"] = replaced_mount(before, mount, entry)
        self.rebuild()
        self.run_worker(partial(self.post, before), exclusive=True)

    def action_remove(self) -> None:
        """Remove the focused row's mount — the remaining list
        posts whole."""
        mount = self.focused_mount()
        if mount is None:
            return
        before = self.mounts()
        self.row["mounts"] = [
            candidate for candidate in before if candidate is not mount
        ]
        self.rebuild()
        self.run_worker(partial(self.post, before), exclusive=True)

    def entry(self, spec: str) -> dict | None:
        """One spec line as the wire's mount entry — the CLI's own
        grammar and shape refusals (:func:`mount_spec`): the
        daemon owns the share and rule checks and names them in
        its 400."""
        try:
            return mount_spec(spec, "mount")
        except SystemExit as exc:
            self.note(str(exc).replace("msks: ", "refused: "))
            return None

    def action_close(self) -> None:
        """Close the editor; the page behind takes the row."""
        self.dismiss(self.row)

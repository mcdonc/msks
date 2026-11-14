"""The mint screen and the workspace picker.

The form that mints a new placeholder — name, scope, coverage,
ttl — walking its fields, with the workspace picker the scoped
coverage field embeds.
"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from ..forms import FormSelect, FormWalk
from .cells import (
    COVERAGE_CHOICES,
    COVERAGE_SCOPED,
    COVERAGE_WIDE,
    MINT_NAME,
    MINT_NOTE,
    MINT_TTL_CHOICES,
    MINT_TTLS,
    SECRET_TTL_SECONDS,
    refused_dest,
    split_entries,
)
from .ui import (
    FailurePanel,
    flash_safe,
    workspace_label,
)


class WorkspacePicker(SelectionList):
    """The mint form's workspace multi-select (#393): space
    toggles the highlighted option, and the arrows keep the
    stock walk through the options — leaving the list at its
    edges: up from the first option returns to the field above,
    down from the last moves to the field below (the
    spatial-navigation rule: the arrows alone reach every field
    in reading order)."""

    BINDINGS = [
        Binding("up", "edge_previous", show=False),
        Binding("down", "edge_next", show=False),
    ]

    def on_focus(self) -> None:
        """A freshly-seeded picker holds no highlight (its options
        land after construction), so the focus puts one on the
        first option — space toggles from the first press."""
        if self.highlighted is None and self.option_count:
            self.highlighted = 0

    def at_top(self) -> bool:
        """Whether the walk leaves upward from here: no options,
        nothing highlighted, or the first option highlighted."""
        return self.option_count == 0 or self.highlighted in (None, 0)

    def at_bottom(self) -> bool:
        """Whether the walk leaves downward from here: no options,
        nothing highlighted, or the last option highlighted."""
        return self.option_count == 0 or self.highlighted == (
            self.option_count - 1
        )

    def action_edge_previous(self) -> None:
        """Up: the interior walks the options, the top edge
        returns the walk to the form."""
        if self.at_top():
            self.app.action_focus_previous()
        else:
            self.action_cursor_up()

    def action_edge_next(self) -> None:
        """Down: the interior walks the options, the bottom edge
        hands the walk back to the form."""
        if self.at_bottom():
            self.app.action_focus_next()
        else:
            self.action_cursor_down()


class MintScreen(FormWalk, ModalScreen[dict | None]):
    """The mint form (#393, #423): the create form's pattern over
    the mint's own fields — name, repeatable destinations, coverage
    (the daemon-wide row, or the workspaces a multi-select picks
    from the tree's own list), the TTL (unbounded, the daemon's
    default, or the renew picker's span), and the value: a masked
    input the operator types or pastes into — the form takes the
    secret's bytes directly (the original #393 complaint). The
    value rides the request
    and is never echoed back. Submit checks the store first
    (``msks secret check``'s endpoint), then mints; a refusal
    anywhere names itself on the note and the fields stay for a
    retry. A mint that lands dismisses with its reply — the row
    carrying the sentinel — and the page replaces
    the form with the sentinel panel; the dismiss clears the
    value field, so the typed secret leaves the widget tree with
    the form (the panel's own closing rule)."""

    # The walk's keys: named here, not on FormWalk — Textual
    # merges BINDINGS from DOMNode bases alone (the mixin's
    # note records the rule), and the actions live there.
    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", show=False),
    ]

    #: Whether the mint's exchange is in the air (#393): the
    #: reply carries the sentinel, so the flight owns the
    #: form until it lands — a second submit or a cancel in the
    #: air would drop the reply on a mint the daemon already took
    #: (and the retry would find the name taken).
    flighting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Static(MINT_NOTE, id="form-note")
            with Horizontal(classes="form-row"):
                yield Static("name", classes="form-label")
                yield Input(
                    placeholder="letters, numbers, underscores",
                    id="field-name",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("dests", classes="form-label")
                yield Input(
                    placeholder="host or .suffix, comma-separated",
                    id="field-dests",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("coverage", classes="form-label")
                yield FormSelect(
                    COVERAGE_CHOICES,
                    value=COVERAGE_WIDE,
                    allow_blank=False,
                    id="field-coverage",
                    compact=True,
                )
            with Horizontal(classes="form-row tall", id="workspaces-row"):
                yield Static("workspaces", classes="form-label")
                yield WorkspacePicker(id="field-workspaces")
            with Horizontal(classes="form-row"):
                yield Static("ttl", classes="form-label")
                yield FormSelect(
                    MINT_TTL_CHOICES,
                    value=MINT_TTLS[0],
                    allow_blank=False,
                    id="field-ttl",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("value", classes="form-label")
                yield Input(
                    placeholder="the real secret — typed or pasted",
                    id="field-value",
                    password=True,
                    compact=True,
                )
            with Horizontal(id="form-buttons"):
                yield Button(
                    "Mint", id="do-mint", variant="primary", compact=True
                )
                yield Button("Cancel", id="do-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#field-name", Input).focus()
        # The daemon-wide default hides the picker row — a hidden
        # control holds no focus, so the walk skips it.
        self.sync_coverage()
        self.run_worker(self.load_workspaces, exclusive=True)

    async def load_workspaces(self) -> None:
        """Seed the picker from the tree's own workspace list
        (#393); a refusal names itself on the note — the form
        stands, and the daemon-wide mint needs no list."""
        try:
            rows = await self.app.data.workspaces()
        except (Exception, SystemExit) as exc:
            self.note(f"workspace list failed: {flash_safe(str(exc))}")
            return
        self.query_one("#field-workspaces", WorkspacePicker).add_options(
            Selection(workspace_label(row), row["id"]) for row in rows
        )

    # -- the fields ------------------------------------------------------

    def field_value(self, field: str) -> str:
        """One plain field's value, stripped."""
        return self.query_one(f"#field-{field}", Input).value.strip()

    def coverage_value(self) -> str:
        """The coverage select's choice."""
        return str(self.query_one("#field-coverage", Select).value)

    def ttl_value(self) -> str | None:
        """The TTL select's choice — the unbounded pick rides no
        ``ttl_s``, the daemon's own default."""
        value = str(self.query_one("#field-ttl", Select).value)
        return None if value == MINT_TTLS[0] else value

    def picked_workspaces(self) -> list[str]:
        """The picker's selected workspace ids, sorted — the
        coverage set the mint scopes to."""
        picker = self.query_one("#field-workspaces", WorkspacePicker)
        return sorted(picker.selected)

    # -- the coverage toggle -----------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        """The coverage select's pick drives the picker row (#393):
        the daemon-wide row needs no workspaces, so the row hides;
        a scoped pick shows it."""
        if event.select.id == "field-coverage":
            self.sync_coverage()

    def sync_coverage(self) -> None:
        """Show the picker row only for a scoped pick."""
        self.query_one("#workspaces-row", Horizontal).display = (
            self.coverage_value() == COVERAGE_SCOPED
        )

    # -- the submit --------------------------------------------------------

    def submit_id(self) -> str:
        """The mint's own primary button — the shared button row
        rides the name."""
        return "do-mint"

    def action_cancel(self) -> None:
        """Cancel — never mid-flight (#393): the reply the daemon
        already owes this form would land on a screen nobody is
        reading, and the retry would find the name taken. The
        flight lands first (it names its own refusal, or the
        panel replaces the form)."""
        if self.flighting:
            return
        self.dismiss_with(None)

    def submit(self) -> None:
        """The mint: the local checks first (a refusal keeps the
        fields), then the store check and the mint through the
        data seam — one worker, one flight, and a submit while a
        flight is in the air is a no-op (its reply is the one the
        daemon already owes this form)."""
        if self.flighting:
            return
        self.run_worker(self.do_mint, exclusive=True)

    def note(self, text: str) -> None:
        """The form's note line: its title, or the refusal that
        keeps a half-filled body home."""
        self.query_one("#form-note", Static).update(text)

    def body(self) -> dict | None:
        """The mint body (#393, #423): the daemon only sees whole
        bodies — the name's shape, at least one destination, the
        scoped coverage's non-empty pick, and a non-empty value
        are checked piecewise, each refusal naming itself on the
        note."""
        identity = self.identity()
        if identity is None:
            return None
        name, dests = identity
        body: dict = {"name": name, "dests": dests}
        if not self.coverage_body(body):
            return None
        ttl = self.ttl_value()
        if ttl is not None:
            body["ttl_s"] = SECRET_TTL_SECONDS[ttl]
        value = self.field_value("value")
        if not value:
            self.note("a value is required")
            return None
        body["value"] = value
        return body

    def identity(self) -> tuple[str, list[str]] | None:
        """The name and the destinations, checked locally (#393);
        None (with the note naming the refusal) when either fails
        its check."""
        name = self.field_value("name")
        if not name:
            self.note("a name is required")
            return None
        if not MINT_NAME.match(name):
            self.note(
                "name: letters, numbers, or underscores, no leading digit"
            )
            return None
        dests = split_entries(self.field_value("dests"))
        if not dests:
            self.note("at least one destination is required")
            return None
        bad = refused_dest(dests)
        if bad is not None:
            self.note(
                f"dest {flash_safe(bad)}: an exact hostname or a "
                "label-anchored suffix like .example.com"
            )
            return None
        return name, dests

    def coverage_body(self, body: dict) -> bool:
        """The scoped coverage's pick into ``body``; False (with
        the note naming the refusal) when a scoped pick holds
        nothing."""
        if self.coverage_value() != COVERAGE_SCOPED:
            return True
        workspaces = self.picked_workspaces()
        if not workspaces:
            self.note(
                "scoped coverage picks at least one workspace — space toggles"
            )
            return False
        body["workspaces"] = workspaces
        return True

    async def do_mint(self) -> None:
        """The mint's flight: the local checks first (a refusal
        keeps the fields and never takes off), then the exchange
        with the flight owning the form until it lands."""
        body = self.body()
        if body is None:
            return
        self.flighting = True
        try:
            await self.mint_flight(body)
        finally:
            self.flighting = False

    async def mint_flight(self, body: dict) -> None:
        """The exchange proper: a refusal lands on the failure
        panel (#426) — the daemon's detail at reading width,
        dismissed by hand — with the fields kept for a retry; a
        mint that lands dismisses with its reply (#393 — the
        sentinel rides the reply; the value rides no reply at
        all)."""
        self.note("minting…")
        try:
            row = await self.app.data.mint_secret(body)
        except (Exception, SystemExit) as exc:
            self.refused("mint", body["name"], exc)
            return
        self.dismiss_with(row)

    def refused(self, verb: str, identity: str | None, exc) -> None:
        """A refused exchange (#426): the failure panel opens over
        the form carrying the daemon's detail, and the note
        returns to its standing text — the flight is down, and
        the fields stay filled for a retry once the panel
        closes."""
        self.note(MINT_NOTE)
        self.app.push_screen(FailurePanel(verb, identity, str(exc)))

    def dismiss_with(self, row: dict | None) -> None:
        """Dismiss and hand the reply to the callback (async — the
        exchange runs as a task, so the modal closes without
        waiting on it). The value field clears first: the typed
        secret leaves the widget tree with the form."""
        try:
            self.query_one("#field-value", Input).value = ""
        except NoMatches:
            pass  # a teardown race unmounted the field first
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._task = asyncio.create_task(self.submitted(row))

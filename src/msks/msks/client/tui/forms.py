"""The tree's forms (#309 create, #331 edit): the shared
label-plus-input walk, the create form with the catalog image
select, and the edit dialog seeded from a workspace's row —
extracted from the app shell so :mod:`msks.client.tui.main_app`
composes them.

Spatial navigation: the arrows and Enter move between a form's
controls in walk order (the app's own focus order; a hidden or
disabled control holds no focus, so the walk skips it), a closed
select answers up/down by walking the form, and Escape or q
cancels — no form traps focus.
"""

import asyncio

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Select, Static

from ...identity import LEGACY_LOGIN_USER
from ..create import invoking_user
from .rows import clip, workspace_label


class FormSelect(Select):
    """A form's select: stock Select with the walk's arrows kept
    — a closed select answers up/down by walking the form's fields
    (Enter or space opens the list; an open list keeps the stock
    arrows for its own rows). The create form's image select and
    the mint form's coverage and TTL selects (#393) ride this one
    shape."""

    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
    ]

    def action_walk_next(self) -> None:
        self.app.action_focus_next()

    def action_walk_previous(self) -> None:
        self.app.action_focus_previous()


class FormWalk:
    """The form screens' shared walk (#309, #393): the arrows and
    Enter move between a form's controls in walk order, and
    Escape or q cancels — the app's own focus order supplies the
    walk (a hidden control holds no focus, so a row a mode hides
    — the mint form's picker under the daemon-wide default — is
    skipped; a select keeps its own down while its list is open,
    and the mint form's picker leaves the walk at its edges).
    The screens re-declare the key bindings themselves: Textual
    merges ``BINDINGS`` from ``DOMNode`` bases alone, so a plain
    mixin's list would never reach the screen."""

    def __init__(self, submitted) -> None:
        super().__init__()
        self.submitted = submitted

    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", show=False),
    ]

    def action_walk_next(self) -> None:
        """Down walks the form — the same walk Enter makes."""
        self.app.action_focus_next()

    def action_walk_previous(self) -> None:
        """Up walks the form back."""
        self.app.action_focus_previous()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a field moves the walk to the next control (the
        buttons included — the arrows make the same walk; the
        action is the app's, the screen hosts the binding)."""
        self.app.action_focus_next()


#: The create form's fields (#309): the create body's optional
#: inputs in walk order — a short label at the left of each
#: row, the hint riding the input's placeholder (the image
#: select's hint is its blank prompt; the root/home and user
#: placeholders name their defaults at mount).
FORM_FIELDS = (
    ("name", "name", "workspace name"),
    ("image", "image ref", "the default image"),
    ("cpus", "vcpus", "default 2"),
    ("mem_mib", "memory", "MiB — 8192"),
    ("root_mib", "root", "MiB"),
    ("home_mib", "home", "MiB"),
    ("user", "user", "the account it seeds"),
)

#: The fields whose values must be whole numbers.
INT_FIELDS = frozenset({"cpus", "mem_mib", "root_mib", "home_mib"})

#: The fields an edit cannot move (#331): name, image, and user
#: are create-time — the daemon answers a mutation with its named
#: 405, and the seed planted them at first boot — so the edit
#: dialog shows them read-only and a value that moved stays home
#: with the note naming it, never silently dropped. The sizes are
#: the editable half: they move through the resize route, which
#: owns the stopped-workspace rule.
CREATE_TIME_FIELDS = frozenset({"name", "image", "user"})


#: The row keys the edit dialog's fields seed from (#331), in
#: form-field order — the sizes and topology read the row's own
#: facts, the image reads its hash, the user reads the login user.
EDIT_ROW_KEYS = {
    "name": "name",
    "image": "image_hash",
    "cpus": "cpus",
    "mem_mib": "mem_mib",
    "root_mib": "root_mib",
    "home_mib": "home_mib",
    "user": "login_user",
}

#: The seeds' fallbacks for a row that predates the field (#331):
#: a workspace without an image hash boots explicit kernel/rootfs
#: paths (the listing's own absent mark), and a row without a
#: login user predates per-workspace users — its account is the
#: image's own, the same default the ssh-key endpoint serves.
EDIT_ROW_FALLBACKS = {"image": "-", "user": LEGACY_LOGIN_USER}

#: The statuses the resize route serves (#380) — the daemon's own
#: allow-list, mirrored here for the ask: a workspace in any other
#: status may keep a live volume attached, so its edit takes the
#: stop-and-resize question before the route can refuse it.
EDIT_FREE_STATUSES = frozenset({"created", "stopped", "absent"})


def whole_number(value: str) -> bool:
    """Whether a form value is a whole number (the sizes and counts
    ride the wire as ints; ASCII digits only — int() refuses some
    unicode digits isdigit() accepts, and a paste can carry them)."""
    return value.isascii() and value.isdigit()


def image_options(rows: list[dict]) -> list[tuple[str, str]]:
    """The image select's options from the catalog listing: the
    reference and the hash as two 12-character columns, the
    designated default marked after them (the hash column keeps
    references that clip to the same 12 characters apart). A
    reference two entries share rides the hash in the option's
    value (name@hash resolves to exactly that entry — name:version
    would pick the oldest of the two)."""
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        ref = f"{row['name']}:{row['version']}"
        value = f"{row['name']}@{row['hash']}" if ref in seen else ref
        seen.add(ref)
        label = f"{clip(ref)} {clip(row['hash'])}"
        if row.get("default"):
            label += " — default"
        options.append((label, value))
    return options


def edit_seeds(row: dict) -> dict[str, str]:
    """The edit dialog's seeded values (#331): every form field's
    current value read off the workspace's row, as the field's
    own text — the sizes and topology as their numbers, a row
    that predates a field seeding its fallback."""
    seeds: dict[str, str] = {}
    for field, key in EDIT_ROW_KEYS.items():
        value = row.get(key)
        if value is None:
            value = EDIT_ROW_FALLBACKS.get(field, "")
        seeds[field] = str(value)
    return seeds


def edit_stop_question(row: dict) -> str:
    """The stop-and-resize question (#380): the workspace runs, its
    sizes move only while it is stopped, and Apply hands the stop
    to the operator to confirm instead of losing the edit to the
    refusal the stop answers."""
    return f"stop {workspace_label(row)} and resize?"


def edit_note(row: dict) -> str:
    """The edit dialog's note (#331): the title, then the rule the
    issue pins — which fields land live (home bytes move at
    once), which wait for a boot (root growth and the new
    topology), and what Apply does with a running workspace (asks
    to stop it, #380), and which fields cannot change (the
    create-time ones, marked * on their labels). The explicit
    line breaks keep the form fitting 80x24 terminals whatever
    the workspace's name carries."""
    label = clip(workspace_label(row), 40)
    return (
        f"edit {label}\n"
        "Apply asks to stop a running VM · home bytes move at once\n"
        "· root growth and topology at next boot · * = create-time"
    )


class WorkspaceForm(FormWalk, ModalScreen[dict | None]):
    """The workspace form (#309 create, #331 edit): one
    label-plus-input row per field, in walk order, Enter and the
    arrows moving between them; the buttons submit and cancel.
    The form stands 80x24 terminals tall — every control, the
    buttons included, must stay on screen. The submitted body goes
    to the callback given at construction (the pushing screen owns
    the exchange and its flashes); local checks refuse here so the
    daemon only sees whole bodies.

    One implementation serves both surfaces: the create subclass
    (:class:`CreateScreen`) leaves every field open and submits
    the filled body; the edit subclass (:class:`EditScreen`)
    seeds every field from the workspace's row, marks the
    create-time fields read-only, and submits the changed sizes as
    a resize body (#331).
    """

    # The walk's keys: named here, not on FormWalk — Textual
    # merges BINDINGS from DOMNode bases alone (the mixin's
    # note records the rule), and the actions live there.
    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", show=False),
    ]

    # -- the mode's hooks ------------------------------------------

    def form_note(self) -> str:
        """The note line's standing text: the form's title, or the
        rule the mode carries."""
        raise NotImplementedError  # pragma: no cover — abstract hook

    def field_label(self, field: str, label: str) -> str:
        """One field's label; an edit marks its create-time fields
        with the note's * legend."""
        return label

    def image_control(self, hint: str):
        """The image field's control."""
        return FormSelect([], prompt=hint, id="field-image", compact=True)

    def editable(self, field: str) -> bool:
        """Whether the operator can change the field here (an edit
        locks its create-time fields read-only)."""
        return True

    def image_value(self) -> str:
        """The image field's value — blank is the daemon's default
        image."""
        select = self.query_one("#field-image", Select)
        return "" if select.is_blank() else str(select.value)

    def first_field(self) -> str:
        """The field the walk starts from."""
        return "name"

    def form_mounted(self) -> None:
        """The mode's mount work, after the shared focus."""

    def submit_id(self) -> str:
        """The submit button's id."""
        return "do-create"

    def submit_label(self) -> str:
        """The submit button's label."""
        return "Create"

    def body(self) -> dict | None:
        """The submitted body; None (with the note naming the
        refusal) keeps the form standing."""
        raise NotImplementedError  # pragma: no cover — abstract hook

    # -- the shared form ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Static(
                self.form_note(),
                id="form-note",
            )
            for field, label, hint in FORM_FIELDS:
                with Horizontal(classes="form-row"):
                    yield Static(
                        self.field_label(field, label), classes="form-label"
                    )
                    if field == "image":
                        yield self.image_control(hint)
                    else:
                        control = Input(
                            placeholder=hint,
                            id=f"field-{field}",
                            compact=True,
                        )
                        control.disabled = not self.editable(field)
                        yield control
            with Horizontal(id="form-buttons"):
                yield Button(
                    self.submit_label(),
                    id=self.submit_id(),
                    variant="primary",
                    compact=True,
                )
                yield Button("Cancel", id="do-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(f"#field-{self.first_field()}", Input).focus()
        self.form_mounted()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == self.submit_id():
            self.submit()
        else:
            self.dismiss_with(None)

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def note(self, text: str) -> None:
        """The form's note line: its title, or the local
        refusal that keeps a half-filled body home."""
        self.query_one("#form-note", Static).update(text)

    def field_value(self, field: str) -> str:
        """One field's value, stripped — the image select's blank
        (no pick) stays the daemon's default image."""
        if field == "image":
            return self.image_value()
        return self.query_one(f"#field-{field}", Input).value.strip()

    def land_value(self, field: str, value: str, body: dict) -> bool:
        """One filled value into the body; False (with the note
        naming it) on a local refusal. Blank stays unset."""
        if not value:
            return True
        if field in INT_FIELDS and not whole_number(value):
            self.note(f"{field}: a whole number, or leave it blank")
            return False
        body[field] = int(value) if field in INT_FIELDS else value
        return True

    def submit(self) -> None:
        """Hand the body to the callback, or keep the form on a
        local refusal."""
        body = self.body()
        if body is not None:
            self.dismiss_with(body)

    def dismiss_with(self, body: dict | None) -> None:
        """Dismiss and hand the body to the callback (async — the
        exchange runs as a task, so the modal closes without
        waiting on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._task = asyncio.create_task(self.submitted(body))


class CreateScreen(WorkspaceForm):
    """The create form (#309): every field open, blank fields left
    to the daemon's defaults, the image picked from the catalog."""

    def form_note(self) -> str:
        return "create a workspace"

    def body(self) -> dict | None:
        """The create body: only the fields the operator filled
        (blank stays the daemon's default), whole numbers checked
        locally — the daemon's validation stays the authority."""
        body: dict = {}
        for field, _label, _hint in FORM_FIELDS:
            if not self.land_value(field, self.field_value(field), body):
                return None
        if "name" not in body:
            self.note("a workspace name is required")
            return None
        return body

    def form_mounted(self) -> None:
        self.query_one("#field-user", Input).placeholder = invoking_user()
        self.run_worker(self.load_hints, exclusive=True)

    async def load_hints(self) -> None:
        """The form's daemon hints: the image select's catalog and
        the size placeholders' defaults. A refusal keeps the form
        standing — blank fields still create against the daemon's
        defaults."""
        try:
            rows = await self.app.data.images()
        except (Exception, SystemExit) as exc:
            self.note(f"image list failed: {escape(str(exc))}")
        else:
            self.query_one("#field-image", Select).set_options(
                image_options(rows)
            )
        try:
            defaults = await self.app.data.create_defaults()
        except (Exception, SystemExit) as exc:
            self.note(f"defaults failed: {escape(str(exc))}")
        else:
            self.size_placeholders(defaults)

    def size_placeholders(self, defaults: dict) -> None:
        """The root/home placeholders: the MiB unit beside the
        default a blank field lands on."""
        for field in ("root_mib", "home_mib"):
            self.query_one(
                f"#field-{field}", Input
            ).placeholder = f"MiB — {defaults[field]}"


class EditScreen(WorkspaceForm):
    """The edit dialog (#331): the create form's layout seeded from
    the workspace's row. The sizes are editable — they submit as a
    resize body through the route that owns the stopped-workspace
    rule — and the create-time fields (name, image, user) are
    marked * and read-only: a value that moved is refused with the
    note naming it, never silently dropped. The walk skips the
    read-only rows (a disabled control cannot hold focus), so the
    arrows move between the sizes and the buttons alone."""

    def __init__(self, row: dict, submitted) -> None:
        self.row = row
        self.seeded = edit_seeds(row)
        super().__init__(submitted)

    def form_note(self) -> str:
        return edit_note(self.row)

    def field_label(self, field: str, label: str) -> str:
        if field in CREATE_TIME_FIELDS:
            return f"{label} *"
        return label

    def image_control(self, hint: str):
        """The image field as a read-only line: the row's image
        hash, not a catalog pick — the create-time image cannot
        move, so the select that would offer a new one stays out."""
        control = Input(placeholder=hint, id="field-image", compact=True)
        control.value = self.seeded["image"]
        control.disabled = True
        return control

    def editable(self, field: str) -> bool:
        return field not in CREATE_TIME_FIELDS

    def image_value(self) -> str:
        return self.query_one("#field-image", Input).value.strip()

    def first_field(self) -> str:
        return "cpus"

    def submit_id(self) -> str:
        return "do-apply"

    def submit_label(self) -> str:
        return "Apply"

    def form_mounted(self) -> None:
        """Seed every plain field with the workspace's current
        values — the prefill the issue pins (the image seeds in
        its own read-only control)."""
        for field, _label, _hint in FORM_FIELDS:
            if field != "image":
                self.query_one(f"#field-{field}", Input).value = self.seeded[
                    field
                ]

    def body(self) -> dict | None:
        """The resize body: the changed sizes alone. A create-time
        field that moved (a programmatic set — the dialog's own
        fields are read-only) stays home with the note naming it,
        and a body with nothing changed stays home too: the
        daemon's nothing-to-resize refusal, said locally where the
        operator can still edit."""
        moved = self.refused_create_time()
        if moved is not None:
            self.note(
                f"{moved} is create-time — delete and recreate the "
                "workspace to change it"
            )
            return None
        body: dict = {}
        if not self.changed_sizes(body):
            return None
        if not body:
            self.note("nothing to resize — change a size, or cancel")
            return None
        return body

    def refused_create_time(self) -> str | None:
        """The first create-time field whose value moved, else None
        — the dialog's own fields are read-only, so a move is a
        programmatic set, and the submit refuses it by name."""
        for field in sorted(CREATE_TIME_FIELDS):
            if self.field_value(field) != self.seeded[field]:
                return field
        return None

    def changed_sizes(self, body: dict) -> bool:
        """Land the changed sizes into ``body``; False (with the
        note naming the refusal) on a junk value. A field left at
        its seeded value rides nothing — the resize moves only
        what the operator changed."""
        for field, _label, _hint in FORM_FIELDS:
            if field in CREATE_TIME_FIELDS:
                continue
            value = self.field_value(field)
            if value != self.seeded[field]:
                if not self.land_value(field, value, body):
                    return False
        return True

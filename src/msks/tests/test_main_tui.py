"""The workspace tree TUI (#309): Pilot-driven, fake seams.

The data seam is a scripted FakeData (the daemon's REST surface),
the decider link rides the FakeWS/FakeFactory connections the
consent app's tests already script, and the pure helpers (the
consent status line's pieces, the follow-up queue) get direct unit
tests.
"""

import asyncio
import json
import stat
import sys
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import websockets
from msks.client import cli
from msks.client.tui import data as data_mod
from msks.client.tui import link as link_mod
from msks.client.tui import main_app
from msks.client.tui.link import DeciderLink
from msks.client.tui.main_app import (
    FLOW_SHELL,
    MainScreen,
    MsksTuiApp,
    TuiFollow,
    WorkspaceScreen,
    image_options,
)
from rich.cells import cell_len
from test_consent_overlay import FakeFactory, FakeWS, press_until, wait_for
from test_consent_tui import frame
from test_consent_tui import (
    request_frame as shared_request_frame,
)
from test_consent_tui import (
    rules_frame as shared_rules_frame,
)
from textual.color import Color
from textual.css.query import NoMatches
from textual.widgets import Button, Input, OptionList, Select, Static

WS = "ws-a"


def request_frame(rid: str, host: str = "api.example") -> str:
    """The shared request fixture, scoped to this suite's
    workspace."""
    return shared_request_frame(rid, host, 443, workspace=WS)


def rules_frame(mode: str = "interactive") -> str:
    """The shared rules fixture, scoped to this suite's workspace;
    a mode with no verdicts for the empty-grant case."""
    if mode == "interactive":
        return shared_rules_frame(workspace=WS)
    return frame(
        "egress.rules",
        {
            "workspace_id": WS,
            "mode": mode,
            "allow_list": [],
            "allowed": [],
            "denied": [],
        },
    )


def row(
    id: str = WS,
    name: str | None = "alpha",
    status: str = "stopped",
    mode: str = "interactive",
    host: str = "host-1",
) -> dict:
    """One listing row as the daemon serves it."""
    return {
        "id": id,
        "name": name,
        "status": status,
        "egress_mode": mode,
        "image_hash": "a" * 64,
        "created_at": "2026-01-02T03:04:05",
        "host": host,
    }


class FakeData:
    """The screens' daemon calls, scripted and recorded."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(r) for r in rows]
        self.calls: list[tuple] = []
        self.fail: set[str] = set()
        self.fetches = 0
        self.refusal = "daemon away"
        self.images_rows: list[dict] = []
        self.defaults: dict = {"root_mib": 10240, "home_mib": 20480}

    async def workspaces(self) -> list[dict]:
        self.fetches += 1
        if "workspaces" in self.fail:
            raise RuntimeError("daemon away")
        return [dict(r) for r in self.rows]

    async def images(self) -> list[dict]:
        if "images" in self.fail:
            raise RuntimeError(self.refusal)
        return [dict(r) for r in self.images_rows]

    async def create_defaults(self) -> dict:
        if "defaults" in self.fail:
            raise RuntimeError(self.refusal)
        return dict(self.defaults)

    async def create(self, body: dict):
        self.calls.append(("create", body))
        if "create" in self.fail:
            raise RuntimeError(self.refusal)
        fresh = dict(row(id="new1", name=body.get("name"), status="created"))
        self.rows.append(fresh)
        return (dict(fresh), None)

    async def start(self, workspace_id: str) -> dict:
        self.calls.append(("start", workspace_id))
        return self.reply("start", {"id": workspace_id, "status": "running"})

    async def stop(self, workspace_id: str) -> dict:
        self.calls.append(("stop", workspace_id))
        return self.reply("stop", {"id": workspace_id, "status": "stopped"})

    async def remove(self, workspace_id: str) -> dict:
        self.calls.append(("remove", workspace_id))
        self.rows = [r for r in self.rows if r["id"] != workspace_id]
        return self.reply("remove", {})

    async def set_egress_mode(
        self, workspace_id: str, mode: str, *, confirm_empty: bool = False
    ) -> dict:
        """The policy PUT (#344) — recorded with its confirmation;
        the reply carries the fresh rules frame the daemon's real
        endpoint returns."""
        self.calls.append(("mode", workspace_id, mode, confirm_empty))
        return self.reply(
            "mode",
            {
                "workspace_id": workspace_id,
                "mode": mode,
                "allow_list": [],
                "allowed": [],
                "denied": [],
                "applied": True,
            },
        )

    async def decide(
        self, workspace_id: str, request_id: str, decision: str, duration: str
    ) -> dict:
        """The consent overlay's decide — recorded, the named
        refusal when it fails."""
        self.calls.append(("decide", workspace_id, request_id, decision))
        return self.reply("decide", {"request_id": request_id})

    async def revoke(self, workspace_id: str, request_id: str) -> dict:
        """The rules screen's revoke — recorded, the named refusal
        when it fails."""
        self.calls.append(("revoke", workspace_id, request_id))
        return self.reply("revoke", {"request_id": request_id})

    def reply(self, verb: str, value):
        """The scripted reply; the named refusal when the verb fails."""
        if verb in self.fail:
            raise RuntimeError(self.refusal)
        return value


def make_app(data: FakeData, follow: TuiFollow | None = None):
    """The app over a scripted data seam."""
    follow = follow or TuiFollow()
    return MsksTuiApp(follow, data=data), follow


def scripted_link(monkeypatch, frames: list[str]) -> FakeFactory:
    """Patch the page's link to a scripted connection (the first
    connection carries the frames; later ones stay quiet)."""
    factory = FakeFactory([FakeWS(frames), FakeWS([])])
    monkeypatch.setattr(
        main_app,
        "DeciderLink",
        lambda ws_id: DeciderLink(
            ws_id, ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        ),
    )
    return factory


def list_children(app) -> int:
    """The workspaces list's row count; -1 in a swap window."""
    try:
        return len(app.query_one("#rows").children)
    except Exception:
        return -1


def action_children(app) -> int:
    """The workspace page's action count; -1 in a swap window."""
    try:
        return len(app.screen.query_one("#actions").children)
    except Exception:
        return -1


def action_text(app, index: int) -> str:
    """One action row's text; empty while its inner widget is
    still mounting."""
    try:
        return str(
            app.screen.query_one("#actions")
            .children[index]
            .query_one(Static)
            .content
        )
    except Exception:
        return ""


def status_text(app) -> str:
    return str(app.query_one("#status", Static).content)


def row_text(app, index: int) -> str:
    """One listing row's text; empty while its inner widget is
    still mounting (the compose stream lags the row count)."""
    try:
        return str(
            app.query_one("#rows").children[index].query_one(Static).content
        )
    except Exception:
        return ""


def consent_text(app) -> str:
    """The consent line's text; empty while the page still mounts."""
    try:
        return str(app.screen.query_one("#consent", Static).content)
    except Exception:
        return ""


def header_text(app) -> str:
    """The header's name line; empty while the page still mounts."""
    try:
        return str(app.screen.query_one("#header", Static).content)
    except Exception:
        return ""


def meta_text(app) -> str:
    """The header's muted meta line; empty while the page still
    mounts."""
    try:
        return str(app.screen.query_one("#header-meta", Static).content)
    except Exception:
        return ""


def on_main(app) -> bool:
    return isinstance(app.screen, MainScreen)


def on_page(app) -> bool:
    return isinstance(app.screen, WorkspaceScreen)


def on_overlay(app) -> bool:
    from msks.client.tui.main_app import ConsentOverlay

    return isinstance(app.screen, ConsentOverlay)


async def open_page(pilot, app) -> WorkspaceScreen:
    """Enter on the list's first row opens the workspace page."""
    await wait_for(lambda: list_children(app) >= 1)
    await press_until(pilot, "enter", lambda: on_page(app))
    return app.screen


# -- the workspaces list --------------------------------------------------


async def test_the_list_rows_open_pages_and_return(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: "alpha" in row_text(app, 0))
        assert "beta" in row_text(app, 1)
        await wait_for(lambda: "2 workspaces" in status_text(app))
        page = await open_page(pilot, app)
        assert WS in meta_text(app)
        # The header's meta line carries the absolute created date
        # (#350) — the fact the list's column reads relative.
        assert "created 2026-01-02" in meta_text(app)
        assert page.link is not None
        # Returning to the list refreshes it (the page's actions may
        # have moved the workspace's status).
        fetches = data.fetches
        await pilot.press("escape")
        await wait_for(lambda: on_main(app) and data.fetches > fetches)


async def test_start_stop_and_remove_from_the_list(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: list_children(app) == 1)
        await press_until(pilot, "s", lambda: data.calls == [("start", WS)])
        await wait_for(lambda: "alpha running" in status_text(app))
        await press_until(pilot, "x", lambda: ("stop", WS) in data.calls)
        await wait_for(lambda: "alpha stopped" in status_text(app))
        # D asks; y answers; the row leaves with the listing.
        await pilot.press("D")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(lambda: ("remove", WS) in data.calls)
        await wait_for(lambda: list_children(app) == 0)
        assert "alpha deleted" in status_text(app)
        assert "No workspaces" in str(app.query_one("#empty", Static).content)
        # The empty state replaces the header row (#347).
        assert not app.query_one("#columns", Static).display


async def test_a_listing_failure_flashes() -> None:
    data = FakeData([])
    data.fail.add("workspaces")
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: "listing failed" in status_text(app))
        await pilot.pause()


async def test_keys_without_a_focused_row_flash() -> None:
    data = FakeData([])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: list_children(app) == 0)
        await pilot.press("s")
        await wait_for(lambda: "no workspace focused" in status_text(app))
        await pilot.press("D")
        await pilot.pause()
        assert "remove" not in [call[0] for call in data.calls]


async def test_the_create_form_posts_and_the_list_refreshes() -> None:
    data = FakeData([])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: list_children(app) == 0)
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.query_one("#field-cpus", Input).value = "4"
        screen.submit()
        await wait_for(lambda: data.calls and data.calls[0][0] == "create")
        body = data.calls[0][1]
        assert body == {"name": "brand-new", "cpus": 4}
        await wait_for(lambda: "created brand-new" in status_text(app))
        await wait_for(lambda: list_children(app) == 1)


async def test_the_create_form_refuses_local_junk() -> None:
    data = FakeData([])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.query_one("#field-cpus", Input).value = "four"
        screen.submit()
        await pilot.pause()
        note = str(screen.query_one("#form-note", Static).content)
        assert "whole number" in note
        assert data.calls == []
        # A nameless body stays home too.
        screen.query_one("#field-cpus", Input).value = ""
        screen.query_one("#field-name", Input).value = ""
        screen.submit()
        await pilot.pause()
        assert "a workspace name is required" in str(
            screen.query_one("#form-note", Static).content
        )
        assert data.calls == []
        # Escape cancels: no exchange, no create.
        await pilot.press("escape")
        await wait_for(lambda: on_main(app))
        assert data.calls == []


async def test_the_create_form_fits_the_small_terminal() -> None:
    # 80x24 is the smallest terminal the form must fit: the last
    # field and the buttons stay inside the screen, above the
    # footer line.
    data = FakeData([])
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        assert screen.query_one("#form").outer_size.height <= 24
        last = screen.query_one("#field-user", Input)
        buttons = screen.query_one("#form-buttons")
        assert last.region.bottom < 24
        assert buttons.region.bottom < 24
        assert screen.query_one("Footer").region.y == 23


def test_image_options_mark_the_default_and_pin_duplicate_refs():
    # The select offers one row per catalog entry: the reference
    # and the hash as clipped columns (two refs sharing a clipped
    # column stay apart in the hash column), the designated
    # default marked.
    rows = [
        {
            "name": "debian-13",
            "version": "2026.01",
            "hash": "a" * 64,
            "default": True,
        },
        {
            "name": "debian-13",
            "version": "2025.12",
            "hash": "b" * 64,
            "default": False,
        },
    ]
    assert image_options(rows) == [
        ("debia…026.01 aaaaa…aaaaaa — default", "debian-13:2026.01"),
        ("debia…025.12 bbbbb…bbbbbb", "debian-13:2025.12"),
    ]
    # Two entries sharing a reference: the second rides its hash
    # (name@hash resolves to exactly that entry).
    rows.append(
        {
            "name": "debian-13",
            "version": "2026.01",
            "hash": "c" * 64,
            "default": False,
        }
    )
    assert image_options(rows)[2] == (
        "debia…026.01 ccccc…cccccc",
        f"debian-13@{'c' * 64}",
    )
    # A reference already inside the column stays whole.
    rows.append(
        {
            "name": "alpine",
            "version": "3.22",
            "hash": "d" * 64,
            "default": False,
        }
    )
    assert image_options(rows)[3] == (
        "alpine:3.22 ddddd…dddddd",
        "alpine:3.22",
    )


async def test_the_create_form_picks_the_image_from_the_catalog() -> None:
    data = FakeData([])
    data.images_rows = [
        {
            "name": "debian-13",
            "version": "2026.01",
            "hash": "a" * 64,
            "default": True,
        },
        {
            "name": "debian-13",
            "version": "2025.12",
            "hash": "b" * 64,
            "default": False,
        },
    ]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        select = screen.query_one("#field-image", Select)
        overlay = select.query_one("SelectOverlay", OptionList)
        # The blank prompt rides the list as its first row.
        await wait_for(lambda: overlay.option_count == 3)
        # The walk itself: the arrows walk from a plain input (down
        # reaches the select without opening it), Enter opens the
        # list, down moves to the first catalog entry (the prompt
        # holds the highlight), Enter picks it.
        await pilot.press("down")
        assert screen.focused is select
        await pilot.press("enter")
        await pilot.press("down")
        await pilot.press("enter")
        assert str(select.value) == "debian-13:2026.01"
        # A closed select walks on down — it does not reopen —
        # and up walks back through it the same way.
        await pilot.press("down")
        assert screen.focused is screen.query_one("#field-cpus", Input)
        await pilot.press("up")
        assert screen.focused is select
        await pilot.press("up")
        assert screen.focused is screen.query_one("#field-name", Input)
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.submit()
        await wait_for(lambda: data.calls and data.calls[0][0] == "create")
        assert data.calls[0][1]["image"] == "debian-13:2026.01"


async def test_an_image_listing_refusal_keeps_the_form_standing() -> None:
    data = FakeData([])
    data.fail.add("images")
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        await wait_for(
            lambda: (
                "image list failed"
                in str(screen.query_one("#form-note", Static).content)
            )
        )
        # The refused select stays blank — blank means the default
        # image, so a create still goes out whole.
        assert screen.query_one("#field-image", Select).is_blank()
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.submit()
        await wait_for(lambda: data.calls and data.calls[0][0] == "create")
        assert "image" not in data.calls[0][1]


async def test_the_size_and_user_placeholders_carry_the_defaults(
    monkeypatch,
) -> None:
    """The root/home placeholders name the daemon's MiB defaults,
    and the user placeholder names the invoking account — a
    blank field's landing spot reads off the form itself."""
    monkeypatch.setattr(main_app, "invoking_user", lambda: "ops")
    data = FakeData([])
    data.defaults = {"root_mib": 5120, "home_mib": 1024}
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        root = screen.query_one("#field-root_mib", Input)
        home = screen.query_one("#field-home_mib", Input)
        await wait_for(lambda: "5120" in root.placeholder)
        assert root.placeholder == "MiB — 5120"
        assert home.placeholder == "MiB — 1024"
        assert screen.query_one("#field-user", Input).placeholder == "ops"


async def test_a_defaults_refusal_keeps_the_form_standing() -> None:
    """A refused defaults listing keeps the unit-only placeholders
    and the form standing — blank fields still create against the
    daemon's defaults."""
    data = FakeData([])
    data.fail.add("defaults")
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        await wait_for(
            lambda: (
                "defaults failed"
                in str(screen.query_one("#form-note", Static).content)
            )
        )
        assert screen.query_one("#field-root_mib", Input).placeholder == "MiB"
        assert screen.query_one("#field-home_mib", Input).placeholder == "MiB"


async def test_a_create_failure_flashes_the_daemons_line() -> None:
    data = FakeData([])
    data.fail.add("create")
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.submit()
        await wait_for(lambda: "create failed" in status_text(app))
        await pilot.pause()


# -- the workspace page ---------------------------------------------------


async def test_the_page_shows_the_consent_state(monkeypatch) -> None:
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "api.example:443" in consent_text(app))
        assert "mode interactive" in consent_text(app)
        # The granted scope, with its expiry.
        assert "no active consent" not in consent_text(app)


async def test_the_page_names_an_empty_grant_set(monkeypatch) -> None:
    scripted_link(monkeypatch, [rules_frame(mode="allow")])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "mode allow" in consent_text(app))
        assert "no active consent" in consent_text(app)


async def test_the_header_splits_the_name_from_the_metadata(
    monkeypatch,
) -> None:
    """#351: the name and its status own the header's first line —
    the status in its state's color, as the list colors it — and
    the id, image hash, host, and created date read muted on the
    second."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "stopped" in header_text(app))
        assert "alpha" in header_text(app)
        assert WS in meta_text(app)
        assert "host-1" in meta_text(app)
        assert "2026-01-02" in meta_text(app)
        header = app.screen.query_one("#header", Static)
        (span,) = header.content.spans
        assert header.content.plain[span.start : span.end] == "stopped"
        assert span.style == main_app.muted_style(app.theme_variables)
        # The meta line renders dimmer than the name line's default
        # foreground — muted beside prominent, both on the panel.
        name = next(s for s in header.render_line(0) if "alpha" in s.text)
        meta = app.screen.query_one("#header-meta", Static)
        (meta_seg,) = [s for s in meta.render_line(0) if s.text.strip()]
        assert sum(meta_seg.style.color.triplet) < sum(
            name.style.color.triplet
        )


async def test_the_header_truncates_gracefully_at_eighty_columns(
    monkeypatch,
) -> None:
    """#351: at 80 columns — a full id, a 12-character image hash,
    a full host name — the muted meta line truncates at the
    terminal's edge (an ellipsis marks the cut) and never wraps,
    and the name still reads in full on its own line."""
    scripted_link(monkeypatch, [])
    data = FakeData(
        [
            row(
                id="a1b2c3d4e5",
                name="a-very-long-workspace-name",
                host="workstation-3.lab.example.internal.company.net",
            )
        ]
    )
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "a-very-long" in header_text(app))
        header = app.screen.query_one("#header", Static)
        meta = app.screen.query_one("#header-meta", Static)
        assert header.region.height == 1
        assert meta.region.height == 1  # crops at the edge, never wraps
        name_line = "".join(s.text for s in header.render_line(0))
        assert "a-very-long-workspace-name" in name_line  # reads in full
        meta_line = "".join(s.text for s in meta.render_line(0))
        assert meta_line.rstrip().endswith("…")  # the cut, marked
        # The host reads up to the edge and cuts mid-word; the
        # created date leaves with it.
        assert "workstation-3.lab.example.inte" in meta_line
        assert (
            "workstation-3.lab.example.internal.company.net" not in meta_line
        )
        assert "created" not in meta_line


async def test_a_hold_arriving_opens_the_overlay_by_itself(
    monkeypatch,
) -> None:
    """#358: the page's list carries only the fixed actions; a
    waiting hold counts itself in the header's indicator, and the
    burst's first hold opens the consent overlay by itself — the
    auto-opened panel."""
    scripted_link(monkeypatch, [rules_frame(), request_frame("r9")])
    data = FakeData([row()])
    app, follow = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await wait_for(lambda: "egress to decide: 1" in header_text(app))
        rows = app.screen.query_one("#actions")
        assert "pending" not in rows.children[0].classes
        await wait_for(lambda: on_overlay(app))
        assert app.screen.auto is True  # type: ignore[attr-defined]


async def test_the_consent_action_opens_the_panel_by_hand(
    monkeypatch,
) -> None:
    """#358: the page's consent action row pushes the overlay by
    hand — with nothing pending it is the consent panel, and it
    stays open until the operator closes it."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await pilot.press("down")  # the consent action
        await press_until(pilot, "enter", lambda: on_overlay(app))
        assert app.screen.auto is False  # type: ignore[attr-defined]
        await pilot.press("q")  # the operator's close
        await wait_for(lambda: on_page(app))


async def test_the_page_runs_start_and_stop(monkeypatch) -> None:
    """The fixed actions in walk order (#309, re-pinned #343): a
    shell (a new terminal), consent, the egress-mode switch,
    start, stop — the LLM token's remint stays on the CLI."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        # Down three times lands on start.
        await pilot.press("down", "down", "down")
        await pilot.press("enter")
        await wait_for(lambda: ("start", WS) in data.calls)
        await wait_for(lambda: "running" in header_text(app))
        await pilot.press("down", "enter")
        await wait_for(lambda: ("stop", WS) in data.calls)
        await wait_for(lambda: "stopped" in header_text(app))


# -- the egress-mode switch (#344) -----------------------------------------


async def test_the_page_switches_the_egress_mode(monkeypatch) -> None:
    """Enter on the mode action opens the decider's picker over
    the page, the current mode highlighted; a picked mode goes
    through the policy endpoint, and the consent line names the
    new mode — the reply carries the fresh rules frame, so the
    line flips as the switch lands, pushed frame or not."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "mode interactive" in consent_text(app))
        assert "Switch the egress mode" in action_text(app, 2)
        await pilot.press("down", "down", "enter")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        options = app.screen.query_one("#modes", OptionList)
        assert options.highlighted == 2  # interactive, the snapshot's
        await pilot.press("up", "up")  # allow
        await pilot.press("enter")
        await wait_for(lambda: ("mode", WS, "allow", False) in data.calls)
        await wait_for(lambda: "mode allow" in consent_text(app))
        assert on_page(app)  # the whole switch stayed on the page


async def test_a_static_pick_with_nothing_allowed_confirms_first(
    monkeypatch,
) -> None:
    """A pick of static with nothing effectively allowed — no
    allowlist, no in-effect allowed verdict — asks the same
    offline-workspace question the decider and the CLI ask: a no
    decides nothing, a yes sends the confirmed switch."""
    scripted_link(monkeypatch, [rules_frame(mode="allow")])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "mode allow" in consent_text(app))
        await pilot.press("down", "down", "enter")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        options = app.screen.query_one("#modes", OptionList)
        assert options.highlighted == 0  # allow, the snapshot's mode
        await pilot.press("down")  # static
        await pilot.press("enter")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        question = str(app.screen.query_one("#question", Static).content)
        assert "NXDOMAIN" in question
        await pilot.press("n")  # decline: nothing sent
        await wait_for(lambda: on_page(app))
        await pilot.pause()
        assert data.calls == []
        # The same pick, answered yes: the confirmed switch goes
        # out, and the page names the new posture.
        await pilot.press("enter")  # the mode row keeps focus
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        await pilot.press("down", "enter")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(lambda: ("mode", WS, "static", True) in data.calls)
        await wait_for(lambda: "mode static" in consent_text(app))


async def test_a_refused_switch_names_itself_on_the_page(
    monkeypatch,
) -> None:
    """A refused switch surfaces its reason on the page itself
    (#343): the app-level flash paints the list's status line,
    which the pushed page hides — the refusal owns the page's
    consent line instead."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    data.fail.add("mode")
    data.refusal = "static needs allow_list entries"
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: "mode interactive" in consent_text(app))
        await pilot.press("down", "down", "enter")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        await pilot.press("up", "up")  # allow
        await pilot.press("enter")
        await wait_for(lambda: "mode switch failed" in consent_text(app))
        assert "static needs allow_list entries" in consent_text(app)
        assert on_page(app)


async def test_escape_on_the_picker_decides_nothing(monkeypatch) -> None:
    """Escape closes the picker and decides nothing — the page's
    rows keep focus; with no rules frame landed, the row's
    recorded mode starts highlighted."""
    scripted_link(monkeypatch, [])
    data = FakeData([row(mode="static")])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await pilot.press("down", "down", "enter")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        options = app.screen.query_one("#modes", OptionList)
        assert options.highlighted == 1  # static, the row's mode
        await pilot.press("escape")
        await wait_for(lambda: on_page(app))
        await pilot.pause()
        assert data.calls == []


# -- the new-terminal shell action (#341) ----------------------------------


def test_the_ssh_child_argv_spawns_this_client() -> None:
    """The spawned ssh invocation: this client's own interpreter
    and module (an editable checkout spawns itself; an installed
    client its own environment), then the ssh command and the
    workspace — ssh over the console because a fresh window gets
    resized, and the console sizes its guest pty once, at connect.
    The child reaches the same daemon through the environment the
    tree's bootstrap materialized."""
    assert main_app.ssh_child_argv("w1") == [
        sys.executable,
        "-m",
        "msks.client.cli",
        "ssh",
        "w1",
    ]


async def test_spawn_window_detaches_quietly() -> None:
    """The spawn runs detached with its stdio on devnull: the
    window borrows no terminal the tree holds."""
    proc = await main_app.spawn_window([sys.executable, "-c", "pass"])
    assert await asyncio.wait_for(proc.wait(), 10) == 0


async def test_the_new_terminal_action_spawns_an_ssh_child(
    monkeypatch,
) -> None:
    """Enter on the page's second action (#341): the configured
    launcher runs an ssh invocation — ssh carries a live window's
    resizes; the console sizes its pty once — appended, and the
    tree keeps running beside the window."""
    scripted_link(monkeypatch, [])
    spawned: list[list[str]] = []

    async def record(argv):
        spawned.append(argv)

        async def closed():
            return 0

        return SimpleNamespace(wait=closed)

    monkeypatch.setattr(main_app, "spawn_window", record)
    data = FakeData([row()])
    conf = SimpleNamespace(terminal_open_cmd=["kitty", "-e"])
    app = MsksTuiApp(TuiFollow(), data=data, conf=conf)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        assert "new terminal" in action_text(app, 0)
        await press_until(pilot, "enter", lambda: len(spawned) == 1)
        assert spawned[0] == [
            "kitty",
            "-e",
            sys.executable,
            "-m",
            "msks.client.cli",
            "ssh",
            WS,
        ]
        await wait_for(lambda: "opened a shell window" in consent_text(app))
        assert on_page(app)  # the tree kept running
        # The hold keeps a running window's task referenced; the
        # done-callback drops it once the window has closed.

        async def closed():
            return 0

        app.hold_child(SimpleNamespace(wait=closed))
        assert len(app.reapers) == 1  # held while the window runs
        await wait_for(lambda: not app.reapers)  # dropped at close


async def test_a_dead_launcher_falls_back_to_this_terminal(
    monkeypatch,
) -> None:
    """A launcher that cannot start (a missing binary) seeds the
    restarted tree's flash with its reason — the exiting tree's
    own status line dies with it — and takes the same-terminal
    shell flow instead, the documented fallback (#341)."""
    scripted_link(monkeypatch, [])

    async def refused(argv):
        raise FileNotFoundError("xterm")

    monkeypatch.setattr(main_app, "spawn_window", refused)
    data = FakeData([row()])
    follow = TuiFollow()
    app = MsksTuiApp(follow, data=data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await pilot.press("enter")
        await pilot.pause()
    assert follow.take() == (FLOW_SHELL, WS)
    assert "shell window failed" in (follow.seed or "")


async def test_the_page_stops_deciding_when_it_closes(monkeypatch) -> None:
    factory = scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_page(pilot, app)
        await wait_for(lambda: "api.example:443" in consent_text(app))
        assert page.link is not None and page.link._task is not None
        await pilot.press("escape")
        await wait_for(lambda: on_main(app))
        assert page.link._task is None  # unmount stopped the link
        await asyncio.sleep(0.05)
        assert len(factory.made) == 1  # and nothing reconnected


# -- the follow-up queue and the runner ------------------------------------


def test_the_follow_queue_takes_once() -> None:
    follow = TuiFollow()
    assert follow.take() is None
    follow.request(FLOW_SHELL, "ws")
    assert follow.take() == (FLOW_SHELL, "ws")
    assert follow.take() is None


def test_run_main_tui_chains_the_flows(monkeypatch) -> None:
    runs: list[int] = []
    flows: list[tuple] = []

    class FakeApp:
        def __init__(self, follow, data=None, conf=None):
            self.follow = follow

        def run(self):
            runs.append(1)
            # The operator opens a shell window whose launcher is
            # dead (the fallback chains), then quits.
            if len(runs) == 1:
                self.follow.request(FLOW_SHELL, "ws-9")

    monkeypatch.setattr(main_app, "MsksTuiApp", FakeApp)
    monkeypatch.setattr(
        main_app,
        "FLOWS",
        {
            FLOW_SHELL: lambda ws: flows.append(("shell", ws)),
        },
    )
    assert main_app.run_main_tui(data=object()) == 0
    assert len(runs) == 2
    assert flows == [("shell", "ws-9")]


def test_run_main_tui_fails_cleanly_without_a_token(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(main_app, "require_terminal", lambda: None)
    monkeypatch.delenv("MSKSC_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="MSKSC_TOKEN"):
        main_app.run_main_tui()


def test_the_tui_subcommand_dispatches(monkeypatch) -> None:
    launched: list = []

    def fake_run(open_ref=None, data=None, conf=None):
        launched.append(open_ref)
        return 0

    monkeypatch.setattr(cli, "run_main_tui", fake_run)
    assert cli.main(["tui"]) == 0
    assert cli.main(["tui", "my-workspace"]) == 0
    assert launched == [None, "my-workspace"]


def test_a_bare_msks_launches_the_tui(monkeypatch) -> None:
    """`msks` with no arguments at all is the TUI (#309) — not a
    usage error."""
    launched: list = []

    def fake_run(open_ref=None, data=None, conf=None):
        launched.append(open_ref)
        return 0

    monkeypatch.setattr(cli, "run_main_tui", fake_run)
    assert cli.main([]) == 0
    assert launched == [None]


async def test_the_tree_reopens_the_page_it_left(monkeypatch) -> None:
    scripted_link(monkeypatch, [rules_frame()])
    follow = TuiFollow()
    follow.reopen = "alpha"
    data = FakeData([row()])
    app = MsksTuiApp(follow, data=data)
    async with app.run_test() as pilot:
        await wait_for(lambda: on_page(app))
        assert follow.reopen == WS
        await pilot.press("escape")
        await wait_for(lambda: on_main(app) and follow.reopen is None)


# -- the decider link ------------------------------------------------------


async def test_the_link_lands_frames_and_stops() -> None:
    factory = FakeFactory(
        [FakeWS([rules_frame(), request_frame("r1")]), FakeWS([])]
    )
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.01, 0.01))
    link.start()
    await wait_for(lambda: link.controller.rules is not None)
    await wait_for(lambda: len(link.controller.pending) == 1)
    assert link.state == "connected"
    link.stop()
    await asyncio.sleep(0.05)
    assert len(factory.made) == 1  # no reconnect after a clean stop


async def test_the_link_ends_on_a_rejected_registration() -> None:
    frames = [
        frame("egress.decider_rejected", {"reason": "unknown workspace"})
    ]
    factory = FakeFactory([FakeWS(frames), FakeWS([])])
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.01,))
    link.start()
    await wait_for(lambda: link.state == link_mod.REJECTED)
    assert "unknown workspace" in link.reject_reason
    await asyncio.sleep(0.05)
    assert len(factory.made) == 1  # a rejection is terminal
    link.stop()


async def test_the_link_reconnects_after_a_drop() -> None:
    factory = FakeFactory(
        [
            FakeWS([], close_code=1011),
            FakeWS([rules_frame()]),
        ]
    )
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.01,))
    link.start()
    await wait_for(lambda: link.controller.rules is not None)
    assert len(factory.made) == 2
    link.stop()


async def test_a_healthy_drop_restarts_the_ladder_at_its_first_rung(
    monkeypatch,
) -> None:
    """#319: the reset after a healthy connection used to fall
    through to the ladder's cap (a negative index into the delay
    tuple), so a drop after minutes of quiet waited the slowest
    delay. The restart lands on the first rung."""
    delays = (0.01, 0.02, 0.3)
    seen: list[float] = []
    real_backoff = link_mod.backoff

    def spying_backoff(ds, attempt):
        delay = real_backoff(ds, attempt)
        seen.append(delay)
        return delay

    monkeypatch.setattr(link_mod, "backoff", spying_backoff)
    factory = FakeFactory(
        [FakeWS([], close_code=1011), FakeWS([], close_code=1011)]
    )
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=delays)
    link.start()
    # >=, not ==: both scripted sockets close instantly, so the
    # ladder's 0.01s first rung can raise a third connection
    # between two 0.02s polls — an equality poll then waits on a
    # count that never comes back (#322 leftover).
    await wait_for(lambda: len(factory.made) >= 2)
    link.stop()
    assert seen and seen[0] == delays[0]  # the first rung, not the cap


# -- the data seam ---------------------------------------------------------


async def test_tui_data_speaks_the_rest_surface(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://api.test")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(data_mod, "invoking_user", lambda: "ops")
    seen: list[tuple] = []
    seen_pub: list[str] = []
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if (
            request.method == "GET"
            and request.url.path == "/api/v1/workspaces"
        ):
            return httpx.Response(200, json=[{"id": "ws1", "name": "n"}])
        if request.method == "GET" and request.url.path == "/api/v1/images":
            return httpx.Response(
                200, json=[{"name": "debian-13", "version": "2026.01"}]
            )
        if (
            request.method == "GET"
            and request.url.path == "/api/v1/create-defaults"
        ):
            return httpx.Response(
                200, json={"root_mib": 10240, "home_mib": 20480}
            )
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(
                200,
                json={
                    "public_key": f"{seen_pub[0]} minted",
                    "private_key": None,
                },
            )
        if request.url.path == "/api/v1/workspaces":
            body = json.loads(request.content)
            seen_pub.append(body["ssh_pubkey"])
            return httpx.Response(
                201,
                json={"id": "ws1", "name": "n", "status": "created"},
            )
        if request.method == "PUT" and request.url.path.endswith(
            "/egress/policy"
        ):
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "workspace_id": "ws1",
                    "mode": "static",
                    "allow_list": [],
                    "allowed": [],
                    "denied": [],
                    "applied": True,
                },
            )
        return httpx.Response(200, json={"id": "ws1", "status": "running"})

    data = data_mod.TuiData(transport=httpx.MockTransport(handler))
    assert await data.workspaces() == [{"id": "ws1", "name": "n"}]
    assert await data.images() == [{"name": "debian-13", "version": "2026.01"}]
    assert await data.create_defaults() == {
        "root_mib": 10240,
        "home_mib": 20480,
    }
    created, path = await data.create({"name": "n"})
    assert created["id"] == "ws1"
    # The client mint's no-escrow exchange, in order after the
    # listing.
    post = seen.index(("POST", "/api/v1/workspaces"))
    assert seen[post + 1] == ("GET", "/api/v1/workspaces/ws1/ssh-key")
    assert path is not None and path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert await data.start("ws1") == {"id": "ws1", "status": "running"}
    assert await data.stop("ws1") == {"id": "ws1", "status": "running"}
    assert await data.remove("ws1") == {"id": "ws1", "status": "running"}
    # The policy PUT (#344): confirm_empty rides only when set —
    # the daemon's refusal names it.
    reply = await data.set_egress_mode("ws1", "interactive")
    assert reply["mode"] == "static" and reply["applied"] is True
    await data.set_egress_mode("ws1", "static", confirm_empty=True)
    assert bodies == [
        {"mode": "interactive"},
        {"mode": "static", "confirm_empty": True},
    ]
    assert (
        "PUT",
        "/api/v1/workspaces/ws1/egress/policy",
    ) in seen
    # The verdict seams (#358): the overlay's decide POST and the
    # rules screen's revoke DELETE — the same exchanges the egress
    # subcommands make.
    assert await data.decide("ws1", "r9", "allow", "5m")
    assert await data.revoke("ws1", "r9")
    assert (
        "POST",
        "/api/v1/workspaces/ws1/egress/requests/r9",
    ) in seen
    assert (
        "DELETE",
        "/api/v1/workspaces/ws1/egress/requests/r9",
    ) in seen


# -- the pure helpers ------------------------------------------------------


def pinned_clock(monkeypatch, when: str = "2026-01-04T12:00:00") -> None:
    """Pin the relative labels' clock (#350): the fixture row's
    stamp (Jan 2) reads ``2d ago`` under the pinned date."""
    monkeypatch.setattr(
        main_app, "clock_now", lambda: datetime.fromisoformat(when)
    )


def test_the_line_helpers() -> None:
    assert main_app.workspace_label(row()) == "alpha"
    assert main_app.workspace_label(row(name=None)) == WS
    listing = main_app.row_content(row())
    assert "alpha" in listing.plain
    assert "stopped" in listing.plain
    assert "interactive" in listing.plain
    # The header's name line (#351): the name in the default
    # foreground, the status beside it carrying the status color.
    name = main_app.header_name(row())
    assert "alpha" in name.plain
    (span,) = name.spans
    assert name.plain[span.start : span.end] == "stopped"
    assert span.style == main_app.muted_style({})
    assert "egress to decide" not in name.plain
    assert "egress to decide: 2" in main_app.header_name(row(), 2).plain
    # A wide-character name keeps the span on the status: the
    # offsets are codepoints, like the listing's span.
    wide = main_app.header_name(row(name="北" * 16))
    (wide_span,) = wide.spans
    assert wide.plain[wide_span.start : wide_span.end] == "stopped"
    # The meta line: the id, the image hash, the host, the date.
    meta = main_app.header_meta(row())
    assert WS in meta and "host-1" in meta and "2026-01-02" in meta
    assert f"image {'a' * 12}" in meta
    assert main_app.created_note(row(id="x"), None) == "created alpha (id x)"
    assert "identity" in main_app.created_note(row(id="x"), "/tmp/id")


def test_the_listing_columns_line_up(monkeypatch) -> None:
    """#347: every field pads to its column's width, so the status,
    egress, image, and date start at the same offset in every row —
    and the header's labels ride the same offsets. A name longer
    than its column clips at its middle instead of pushing the rest
    of the row sideways, and the offsets are display cells: a
    wide-character name cannot shift the columns either. The
    status and egress cells clip to their columns too, so a
    vocabulary the daemon grows cannot misalign a row."""
    pinned_clock(monkeypatch)  # the created cell reads "2d ago"
    short = main_app.row_content(row(name="ab")).plain
    long_name = main_app.row_content(row(name="n" * 40)).plain
    wide = main_app.row_content(row(name="北" * 16)).plain
    long_status = main_app.row_content(
        row(name="ab", status="provisioning")
    ).plain
    header = main_app.list_header()
    for label, cell in (
        ("STATUS", "stopped"),
        ("EGRESS", "interactive"),
        ("IMAGE", "aaaaa…aaaaaa"),
        ("CREATED", "2d ago"),
    ):
        offsets = {
            cell_len(text[: text.index(cell)]) for text in (short, wide)
        }
        assert len(offsets) == 1
        assert cell_len(header[: header.index(label)]) == offsets.pop()
    # A clipped name keeps the columns; a wide name pads to the
    # same display width (32 cells of CJK land at 22 by clipping).
    assert "…" in long_name
    assert cell_len(wide[: wide.index("stopped")]) == cell_len(
        short[: short.index("stopped")]
    )
    # An over-wide status clips inside its column, not past it:
    # the egress column still starts where the short row's does.
    assert "…" in long_status
    assert cell_len(long_status[: long_status.index("interactive")]) == (
        cell_len(short[: short.index("interactive")])
    )
    # The head helper keeps a text that already fits its budget
    # whole (a clip's head call can never exhaust it — the guard
    # saw the full text wider than the column — so it is pinned
    # here directly).
    assert main_app.cell_prefix("北a", 3) == "北a"


def test_the_created_column_buckets_by_the_pinned_rule() -> None:
    """#350: the CREATED column reads a relative label, bucketed
    by one pinned rule over whole calendar days between the
    creation and the clock — today and yesterday for the first
    two days, ``Nd ago`` under a week, ``Nw ago`` under a month
    (13 days reads ``1w ago``), ``Nmo ago`` under a year, ``Ny
    ago`` past it. The rule reads calendar days, not 24-hour
    spans: a stamp 20 hours old that crossed midnight reads
    yesterday. A clock that trails its stamp (skew) stays at
    today, and a stamp that is missing or unparseable reads
    ``-``."""
    label = main_app.created_label
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    assert label(None) == "-"
    assert label("") == "-"
    assert label("not a stamp") == "-"
    assert label(now.isoformat(), now) == "today"
    # Skew: the stamp sits after the clock.
    assert label("2026-06-16T00:00:00Z", now) == "today"
    # Calendar days: 20 hours that crossed midnight read
    # yesterday.
    assert label("2026-06-14T16:00:00Z", now) == "yesterday"
    # The stamp reads in the clock's own calendar day: the same
    # stamp, 16:00 UTC on the 14th, is already the 15th at UTC+13
    # — the operator's day reads it today (a dropped conversion
    # would read yesterday).
    late = datetime(2026, 6, 15, 8, 0, 0, tzinfo=timezone(timedelta(hours=13)))
    assert label("2026-06-14T16:00:00Z", late) == "today"
    for days, expected in (
        (2, "2d ago"),
        (6, "6d ago"),
        (7, "1w ago"),
        (13, "1w ago"),
        (29, "4w ago"),
        (30, "1mo ago"),
        (364, "12mo ago"),
        (365, "1y ago"),
        (400, "1y ago"),
        (730, "2y ago"),
    ):
        stamp = (now - timedelta(days=days)).isoformat()
        assert label(stamp, now) == expected, days


async def test_the_listing_header_row_shows_with_rows() -> None:
    """#347: the column labels ride above the rows; the empty
    state takes the header's place when the last row leaves."""
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        columns = app.query_one("#columns", Static)
        await wait_for(lambda: columns.display)
        await wait_for(lambda: "alpha" in row_text(app, 0))
        # The header's labels line up with the row's cells on the
        # rendered screen — content_region, not region: padding
        # shifts the content box, and region stays 0 under either.
        # The first row needs its layout settled first — the
        # frame's layout lands one refresh after its content.
        first = app.query_one("#rows").children[0]
        await wait_for(lambda: first.content_region.width > 0)
        assert columns.content_region.x == first.content_region.x
        data.rows = []
        await pilot.press("r")
        await wait_for(lambda: list_children(app) == 0)
        assert not columns.display


async def test_the_listing_renders_inside_a_frame(monkeypatch) -> None:
    """#349: the listing — header row and rows together — renders
    inside a rounded border, the same framing the create form
    carries, so the rows read as one framed table between the
    status bar and the footer; the status bar above stays a
    borderless line, and the create form keeps its framing."""
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: list_children(app) == 1)
        listing = app.query_one("#listing")
        primary = Color.parse(app.theme_variables["primary"])
        assert listing.styles.border_top == ("round", primary)
        assert {edge for edge, _color in listing.styles.border} == {"round"}
        # The rows sit inside the frame: the content starts past
        # the border's column.
        assert app.query_one("#columns").content_region.x >= 2
        # The status bar above stays a borderless line.
        assert app.query_one("#status").styles.border_top != (
            "round",
            primary,
        )
        # The create form keeps its own framing.
        await press_until(
            pilot, "c", lambda: type(app.screen).__name__ == "CreateScreen"
        )
        form = app.screen.query_one("#form")
        assert form.styles.border_top == ("round", primary)


async def test_the_status_bar_weights_the_count_over_the_url(
    monkeypatch,
) -> None:
    """#349: the workspace count reads in the bold default
    foreground — the fact that moves while the operator works —
    and the daemon's URL rides after it in muted text; at 80
    columns the status bar stays one line."""
    scripted_link(monkeypatch, [])
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:8660")
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _ = make_app(data)
    async with app.run_test():
        await wait_for(lambda: "2 workspaces" in status_text(app))
        status = app.query_one("#status", Static)

        def segments():
            return [s for s in status.render_line(0) if s.text.strip()]

        await wait_for(lambda: len(segments()) >= 2)
        count = next(s for s in segments() if "2 workspaces" in s.text)
        url = next(s for s in segments() if "127.0.0.1:8660" in s.text)
        assert count.style.bold
        assert not url.style.bold
        # Muted: dimmer than the count's default foreground.
        assert sum(url.style.color.triplet) < sum(count.style.color.triplet)
        # One line at the 80-column terminal.
        assert status.size.height == 1


async def test_a_long_daemon_url_keeps_the_status_bar_one_line(
    monkeypatch,
) -> None:
    """#349: a long MSKSC_URL clips to its budget (the middle
    ellipsis keeping the port half) so the standing line stays one
    row at 80 columns — the URL is a hint, not data."""
    long_url = "https://msks-daemon.really-long-hostname.example.internal:8660"
    monkeypatch.setenv("MSKSC_URL", long_url)
    content = main_app.status_content(2, long_url)
    assert "…" in content.plain and "8660" in content.plain
    assert cell_len(content.plain) <= 78
    scripted_link(monkeypatch, [])
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _ = make_app(data)
    async with app.run_test():
        await wait_for(lambda: "2 workspaces" in status_text(app))
        status = app.query_one("#status", Static)
        assert status.size.height == 1


async def test_a_scrolling_list_keeps_the_created_label(
    monkeypatch,
) -> None:
    """#349/#350: the frame's border and the scrollbar both spend
    cells of the 80-column row — the widest label the created
    column reads (``yesterday``) still renders whole on the first
    row of a list long enough to scroll."""
    # The pinned clock sits one day past the fixture stamp (Jan
    # 2), so the created cell reads "yesterday" — the widest
    # label the pinned rule renders.
    monkeypatch.setattr(
        main_app,
        "clock_now",
        lambda: datetime.fromisoformat("2026-01-03T12:00:00"),
    )
    rows = [
        dict(row(), name=f"ws-{i:02d}", id=f"ws-{i:02d}") for i in range(30)
    ]
    data = FakeData(rows)
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)):
        await wait_for(lambda: list_children(app) == 30)
        first = app.query_one("#rows").children[0]

        def first_static():
            """The row's inner Static, or None while the compose
            stream lags the row count (the suite's known rule)."""
            try:
                return first.query_one(Static)
            except NoMatches:
                return None

        await wait_for(lambda: first_static() is not None)
        await wait_for(
            lambda: (s := first_static()) is not None and s.region.width > 0
        )
        rendered = "".join(seg.text for seg in first_static().render_line(0))
        assert "yesterday" in rendered
        assert cell_len(rendered) <= 74


async def test_the_status_column_carries_its_states_color(
    monkeypatch,
) -> None:
    """#348: the status cell alone carries a color — running in
    the theme's success color, stopped in muted text, any other
    state in the warning color — while the name, egress, image,
    and date keep the default foreground. The colors are theme
    variables the render resolves against the active theme, and
    each row carries its status as a class."""
    pinned_clock(monkeypatch)  # the created cell reads "2d ago"
    data = FakeData(
        [
            row(status="stopped"),
            row(id="ws-new", name="gamma", status="created"),
            row(id="ws-run", name="beta", status="running"),
        ]
    )
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: list_children(app) == 3)
        rows = app.query_one("#rows")

        def row_static(item):
            """The row's inner Static, or None while it is still
            mounting (the compose stream lags the row count)."""
            try:
                return item.query_one(Static)
            except NoMatches:
                return None

        def row_segments(item):
            """The row's rendered segments, or None before the
            first render."""
            static = row_static(item)
            if static is None:
                return None
            segs = [s for s in static.render_line(0) if s.text.strip()]
            return segs or None

        await wait_for(
            lambda: all(row_segments(item) for item in rows.children)
        )
        # The highlight composes its own color over the row it
        # sits on, so the exact theme-color checks ride the two
        # unfocused rows: walk the highlight to the last one.
        await pilot.press("down", "down")
        await wait_for(
            lambda: (
                "-highlight" in rows.children[2].classes
                and "-highlight" not in rows.children[0].classes
            )
        )
        muted = main_app.muted_style(app.theme_variables)
        for item, name, style, status in zip(
            rows.children,
            ("alpha", "gamma", "beta"),
            (muted, "$warning", "$success"),
            ("stopped", "created", "running"),
            strict=True,
        ):
            static = row_static(item)
            content = static.content
            (span,) = content.spans
            # The span covers the status cell alone, and the row's
            # class is the status itself.
            assert content.plain[span.start : span.end] == status
            assert span.style == style
            assert status in item.classes
            # The rendered segments carry the acceptance colors:
            # the status takes its state's theme color while the
            # name and the date share the row's default
            # foreground — the same color both plain columns
            # share, whatever the highlight does to the row. The
            # muted entry rides the theme's own ratio (see its
            # branch below for how the rendered color is pinned).
            segs = row_segments(item)

            def segment(text):
                return next(s for s in segs if text in s.text)

            rendered = tuple(segment(status).style.color.triplet)
            name_color = tuple(segment(name).style.color.triplet)
            date_color = tuple(segment("2d ago").style.color.triplet)
            if status == "running":
                # The focused row composes the highlight over the
                # span, so its status stands out from its own
                # name without matching the raw theme color.
                assert rendered != name_color
            elif status == "created":
                assert (
                    rendered == Color.parse(app.theme_variables["warning"]).rgb
                )
            else:
                # Muted text: dimmer than the row's own default
                # foreground (the muted color rides the theme's
                # ratio through the span style above — the auto
                # base of "$text-muted" composes a few values
                # differently in a span than in widget css, so the
                # rendered muted color is pinned by luminance,
                # not by equality with the header's color).
                assert sum(rendered) < sum(name_color)
                assert rendered != name_color
            assert date_color == name_color


def test_a_status_outside_the_map_still_names_itself() -> None:
    """#348: a state the map does not know takes the warning
    color, and a status that does not read as one ASCII CSS word
    names the row ``other`` (Textual's class names are ASCII — a
    wider word would raise, so the guard hands it the bucket
    class instead)."""
    assert main_app.status_color("paused") == "$warning"
    assert main_app.status_color("running") == "$success"
    assert (
        main_app.status_color("stopped", {"text-muted": "auto 40%"})
        == "$text 40%"
    )
    assert main_app.status_class("paused") == "paused"
    assert main_app.status_class("not running") == "other"
    assert main_app.status_class("") == "other"
    assert main_app.status_class("статус") == "other"
    assert main_app.status_class("状態") == "other"


def test_the_muted_style_rides_the_theme_ratio() -> None:
    """#348: the muted color rides the theme's text variable at
    the theme's own muted ratio, and falls back to the 60%
    Textual's own themes use when a theme spells its muted color
    without a ratio."""
    assert main_app.muted_style({"text-muted": "auto 60%"}) == "$text 60%"
    assert main_app.muted_style({"text-muted": "auto 40%"}) == "$text 40%"
    assert main_app.muted_style({"text-muted": "#888888"}) == "$text 60%"
    assert main_app.muted_style({}) == "$text 60%"


def test_the_flash_line_expires() -> None:
    flash = main_app.FlashLine()
    assert flash.text("default") == "default"
    flash.set("held")
    assert flash.text("default") == "held"
    flash.until = 0.0
    assert flash.text("default") == "default"


# -- the coverage corners ---------------------------------------------------


async def test_the_refused_close_retries_slowly(monkeypatch) -> None:
    """A 4401 close labels itself and keeps retrying — the interval
    is the module's, patched here so the retry lands in test time."""
    monkeypatch.setattr(link_mod, "REFUSED_RETRY_INTERVAL", 0.3)
    factory = FakeFactory(
        [FakeWS([], close_code=4401), FakeWS([rules_frame()])]
    )
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.01,))
    link.start()
    await wait_for(lambda: link.state == link_mod.REFUSED)
    await wait_for(lambda: len(factory.made) == 2)  # the retry dialed
    await wait_for(lambda: link.controller.rules is not None)
    link.stop()


async def test_the_link_survives_a_dial_failure() -> None:
    dials = []

    class Broken:
        def __init__(self):
            dials.append(1)

        def __aenter__(self):
            raise RuntimeError("dial failed")

        def __aexit__(self, *exc):
            return None

    link = DeciderLink(WS, ws_factory=Broken, reconnect_delays=(0.01,))
    link.start()
    # The initial state is already "reconnecting" — the second dial
    # is the proof the except path ran and the loop climbed its
    # ladder.
    await wait_for(lambda: len(dials) >= 2)
    link.stop()


async def test_the_link_reconnects_past_a_serving_error() -> None:
    class Exploding(FakeWS):
        async def __anext__(self) -> str:
            raise RuntimeError("view bug")

    factory = FakeFactory([Exploding([]), FakeWS([rules_frame()])])
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.01,))
    link.start()
    await wait_for(lambda: link.controller.rules is not None)
    assert len(factory.made) == 2
    link.stop()


async def test_start_and_stop_are_idempotent() -> None:
    link = DeciderLink(WS, ws_factory=FakeFactory([]))
    link.stop()  # nothing started: a no-op
    link.start()
    link.start()  # already running: a no-op
    link.stop()
    await asyncio.sleep(0)
    assert link._task is None


def test_the_consent_line_names_a_rejection() -> None:
    link = DeciderLink(WS)
    link.state = link_mod.REJECTED
    link.reject_reason = "unknown workspace"
    assert "unknown workspace" in main_app.consent_line(link, row())


def test_the_default_flow_runners(monkeypatch) -> None:
    ran: list[tuple] = []

    monkeypatch.setattr(
        main_app, "run_workspace_shell", lambda ws: ran.append(("shell", ws))
    )
    main_app.run_follow_up((FLOW_SHELL, "w2"))
    assert ran == [("shell", "w2")]


def test_run_main_tui_pre_flights_the_env(monkeypatch) -> None:
    """The env and TLS context are read before the first screen
    draws; with them patched, one no-op app run returns success."""

    class QuietApp:
        def __init__(self, follow, data=None, conf=None):
            pass

        def run(self):
            return None

    monkeypatch.setattr(main_app, "require_terminal", lambda: None)
    monkeypatch.setenv("MSKSC_URL", "https://api.test")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    monkeypatch.setattr(main_app, "shared_ssl", lambda: None)
    monkeypatch.setattr(main_app, "MsksTuiApp", QuietApp)
    assert main_app.run_main_tui() == 0


async def test_the_r_key_refreshes_and_q_quits(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: "alpha" in row_text(app, 0))
        fetches = data.fetches
        await pilot.press("r")
        await wait_for(lambda: data.fetches > fetches)
        # q quits the tree: the app exits, the context returns.
        await pilot.press("q")


async def test_a_refused_remove_decides_nothing(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: "alpha" in row_text(app, 0))
        # press_until, not one press: a key landing in the list
        # rebuild's swap window reads as nothing focused and no-ops
        # — no poll window survives a swallowed press (#322).
        await press_until(
            pilot, "D", lambda: type(app.screen).__name__ == "ConfirmScreen"
        )
        await pilot.press("n")
        await wait_for(lambda: on_main(app))
        assert "remove" not in [call[0] for call in data.calls]


async def test_a_reopen_that_finds_nothing_stays_on_the_list(
    monkeypatch,
) -> None:
    scripted_link(monkeypatch, [])
    follow = TuiFollow()
    follow.reopen = "ghost"
    app = MsksTuiApp(follow, data=FakeData([row()]))
    async with app.run_test() as pilot:
        await wait_for(lambda: "alpha" in row_text(app, 0))
        await pilot.pause()


async def test_a_reopen_that_cannot_list_stays_quiet(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    data.fail.add("workspaces")
    follow = TuiFollow()
    follow.reopen = "alpha"
    app = MsksTuiApp(follow, data=data)
    async with app.run_test() as pilot:
        await wait_for(lambda: on_main(app))
        await pilot.pause()


async def test_a_page_that_cannot_refresh_keeps_its_row(monkeypatch) -> None:
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    data.fail.add("workspaces")
    app, _ = make_app(data)
    async with app.run_test():
        app.push_screen(WorkspaceScreen(data.rows[0]))
        await wait_for(lambda: on_page(app))
        await wait_for(lambda: "api.example:443" in consent_text(app))
        assert WS in meta_text(app)  # the stale row still paints


async def test_the_headers_count_follows_the_queue(monkeypatch) -> None:
    """#354: a hold that lands after the page opened raises the
    header's count within the tick, a resolution lowers it, and
    the segment leaves with the last hold — the list keeps only
    its fixed actions throughout."""
    ws = FakeWS([rules_frame()])
    factory = FakeFactory([ws, FakeWS([])])
    monkeypatch.setattr(
        main_app,
        "DeciderLink",
        lambda ws_id: DeciderLink(
            ws_id, ws_factory=factory, reconnect_delays=(0.01, 0.01)
        ),
    )
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        assert "egress to decide" not in header_text(app)
        ws.push(request_frame("late1"))
        # The burst's first hold opens the consent overlay by itself
        # (#358); the header the count rides on sits behind it, so
        # the test parks the panel — the burst stays surfaced by the
        # header alone, exactly the state the count is about.
        await wait_for(lambda: on_overlay(app))
        await pilot.press("q")
        await wait_for(lambda: on_page(app))
        await wait_for(lambda: "egress to decide: 1" in header_text(app))
        ws.push(request_frame("late2"))
        await wait_for(lambda: "egress to decide: 2" in header_text(app))
        ws.push(
            frame(
                "egress.resolved",
                {"request_id": "late1", "decision": "allowed"},
            )
        )
        await wait_for(lambda: "egress to decide: 1" in header_text(app))
        assert action_children(app) == 5
        ws.push(
            frame(
                "egress.resolved",
                {"request_id": "late2", "decision": "denied"},
            )
        )
        await wait_for(lambda: "egress to decide" not in header_text(app))
        # A dropped link stops counting: the dead socket's snapshot
        # may hold holds the server already resolved, and the
        # re-registration clears it anyway. A live link counts
        # again the moment it stands.
        ws.push(request_frame("late3"))
        # The next burst opens the panel again; park it once more.
        await wait_for(lambda: on_overlay(app))
        await pilot.press("q")
        await wait_for(lambda: on_page(app))
        await wait_for(lambda: "egress to decide: 1" in header_text(app))
        assert page.link is not None
        page.link.state = link_mod.RECONNECTING
        page.paint_header()
        assert "egress to decide" not in header_text(app)
        page.link.state = link_mod.CONNECTED
        page.paint_header()
        assert "egress to decide: 1" in header_text(app)
        await pilot.pause()


async def test_the_swap_windows_self_heal(monkeypatch) -> None:
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        # Tear the action list away: the next sync rebuilds it, and
        # the paint paths swallow the missing widgets.
        actions = page.query_one("#actions")
        await actions.remove()
        await page.query_one("#header").remove()
        await page.query_one("#header-meta").remove()
        await page.query_one("#consent").remove()
        page.paint_header()  # swallowed: the worker self-heals
        page.paint_consent()
        page.sync_actions()
        await wait_for(lambda: action_children(app) == 5)


async def test_a_rebuild_over_a_standing_list_keeps_focus(
    monkeypatch,
) -> None:
    """A rebuild that lands while the list stands (a re-armed
    flight over the mount window) swaps the list for a fresh one
    and keeps the focused row by its key."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await pilot.press("down", "down")  # the egress-mode row
        before = page.actions_widget()
        page.rebuilds.request()
        await wait_for(
            lambda: not page.rebuilds.scheduled and not page.rebuilds.pending
        )
        await pilot.pause()
        rows = page.actions_widget()
        assert rows is not None and rows is not before  # a fresh list
        assert rows.index == 2  # the focused row kept by its key
        assert "Switch the egress mode" in action_text(app, 2)


async def test_a_bare_listing_read_in_a_swap_window(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        screen = app.screen
        await wait_for(lambda: "alpha" in row_text(app, 0))
        await screen.query_one("#rows").remove()
        assert screen.rows_widget() is None
        assert screen.focused_row() is None
        await screen.query_one("#status").remove()
        screen.sync_status()  # swallowed: the timer self-heals
        await pilot.pause()


async def test_a_page_without_rows_runs_nothing() -> None:
    """Enter on a page whose rows are gone (a swap window) decides
    nothing — and does not crash."""
    page = WorkspaceScreen(row())
    assert page.focused_action() is None
    assert await page.action_run() is None


async def test_the_form_walks_with_enter_and_the_buttons_finish(
    monkeypatch,
) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "walk-test"
        # Enter in a field moves the walk to the next one (the
        # image select among them — Enter opens it, not walks).
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is screen.query_one("#field-image", Select)
        # The create button submits; the cancel button dismisses
        # with nothing.
        screen.query_one("#do-create", Button).press()
        await wait_for(lambda: data.calls and data.calls[0][0] == "create")
        await press_until(
            pilot, "c", lambda: type(app.screen).__name__ == "CreateScreen"
        )
        app.screen.query_one("#do-cancel", Button).press()
        await wait_for(lambda: on_main(app))
        assert len(data.calls) == 1


async def test_a_start_failure_and_a_remove_failure_flash(monkeypatch) -> None:
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    data.fail.update({"start", "remove"})
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await wait_for(lambda: "alpha" in row_text(app, 0))
        await press_until(pilot, "s", lambda: ("start", WS) in data.calls)
        await wait_for(lambda: "start failed" in status_text(app))
        await press_until(
            pilot, "D", lambda: type(app.screen).__name__ == "ConfirmScreen"
        )
        await press_until(pilot, "y", lambda: ("remove", WS) in data.calls)
        await wait_for(lambda: "remove failed" in status_text(app))
        # A refused answer (n on the same question) decides nothing:
        # the task that would run the delete is given time to land.
        await pilot.press("D")
        await press_until(
            pilot, "n", lambda: type(app.screen).__name__ == "MainScreen"
        )
        await asyncio.sleep(0.1)
        await pilot.pause()
        assert data.calls.count(("remove", WS)) == 1


async def test_page_action_failures_flash(monkeypatch) -> None:
    """A refused page action names itself on the page's consent
    line — the app-level flash paints the list's status line,
    which the pushed page hides (#343), so the page's own actions
    flash on the page."""
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    data.fail.update({"start", "stop"})
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await pilot.press("down", "down", "down")
        await press_until(pilot, "enter", lambda: ("start", WS) in data.calls)
        await wait_for(lambda: "start failed" in consent_text(app))
        await pilot.press("down")
        await press_until(pilot, "enter", lambda: ("stop", WS) in data.calls)
        await wait_for(lambda: "stop failed" in consent_text(app))
        assert "stopped" in header_text(app)  # the row kept its status


async def test_a_row_that_leaves_the_listing_keeps_the_page(
    monkeypatch,
) -> None:
    scripted_link(monkeypatch, [rules_frame()])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        data.rows.clear()  # the workspace left between refreshes
        page.refresh_row()
        await pilot.pause()
        await pilot.pause()
        assert WS in meta_text(app)  # the page keeps its row


async def test_a_bare_page_paints_and_unmounts_quietly(monkeypatch) -> None:
    """A bare, never-mounted page (its link is None) decides
    nothing and crashes nothing in the paint and mode paths — the
    unmount's no-link path included, and a tick under a vanished
    tree stays quiet."""

    def boom():
        raise NoMatches("gone")

    page = WorkspaceScreen(row())
    assert page.pending_count() == 0  # no link: nothing waiting
    page.paint_consent()
    page.paint_header()  # no link: zero holds, the swallow holds
    assert page.page_rules() is None  # no link: nothing to read
    page.land_rules_reply({"mode": "allow"})  # no link: keeps quiet
    monkeypatch.setattr(page, "paint_consent", boom)
    page.tick()  # swallowed: teardown noise, not a crash
    page.on_unmount()


# -- the review fixes -------------------------------------------------------


async def test_a_close_at_the_registration_send_reconnects() -> None:
    """A connection the daemon closes at the registration send (a
    restart, a revoked token) takes the backoff ladder, not a dead
    link — the send lives inside the serve loop's guarded span."""

    class ClosedAtSend(FakeWS):
        async def send(self, text: str) -> None:
            raise websockets.ConnectionClosed(
                websockets.frames.Close(1006, "gone"), None
            )

    factory = FakeFactory([ClosedAtSend([]), FakeWS([rules_frame()])])
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.01,))
    link.start()
    await wait_for(lambda: link.controller.rules is not None)
    assert len(factory.made) == 2  # the drop reconnected
    assert link.state == link_mod.CONNECTED
    link.stop()


async def test_the_consent_line_names_a_drop_after_rules() -> None:
    """Once the rules have landed, a dropped connection still shows
    beside the stale snapshot — silence never reads as data."""
    link = DeciderLink(WS)
    link.controller.apply_frame(rules_frame())
    link.state = link_mod.RECONNECTING
    line = main_app.consent_line(link, row())
    assert "mode interactive" in line
    assert "api.example:443" in line
    assert "reconnecting" in line
    link.state = link_mod.CONNECTED
    assert "connected" not in main_app.consent_line(link, row())


def test_whole_number_refuses_unicode_digits() -> None:
    """A pasted ④ passes isdigit but crashes int — the local check
    refuses what the conversion cannot take."""
    assert main_app.whole_number("4096")
    assert not main_app.whole_number("④")
    assert not main_app.whole_number("-1")


def test_a_refused_flow_returns_to_the_tree(monkeypatch) -> None:
    """A flow that refuses with the CLI's one-line SystemExit (a
    console shell that cannot reach the daemon) hands the terminal
    back to the tree instead of ending the session."""
    runs: list[int] = []

    class FakeApp:
        def __init__(self, follow, data=None, conf=None):
            self.follow = follow

        def run(self):
            runs.append(1)
            if len(runs) == 1:
                self.follow.request(FLOW_SHELL, "ws-9")

    def refused(workspace_id: str) -> None:
        raise SystemExit("msks: cannot reach the daemon")

    monkeypatch.setattr(main_app, "MsksTuiApp", FakeApp)
    monkeypatch.setattr(main_app, "FLOWS", {FLOW_SHELL: refused})
    assert main_app.run_main_tui(data=object()) == 0
    assert len(runs) == 2  # the tree restarted after the refusal


# -- the second review's fixes ----------------------------------------------


async def test_a_clean_close_names_itself_and_reconnects() -> None:
    """websockets exits the async-for normally on an OK close (a
    restarting daemon): the link takes the ladder, and the state
    names the window instead of reading connected on a dead
    socket."""

    class CleanClose(FakeWS):
        async def __anext__(self) -> str:
            if self.frames:
                return self.frames.pop(0)
            raise StopAsyncIteration

    factory = FakeFactory([CleanClose([rules_frame()]), FakeWS([])])
    link = DeciderLink(WS, ws_factory=factory, reconnect_delays=(0.3,))
    link.start()
    await wait_for(lambda: link.state == link_mod.RECONNECTING)
    await wait_for(lambda: len(factory.made) == 2)
    link.stop()


def test_a_refused_flow_seeds_the_restarted_tree(monkeypatch) -> None:
    """The refusal line rides the follow queue: the restarted tree
    flashes it (a SystemExit prints nowhere until the interpreter's
    top level, which the restart would eat)."""
    runs: list[str | None] = []

    class FakeApp:
        def __init__(self, follow, data=None, conf=None):
            self.follow = follow

        def run(self):
            runs.append(self.follow.seed)
            if len(runs) == 1:
                self.follow.request(FLOW_SHELL, "ws-9")

    def refused(workspace_id: str) -> None:
        raise SystemExit("msks: cannot reach the daemon")

    monkeypatch.setattr(main_app, "MsksTuiApp", FakeApp)
    monkeypatch.setattr(main_app, "FLOWS", {FLOW_SHELL: refused})
    assert main_app.run_main_tui(data=object()) == 0
    assert runs == [None, "msks: cannot reach the daemon"]


async def test_the_tree_flashes_a_seeded_refusal(monkeypatch) -> None:
    follow = TuiFollow()
    follow.seed = "msks: cannot reach the daemon"
    app = MsksTuiApp(follow, data=FakeData([]))
    async with app.run_test() as pilot:
        await wait_for(lambda: "cannot reach" in status_text(app))
        assert follow.seed is None  # shown once, then spent
        await pilot.pause()


async def test_a_markup_refusal_never_crashes_the_screen(
    monkeypatch,
) -> None:
    """The daemon echoes operator-typed text back in its refusals —
    a stray rich markup bracket in one must flash literally, not crash
    the tree."""
    scripted_link(monkeypatch, [])
    data = FakeData([])
    data.fail.add("create")
    data.refusal = "no such image: debian-12[/][/]"
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.submit()
        await wait_for(lambda: "create failed" in status_text(app))
        # Rendered literally (rich's escape form in the raw content,
        # the brackets on screen) — no MarkupError, no dead tree.
        assert "no such image: debian-12" in status_text(app)
        await pilot.pause()


async def test_the_form_sets_the_login_user(monkeypatch) -> None:
    """The user field rides the body; blank keeps the invoking
    default (the host whose own name cannot seed a guest account
    can still create from the TUI)."""
    scripted_link(monkeypatch, [])
    data = FakeData([])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await pilot.press("c")
        await wait_for(lambda: type(app.screen).__name__ == "CreateScreen")
        screen = app.screen
        screen.query_one("#field-name", Input).value = "brand-new"
        screen.query_one("#field-user", Input).value = "ops"
        screen.submit()
        await wait_for(lambda: data.calls and data.calls[0][0] == "create")
        assert data.calls[0][1]["user"] == "ops"


def test_the_tui_needs_a_terminal(monkeypatch) -> None:
    """A pipe on either side cannot host the tree — the one-line
    refusal, before any screen draws; a tty on both sides passes
    quietly."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="interactive tty"):
        main_app.run_main_tui()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    main_app.require_terminal()


async def test_a_reopen_never_stacks_a_second_page(monkeypatch) -> None:
    """The operator opening a page by hand while the reopen worker
    fetches wins: the worker's answer arrives to a page already
    open, and it stacks nothing."""
    scripted_link(monkeypatch, [])

    class GatedData(FakeData):
        def __init__(self, rows):
            super().__init__(rows)
            self.gate = asyncio.Event()

        async def workspaces(self):
            await self.gate.wait()
            return await super().workspaces()

    data = GatedData([row()])
    follow = TuiFollow()
    follow.reopen = "alpha"
    app = MsksTuiApp(follow, data=data)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceScreen(data.rows[0]))
        await wait_for(lambda: on_page(app))
        data.gate.set()  # the reopen answer lands on an open page
        await pilot.pause()
        await pilot.pause()
        pages = [
            screen
            for screen in app.screen_stack
            if isinstance(screen, WorkspaceScreen)
        ]
        assert len(pages) == 1

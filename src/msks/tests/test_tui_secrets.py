"""The secrets page (#390): Pilot-driven, fake seams.

The page rides the tree app's data seam (FakeData's secrets
surface, scripted and recorded), the audit view's link rides the
FakeWS/FakeFactory connections the consent suite scripts, and the
pure helpers (coverage, TTL, the audit seed) get direct tests in
test_consent_tui_helpers.py.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from msks.client.tui import rows as rows_mod
from msks.client.tui import secrets as secrets_mod
from msks.client.tui.link import AuditLink
from msks.client.tui.secrets import (
    MINT_NOTE,
    SCOPED_SENTINEL,
    SECRET_TTLS,
    WIDE_SENTINEL,
    MintScreen,
    SecretAuditScreen,
    SecretsScreen,
    SentinelPanel,
    WorkspacePicker,
    coverage_text,
    masked_sentinel,
    osc52_copy,
    osc52_sequence,
    secret_row_cells,
    sentinel_reach,
    split_entries,
    ttl_default,
    ttl_text,
)
from test_consent_overlay import FakeFactory, FakeWS, press_until, wait_for
from test_main_tui import (
    FakeData,
    close_focused,
    failure_detail,
    failure_title,
    list_children,
    make_app,
    on_failure,
    row,
)
from textual.widgets import Button, Input, Select, Static


def secret_row(
    id: int = 1,
    name: str = "github_api",
    workspaces: list[str] | None = None,
    dests: list[str] | None = None,
    expires_at: str | None = None,
    created_at: str = "2030-01-02T03:04:05",
) -> dict:
    """One placeholder row as the listing serves it (#390): no
    sentinel, the coverage as the JSON list ([] daemon-wide)."""
    return {
        "id": id,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": list(dests or ["api.github.com"]),
        "created_at": created_at,
        "expires_at": expires_at,
    }


def audit_row(
    id: int,
    kind: str,
    *,
    workspaces: list[str] | None = None,
    name: str = "github_api",
    created_at: str = "2030-01-02T03:04:05",
) -> dict:
    """One recorded audit row as the listing serves it (#390):
    newest first is the endpoint's order — the caller stacks them
    newest first, as the daemon does."""
    return {
        "id": id,
        "kind": kind,
        "workspaces": list(workspaces or []),
        "name": name,
        "dests": ["api.github.com"],
        "created_at": created_at,
    }


def live_frame(kind: str, **data) -> str:
    """One interceptor audit frame as the daemon publishes it —
    daemon-wide rows carry the ``*`` spelling, scoped rows their
    coverage list."""
    payload = {"name": "github_api"}
    payload.update(data)
    return json.dumps({"event": f"secret.{kind}", "data": payload})


def on_secrets(app) -> bool:
    return isinstance(app.screen, SecretsScreen)


def on_audit(app) -> bool:
    return isinstance(app.screen, SecretAuditScreen)


async def open_secrets(pilot, app) -> SecretsScreen:
    """Press `e` on the workspaces list (#431)."""
    await press_until(pilot, "e", lambda: on_secrets(app))
    return app.screen


def secrets_children(app) -> int:
    """The page's row count; -1 inside a rebuild's swap window (or
    when the page is not on top)."""
    try:
        return len(app.screen.query_one("#secret-rows").children)
    except Exception:
        return -1


def secret_text(app, index: int) -> str:
    """One page row's text; empty while its inner widget is still
    mounting."""
    try:
        return str(
            app.screen.query_one("#secret-rows")
            .children[index]
            .query_one(Static)
            .content
        )
    except Exception:
        return ""


def secrets_status(app) -> str:
    """The page's status line; empty while the screen still
    mounts."""
    try:
        return str(app.screen.query_one("#status", Static).content)
    except Exception:
        return ""


def audit_children(app) -> int:
    """The audit view's row count; -1 inside a rebuild's swap
    window (or when the view is not on top)."""
    try:
        return len(app.screen.query_one("#audit-rows").children)
    except Exception:
        return -1


def audit_text(app, index: int) -> str:
    """One audit row's text; empty while its inner widget is still
    mounting."""
    try:
        return str(
            app.screen.query_one("#audit-rows")
            .children[index]
            .query_one(Static)
            .content
        )
    except Exception:
        return ""


def audit_status(app) -> str:
    """The audit view's status line; empty while the screen still
    mounts."""
    try:
        return str(app.screen.query_one("#audit-status", Static).content)
    except Exception:
        return ""


def audit_empty(app) -> str:
    """The audit view's empty line; empty while the screen still
    mounts."""
    try:
        return str(app.screen.query_one("#audit-empty", Static).content)
    except Exception:
        return ""


async def open_audit(pilot, app, link_factory) -> SecretAuditScreen:
    """Push the audit view over the list with the given link —
    the page's own `e` path lands here."""
    app.push_screen(SecretAuditScreen(app.data, link_factory=link_factory))
    await wait_for(lambda: on_audit(app))
    return app.screen


async def pick_option(pilot, app, downs: int) -> None:
    """Walk a pushed picker and pick — the presses wait for the
    modal's option list to settle first (a press landing before
    its focus does reaches the surface below)."""

    def highlighted():
        try:
            return app.screen.query_one("#pick-options").highlighted
        except Exception:
            return None  # the compose stream settles async

    await wait_for(lambda: highlighted() is not None)
    for _ in range(downs):
        await pilot.press("down")
    await pilot.press("enter")


# -- the main screen's path to the page -----------------------------------


async def test_e_opens_the_page_and_back() -> None:
    """`e` on the workspaces list opens the secrets page (#431),
    Escape returns to the list."""
    data = FakeData([row(), row(id="ws-b", name="beta")])
    data.secret_rows = [secret_row()]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        # The listing holds the workspaces alone (#431): the
        # branch row is gone.
        await wait_for(lambda: list_children(app) == 2)
        page = await open_secrets(pilot, app)
        assert page is app.screen
        await pilot.press("escape")
        await wait_for(lambda: not on_secrets(app))


# -- the page -------------------------------------------------------------


async def test_the_page_lists_rows_with_the_live_countdown(
    monkeypatch,
) -> None:
    """Every row shows coverage, name, destinations, the TTL, and
    the created label; the TTL repaints as the clock moves (#390)."""
    now = {"at": datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)}
    monkeypatch.setattr(rows_mod, "clock_now", lambda: now["at"])
    monkeypatch.setattr(secrets_mod, "clock_now", lambda: now["at"])
    data = FakeData([])
    data.secret_rows = [
        secret_row(id=1, expires_at="2030-06-01T12:59:00"),
        secret_row(
            id=2,
            name="ci",
            workspaces=["ws-a", "ws-b"],
            dests=[".github.com"],
        ),
    ]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: "github_api" in secret_text(app, 0))
        wide = secret_text(app, 0)
        assert wide.startswith("*")  # the daemon-wide row's coverage
        assert "api.github.com" in wide
        assert "59m" in wide  # an hour out, at the pinned clock
        scoped = secret_text(app, 1)
        assert "ws-a,ws-b" in scoped
        assert "never" in scoped
        assert "5mo ago" in scoped  # created 2030-01-02, clock 2030-06-01
        # The tick repaints the TTL cells in place as the clock moves.
        now["at"] = datetime(2030, 6, 1, 12, 30, 0, tzinfo=UTC)
        app.screen.tick()
        await wait_for(lambda: "29m" in secret_text(app, 0))
        assert "2 placeholders" in secrets_status(app)


async def test_a_listing_failure_flashes() -> None:
    data = FakeData([])
    data.fail.add("secrets")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: "listing failed" in secrets_status(app))


async def test_the_empty_daemon_states_it() -> None:
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 0)
        assert "No placeholders" in str(
            app.screen.query_one("#secret-empty", Static).content
        )


async def test_revoke_confirms_then_retires_the_row() -> None:
    """`x` asks first (#390: a revoke retires the row everywhere
    at once); a yes deletes through the seam, a no decides
    nothing."""
    data = FakeData([])
    data.secret_rows = [secret_row(id=7), secret_row(id=8, name="ci")]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 2)
        await pilot.press("x")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("n")  # a no decides nothing
        await wait_for(lambda: on_secrets(app))
        assert data.secret_calls == []
        await pilot.press("x")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(lambda: ("revoke", 7) in data.secret_calls)
        await wait_for(lambda: secrets_children(app) == 1)
        assert "revoked */github_api" in secrets_status(app)


async def test_revoke_names_a_leftover_store_value() -> None:
    data = FakeData([])
    data.secret_rows = [secret_row(id=7)]

    async def revoke_secret(placeholder_id: int) -> dict:
        data.secret_calls.append(("revoke", placeholder_id))
        return {"revoked": placeholder_id, "store_cleaned": False}

    data.revoke_secret = revoke_secret
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await pilot.press("x")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(
            lambda: "store value left behind" in secrets_status(app)
        )


async def test_a_renewal_failure_flashes_on_the_status_line() -> None:
    """A refused renew names itself on the page's status line —
    the app-level guard paints the list's line, which the pushed
    page hides (#343)."""
    data = FakeData([])
    data.secret_rows = [secret_row(id=7)]
    data.fail.add("renew-secret")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "DurationScreen")
        await pilot.press("enter")
        await wait_for(lambda: "renew failed" in secrets_status(app))


async def test_a_revoke_failure_flashes_on_the_status_line() -> None:
    """A refused revoke names itself the same way."""
    data = FakeData([])
    data.secret_rows = [secret_row(id=7)]
    data.fail.add("revoke-secret")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await pilot.press("x")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(lambda: "revoke failed" in secrets_status(app))


async def test_renew_picks_a_duration_and_extends_in_place() -> None:
    """`r` opens the duration picker over TTL-appropriate choices
    (#390); the pick extends the row's lifetime, the sentinel and
    identity untouched."""
    data = FakeData([])
    half_hour_out = (datetime.now(UTC) + timedelta(seconds=1800)).isoformat()
    data.secret_rows = [secret_row(id=7, expires_at=half_hour_out)]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "DurationScreen")
        options = app.screen.query_one("#pick-options")
        labels = [
            str(option.prompt)
            for option in options._options  # noqa: SLF001
        ]
        assert labels == list(SECRET_TTLS)
        assert options.highlighted == 0  # 1h, nearest the 30m left
        await pilot.press("enter")
        await wait_for(lambda: ("renew", 7, 3600) in data.secret_calls)
        await wait_for(
            lambda: (
                "renewed */github_api" in secrets_status(app)
                and "expires " in secrets_status(app)
            )
        )


async def test_a_cancelled_renew_decides_nothing() -> None:
    """A cancelled pick extends nothing."""
    data = FakeData([])
    data.secret_rows = [secret_row()]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await press_until(
            pilot,
            "r",
            lambda: type(app.screen).__name__ == "DurationScreen",
        )
        await pilot.press("escape")
        await wait_for(lambda: on_secrets(app))
        assert data.secret_calls == []


async def test_keys_without_a_focused_row_flash() -> None:
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 0)
        await pilot.press("x")
        await wait_for(lambda: "no placeholder focused" in secrets_status(app))
        await pilot.press("r")
        await pilot.pause()
        assert data.secret_calls == []


async def test_enter_on_a_row_shows_the_sentinel_panel() -> None:
    """Enter on the focused row (#440): the page fetches the row's
    sentinel and opens the panel over it masked — the prefix and
    the reach visible, the body bullets until revealed — and
    closing it clears the text and lands the focus back on the
    same row (moved off the top first, so a reset to the top
    would answer a different id)."""
    data = FakeData([])
    data.secret_rows = [secret_row(), secret_row(id=2, name="beta")]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 2)
        await pilot.press("down")  # the highlight sits on row 2
        await pilot.press("enter")
        await wait_for(lambda: on_panel(app))
        assert data.secret_calls == [("show", 2)]
        text = panel_text(app)
        assert "mskssec2_" + "t" * 43 not in text  # masked on open
        assert "mskssec2_" + "\u2022" * 43 in text
        assert "every accepting workspace" in text
        assert "shown once" not in text
        await pilot.press("s")  # the reveal
        await pilot.pause()
        assert "mskssec2_" + "t" * 43 in panel_text(app)
        sentinel_line = app.screen.query_one("#panel-sentinel", Static)
        await pilot.press("escape")
        await wait_for(lambda: on_secrets(app))
        assert str(sentinel_line.content) == ""  # closed: text cleared
        await pilot.press("enter")  # focus returned to the same row
        await wait_for(lambda: on_panel(app))
        assert data.secret_calls == [("show", 2), ("show", 2)]
        await pilot.press("q")
        await wait_for(lambda: on_secrets(app))


async def test_a_click_on_a_row_shows_its_sentinel() -> None:
    """A mouse click on a row opens the same panel (#440) — the
    clicked row's, scoped prefix and reach named from the string,
    the body masked until the Show button reveals it."""
    data = FakeData([])
    data.secret_rows = [secret_row(id=3, workspaces=["ws-a"], name="scoped")]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await pilot.click("#secret-rows ListItem")
        await wait_for(lambda: on_panel(app))
        assert data.secret_calls == [("show", 3)]
        text = panel_text(app)
        assert "mskssec1_" + "t" * 43 not in text
        assert "the chosen workspaces: ws-a" in text
        await pilot.click("#do-show")
        await wait_for(lambda: "mskssec1_" + "t" * 43 in panel_text(app))


async def test_a_failed_sentinel_fetch_flashes_and_stays() -> None:
    """A refused fetch names itself on the page's status line and
    the page stands — no panel over a refusal."""
    data = FakeData([])
    data.secret_rows = [secret_row()]
    data.fail.add("show-secret")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        await pilot.press("enter")
        await wait_for(lambda: "sentinel failed" in secrets_status(app))
        assert on_secrets(app)


async def test_a_selection_without_its_row_fetches_nothing() -> None:
    """A Selected event whose row is already gone — a rebuild's
    swap window — fetches nothing; the page stands (#440)."""
    data = FakeData([])
    data.secret_rows = [secret_row()]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        event = SimpleNamespace(item=SimpleNamespace(secret_id=404))
        app.screen.on_list_view_selected(event)
        await pilot.pause()
        assert data.secret_calls == []
        assert on_secrets(app)


# -- the mint form (#393) --------------------------------------------------


def on_mint(app) -> bool:
    return isinstance(app.screen, MintScreen)


def on_panel(app) -> bool:
    return isinstance(app.screen, SentinelPanel)


async def open_mint(pilot, app) -> MintScreen:
    """`c` from the page — the operator's own path to the form."""
    await press_until(pilot, "c", lambda: on_mint(app))
    return app.screen


def mint_note(app) -> str:
    """The form's note line; empty while the form still mounts."""
    try:
        return str(app.screen.query_one("#form-note", Static).content)
    except Exception:
        return ""


def panel_text(app) -> str:
    """Every line the panel paints, joined — the surface the
    panel owns."""
    try:
        return "\n".join(
            str(widget.content)
            for widget in app.screen.query(Static)
            if widget.id != "form-note"
        )
    except Exception:
        return ""


def fill_mint(
    screen, *, name: str = "github_api", value: str = "hunter2"
) -> None:
    """Fill the form's plain fields with a body that mints — the
    masked value field included."""
    screen.query_one("#field-name", Input).value = name
    screen.query_one("#field-dests", Input).value = "api.github.com,.gh"
    screen.query_one("#field-value", Input).value = value


async def test_the_form_mints_and_the_panel_masks_until_revealed(
    tmp_path,
) -> None:
    """The create action's whole path (#393): the store check
    rides ahead of the mint, the daemon-wide body carries neither
    a coverage list nor a ttl but does carry the operator's value,
    the panel opens with the sentinel masked (the reach decoded
    from the prefix), the reveal shows it and the hide masks it
    again, and closing clears the text and lands the row on the
    page."""
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        assert form.query_one("#field-value", Input).password  # masked
        fill_mint(form)
        await pilot.click("#do-mint")
        await wait_for(lambda: on_panel(app))
        # The store check rode ahead of the mint.
        assert data.calls == [("secret-check",)]
        assert [call[0] for call in data.secret_calls] == ["mint"]
        body = data.secret_calls[0][1]
        assert body["name"] == "github_api"
        assert body["dests"] == ["api.github.com", ".gh"]
        assert "workspaces" not in body  # the daemon-wide default
        assert "ttl_s" not in body  # unbounded, the daemon's default
        assert body["value"] == "hunter2"
        # The panel opens masked — the prefix and the reach stand,
        # the body is bullets — and the operator's value appears
        # nowhere on the panel.
        sentinel = "mskssec2_" + "s" * 43
        masked = "mskssec2_" + "\u2022" * 43
        await wait_for(lambda: masked in panel_text(app))
        assert sentinel not in panel_text(app)
        assert "hunter2" not in panel_text(app)
        assert "every accepting workspace" in panel_text(app)
        # The reveal — the focused Show button — and the hide.
        await pilot.press("enter")
        await wait_for(lambda: sentinel in panel_text(app))
        assert panel_text(app).count(sentinel) == 1
        await pilot.press("s")  # the hide
        await wait_for(lambda: sentinel not in panel_text(app))
        assert masked in panel_text(app)
        await pilot.press("s")  # revealed again for the close
        await pilot.pause()
        assert sentinel in panel_text(app)
        sentinel_line = app.screen.query_one("#panel-sentinel", Static)
        await pilot.press("q")
        await wait_for(lambda: on_secrets(app))
        assert str(sentinel_line.content) == ""  # closed: text cleared
        # The form's value field cleared on its own dismiss (or the
        # whole form unmounted first): the typed secret leaves the
        # widget tree with the form either way.
        try:
            leftover = form.query_one("#field-value", Input).value
        except Exception:
            leftover = ""  # the form unmounted with the field
        assert leftover == ""
        await wait_for(lambda: secrets_children(app) == 1)
        assert "minted */github_api" in secrets_status(app)


async def test_a_scoped_mint_picks_workspaces_from_the_tree(tmp_path) -> None:
    """A scoped pick shows the picker seeded from the tree's own
    workspace list (#393); the toggled ids ride the body as its
    coverage set, and the sentinel's prefix and reach line name
    the scoped row."""
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        picker = form.query_one("#field-workspaces", WorkspacePicker)
        await wait_for(lambda: picker.option_count == 2)
        assert not form.query_one("#workspaces-row").display  # wide default
        form.query_one("#field-coverage", Select).value = "scoped"
        await pilot.pause()
        assert form.query_one("#workspaces-row").display
        picker.focus()
        await pilot.pause()
        await pilot.press("space")  # alpha — highlighted on focus
        await pilot.press("down")
        await pilot.press("space")  # beta
        fill_mint(form)
        form.query_one("#field-ttl", Select).value = "7d"
        form.submit()
        await wait_for(lambda: on_panel(app))
        body = data.secret_calls[0][1]
        assert body["workspaces"] == ["ws-a", "ws-b"]
        assert body["ttl_s"] == 604800
        assert ("mskssec1_" + "s" * 43) not in panel_text(app)  # masked
        await pilot.press("s")  # the reveal
        text = panel_text(app)
        assert ("mskssec1_" + "s" * 43) in text
        assert "the chosen workspaces: ws-a,ws-b" in text


async def test_a_failed_mint_opens_the_panel_and_keeps_the_fields(
    tmp_path,
) -> None:
    """A refused mint opens the failure panel over the form
    (#426): the daemon's detail verbatim beside the name the form
    submitted, and closing it returns to the form with the fields
    kept for a retry."""
    data = FakeData([])
    data.fail.add("mint")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.submit()
        await wait_for(lambda: on_failure(app))
        # The title and body land with the panel's compose, a hop
        # after the screen swap — the wait spans it under xdist
        # load (#449).
        await wait_for(lambda: failure_title(app))
        assert "mint failed" in failure_title(app)
        assert "github_api" in failure_title(app)
        assert "placeholder named github_api" in failure_detail(app)
        await pilot.press("q")
        await wait_for(lambda: on_mint(app))
        assert form.query_one("#field-name", Input).value == "github_api"
        assert mint_note(app) == MINT_NOTE  # nothing in the air
        data.fail.clear()
        form.submit()  # the fields kept: the retry mints as-is
        await wait_for(lambda: on_panel(app))
        assert len(data.secret_calls) == 2


async def test_the_failure_panel_holds_until_closed(tmp_path) -> None:
    """The failure panel has no timeout and owns the keys while it
    stands (#426): the page's own actions behind it answer
    nothing, and only the closing keys dismiss it."""
    data = FakeData([row()])
    data.secret_rows = [secret_row()]
    data.fail.add("mint")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.submit()
        await wait_for(lambda: on_failure(app))
        # The Close button holds the focus — Enter reaches it —
        # and arrows find no other control to walk into.  The
        # screen swap, the compose, and the panel's on_mount
        # focus move each land on the pump after the push, so
        # the wait spans the whole chain under xdist load (#449)
        # — the predicate reads as false through the hops it has
        # not reached yet.
        await wait_for(lambda: close_focused(app))
        exchanges = len(data.secret_calls)
        await pilot.press("x", "r", "down", "up")
        await pilot.pause()
        assert on_failure(app)  # still standing
        assert data.secret_calls[exchanges:] == []  # no new exchange
        await pilot.press("escape")
        await wait_for(lambda: on_mint(app))


async def test_a_refused_store_check_opens_the_panel_and_no_mint_runs(
    tmp_path,
) -> None:
    """The store pre-flight (#393, #426): a store that cannot
    answer opens the failure panel — its refusal names no one
    body, so the title carries the verb alone — and no mint
    leaves."""
    data = FakeData([])
    data.fail.add("secret-check")
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.submit()
        await wait_for(lambda: on_failure(app))
        await wait_for(lambda: failure_title(app))
        assert failure_title(app) == "secret store check failed"
        assert "daemon away" in failure_detail(app)
        await pilot.press("enter")  # the focused Close button
        await wait_for(lambda: on_mint(app))
        assert data.secret_calls == []


async def test_the_panel_copies_the_sentinel_over_osc52(
    tmp_path, monkeypatch
) -> None:
    """`c` on the panel hands the sentinel to the OSC 52 copy
    (#393) and names the copy on the note — the real string is
    what lands, with the display still masked."""
    data = FakeData([])
    copied: list[str] = []

    def record_copy(app, text) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr(secrets_mod, "osc52_copy", record_copy)
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.submit()
        await wait_for(lambda: on_panel(app))
        sentinel = "mskssec2_" + "s" * 43
        await pilot.press("c")  # masked on screen, real in the copy
        assert copied == [sentinel]
        assert "copied to the clipboard" in panel_text(app)
        assert "OSC 52" in panel_text(app)
        assert sentinel not in panel_text(app)  # the copy reveals nothing
        # A copy with no driver to write through names that.
        monkeypatch.setattr(secrets_mod, "osc52_copy", lambda app, text: False)
        await pilot.press("c")
        assert "the copy did not land" in panel_text(app)
        assert "no terminal to write through" in panel_text(app)


async def test_local_refusals_keep_the_body_home(tmp_path) -> None:
    """Every local check refuses on the note with no exchange
    (#393): the name's shape, the destinations, and the scoped
    pick."""
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        await wait_for(lambda: picker_seeded(form))
        # No name.
        form.query_one("#field-dests", Input).value = "api.github.com"
        form.submit()
        await wait_for(lambda: "a name is required" in mint_note(app))
        # A junk name.
        form.query_one("#field-name", Input).value = "1abc"
        form.submit()
        await wait_for(lambda: "no leading digit" in mint_note(app))
        # No destinations.
        form.query_one("#field-name", Input).value = "github_api"
        form.query_one("#field-dests", Input).value = ""
        form.submit()
        await wait_for(lambda: "at least one destination" in mint_note(app))
        # A junk destination.
        form.query_one("#field-dests", Input).value = "-bad.com"
        form.submit()
        await wait_for(lambda: "-bad.com" in mint_note(app))
        # A scoped pick with nothing toggled.
        form.query_one("#field-dests", Input).value = "api.github.com"
        form.query_one("#field-coverage", Select).value = "scoped"
        await pilot.pause()
        form.submit()
        await wait_for(lambda: "at least one workspace" in mint_note(app))
        form.query_one("#field-coverage", Select).value = "daemon-wide"
        await pilot.pause()
        # No value.
        form.query_one("#field-value", Input).value = ""
        form.submit()
        await wait_for(lambda: "a value is required" in mint_note(app))
        assert data.calls == []
        assert data.secret_calls == []
        # Escape cancels: no exchange either.
        await pilot.press("escape")
        await wait_for(lambda: on_secrets(app))
        assert data.calls == []


def picker_seeded(form) -> bool:
    """Whether the picker finished its seed — the refusal tests
    submit only after the workspaces listing answered."""
    try:
        return form.query_one("#field-workspaces").option_count >= 0
    except Exception:
        return False


async def test_the_form_fits_the_small_terminal(tmp_path) -> None:
    """The form with its picker shown stands 80x24 terminals
    (#393): every control and the buttons stay inside the screen,
    above the footer line."""
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _follow = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        form.query_one("#field-coverage", Select).value = "scoped"
        await pilot.pause()
        await wait_for(lambda: picker_seeded(form))
        assert form.query_one("#form").outer_size.height <= 23
        buttons = form.query_one("#form-buttons")
        assert buttons.region.bottom < 24
        assert form.query_one("Footer").region.y == 23
        # The refusal path fits too: a note echoing a long junk
        # destination clips at one row instead of pushing the form
        # off-screen.
        form.query_one("#field-coverage", Select).value = "daemon-wide"
        await pilot.pause()
        form.query_one("#field-name", Input).value = "github_api"
        form.query_one("#field-dests", Input).value = "-" + "x" * 120
        form.submit()
        await wait_for(lambda: "an exact hostname" in mint_note(app))
        assert form.query_one("#form").outer_size.height <= 23
        assert buttons.region.bottom < 24


async def test_the_arrows_walk_the_form_and_leave_the_picker_at_its_edges(
    tmp_path,
) -> None:
    """Spatial navigation (#393): the arrows walk the fields in
    reading order, and the picker releases the walk at its edges
    — up from the first option returns to the field above, down
    from the last moves on."""
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        await wait_for(lambda: picker_seeded(form))
        assert form.focused is form.query_one("#field-name", Input)
        await pilot.press("down")
        assert form.focused is form.query_one("#field-dests", Input)
        # The wide default hides the picker row: the walk skips it.
        await pilot.press("down")
        assert form.focused is form.query_one("#field-coverage", Select)
        form.query_one("#field-coverage", Select).value = "scoped"
        await pilot.pause()
        picker = form.query_one("#field-workspaces", WorkspacePicker)
        picker.focus()
        await pilot.pause()
        await pilot.press("up")  # the top edge returns the walk
        assert form.focused is form.query_one("#field-coverage", Select)
        picker.focus()
        await pilot.pause()
        await pilot.press("down")  # into the options
        await pilot.press("up")  # the interior walk moves the highlight
        assert picker.highlighted == 0
        await pilot.press("down")  # the second option — the bottom edge
        await pilot.press("down")  # hands the walk on
        assert form.focused is form.query_one("#field-ttl", Select)
        await pilot.press("down")  # the masked value field closes the walk
        assert form.focused is form.query_one("#field-value", Input)


# -- the audit view -------------------------------------------------------


async def test_the_audit_view_replays_and_streams() -> None:
    """The recorded rows replay newest first, live frames stream
    in beside them, the sighting carries the marker, and the link
    registers as nothing — no decider authority (#390)."""
    data = FakeData([])
    data.audit_rows = [
        audit_row(2, "revoke"),
        audit_row(
            1,
            "mint",
            workspaces=["ws-a"],
            created_at="2030-01-01T03:04:05",
        ),
    ]
    factory = FakeFactory(
        [
            FakeWS(
                [
                    live_frame(
                        "swap",
                        workspace_id="ws-a",
                        host="a.example",
                        ts=2000000000.0,
                    )
                ]
            )
        ]
    )

    def link_factory() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_audit(pilot, app, link_factory)
        # Newest first: the live swap (now), the revoke, the mint —
        # waited on their text, not the row count (the compose
        # stream lags the count; the count alone races the reads).
        await wait_for(lambda: "swap" in audit_text(app, 0))
        assert "revoke" in audit_text(app, 1)
        assert "mint" in audit_text(app, 2)
        assert "*/github_api" in audit_text(app, 1)  # daemon-wide coverage
        # A live sighting lands: the row carries the marker.
        factory.made[0].push(
            live_frame(
                "sighting",
                workspace_id="ws-b",
                host="evil.example",
                ts=2000000001.0,
            )
        )
        await wait_for(lambda: audit_children(app) == 4)
        await wait_for(lambda: audit_text(app, 0).startswith("! "))
        marked = app.screen.query_one("#audit-rows").children[0]
        assert "sighting" in marked.classes
        # No registration was sent: the view claims no decider.
        assert factory.made[0].sent == []
        await pilot.press("r")
        await wait_for(lambda: not on_audit(app))


async def test_the_audit_seed_dedups_against_the_live_tail() -> None:
    """One fact lands once however it arrives (#305 carried to the
    seed): a live frame that raced the read drops the replayed
    row's second delivery."""
    data = FakeData([])
    data.audit_rows = [
        audit_row(1, "mint", workspaces=["ws-a"]),
        audit_row(
            2,
            "revoke",
            workspaces=["ws-a"],
            name="other",
            created_at="2030-01-03T03:04:05",
        ),
    ]
    factory = FakeFactory(
        [
            FakeWS(
                [
                    live_frame(
                        "mint",
                        workspace_id="ws-a",
                        workspaces=["ws-a"],
                        audit_id=1,
                    )
                ]
            )
        ]
    )

    def link_factory() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_audit(pilot, app, link_factory)

        def rows_show_both() -> bool:
            texts = " | ".join(
                audit_text(app, i) for i in range(audit_children(app))
            )
            return "mint" in texts and "revoke" in texts

        await wait_for(rows_show_both)


async def test_the_audit_view_filters_by_kind_and_workspace() -> None:
    """`k` and `w` pick the filters: the kind filter matches the
    row's kind, the workspace filter its coverage — daemon-wide
    rows cover every workspace (#305's replay rule)."""
    data = FakeData([])
    data.audit_rows = [
        audit_row(2, "revoke", workspaces=["ws-b"]),
        audit_row(1, "mint", workspaces=["ws-a"]),
    ]
    factory = FakeFactory(
        [
            FakeWS(
                [
                    live_frame(
                        "sighting",
                        workspace_id="ws-b",
                        host="evil.example",
                        ts=2000000000.0,
                    )
                ]
            )
        ]
    )

    def link_factory() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        view = await open_audit(pilot, app, link_factory)
        await wait_for(lambda: audit_children(app) == 3)
        # The kind filter: mint alone. The presses retry — a press
        # inside a rebuild's swap window no-ops (by design).
        await press_until(
            pilot, "k", lambda: type(app.screen).__name__ == "PickerScreen"
        )
        await pick_option(pilot, app, 3)  # all -> mint
        await wait_for(lambda: "mint" in audit_text(app, 0))
        assert "kind mint" in audit_status(app)
        # Cancel keeps the filter.
        await press_until(
            pilot, "k", lambda: type(app.screen).__name__ == "PickerScreen"
        )
        await pilot.press("escape")
        await wait_for(lambda: audit_children(app) == 1)
        # Back to all rows: the workspace filter rides beside it.
        view.kind = None
        view.rebuilds.request()
        await wait_for(lambda: audit_children(app) == 3)
        await press_until(
            pilot, "w", lambda: type(app.screen).__name__ == "PickerScreen"
        )
        options = app.screen.query_one("#pick-options")
        labels = [
            str(option.prompt)
            for option in options._options  # noqa: SLF001
        ]
        assert labels == ["all", "ws-a", "ws-b"]
        # Cancel keeps the workspace filter too.
        await pilot.press("escape")
        await wait_for(lambda: audit_children(app) == 3)
        await press_until(
            pilot, "w", lambda: type(app.screen).__name__ == "PickerScreen"
        )
        await pick_option(pilot, app, 2)  # all -> ws-b
        await wait_for(lambda: audit_children(app) == 2)
        assert "workspace ws-b" in audit_status(app)
        # ws-b sees its scoped revoke and its tap's sighting; the
        # daemon-wide rule needs no rows here, the scoped mint stays
        # hidden.
        await wait_for(lambda: "ws-b" in audit_text(app, 0))
        texts = " | ".join(audit_text(app, i) for i in range(2))
        assert "mint" not in texts
        # Both filters together can match nothing: the empty line
        # names the filters, not the log's emptiness.
        view.kind = "mint"
        view.rebuilds.request()
        await wait_for(lambda: "No events match" in audit_empty(app))


async def test_an_empty_audit_log_states_it() -> None:
    data = FakeData([])
    factory = FakeFactory([FakeWS([])])

    def link_factory() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_audit(pilot, app, link_factory)
        await wait_for(lambda: "No placeholder events" in audit_empty(app))


async def test_a_seed_failure_names_itself() -> None:
    """A listing the daemon cannot serve names itself on the
    status line — the live stream alone still stands."""
    data = FakeData([])
    data.fail.add("secret-audit")
    factory = FakeFactory([FakeWS([])])

    def link_factory() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_audit(pilot, app, link_factory)
        await wait_for(lambda: "replay failed" in audit_status(app))


async def test_the_page_opens_the_audit_view() -> None:
    """The page's `e` lands on the audit view (#390)."""
    data = FakeData([])
    data.secret_rows = [secret_row()]
    app, _follow = make_app(data)
    factory = FakeFactory([FakeWS([])])

    def audit_link() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    async with app.run_test() as pilot:
        page = await open_secrets(pilot, app)
        assert isinstance(page.audit_link(), AuditLink)  # the default seam
        page.audit_link = audit_link
        await pilot.press("e")
        await wait_for(lambda: on_audit(app))
        # Back returns to the page.
        await pilot.press("escape")
        await wait_for(lambda: on_secrets(app))


async def test_the_swap_windows_self_heal() -> None:
    """The page's paint paths swallow missing widgets (the page's
    own rule): a row that lost its Static skips alone, a missing
    list is the quiet return, and the next refresh heals."""
    data = FakeData([])
    data.secret_rows = [secret_row()]
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        page = await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 1)
        rows = page.rows_widget()
        await rows.children[0].query_one(Static).remove()
        page.sync_rows()  # swallowed: a row lost its Static
        await rows.remove()
        page.sync_rows()  # no list mounted: the quiet return
        assert page.focused_row() is None  # the swap window decides nothing
        await page.query_one("#status").remove()
        await page.query_one("#secret-columns").remove()
        await page.query_one("#secret-empty").remove()
        page.sync_status()  # swallowed: the refresh self-heals
        page.refresh_rows()
        await wait_for(lambda: secrets_children(app) == 1)


async def test_the_audit_swap_windows_self_heal() -> None:
    """The audit view's rebuild heals the same way: a missing list
    always rebuilds, an unchanged log takes the header-only path,
    and the status line's own guard swallows a teardown race."""
    data = FakeData([])
    data.audit_rows = [audit_row(1, "mint")]
    factory = FakeFactory([FakeWS([])])

    def link_factory() -> AuditLink:
        return AuditLink(
            ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        view = await open_audit(pilot, app, link_factory)
        await wait_for(lambda: audit_children(app) == 1)
        # An unchanged log takes the header-only path.
        view.rebuilds.request()
        await wait_for(
            lambda: not view.rebuilds.scheduled and not view.rebuilds.pending
        )
        await view.query_one("#audit-rows").remove()
        view.rebuilds.request()  # the swap is also the heal
        await wait_for(lambda: audit_children(app) == 1)
        await view.query_one("#audit-status").remove()
        view.sync_status()  # swallowed
        view.tick()
        await pilot.pause()


def test_an_unmounted_view_closes_nothing() -> None:
    """A view that never mounted owns no link — unmount decides
    nothing."""
    view = SecretAuditScreen(FakeData([]))
    view.on_unmount()


async def test_the_audit_link_ignores_decider_traffic() -> None:
    """The audit connection lands audit frames alone: the hub's
    other traffic — a foreign workspace's hold, its rules frame —
    is not the view's state, and an unregistered subscriber would
    otherwise hoard holds it can never resolve (the log has its
    bound; the pending map has none)."""
    link = AuditLink()
    hold = json.dumps(
        {
            "event": "egress.request",
            "data": {
                "request": {
                    "id": "r1",
                    "workspace_id": "ws-a",
                    "dest_host": "api.example",
                    "dest_port": 443,
                    "requested_at": 1.0,
                }
            },
        }
    )
    rules = json.dumps(
        {
            "event": "egress.rules",
            "data": {"workspace_id": "ws-a", "mode": "allow"},
        }
    )
    sighting = live_frame(
        "sighting", workspace_id="ws-b", host="evil.example", ts=1.0
    )
    for frame in (hold, rules, sighting, "junk"):
        assert link.land_frame(frame) is False
    assert link.controller.pending == {}
    assert link.controller.rules is None
    assert len(link.controller.events) == 1
    assert link.sightings == []  # the page's own link owns the flash


async def test_the_audit_link_survives_every_close_kind() -> None:
    """The audit connection takes the decider link's own ladder
    over every close kind — a refused-code close, a clean close,
    a wedged socket — without registering and without losing the
    log it already landed (#390)."""

    class CleanClose(FakeWS):
        async def __anext__(self) -> str:
            if self.frames:
                return self.frames.pop(0)
            raise StopAsyncIteration

    class Wedged(FakeWS):
        async def __anext__(self) -> str:
            if self.frames:
                return self.frames.pop(0)
            raise RuntimeError("socket wedged")

    factory = FakeFactory(
        [
            FakeWS(
                [live_frame("swap", workspace_id="ws-a", host="a.example")],
                close_code=1011,
            ),
            CleanClose([]),
            Wedged([]),
            FakeWS([]),
        ]
    )
    link = AuditLink(ws_factory=factory, reconnect_delays=(0.01,))
    link.start()
    await wait_for(lambda: len(factory.made) == 4)
    await wait_for(lambda: link.state == "connected")
    assert len(link.controller.events) == 1  # the log spans reconnects
    assert all(ws.sent == [] for ws in factory.made)  # nothing registered
    link.stop()


# -- the pure helpers -----------------------------------------------------


def test_coverage_and_ttl_labels() -> None:
    """The coverage label reads `*` or the sorted id list; the TTL
    reads its remaining label with the honest fallbacks."""
    assert coverage_text(secret_row()) == "*"
    assert coverage_text(secret_row(workspaces=["b", "a"])) == "a,b"
    assert ttl_text(secret_row()) == "never"
    assert ttl_text(secret_row(expires_at="junk")) == "-"
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert ttl_text(secret_row(expires_at="2030-01-01T00:30:00"), now) == (
        "30m"
    )
    assert (
        ttl_text(secret_row(expires_at="2029-12-31T23:00:00"), now)
        == "expired"
    )


def test_secret_row_cells_clip_to_columns() -> None:
    row = secret_row(
        workspaces=["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"],
        dests=["x" * 30],
    )
    cells = secret_row_cells(row)
    assert len(cells) == 5
    assert "…" in cells[0]  # clipped coverage, both ends kept
    assert cells[1] == "github_api"


def test_ttl_default_picks_the_nearest_choice() -> None:
    """A bounded row renews onto something like what it had; an
    unbounded row starts at the longest choice."""
    bounded = secret_row(
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=25 * 3600)
        ).isoformat()
    )
    assert ttl_default(bounded) == "1d"
    assert ttl_default(secret_row()) == SECRET_TTLS[-1]


def test_sentinel_reach_decodes_the_prefixes() -> None:
    """The reach line reads the sentinel's own prefix (#393):
    ``mskssec2_`` covers every accepting workspace, ``mskssec1_``
    the row's chosen set."""
    wide = secret_row()
    wide["sentinel"] = "mskssec2_" + "x" * 43
    assert sentinel_reach(wide) == "every accepting workspace"
    scoped = secret_row(workspaces=["ws-b", "ws-a"])
    scoped["sentinel"] = "mskssec1_" + "x" * 43
    assert sentinel_reach(scoped) == "the chosen workspaces: ws-a,ws-b"


def test_split_entries_carry_the_repeatable_flag_shape() -> None:
    """One comma-separated input holds the repeatable entries a
    flag would carry (#393): whitespace strips, empty segments
    drop."""
    assert split_entries("a, b , ,c ") == ["a", "b", "c"]
    assert split_entries("") == []


def test_osc52_sequence_encodes_the_payload() -> None:
    """The copy sequence is the base64 payload inside the ``52;c;``
    selection, BEL-terminated (#393)."""
    payload = base64.b64encode(b"hunter2").decode("ascii")
    assert osc52_sequence("hunter2") == f"\x1b]52;c;{payload}\x07"


def test_osc52_copy_writes_through_the_driver() -> None:
    """The copy rides the app's driver (#393); an app with no
    driver — a teardown race — copies nothing and crashes
    nowhere."""
    written: list[str] = []
    driver = SimpleNamespace(write=written.append, flush=lambda: None)
    osc52_copy(SimpleNamespace(_driver=driver), "hunter2")
    assert written == [osc52_sequence("hunter2")]
    assert osc52_copy(SimpleNamespace(), "hunter2") is False
    assert len(written) == 1


def test_masked_sentinel_keeps_the_prefix_and_masks_the_body() -> None:
    """The masked display: a known prefix stays — it names the
    reach — and the body renders one bullet per character; a
    sentinel matching no prefix masks whole."""
    scoped = SCOPED_SENTINEL + "abc"
    wide = WIDE_SENTINEL + "abcdef"
    assert masked_sentinel(scoped) == SCOPED_SENTINEL + "\u2022" * 3
    assert masked_sentinel(wide) == WIDE_SENTINEL + "\u2022" * 6
    assert masked_sentinel("junk") == "\u2022" * 4
    assert masked_sentinel("") == ""


async def test_a_junk_dest_carrying_markup_refuses_without_crashing(
    tmp_path,
) -> None:
    """A pasted fragment with a stray closing tag is operator
    input echoed on a markup-parsing line (#393): the refusal
    names it escaped, and the app stands."""
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.query_one("#field-dests", Input).value = "api.github.com,x[/y"
        form.submit()
        await pilot.pause()
        await wait_for(lambda: "an exact hostname" in mint_note(app))
        assert on_mint(app)
        assert app._exception is None  # the echo raised nowhere
        assert data.secret_calls == []


async def test_the_flight_owns_the_form_until_its_reply_lands(
    tmp_path,
) -> None:
    """The value and sentinel ride exactly one reply (#393): a
    second submit or a cancel while the exchange is in the air
    decides nothing, and the reply still reaches its panel."""
    import asyncio

    data = FakeData([])
    data.mint_gate = asyncio.Event()
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.submit()
        await wait_for(lambda: "minting" in mint_note(app))
        form.submit()  # a second Mint press mid-flight
        await pilot.press("escape")  # a cancel mid-flight
        await pilot.pause()
        assert on_mint(app)  # the flight keeps the form standing
        assert len(data.secret_calls) == 1
        data.mint_gate.set()
        await wait_for(lambda: on_panel(app))
        assert "mskssec2_" in panel_text(app)


async def test_the_keyboard_path_picks_the_scoped_coverage(
    tmp_path,
) -> None:
    """The coverage select's own keyboard leg (#393): Enter opens
    the list, the arrows move it, Enter picks — the operator's
    path, not the programmatic set."""
    data = FakeData([row(), row(id="ws-b", name="beta")])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        picker = form.query_one("#field-workspaces", WorkspacePicker)
        await wait_for(lambda: picker.option_count == 2)
        form.query_one("#field-coverage", Select).focus()
        await pilot.pause()
        await pilot.press("enter")  # open the list
        await pilot.press("down")  # the scoped choice
        await pilot.press("enter")  # pick it
        await wait_for(lambda: form.query_one("#workspaces-row").display)
        assert form.query_one("#field-coverage", Select).value == "scoped"


async def test_a_refused_workspace_listing_names_itself_on_the_form() -> None:
    """The picker's seed (#393): a listing the daemon cannot serve
    names itself on the note; the form stands and the daemon-wide
    mint needs no list."""
    data = FakeData([])
    data.fail.add("workspaces")
    app, _follow = make_app(data)
    async with app.run_test():
        # The listing's own refusal would strand the walk to the
        # page, so the form rides the page's callback directly.
        app.push_screen(MintScreen(SecretsScreen().minted))
        await wait_for(lambda: "workspace list failed" in mint_note(app))
        assert on_mint(app)


async def test_the_form_cancels_without_an_exchange(tmp_path) -> None:
    """Every way out without a mint decides nothing (#393):
    Escape and the Cancel button both dismiss with no reply, and
    no call leaves the page. The dismiss clears the value field —
    and closes over a field already gone (a teardown race, read
    as noise) the same way."""
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        form.query_one("#field-value", Input).value = "typed-secret"
        await pilot.press("escape")
        await wait_for(lambda: on_secrets(app))
        form = await open_mint(pilot, app)
        await form.query_one("#field-value", Input).remove()
        await pilot.press("escape")
        await wait_for(lambda: on_secrets(app))
        form = await open_mint(pilot, app)
        await pilot.click("#do-cancel")
        await wait_for(lambda: on_secrets(app))
        assert data.calls == []
        assert data.secret_calls == []


async def test_the_panel_closes_on_its_button(tmp_path) -> None:
    """The Close button clears the panel's text and returns the
    page (#393); the Show button reveals the sentinel first, and
    the Copy button copies it from either state."""
    data = FakeData([])
    app, _follow = make_app(data)
    async with app.run_test() as pilot:
        await open_secrets(pilot, app)
        form = await open_mint(pilot, app)
        fill_mint(form)
        form.submit()
        await wait_for(lambda: on_panel(app))
        panel = app.screen
        sentinel_line = panel.query_one("#panel-sentinel", Static)
        await pilot.press("enter")  # the focused Show button
        assert ("mskssec2_" + "s" * 43) in panel_text(app)
        panel.query_one("#do-copy", Button).focus()
        await pilot.press("enter")  # the Copy button
        assert "copied to the clipboard" in panel_text(app)
        panel.query_one("#do-close", Button).focus()
        await panel.query_one("#panel-rule", Static).remove()
        # The Close button closes over a line already gone — a
        # teardown race, read as noise — and still clears the rest.
        await pilot.press("enter")
        await wait_for(lambda: on_secrets(app))
        assert str(sentinel_line.content) == ""

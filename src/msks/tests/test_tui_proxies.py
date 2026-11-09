"""The proxies page (#809, part #807): Pilot-driven, fake seams.

The page rides the tree app's data seam (FakeData's proxies
surface, scripted and recorded), the subscriber link gets direct
land_frame tests over the consent suite's FakeWS shapes, and the
pure helpers (the spec text, the port pairs, the compact line's
paint) get direct tests here.
"""

import json

from msks.client.tui.proxies import (
    ProxiesScreen,
    ProxyAddScreen,
    ProxyLink,
    pair_text,
    proxies_line,
    proxy_row_content,
    spec_text,
    status_text,
)
from test_consent_page import FakeFactory, press_until, wait_for
from test_main_tui import (
    WS,
    FakeData,
    make_app,
    open_page,
    row,
    scripted_link,
)
from textual.widgets import Input, Select, Static


def proxy_row(
    id: int = 1,
    workspace_id: str = WS,
    direction: str = "forward",
    listen_port: int = 8888,
    listen_host: str | None = None,
    target_port: int = 8889,
    target_host: str | None = None,
    label: str | None = None,
    status: str = "listening",
    reason: str | None = None,
) -> dict:
    """One proxy row as the daemon's listing serves it."""
    out: dict = {
        "id": id,
        "workspace_id": workspace_id,
        "direction": direction,
        "listen_port": listen_port,
        "target_port": target_port,
        "label": label,
        "status": status,
        "reason": reason,
    }
    if listen_host is not None:
        out["listen_host"] = listen_host
    if target_host is not None:
        out["target_host"] = target_host
    return out


# -- the pure helpers ------------------------------------------------------


def test_spec_text_reads_the_cli_grammar() -> None:
    """One row's spec is the exact grammar ``msks proxy add``
    reads: the direction's letter, the forward's bind only when
    the row names one, the host field's two spellings, and the
    remote's daemon-side target."""
    assert (
        spec_text(proxy_row(listen_port=2201, target_port=22))
        == "-L 2201:localhost:22"
    )
    assert (
        spec_text(
            proxy_row(listen_port=2201, listen_host="0.0.0.0", target_port=22)
        )
        == "-L 0.0.0.0:2201:localhost:22"
    )
    assert (
        spec_text(
            proxy_row(
                direction="remote",
                listen_port=9200,
                target_host="localhost",
                target_port=9200,
            )
        )
        == "-R 9200:localhost:9200"
    )


def test_pair_text_points_the_traffic() -> None:
    """#807's display spec: the forward pair reads ``listen ->
    target``, the remote pair ``listen <- target``."""
    assert pair_text(proxy_row()) == "8888 -> 8889"
    assert (
        pair_text(proxy_row(direction="remote", target_port=8882))
        == "8888 <- 8882"
    )


def test_status_text_carries_its_reason() -> None:
    """The status word, its reason riding along when one stands —
    a listening row the guest cannot reach says so."""
    assert status_text(proxy_row()) == "listening"
    assert (
        status_text(
            proxy_row(status="listening", reason="tap admission failed")
        )
        == "listening (tap admission failed)"
    )


def test_the_compact_line_paints_the_spec_shape() -> None:
    """#807's compact row: the forwards before the bar, the
    remotes after it, one ``·``-free comma join inside each half —
    each pair green while it listens, red while its bind refused,
    the label column muted. Empty paints nothing."""
    listening = proxy_row(listen_port=8888, target_port=8889)
    second = proxy_row(
        id=2, listen_port=9111, target_port=9115, status="stopped"
    )
    failed = proxy_row(
        id=3,
        direction="remote",
        listen_port=8881,
        target_port=8882,
        status="failed",
        reason="bind refused",
    )
    line = proxies_line([listening, second, failed])
    assert line.plain == (" proxies 8888 -> 8889, 9111 -> 9115 | 8881 <- 8882")
    styles = {span.style for span in line.spans}
    assert "$success" in styles  # the listening pair, green
    assert "$error" in styles  # the failed pair, red
    # The stopped pair paints the line's own foreground: no span.
    assert proxies_line([]).plain == ""


def test_the_row_content_paints_the_status_cell() -> None:
    """The page row's cells at the fixed columns, the status cell
    alone carrying its color — a stopped row paints none."""
    failed = proxy_row(
        direction="remote", status="failed", reason="bind refused"
    )
    line = proxy_row_content(failed)
    assert "-R 8888:127.0.0.1:8889" in line.plain
    assert any(span.style == "$error" for span in line.spans)
    stopped = proxy_row(status="stopped")
    assert proxy_row_content(stopped).spans == []


# -- the subscriber link ---------------------------------------------------


def test_the_link_pokes_on_proxy_frames() -> None:
    """The page's connection is a plain subscriber: every
    ``proxy.*`` frame pokes the refresh, the rest of the hub's
    traffic lands nowhere, and nothing registers."""
    pokes = []
    link = ProxyLink(lambda: pokes.append(True), ws_factory=FakeFactory([]))
    link.land_frame(json.dumps({"event": "proxy.listening", "data": {}}))
    link.land_frame(json.dumps({"event": "proxy.created", "data": {}}))
    link.land_frame(json.dumps({"event": "egress.holds", "data": {}}))
    link.land_frame("not json")
    assert pokes == [True, True]


def test_the_workspace_link_buffers_proxy_frames() -> None:
    """The workspace page's own link lands proxy transitions in
    its buffer — the compact line's refetch pokes — beside the
    controller's own frames (a rules frame lands untouched)."""
    from msks.client.tui.link import DeciderLink

    link = DeciderLink("ws-1", ws_factory=FakeFactory([]))
    link.land_frame(
        json.dumps({"event": "egress.rules", "data": {"mode": "ask"}})
    )
    assert link.take_proxy_events() == 0
    link.land_frame(json.dumps({"event": "proxy.closed", "data": {}}))
    link.land_frame(json.dumps({"event": "proxy.removed", "data": {}}))
    assert link.take_proxy_events() == 2
    assert link.take_proxy_events() == 0  # taken, not re-counted


# -- the page --------------------------------------------------------------


def proxy_children(app) -> int:
    """The page's row count; -1 in a swap window."""
    try:
        rows = app.screen.query_one("#proxy-rows")
    except Exception:
        return -1
    return len(rows.children)


def proxy_status(app) -> str:
    """The page's status line."""
    return str(app.screen.query_one("#proxy-status", Static).content)


async def open_proxies(pilot, app, scoped: bool):
    """Open the proxies page — scoped from a workspace page,
    daemon-wide from the list."""
    if scoped:
        await open_page(pilot, app)
        await press_until(
            pilot,
            "p",
            lambda: isinstance(app.screen, ProxiesScreen),
        )
        return app.screen
    await press_until(
        pilot, "p", lambda: isinstance(app.screen, ProxiesScreen)
    )
    return app.screen


def quiet_link(monkeypatch) -> None:
    """The page's subscriber stays quiet in the tests — no dial."""
    monkeypatch.setattr(
        ProxiesScreen,
        "make_link",
        lambda self: ProxyLink(lambda: None, ws_factory=FakeFactory([])),
    )


async def test_the_page_lists_the_daemon_wide_rows(monkeypatch) -> None:
    """From the workspaces list (``p``), the page carries every
    workspace's rows with their live status; the status line names
    the daemon-wide scope."""
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [
        proxy_row(),
        proxy_row(
            id=2,
            workspace_id="ws-2",
            direction="remote",
            listen_port=8881,
            target_port=8882,
            status="failed",
            reason="bind refused",
        ),
    ]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app, scoped=False)
        await wait_for(lambda: proxy_children(app) == 2)
        text = (
            app.screen.query_one("#proxy-rows")
            .children[1]
            .query_one(Static)
            .content
        )
        assert "-R 8881:127.0.0.1:8882" in str(text)
        assert "failed" in str(text)
        assert "every workspace" in proxy_status(app)
        assert page.workspace is None


async def test_the_page_scopes_to_the_workspace(monkeypatch) -> None:
    """From the workspace page (``p``, the action row), the page
    carries that workspace's rows alone and its status line names
    the scope."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [
        proxy_row(),
        proxy_row(id=2, workspace_id="ws-2", listen_port=9111),
    ]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app, scoped=True)
        await wait_for(lambda: proxy_children(app) == 1)
        assert "alpha" in proxy_status(app)
        assert page.workspace is not None
        # q returns to the workspace page, not the list.
        await pilot.press("q")
        await wait_for(lambda: type(app.screen).__name__ == "WorkspaceScreen")


async def test_the_page_adds_through_the_form(monkeypatch) -> None:
    """``a`` opens the form; a spec in the CLI's grammar lands
    through the data seam — the direction's letter picks the
    parser, the label rides when filled — and the page refreshes
    with the flash naming the spec."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        # The scope fixes the workspace: the select carries one
        # choice, and the walk starts at the spec.
        form = app.screen
        assert form.query_one("#field-workspace", Select).is_disabled
        form.query_one("#field-spec", Input).value = "2201:localhost:22"
        form.query_one("#field-proxy-label", Input).value = "ssh"
        await pilot.press("enter")  # the label field's submit walks on
        await press_until(
            pilot, "enter", lambda: not isinstance(app.screen, ProxyAddScreen)
        )
        await wait_for(
            lambda: (
                data.proxy_calls
                == [
                    (
                        WS,
                        {
                            "direction": "forward",
                            "listen_port": 2201,
                            "target_port": 22,
                            "label": "ssh",
                        },
                    )
                ]
            )
        )
        await wait_for(lambda: proxy_children(app) == 1)
        assert "-L 2201:localhost:22" in proxy_status(app)


async def test_the_form_refuses_a_foreign_host_home(
    monkeypatch,
) -> None:
    """The form parses with the CLI's own parser: a -L spec naming
    a host besides the workspace itself keeps the form standing
    with the rule on the note — the daemon never sees the body."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        form.query_one("#field-spec", Input).value = "2201:db.internal:22"
        form.query_one("#do-add").press()
        await pilot.pause()
        note = form.query_one("#form-note", Static).content
        assert "use -R" in str(note)
        assert data.proxy_calls == []


async def test_the_form_picks_the_remote_parser(monkeypatch) -> None:
    """The direction's letter picks the parser: an -R pick reads
    the remote grammar and carries the daemon-side target host."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        form.query_one("#field-direction", Select).value = "-R"
        form.query_one("#field-spec", Input).value = "9200:localhost:9200"
        await press_until(
            pilot, "enter", lambda: not isinstance(app.screen, ProxyAddScreen)
        )
        await wait_for(lambda: len(data.proxy_calls) == 1)
        assert data.proxy_calls[0][1] == {
            "direction": "remote",
            "listen_port": 9200,
            "target_host": "localhost",
            "target_port": 9200,
        }


async def test_the_page_removes_behind_a_confirmation(
    monkeypatch,
) -> None:
    """``r`` asks first; a yes removes through the data seam and
    the page refreshes — Enter answers the same question."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        assert "-L 8888:localhost:8889" in app.screen.question
        await pilot.press("y")
        await wait_for(lambda: (WS, 1) in data.proxy_removes)
        await wait_for(lambda: proxy_children(app) == 0)
        assert (
            "No proxies"
            in app.screen.query_one("#proxy-empty", Static).content
        )


async def test_the_page_refuses_a_remove_gently(monkeypatch) -> None:
    """A refused remove names itself on the page's status line —
    the row stands."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    data.fail.add("proxy-remove")
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(lambda: "remove failed" in proxy_status(app))
        assert proxy_children(app) == 1


async def test_a_frame_poke_refreshes_the_listing(monkeypatch) -> None:
    """A proxy transition on the subscriber pokes the page's
    refresh, so a row another surface removed leaves the listing
    without a keypress."""
    monkeypatch.setattr(
        ProxiesScreen,
        "make_link",
        lambda self: ProxyLink(self.refresh_rows, ws_factory=FakeFactory([])),
    )
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=False)
        await wait_for(lambda: proxy_children(app) == 1)
        # The row leaves the daemon; the poke the hub's frame
        # would deliver — the link's own callback — serves it
        # without a key.
        data.proxy_rows = []
        app.screen.link.on_event()
        await wait_for(lambda: proxy_children(app) == 0)


# -- the workspace page's compact line --------------------------------------


async def test_the_workspace_page_paints_the_compact_line(
    monkeypatch,
) -> None:
    """The page's compact proxies line: the port pairs beside the
    mounts line, fetched at page start and painted green while a
    pair listens; the action row and ``p`` both open the scoped
    page."""
    from test_main_tui import scripted_link

    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [
        proxy_row(),
        proxy_row(
            id=2,
            direction="remote",
            listen_port=8881,
            target_port=8882,
            status="failed",
        ),
    ]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(
            lambda: (
                "8888 -> 8889 | 8881 <- 8882"
                in str(app.screen.query_one("#proxy-line", Static).content)
            )
        )
        line = app.screen.query_one("#proxy-line", Static).content
        assert {span.style for span in line.spans} == {
            "$text 60%",
            "$success",
            "$error",
        }


async def test_the_workspace_page_refetches_on_a_frame(
    monkeypatch,
) -> None:
    """A proxy transition on the page's own link refetches the
    compact line on the next tick — the same immediacy the page's
    subscriber gives the proxies page."""
    from test_main_tui import scripted_link

    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_page(pilot, app)
        await wait_for(lambda: page.proxy_rows == [])
        # The daemon gains a row; the frame the hub publishes
        # lands on the page's link and the tick refetches.
        data.proxy_rows = [proxy_row()]
        page.link.land_frame(
            json.dumps({"event": "proxy.listening", "data": {}})
        )
        await wait_for(
            lambda: (
                "8888 -> 8889"
                in str(app.screen.query_one("#proxy-line", Static).content)
            )
        )


def test_the_proxies_line_handles_one_half() -> None:
    """A workspace whose rows sit in one direction paints that
    half alone — no empty side, no orphan bar."""
    only_remote = proxies_line(
        [proxy_row(direction="remote", target_port=8882)]
    )
    assert only_remote.plain == " proxies 8888 <- 8882"
    only_forward = proxies_line([proxy_row()])
    assert only_forward.plain == " proxies 8888 -> 8889"


# -- the coverage the pilot walks above do not reach ------------------------


async def test_the_scope_free_form_seeds_the_workspaces(
    monkeypatch,
) -> None:
    """Opened from the list, the add form seeds its workspace
    select from the tree's own list and a body lands on the picked
    workspace; a listing that will not come names itself on the
    note and the form stands."""
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=False)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        select = form.query_one("#field-workspace", Select)

        def seeded() -> bool:
            return bool(select._options)  # noqa: SLF001

        await wait_for(seeded)
        select.value = WS  # the operator's pick
        spec = form.query_one("#field-spec", Input)
        spec.value = "2201:localhost:22"
        spec.focus()  # the walk starts where the operator stands
        await press_until(
            pilot, "enter", lambda: not isinstance(app.screen, ProxyAddScreen)
        )
        await wait_for(lambda: len(data.proxy_calls) == 1)
        # A listing that will not come: the note names it, the form
        # stands, and a submit names the missing pick.
        data.fail.add("workspaces")
        app.screen.action_add()
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        await wait_for(
            lambda: (
                "workspace list failed"
                in str(form.query_one("#form-note", Static).content)
            )
        )
        form.query_one("#do-add").press()
        await pilot.pause()
        assert "pick a workspace first" in str(
            form.query_one("#form-note", Static).content
        )


async def test_a_blank_spec_names_the_one_required_field(
    monkeypatch,
) -> None:
    """A blank spec keeps the form standing with the rule on the
    note; the Cancel button decides nothing and the page keeps its
    rows."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        form.query_one("#do-add").press()
        await pilot.pause()
        assert "one required field" in str(
            form.query_one("#form-note", Static).content
        )
        form.query_one("#do-cancel").press()
        await pilot.pause()
        assert not isinstance(app.screen, ProxyAddScreen)
        assert data.proxy_calls == []


async def test_the_page_names_an_unfocused_remove(monkeypatch) -> None:
    """``r`` with nothing focused (the empty page) names itself on
    the status line and calls nothing."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await wait_for(lambda: proxy_children(app) == 0)
        await pilot.press("r")
        await wait_for(lambda: "no proxy focused" in proxy_status(app))
        assert data.proxy_removes == []


async def test_a_refused_confirm_keeps_the_row(monkeypatch) -> None:
    """A ``no`` at the confirmation decides nothing — the row
    stands and the daemon sees no delete."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("n")
        await wait_for(lambda: isinstance(app.screen, ProxiesScreen))
        assert data.proxy_removes == []
        assert proxy_children(app) == 1


async def test_a_refused_listing_names_itself(monkeypatch) -> None:
    """A listing that will not come flashes on the page's status
    line — the empty state names the add key."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.fail.add("workspace-proxies")
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app, scoped=True)
        await wait_for(lambda: "listing failed" in proxy_status(app))


async def test_the_page_heals_its_swap_windows(monkeypatch) -> None:
    """The page's paint paths swallow missing widgets (the tree's
    own rule): a row that lost its Static skips alone, a missing
    list is the quiet return, the status line's own guard swallows
    its teardown race, and the next refresh heals."""
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app, scoped=False)
        await wait_for(lambda: proxy_children(app) == 1)
        rows = page.rows_widget()
        await rows.children[0].query_one(Static).remove()
        page.paint_rows()  # swallowed: a row lost its Static
        await rows.remove()
        page.paint_rows()  # no list mounted: the quiet return
        assert page.focused_row() is None  # the swap window decides nothing
        await page.query_one("#proxy-status").remove()
        await page.query_one("#proxy-columns").remove()
        await page.query_one("#proxy-empty").remove()
        page.sync_status()  # swallowed: the refresh self-heals
        page.refresh_rows()
        await wait_for(lambda: proxy_children(app) == 1)


async def test_a_wider_terminal_widens_the_columns(monkeypatch) -> None:
    """A resize re-widths the page's columns and repaints the
    standing rows in place — identity and focus stay put; a bare
    page (compose not built) keeps the widths for the first
    build."""
    from msks.client.tui.proxies import PROXY_COLUMNS
    from textual.events import Resize
    from textual.geometry import Size

    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row(listen_host="0.0.0.0")]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        page = await open_proxies(pilot, app, scoped=False)
        await wait_for(lambda: proxy_children(app) == 1)
        before = page.columns
        await pilot.resize_terminal(120, 24)
        await pilot.pause()
        assert page.columns != before  # the growing columns took the spare
        text = str(page.rows_widget().children[0].query_one(Static).content)
        assert (
            "-L 0.0.0.0:8888:localhost:8889" in text
        )  # whole at the wider frame
    # The bare page: no compose has built, the resize keeps the
    # widths and the first rows build at them.
    bare = ProxiesScreen()
    bare.on_resize(Resize(Size(120, 24), Size(80, 24)))
    assert bare.columns != PROXY_COLUMNS


def test_the_default_link_is_the_pages_own_poke() -> None:
    """The unpatched link factory wires the page's refresh as the
    poke — the tests' quiet seam replaces this one connection."""
    from msks.client.tui.proxies import ProxyLink

    assert isinstance(ProxiesScreen().make_link(), ProxyLink)


async def test_the_data_seam_makes_the_daemons_exchanges(
    monkeypatch,
) -> None:
    """The page's data seam rides the daemon's own routes: the
    daemon-wide and per-workspace listings, the create, and the
    remove — one MockTransport exchange each."""
    import httpx
    from msks.client.tui import data as data_mod

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            if request.url.path == "/api/v1/proxies":
                return httpx.Response(200, json=[proxy_row()])
            return httpx.Response(200, json=[proxy_row()])
        if request.method == "POST":
            return httpx.Response(
                200, json=proxy_row(listen_port=2201, target_port=22)
            )
        return httpx.Response(
            200, json={"id": 1, "workspace_id": WS, "removed": True}
        )

    monkeypatch.setenv("MSKSC_URL", "https://api.test")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    data = data_mod.TuiData(transport=httpx.MockTransport(handler))
    rows = await data.proxies()
    assert rows[0]["workspace_id"] == WS
    mine = await data.workspace_proxies(WS)
    assert mine[0]["listen_port"] == 8888
    created = await data.create_proxy(
        WS,
        {"direction": "forward", "listen_port": 2201, "target_port": 22},
    )
    assert created["status"] == "listening"
    removed = await data.remove_proxy(WS, 1)
    assert removed["removed"] is True
    assert seen == [
        ("GET", "/api/v1/proxies"),
        ("GET", f"/api/v1/workspaces/{WS}/proxies"),
        ("POST", f"/api/v1/workspaces/{WS}/proxies"),
        ("DELETE", f"/api/v1/workspaces/{WS}/proxies/1"),
    ]

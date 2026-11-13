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
    action_children,
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


def colored_pairs(line) -> dict[str, str]:
    """The line's colored spans as pair-text -> style: the extents
    are the claim — a span computed off the display order colors
    the neighbor's cells, so the plain text each span covers is
    pinned, not just its style."""
    out = {}
    for span in line.spans:
        out[line.plain[span.start : span.end]] = span.style
    return out


def test_the_compact_line_paints_the_spec_shape() -> None:
    """#807's compact row: the forwards before the bar, the
    remotes after it, one ``·``-free comma join inside each half —
    each pair green while it listens, red while its bind refused,
    the label column muted. Empty paints nothing."""
    failed = proxy_row(  # served first: a remote, failed
        id=1,
        direction="remote",
        listen_port=8881,
        target_port=8882,
        status="failed",
        reason="bind refused",
    )
    listening = proxy_row(id=2, listen_port=8888, target_port=8889)
    second = proxy_row(
        id=3, listen_port=9111, target_port=9115, status="stopped"
    )
    line = proxies_line([failed, listening, second])
    assert line.plain == (" proxies 8888 -> 8889, 9111 -> 9115 | 8881 <- 8882")
    pairs = colored_pairs(line)
    assert pairs["8888 -> 8889"] == "$success"  # the listening pair
    assert pairs["8881 <- 8882"] == "$error"  # the failed pair
    assert pairs["proxies "] == "$text 60%"  # the muted label
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


async def open_proxies(pilot, app):
    """Open the workspace's proxies page — the page's ``p``."""
    await open_page(pilot, app)
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


async def test_the_page_lists_only_the_workspaces_rows(
    monkeypatch,
) -> None:
    """The page carries its workspace's rows alone — another
    workspace's rows stay off it — with their live status, and the
    status line names the workspace."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [
        proxy_row(),
        proxy_row(
            id=2,
            workspace_id="ws-other",
            direction="remote",
            listen_port=8881,
            target_port=8882,
            status="failed",
            reason="bind refused",
        ),
    ]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        text = str(
            app.screen.query_one("#proxy-rows")
            .children[0]
            .query_one(Static)
            .content
        )
        assert "-L 8888:localhost:8889" in text
        assert "listening" in text
        assert "alpha" in proxy_status(app)
        # The page's row carries the host's open-shell count beside the
        # daemon's fields (#858).
        assert page.workspace == {**data.rows[0], "shells": 0}


async def test_the_action_row_opens_the_page(monkeypatch) -> None:
    """The configure group's Proxies row opens the page — the
    awaited path the ``p`` key shares, walked with the arrows the
    way the spatial rules read (a regression here cost the row's
    Enter its screen)."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 8)
        # Down four times lands on the Proxies row (the shell,
        # egress, edit, and mounts rows before it).
        await pilot.press("down", "down", "down", "down")
        await press_until(
            pilot, "enter", lambda: isinstance(app.screen, ProxiesScreen)
        )
        await wait_for(lambda: proxy_children(app) == 1)


async def test_the_page_returns_to_the_workspace_page(
    monkeypatch,
) -> None:
    """The page opens over the workspace page and ``q`` returns
    there — the tree holds no daemon-wide proxies surface."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        assert "alpha" in proxy_status(app)
        # The page's row carries the host's open-shell count beside the
        # daemon's fields (#858).
        assert page.workspace == {**data.rows[0], "shells": 0}
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
        await open_proxies(pilot, app)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        # The scope fixes the workspace: the field is the name it
        # reads, and the walk starts at the spec.
        form = app.screen
        assert (
            str(form.query_one("#field-workspace", Static).content) == "alpha"
        )
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
        await open_proxies(pilot, app)
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
        await open_proxies(pilot, app)
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


async def test_a_markup_carrying_bind_removes_whole(monkeypatch) -> None:
    """The -L parser leaves the bind field's charset open, so a
    stored bind can carry a closing markup tag: the add form takes
    it, and the remove question renders it escaped — the confirm
    stands, the TUI with it."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row(listen_host="x[/y]", status="stopped")]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("D")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        assert r"x\[/y]" in str(app.screen.question)  # the tag, escaped
        await pilot.press("n")
        await wait_for(lambda: isinstance(app.screen, ProxiesScreen))


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
        await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("D")
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
        await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("D")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("y")
        await wait_for(lambda: "remove failed" in proxy_status(app))
        assert proxy_children(app) == 1


async def test_an_unchanged_listing_keeps_its_list(monkeypatch) -> None:
    """The standing interval's read of the same rows repaints
    nothing (the workspaces list's own rule): the list widget
    stands — no swap, no flicker on the clock — and a listing that
    did move still swaps."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        standing = app.screen.rows_widget()
        app.screen.refresh_rows()  # the interval's read: same rows
        await wait_for(lambda: app.screen.rows_widget() is standing)
        data.proxy_rows = [proxy_row(), proxy_row(id=2)]
        app.screen.refresh_rows()  # a listing that moved: swap
        await wait_for(lambda: proxy_children(app) == 2)
        assert app.screen.rows_widget() is not standing


async def test_a_refresh_never_takes_the_loading_state(
    monkeypatch,
) -> None:
    """The refresh's in-flight mark is a plain flag, never the
    widget's own ``loading`` reactive — setting that would replace
    the page with Textual's loading indicator on every cycle (the
    five-second flash). A fetch held mid-flight proves the page
    itself stays painted."""
    import asyncio

    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    release = asyncio.Event()

    class Gated(FakeData):
        armed = False

        async def workspace_proxies(self, workspace_id):
            if Gated.armed:
                await release.wait()
            return await super().workspace_proxies(workspace_id)

    data = Gated([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        standing = page.rows_widget()
        Gated.armed = True  # the next read holds mid-flight
        page.refresh_rows()
        await pilot.pause()
        await pilot.pause()
        assert page.loading is False  # the reactive, never tripped
        assert page.rows_widget() is standing  # the page stays painted
        release.set()
        await wait_for(lambda: page.rows_loading is False)


async def test_a_frame_poke_refreshes_the_listing(monkeypatch) -> None:
    """A proxy transition on the subscriber pokes the page's
    refresh, so a row another surface removed leaves the listing
    without a keypress."""
    monkeypatch.setattr(
        ProxiesScreen,
        "make_link",
        lambda self: ProxyLink(self.refresh_rows, ws_factory=FakeFactory([])),
    )
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
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
        page = await open_page(pilot, app)
        await wait_for(
            lambda: (
                "8888 -> 8889 | 8881 <- 8882"
                in str(app.screen.query_one("#proxy-line", Static).content)
            )
        )
        line = app.screen.query_one("#proxy-line", Static).content
        assert colored_pairs(line) == {
            "proxies ": "$text 60%",
            "8888 -> 8889": "$success",
            "8881 <- 8882": "$error",
        }
        # The teardown race the per-second repaint can hit: a line
        # already unmounted under the timer paints nothing.
        await page.query_one("#proxy-line").remove()
        page.paint_proxies()  # swallowed: the next tick heals


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
    assert colored_pairs(only_remote) == {
        "proxies ": "$text 60%",
        "8888 <- 8882": "$success",
    }
    only_forward = proxies_line([proxy_row()])
    assert only_forward.plain == " proxies 8888 -> 8889"


# -- the coverage the pilot walks above do not reach ------------------------


async def test_a_markup_carrying_spec_refusal_crashes_nothing(
    monkeypatch,
) -> None:
    """The parser's refusal embeds the raw spec; one carrying a
    closing markup tag lands on the note escaped — the form
    stands, the TUI with it."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        form.query_one("#field-spec", Input).value = "2201:local[/host:22"
        form.query_one("#do-add").press()
        await pilot.pause()
        assert isinstance(app.screen, ProxyAddScreen)  # still standing
        note = str(form.query_one("#form-note", Static).content)
        assert "use -R" in note and "local" in note


async def test_the_walk_crosses_the_direction_select(monkeypatch) -> None:
    """The direction select is the form walk's own: Down from it
    reaches the spec field with the overlay closed, and Up returns
    — arrows alone cross every control, the tree's
    spatial-navigation rule."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        form.query_one("#field-direction").focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is form.query_one("#field-spec", Input)
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is form.query_one("#field-direction")


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
        await open_proxies(pilot, app)
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
        await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 0)
        await pilot.press("D")
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
        await open_proxies(pilot, app)
        await wait_for(lambda: proxy_children(app) == 1)
        await pilot.press("D")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("n")
        await wait_for(lambda: isinstance(app.screen, ProxiesScreen))
        assert data.proxy_removes == []
        assert proxy_children(app) == 1


async def test_a_refused_create_flashes_on_the_status_line(
    monkeypatch,
) -> None:
    """A refused add — the daemon's named 409 among them — flashes
    on the page's status line; the form's fields are gone with the
    form and the listing stands."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.proxy_refusal = (
        "a forward proxy on port 2201 already exists (workspace "
        f"{WS}, proxy 7)"
    )
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
        await pilot.press("a")
        await wait_for(lambda: isinstance(app.screen, ProxyAddScreen))
        form = app.screen
        form.query_one("#field-spec", Input).value = "2201:localhost:22"
        await press_until(
            pilot, "enter", lambda: not isinstance(app.screen, ProxyAddScreen)
        )
        await wait_for(lambda: "add failed" in proxy_status(app))
        assert proxy_children(app) == 0


async def test_a_refused_listing_names_itself(monkeypatch) -> None:
    """A listing that will not come flashes on the page's status
    line — the empty state names the add key."""
    scripted_link(monkeypatch, [])
    quiet_link(monkeypatch)
    data = FakeData([row()])
    data.fail.add("workspace-proxies")
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        await open_proxies(pilot, app)
        await wait_for(lambda: "listing failed" in proxy_status(app))


async def test_the_page_heals_its_swap_windows(monkeypatch) -> None:
    """The page's paint paths swallow missing widgets (the tree's
    own rule): a row that lost its Static skips alone, a missing
    list is the quiet return, the status line's own guard swallows
    its teardown race, and the next refresh heals."""
    quiet_link(monkeypatch)
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    data.proxy_rows = [proxy_row()]
    app, _ = make_app(data)
    async with app.run_test() as pilot:
        page = await open_proxies(pilot, app)
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
    scripted_link(monkeypatch, [])
    data = FakeData([row()])
    data.proxy_rows = [proxy_row(listen_host="0.0.0.0")]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        page = await open_proxies(pilot, app)
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
    bare = ProxiesScreen(row())
    bare.on_resize(Resize(Size(120, 24), Size(80, 24)))
    assert bare.columns != PROXY_COLUMNS


def test_the_default_link_is_the_pages_own_poke() -> None:
    """The unpatched link factory wires the page's refresh as the
    poke — the tests' quiet seam replaces this one connection. A
    bare page unmounts without its link: the close is the quiet
    no-op."""
    from msks.client.tui.proxies import ProxyLink

    bare = ProxiesScreen(row())
    assert isinstance(bare.make_link(), ProxyLink)
    bare.on_unmount()  # no link was made: nothing to stop


async def test_the_data_seam_makes_the_daemons_exchanges(
    monkeypatch,
) -> None:
    """The page's data seam rides the daemon's own routes: the
    workspace's listing, the create, and the remove — one
    MockTransport exchange each."""
    import httpx
    from msks.client.tui import data as data_mod

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
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
        ("GET", f"/api/v1/workspaces/{WS}/proxies"),
        ("POST", f"/api/v1/workspaces/{WS}/proxies"),
        ("DELETE", f"/api/v1/workspaces/{WS}/proxies/1"),
    ]

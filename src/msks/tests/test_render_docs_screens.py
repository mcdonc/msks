"""Render the TUI chapter's screens into ``docs/screens/`` (#589).

The docs' TUI chapter describes each screen in prose; this module
renders the screens themselves, the way the chapter's readers see
them: Textual's own ``export_screenshot`` over the same scripted
fixtures the suite drives, so every rendering is deterministic —
the same rows, the same frames, the same focused element, every
run — and regenerates in one command when a screen changes::

    RENDER_DOCS_SCREENS=1 \\
      devenv --quiet -O dotenv.enable:bool false shell -- \\
      pytest -q src/msks/tests/test_render_docs_screens.py

Without the variable the module skips: the suite's ordinary runs
never render, and CI stays green on a docs-only change. The
SVGs land under ``docs/screens/`` and are committed with the
docs they illustrate — a deliberately stale rendering is a
review flag, the same class as a stale sentence.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from msks.client.tui.consent_ui import FailurePanel
from msks.client.tui.link import AuditLink
from msks.client.tui.secrets import MintScreen, SentinelPanel
from msks.client.tui.unlock import PassphraseScreen
from test_main_tui import (
    CHANNEL,
    FakeData,
    FakeFactory,
    FakeWS,
    action_children,
    list_children,
    make_app,
    on_consent,
    open_edit,
    open_page,
    press_until,
    request_frame,
    row,
    rules_frame,
    scripted_link,
    scripted_manifest,
    wait_for,
)
from test_tui_secrets import (
    audit_row,
    live_frame,
    open_audit,
    open_secrets,
    secret_row,
    secrets_children,
)
from textual.widgets import OptionList

#: The module's gate: the variable names the render; unset, skip.
ENV = "RENDER_DOCS_SCREENS"

#: Where the SVGs land: the repo root's docs/screens/.
SCREENS_DIR = Path(__file__).parents[3] / "docs" / "screens"

#: One hold on the consent page's queue, the fixture's shape.
HOLD_FRAME = request_frame("r9")


pytestmark = pytest.mark.skipif(
    os.environ.get(ENV) != "1",
    reason=f"set {ENV}=1 to render the docs' screens",
)


async def export(app, name: str) -> None:
    """Write the app's current screen as the named SVG."""
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    svg = app.export_screenshot()
    (SCREENS_DIR / f"{name}.svg").write_text(svg, encoding="utf-8")


def screen_name(app) -> str:
    """The focused screen's class name, the waiters' shorthand."""
    return type(app.screen).__name__


def holds_settled(app) -> bool:
    """The consent page's held-requests list carries its row."""
    try:
        rows = app.screen.query_one("#hold-rows")
    except Exception:
        return False
    return len(rows.children) >= 1


def audit_settled(app) -> bool:
    """The audit view's replay carries the fixture's rows."""
    from msks.client.tui.secrets import SecretAuditScreen

    if not isinstance(app.screen, SecretAuditScreen):
        return False
    try:
        rows = app.screen.query_one("#audit-rows")
    except Exception:
        return False
    return len(rows.children) >= 3


async def test_the_workspaces_list() -> None:
    """The tree's root: two rows, one running one stopped, the
    status bar's count and daemon URL below."""
    data = FakeData(
        [
            row(status="running"),
            row(id="ws2", name="beta", status="stopped"),
        ]
    )
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for(lambda: list_children(app) == 2)
        await pilot.pause()
        await export(app, "workspaces-list")


async def test_the_create_form() -> None:
    """`c` over the list: the form's fields and the image catalog
    picker."""
    data = FakeData([row()])
    data.images_rows = [
        {
            "name": "debian-13",
            "version": "2026.01",
            "hash": "a" * 64,
            "default": True,
        }
    ]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await wait_for(lambda: screen_name(app) == "CreateScreen")
        await pilot.pause()
        await export(app, "create-form")


async def test_the_failure_panel() -> None:
    """A refused create: the panel over the list, the daemon's
    refusal verbatim, the Close button focused."""
    refusal = "400: a workspace named alpha already exists"
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(FailurePanel("create", "alpha", refusal))
        await pilot.pause()
        await export(app, "failure-panel")


async def test_the_workspace_page(monkeypatch) -> None:
    """The per-workspace loop: the two-line header, the consent
    line, the three action groups."""
    scripted_link(monkeypatch, [rules_frame()])
    app, _ = make_app(FakeData([row(status="running")]))
    async with app.run_test(size=(80, 24)) as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await pilot.pause()
        await export(app, "workspace-page")


async def test_the_edit_dialog(monkeypatch) -> None:
    """The prefilled sizes-and-topology form over the page."""
    scripted_link(monkeypatch, [rules_frame()])
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await open_edit(pilot, app)
        await pilot.pause()
        await export(app, "edit-dialog")


async def test_the_consent_page(monkeypatch) -> None:
    """The decider as a page: a held request over the in-effect
    verdicts."""
    scripted_link(monkeypatch, [rules_frame(), HOLD_FRAME])
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await press_until(pilot, "e", lambda: on_consent(app))
        await wait_for(lambda: on_consent(app) and holds_settled(app))
        await export(app, "consent-page")


async def test_the_duration_picker(monkeypatch) -> None:
    """`A` over a held request: the duration choices, the default
    highlighted."""
    scripted_link(monkeypatch, [rules_frame(), HOLD_FRAME])
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await press_until(pilot, "e", lambda: on_consent(app))
        await wait_for(lambda: on_consent(app) and holds_settled(app))
        await pilot.press("A")
        await wait_for(lambda: screen_name(app) == "DurationScreen")
        await pilot.pause()
        await export(app, "duration-picker")


async def test_the_mode_picker(monkeypatch) -> None:
    """`m` on the consent page: the three postures, the current
    one highlighted."""
    scripted_link(monkeypatch, [rules_frame()])
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        await open_page(pilot, app)
        await wait_for(lambda: action_children(app) == 5)
        await press_until(pilot, "e", lambda: on_consent(app))
        await pilot.press("m")
        await wait_for(lambda: screen_name(app) == "ModeScreen")
        await pilot.pause()
        await export(app, "mode-picker")


async def test_the_secrets_page() -> None:
    """The placeholder list: reach, destinations, state, expiry —
    two rows, one scoped one daemon-wide."""
    data = FakeData([row()])
    data.secret_rows = [
        secret_row(1),
        secret_row(2, name="pypi_token", workspaces=["ws1"]),
    ]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_secrets(pilot, app)
        await wait_for(lambda: secrets_children(app) == 2)
        await pilot.pause()
        await export(app, "secrets-page")


async def test_the_mint_form() -> None:
    """`c` on the secrets page: the fields, the coverage
    multi-select, the TTL picker."""
    from msks.client.tui.secrets import SecretsScreen

    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(MintScreen(SecretsScreen().minted))
        await pilot.pause()
        await export(app, "mint-form")


async def test_the_sentinel_panel() -> None:
    """The mint's reply: the sentinel masked, Show and Copy
    beneath."""
    fresh = {
        **secret_row(3),
        "sentinel": "mskssec1_" + "b" * 24,
    }
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(SentinelPanel(fresh))
        await pilot.pause()
        await export(app, "sentinel-panel")


async def test_the_audit_view(monkeypatch) -> None:
    """The daemon-wide replay: mints and sightings newest first,
    the off-allowlist sighting highlighted."""
    monkeypatch_delays = (0.01, 0.01, 0.01)
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
            ws_factory=factory, reconnect_delays=monkeypatch_delays
        )

    data = FakeData([row()])
    data.audit_rows = [
        audit_row(2, "sighting"),
        audit_row(1, "mint", workspaces=["ws1"]),
        audit_row(0, "revoke"),
    ]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_audit(pilot, app, link_factory)
        await wait_for(lambda: audit_settled(app))
        await export(app, "audit-view")


async def test_the_image_page(monkeypatch) -> None:
    """`i` over the list: the catalog group, then the published
    channel's rows with their markers."""
    scripted_manifest(monkeypatch, CHANNEL)
    data = FakeData([row()])
    data.images_rows = [
        {
            "name": "debian-13",
            "version": "2026.01",
            "hash": "a" * 64,
            "default": True,
        },
        {
            "name": "nixos",
            "version": "26.05",
            "hash": "b" * 64,
            "default": False,
        },
    ]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("i")
        await wait_for(lambda: screen_name(app) == "ImageImportScreen")
        await wait_for(lambda: app.screen.query(OptionList) != [])
        await pilot.pause()
        await export(app, "image-page")


async def test_the_image_detail(monkeypatch) -> None:
    """Enter on a catalog row: the full description, the hash,
    the built date."""
    scripted_manifest(monkeypatch, CHANNEL)
    data = FakeData([row()])
    data.images_rows = [
        {
            "name": "debian-13",
            "version": "2026.01",
            "hash": "a" * 64,
            "default": True,
            "description": "Debian 13.6 cloud image, kernel 6.12, "
            "cloud-init, the egress module set and consent stack",
            "imported": "2026-01-02T03:04:05",
            "built_at": "2026-01-01T00:00:00",
        }
    ]
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("i")
        await wait_for(lambda: screen_name(app) == "ImageImportScreen")
        await wait_for(lambda: app.screen.query(OptionList) != [])
        await pilot.press("enter")
        await wait_for(lambda: screen_name(app) == "ImageDetailScreen")
        await pilot.pause()
        await export(app, "image-detail")


async def test_the_first_run_import(monkeypatch) -> None:
    """The first run's image page (first-run chapter, Importing a
    guest VM image): the catalog group empty, the published
    channel enumerated beneath it, and the import modal holding
    the terminal mid-download with its elapsed timer."""
    import asyncio

    scripted_manifest(monkeypatch, CHANNEL)
    release = asyncio.Event()

    class Held(FakeData):
        async def import_image(self, source: str) -> dict:
            self.import_calls.append(source)
            await release.wait()
            return {
                "ref": "debian:13.6",
                "hash": "a" * 64,
                "name": "debian",
                "version": "13.6",
            }

    data = Held([row()])
    data.images_rows = []  # first run: nothing imported yet
    app, _ = make_app(data)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("i")
        await wait_for(lambda: screen_name(app) == "ImageImportScreen")
        await wait_for(lambda: app.screen.query(OptionList) != [])
        await pilot.press("enter")
        await wait_for(lambda: screen_name(app) == "ImportingScreen")
        await wait_for(lambda: data.import_calls == [CHANNEL[0]["url"]])
        # One elapsed tick paints the label (the ref and the
        # running seconds) before the shot.
        app.screen.tick()
        await pilot.pause()
        await export(app, "first-run-import")
        app.screen.action_cancel()
        await wait_for(lambda: screen_name(app) != "ImportingScreen")
        release.set()


async def test_the_term_popup_decider() -> None:
    """The popup's own app: the held-request queue and verdict
    keys `msks-term-popup` runs inside its hidden tmux session
    (the tmux chrome itself is not Textual and stays a terminal
    capture)."""
    import test_decide_app as da_test

    frames = [
        da_test.rules_frame(),
        da_test.request_frame("r9"),
    ]
    _, seam = da_test.verdict_seam()
    app = da_test.build(frames, seam)
    async with app.run_test(size=(80, 24)) as pilot:
        await da_test.until(lambda: len(da_test.row_texts(app)) >= 1)
        await pilot.pause()
        await export(app, "term-popup-decider")


async def test_the_unlock_modal() -> None:
    """The session's one passphrase prompt over the tree."""
    app, _ = make_app(FakeData([row()]))
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PassphraseScreen(Path("identity")))
        await pilot.pause()
        await export(app, "unlock-modal")

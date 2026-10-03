"""The interceptor manager: armed state, listeners, events (#199)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.app import build_app
from msks.events import EventHub
from msks.interceptor import Interceptor, PlaceholderEntry
from msks.interceptor import manager as manager_mod
from msks.microvm import VmSpec
from msks.microvm.errors import MicrovmError
from msks.secretstore import backend_ref, new_sentinel
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings

TAP_IP = "172.31.0.2"
_TAPS = {"ws-a": "172.31.0.2", "ws-b": "172.31.0.3", "ws-c": "172.31.0.4"}


def tap_ip(workspace_id: str) -> str:
    """One stable fake tap address per workspace (#339): the
    daemon-wide arm binds one listener per tap, so two attached
    workspaces must not share an address."""
    if workspace_id in _TAPS:
        return _TAPS[workspace_id]
    return f"172.31.{sum(workspace_id.encode()) % 250}.{len(workspace_id)}"


class FakeProxyserver:
    def __init__(self) -> None:
        self.ok = True

    async def setup_servers(self) -> bool:
        return self.ok


class FakeMaster:
    """The master surface the manager touches, recorded."""

    built: list[FakeMaster] = []

    def __init__(self, owner) -> None:
        self.owner = owner
        self.options = SimpleNamespace(mode=[], update=self.update)
        self.addons = SimpleNamespace(get=self.addons_get)
        self.proxyserver = FakeProxyserver()
        self.shutdown_calls = 0
        self.exiting = asyncio.Event()
        FakeMaster.built.append(self)

    def update(self, **kwargs) -> None:
        self.options.mode = kwargs.get("mode", self.options.mode)

    def addons_get(self, name: str):
        return self.proxyserver if name == "proxyserver" else None

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.exiting.set()

    async def run(self) -> None:
        await self.exiting.wait()


class HangingMaster(FakeMaster):
    """A master whose run() ignores shutdown (the timeout path)."""

    async def run(self) -> None:
        await asyncio.sleep(3600)


class FakeNet:
    """The three NetManager seams the manager reads."""

    def __init__(self, live: set[str] | None = None) -> None:
        self.live = live if live is not None else {"ws-a"}
        self.interceptions: list[tuple[str, int | None]] = []

    def attachment_for(self, workspace_id: str):
        if workspace_id not in self.live:
            return None
        return SimpleNamespace(tap_ip=tap_ip(workspace_id))

    def attached_workspaces(self) -> list[str]:
        """Attach order (#339): the daemon-wide refresh's read."""
        return [wid for wid in ("ws-a", "ws-b", "ws-c") if wid in self.live]

    async def apply_interception(self, workspace_id, port) -> None:
        self.interceptions.append((workspace_id, port))


class FakeSecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def read(self, ref: str) -> str:
        return self.values[ref]


def fake_master_factory(cls=FakeMaster):
    def factory(owner, trust_bundle):
        return cls(owner)

    return factory


@pytest.fixture
def app(tmp_path):
    app = build_app(
        Settings(
            vmm=VmmSettings(state_dir=tmp_path),
            net=NetSettings(interceptor_port=8643),
            server=ServerSettings(db_path=tmp_path / "msks.db"),
        )
    )
    app.state.model.migrate()
    app.state.net = FakeNet()
    app.state.secrets = FakeSecrets()
    FakeMaster.built.clear()
    app.state.interceptor = Interceptor(
        app, master_factory=fake_master_factory()
    )
    return app


async def ensure_workspace(app, workspace_id: str, **spec_kw) -> None:
    """The workspace row a coverage read needs (the manager reads
    its secret_coverage setting, #339)."""
    if await app.state.model.get_workspace(workspace_id) is None:
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id=workspace_id,
                kernel=Path("/k"),
                rootfs=Path("/r"),
                **spec_kw,
            )
        )


async def mint_placeholder(
    app, workspace_id="ws-a", name="api", coverage=None, **kw
):
    """One placeholder row (#339: *coverage* is the row's set —
    ``[]`` mints the daemon-wide row, the default scopes it to one
    workspace)."""
    if coverage is None:
        coverage = [workspace_id]
    for wid in coverage:
        await ensure_workspace(app, wid)
    return await app.state.model.create_placeholder(
        coverage,
        name,
        new_sentinel(daemon_wide=not coverage),
        kw.get("dests", ["api.example.com"]),
        backend_ref(coverage, name),
        kw.get("expires_at"),
    )


def rows_entry(row) -> PlaceholderEntry:
    return PlaceholderEntry(
        sentinel=row["sentinel"],
        name=row["name"],
        placeholder_id=row["id"],
        dests=tuple(row["dests"]),
        backend_ref=row["backend_ref"],
    )


async def test_refresh_without_an_attachment_arms_nothing(app) -> None:
    app.state.net = FakeNet(live=set())
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    assert FakeMaster.built == []
    assert app.state.interceptor.workspace_for_tap(TAP_IP) is None


async def test_refresh_without_placeholders_arms_nothing(app) -> None:
    """No rows at all: no master, no listener."""
    await app.state.interceptor.refresh("ws-a")
    assert FakeMaster.built == []


async def test_refresh_arms_a_running_workspace(app) -> None:
    row = await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    assert len(FakeMaster.built) == 1
    master = FakeMaster.built[0]
    assert master.options.mode == [f"transparent@{TAP_IP}:8643"]
    assert app.state.net.interceptions == [("ws-a", 8643)]
    assert app.state.interceptor.workspace_for_tap(TAP_IP) == "ws-a"
    entries = app.state.interceptor.entries_for("ws-a")
    assert rows_entry(row).sentinel in entries
    # The CA pair landed in the workspace's state directory.
    assert (
        app.state.settings.vmm.state_dir
        / "vms"
        / "ws-a"
        / "interceptor-ca.crt"
    ).exists()


async def test_a_later_refresh_updates_entries_in_place(app) -> None:
    await mint_placeholder(app, name="one")
    await app.state.interceptor.refresh("ws-a")
    second = await mint_placeholder(app, name="two")
    await app.state.interceptor.refresh("ws-a")
    assert len(FakeMaster.built) == 1  # the master is not rebuilt
    assert second["sentinel"] in app.state.interceptor.entries_for("ws-a")
    assert len(FakeMaster.built[0].options.mode) == 1


async def test_refresh_disarms_when_the_last_placeholder_goes(app) -> None:
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    master = FakeMaster.built[0]
    for row in await app.state.model.list_placeholders():
        await app.state.model.delete_placeholder(row["id"])
    await app.state.interceptor.refresh("ws-a")
    assert master.options.mode == []
    assert app.state.net.interceptions[-1] == ("ws-a", None)
    assert app.state.interceptor.workspace_for_tap(TAP_IP) is None


async def test_expired_rows_neither_arm_nor_answer(app) -> None:
    """A row past its deadline is not active (#198): the workspace
    does not arm on it."""
    await mint_placeholder(
        app, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    await app.state.interceptor.refresh("ws-a")
    assert FakeMaster.built == []
    assert await app.state.interceptor.active_entries("ws-a") == {}


async def test_on_detach_drops_the_listener_without_a_table_swap(app) -> None:
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    master = FakeMaster.built[0]
    await app.state.interceptor.on_detach("ws-a")
    assert master.options.mode == []
    assert app.state.net.interceptions == [("ws-a", 8643)]  # no None swap
    assert app.state.interceptor.workspace_for_tap(TAP_IP) is None


async def test_stop_shuts_the_master_down(app) -> None:
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    master = FakeMaster.built[0]
    await app.state.interceptor.stop()
    assert master.shutdown_calls == 1
    # Idempotent: a second stop is a no-op.
    await app.state.interceptor.stop()


async def test_stop_cancels_a_master_that_will_not_land(
    app, monkeypatch
) -> None:
    monkeypatch.setattr(manager_mod, "SHUTDOWN_TIMEOUT_S", 0.1)
    app.state.interceptor = Interceptor(
        app, master_factory=fake_master_factory(HangingMaster)
    )
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    await app.state.interceptor.stop()
    assert FakeMaster.built[0].shutdown_calls == 1


async def test_a_failed_listener_rolls_the_arm_back(app) -> None:
    await mint_placeholder(app)
    app.state.interceptor._master_factory = fake_master_factory()
    await app.state.interceptor.ensure_master()
    FakeMaster.built[0].proxyserver.ok = False
    with pytest.raises(MicrovmError, match="listener failed"):
        await app.state.interceptor.refresh("ws-a")
    assert app.state.interceptor.workspace_for_tap(TAP_IP) is None
    assert FakeMaster.built[0].options.mode == []


async def test_matching_entry_answers_the_hello_question(app) -> None:
    await mint_placeholder(app, dests=[".example.com"])
    await app.state.interceptor.refresh("ws-a")
    found = app.state.interceptor.matching_entry("ws-a", "deep.example.com")
    assert found is not None and found.dests == (".example.com",)
    assert app.state.interceptor.matching_entry("ws-a", "other.net") is None


async def test_a_daemon_wide_row_arms_every_attached_workspace(app) -> None:
    """#339: one daemon-wide row, one sentinel — every attached
    workspace arms on it, one listener per tap, and a workspace
    that attaches later arms at its first refresh (the first-boot
    story: coverage is policy, not a mint-time snapshot)."""
    app.state.net = FakeNet(live={"ws-a", "ws-b"})
    await ensure_workspace(app, "ws-a")
    await ensure_workspace(app, "ws-b")
    row = await mint_placeholder(app, coverage=[])
    await app.state.interceptor.refresh("ws-a")
    await app.state.interceptor.refresh("ws-b")
    modes = FakeMaster.built[0].options.mode
    assert sorted(modes) == sorted(
        [f"transparent@{tap_ip(ws)}:8643" for ws in ("ws-a", "ws-b")]
    )
    for ws in ("ws-a", "ws-b"):
        entries = app.state.interceptor.entries_for(ws)
        assert entries[row["sentinel"]].dests == ("api.example.com",)
        assert (
            app.state.interceptor.matching_entry(ws, "api.example.com")
            is not None
        )
    # A workspace created after the mint arms with it on first
    # refresh (#339's decision).
    app.state.net.live = {"ws-a", "ws-b", "ws-c"}
    await ensure_workspace(app, "ws-c")
    await app.state.interceptor.refresh("ws-c")
    modes = FakeMaster.built[0].options.mode
    assert f"transparent@{tap_ip('ws-c')}:8643" in modes
    assert app.state.interceptor.entries_for("ws-c")[
        row["sentinel"]
    ].dests == ("api.example.com",)


async def test_a_scoped_workspace_takes_daemon_wide_detection_only(
    app,
) -> None:
    """#339's escape hatch: a workspace with secret_coverage
    ``scoped`` never arms from a daemon-wide row, and the sentinel
    appears in its table for detection alone — empty dests, so no
    destination ever matches it (a carried sighting, never a
    swap)."""
    await ensure_workspace(app, "ws-a")
    row = await mint_placeholder(app, coverage=[])
    await app.state.model.set_secret_coverage("ws-a", "scoped")
    await app.state.interceptor.refresh("ws-a")
    assert FakeMaster.built == []  # nothing covers it: no arm
    covering, table = await app.state.interceptor.entry_tables("ws-a")
    assert covering == {}
    detection = table[row["sentinel"]]
    assert detection.dests == ()
    assert (
        app.state.interceptor.matching_entry("ws-a", "api.example.com") is None
    )
    # Flipping back to all arms it with any live daemon-wide row.
    await app.state.model.set_secret_coverage("ws-a", "all")
    await app.state.interceptor.refresh("ws-a")
    assert app.state.interceptor.workspace_for_tap(tap_ip("ws-a")) == "ws-a"
    assert app.state.interceptor.entries_for("ws-a")[
        row["sentinel"]
    ].dests == ("api.example.com",)


async def test_a_foreign_scoped_sentinel_is_detection_only(app) -> None:
    """A scoped row's sentinel swaps only on its workspaces' taps
    and reads as an off-allowlist sighting elsewhere (#339): the
    other workspace's table carries it with empty dests."""
    await ensure_workspace(app, "ws-a")
    await ensure_workspace(app, "ws-b")
    foreign = await mint_placeholder(app, workspace_id="ws-b")
    await app.state.interceptor.refresh("ws-a")
    covering, table = await app.state.interceptor.entry_tables("ws-a")
    assert covering == {}  # ws-b's row does not arm ws-a
    assert table[foreign["sentinel"]].dests == ()


async def test_the_scoped_escape_hatch_keeps_direct_mints(app) -> None:
    """A scoped workspace still arms from placeholders minted
    directly at it (#339) — the hatch exempts only daemon-wide
    rows."""
    await ensure_workspace(app, "ws-a")
    await app.state.model.set_secret_coverage("ws-a", "scoped")
    await mint_placeholder(app, name="direct")
    await mint_placeholder(app, name="wide", coverage=[])
    await app.state.interceptor.refresh("ws-a")
    covering, table = await app.state.interceptor.entry_tables("ws-a")
    assert [entry.name for entry in covering.values()] == ["direct"]
    assert len(table) == 2  # the daemon-wide sentinel stays detectable


async def test_sentinel_live_reads_the_row(app) -> None:
    row = await mint_placeholder(app)
    assert await app.state.interceptor.sentinel_live(row["sentinel"])
    await app.state.model.delete_placeholder(row["id"])
    assert not await app.state.interceptor.sentinel_live(row["sentinel"])


async def test_secret_for_reads_the_store_cache(app) -> None:
    row = await mint_placeholder(app)
    app.state.secrets.values[row["backend_ref"]] = "the-real-one"
    entry = rows_entry(row)
    assert await app.state.interceptor.secret_for(entry) == "the-real-one"


async def test_swap_and_sighting_events_reach_the_hub(app) -> None:
    hub: EventHub = app.state.hub
    queue = hub.subscribe()
    row = await mint_placeholder(app)
    entry = rows_entry(row)
    await app.state.interceptor.publish_swap("ws-a", entry, "api.example.com")
    await app.state.interceptor.publish_sighting(
        "ws-a", entry, "elsewhere.example.net"
    )
    first = json.loads(await asyncio.wait_for(queue.get(), 1))
    second = json.loads(await asyncio.wait_for(queue.get(), 1))
    assert first["event"] == "secret.swap"
    assert first["data"]["placeholder_id"] == row["id"]
    assert first["data"]["workspace_id"] == "ws-a"
    assert first["data"]["name"] == "api"
    assert first["data"]["host"] == "api.example.com"
    assert first["data"]["ts"] > 0.0
    assert second["event"] == "secret.sighting"
    assert second["data"]["host"] == "elsewhere.example.net"
    assert second["data"]["placeholder_id"] == row["id"]


async def test_build_master_orders_the_addon_first(tmp_path) -> None:
    """The real master: the interceptor addon registers before the
    defaults, no listener ships, the daemon state owns the confdir,
    and upstream verification loads the trust bundle (#424)."""
    from pathlib import Path

    import certifi

    app = build_app(
        Settings(
            vmm=VmmSettings(state_dir=tmp_path),
            server=ServerSettings(db_path=tmp_path / "msks.db"),
        )
    )
    bundle = await app.state.probe.upstream_trust_bundle()
    master = manager_mod.build_master(app.state.interceptor, bundle)
    names = [type(a).__name__ for a in master.addons.chain]
    assert names.index("InterceptorAddon") < names.index("TlsConfig")
    assert master.options.mode == []
    assert str(tmp_path / "interceptor") in master.options.confdir
    assert master.options.ssl_verify_upstream_trusted_ca == bundle
    # The bundle REPLACES mitmproxy's default lookup, so it carries
    # the platform roots it replaced plus the probe CA — an option
    # holding the probe CA alone would break every real service.
    body = Path(bundle).read_bytes()
    platform = Path(certifi.where()).read_bytes()
    assert body.startswith(
        (tmp_path / "probe" / "interceptor-ca.crt").read_bytes()
    )
    assert body.endswith(platform)
    assert master.options.connection_strategy == "lazy"
    assert master.options.keep_host_header is True


class CrashingMaster(FakeMaster):
    """A master whose run() dies (the stop() resilience path)."""

    async def run(self) -> None:
        raise RuntimeError("mitmproxy exploded")


async def test_stop_survives_a_master_that_died(app, monkeypatch) -> None:
    """A dead master's stored error must not cascade into daemon
    shutdown: stop() logs it and finishes (#260 review)."""
    monkeypatch.setattr(manager_mod, "SHUTDOWN_TIMEOUT_S", 0.1)
    app.state.interceptor = Interceptor(
        app, master_factory=fake_master_factory(CrashingMaster)
    )
    await app.state.interceptor.ensure_master()
    task = app.state.interceptor._task
    while not task.done():
        await asyncio.sleep(0)
    assert isinstance(task.exception(), RuntimeError)
    await app.state.interceptor.stop()
    assert FakeMaster.built[0].shutdown_calls == 1


async def test_a_dead_master_is_retired_and_rebuilt_on_refresh(app) -> None:
    """A master whose run task ended serves nothing: the armed books
    clear, and the next refresh builds a fresh master and re-arms
    (#260 review, round 5)."""
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    first = FakeMaster.built[0]
    first.exiting.set()  # its run task lands
    task = app.state.interceptor._task
    while not task.done():
        await asyncio.sleep(0)
    await mint_placeholder(app, name="second")
    await app.state.interceptor.refresh("ws-a")
    assert len(FakeMaster.built) == 2
    assert app.state.interceptor.workspace_for_tap(TAP_IP) == "ws-a"
    assert FakeMaster.built[1].options.mode == [f"transparent@{TAP_IP}:8643"]


async def test_a_cancelled_master_run_retires_quietly(app) -> None:
    """A cancelled run task carries no exception to read: the books
    still clear, and the next refresh rebuilds."""
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    task = app.state.interceptor._task
    task.cancel()
    while not task.done():
        await asyncio.sleep(0)
    built = len(FakeMaster.built)
    await app.state.interceptor.refresh("ws-a")  # retire, then re-arm
    assert len(FakeMaster.built) == built + 1
    assert app.state.interceptor.workspace_for_tap(TAP_IP) == "ws-a"


async def test_log_dead_master_names_the_error(caplog) -> None:
    async def boom() -> None:
        raise RuntimeError("mitmproxy died")

    task = asyncio.create_task(boom())
    while not task.done():
        await asyncio.sleep(0)
    with caplog.at_level("ERROR", logger="msks.interceptor.manager"):
        manager_mod.log_dead_master(task)
    assert "mitmproxy died" in caplog.text


async def test_arming_without_a_probe_service_builds_the_bundle(
    app,
) -> None:
    """A stripped-down host with no probe service still arms, with
    the upstream trust bundle built through the module path (#424):
    the master's verification never depends on the service object."""
    from pathlib import Path

    seen: dict = {}

    def factory(owner, trust_bundle):
        seen["bundle"] = trust_bundle
        return FakeMaster(owner)

    app.state.interceptor._master_factory = factory
    app.state.probe = None
    await mint_placeholder(app)
    await app.state.interceptor.refresh("ws-a")
    assert Path(seen["bundle"]).is_file()
    assert seen["bundle"].endswith("upstream-bundle.pem")

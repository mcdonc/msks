"""The interceptor manager: armed state, listeners, events (#199)."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.app import build_app
from msks.interceptor import Interceptor, PlaceholderEntry
from msks.interceptor import manager as manager_mod
from msks.microvm import VmSpec
from msks.microvm.errors import MicrovmError
from msks.secretstore import backend_ref, new_sentinel
from msks.server.events import EventHub
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings

TAP_IP = "172.31.0.2"


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
    """The two NetManager seams the manager reads."""

    def __init__(self, live: set[str] | None = None) -> None:
        self.live = live if live is not None else {"ws-a"}
        self.interceptions: list[tuple[str, int | None]] = []

    def attachment_for(self, workspace_id: str):
        if workspace_id not in self.live:
            return None
        return SimpleNamespace(tap_ip=TAP_IP)

    async def apply_interception(self, workspace_id, port) -> None:
        self.interceptions.append((workspace_id, port))


class FakeSecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def read(self, ref: str) -> str:
        return self.values[ref]


def fake_master_factory(cls=FakeMaster):
    def factory(owner):
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


async def mint_placeholder(app, workspace_id="ws-a", name="api", **kw):
    if await app.state.model.get_workspace(workspace_id) is None:
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id=workspace_id,
                kernel=Path("/k"),
                rootfs=Path("/r"),
            )
        )
    return await app.state.model.create_placeholder(
        workspace_id,
        name,
        new_sentinel(),
        kw.get("dests", ["api.example.com"]),
        backend_ref(workspace_id, name),
        kw.get("expires_at"),
    )


def rows_entry(row) -> PlaceholderEntry:
    return PlaceholderEntry(
        sentinel=row["sentinel"],
        name=row["name"],
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
    for row in await app.state.model.workspace_placeholders("ws-a"):
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
    first = await asyncio.wait_for(queue.get(), 1)
    second = await asyncio.wait_for(queue.get(), 1)
    assert '"secret.swap"' in first
    assert "api.example.com" in first
    assert '"secret.sighting"' in second


async def test_build_master_orders_the_addon_first(tmp_path) -> None:
    """The real master: the interceptor addon registers before the
    defaults, no listener ships, and the daemon state owns the
    confdir."""
    app = build_app(
        Settings(
            vmm=VmmSettings(state_dir=tmp_path),
            server=ServerSettings(db_path=tmp_path / "msks.db"),
        )
    )
    master = manager_mod.build_master(app.state.interceptor)
    names = [type(a).__name__ for a in master.addons.chain]
    assert names.index("InterceptorAddon") < names.index("TlsConfig")
    assert master.options.mode == []
    assert str(tmp_path / "interceptor") in master.options.confdir
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

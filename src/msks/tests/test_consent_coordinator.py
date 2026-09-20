"""The consent engine: holds, verdicts, timeouts, revocation (#69)."""

import asyncio
from pathlib import Path

import pytest
from msks.app import build_app
from msks.consent.coordinator import duration_ttl
from msks.consent.specs import MODE_ALLOW, MODE_INTERACTIVE, MODE_STATIC
from msks.microvm import VmSpec
from msks.model.egress_consent import DECISION_ALLOWED
from msks.server.events import EventHub
from msks.settings import NetSettings, ServerSettings, Settings


class RecordingNet:
    """The manager's consent seam, recorded."""

    def __init__(self) -> None:
        self.app = None
        self.calls: list[tuple] = []

    async def clear_consent_dest(self, workspace_id, host, port):
        self.calls.append(("clear", workspace_id, host, port))

    def host_for(self, workspace_id, ip):
        return None


@pytest.fixture
async def engine_app(tmp_path: Path):
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "e.db"),
            net=NetSettings(consent_timeout_s=0.05, consent_rate_limit=3),
        )
    )
    app.state.model.migrate()
    for wid, mode in (
        ("ws-allow", MODE_ALLOW),
        ("ws-static", MODE_STATIC),
        ("ws-interactive", MODE_INTERACTIVE),
    ):
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id=wid,
                kernel=Path("/k"),
                rootfs=Path("/r"),
                egress_mode=mode,
            )
        )
    app.state.net = RecordingNet()
    frames: list[tuple[str, dict]] = []
    hub = EventHub()
    queue = hub.subscribe()

    class Capture:
        async def publish(self, event, data):
            frames.append((event, data))
            await hub.publish(event, data)

    app.state.hub = Capture()
    try:
        yield app, frames, queue
    finally:
        await app.state.model.close()


async def drain(queue) -> None:
    """Let fire-and-forget publishes land, then empty the queue."""
    await asyncio.sleep(0.01)
    while not queue.empty():
        queue.get_nowait()


async def verdict_of(future: asyncio.Future) -> dict:
    return await asyncio.wait_for(future, 1.0)


async def test_allow_mode_records_and_allows(engine_app) -> None:
    app, frames, queue = engine_app
    future = await app.state.consent.hold("ws-allow", "x.example", 443)
    assert (await verdict_of(future))["decision"] == "allow"
    rows = await app.state.model.egress_consent.list_requests("ws-allow")
    assert [r["decision"] for r in rows] == ["allowed"]
    assert rows[0]["decided_by"] is None  # a policy row, not a verdict
    await drain(queue)
    assert frames == []  # allow mode prompts nobody


async def test_static_mode_records_and_denies(engine_app) -> None:
    app, frames, _queue = engine_app
    future = await app.state.consent.hold("ws-static", "x.example", 443)
    assert (await verdict_of(future))["decision"] == "deny"
    rows = await app.state.model.egress_consent.list_requests("ws-static")
    assert [r["decision"] for r in rows] == ["denied"]
    assert frames == []


async def test_interactive_without_decider_denies_fast(engine_app) -> None:
    app, frames, _queue = engine_app
    future = await app.state.consent.hold("ws-interactive", "x.example", 443)
    verdict = await verdict_of(future)
    assert verdict["decision"] == "deny"
    assert verdict["reason"] == "static"  # no human, no hold
    rows = await app.state.model.egress_consent.list_requests("ws-interactive")
    assert [r["decision"] for r in rows] == ["denied"]
    assert rows[0]["decided_by"] is None


async def test_interactive_holds_prompts_and_resolves(engine_app) -> None:
    app, frames, queue = engine_app
    app.state.deciders.register(1, "ws-interactive")
    future = await app.state.consent.hold("ws-interactive", "api.example", 443)
    assert not future.done()
    await drain(queue)
    requests = [f for e, f in frames if e == "egress.request"]
    assert requests and requests[0]["request"]["dest_host"] == "api.example"
    verdict = await app.state.consent.resolve(
        requests[0]["request"]["id"], DECISION_ALLOWED, "token", "5m"
    )
    assert verdict["decision"] == "allow"
    assert (await verdict_of(future))["duration"] == "5m"
    # Session memory recorded before the future resolved.
    ttl = app.state.consent.session.allow_ttl(
        "ws-interactive", "api.example", 443
    )
    assert ttl is not None and 0 < ttl <= 300
    assert (
        app.state.consent.session.allow_ttl(
            "ws-interactive", "api.example", 8443
        )
        is None
    )  # port-scoped
    await drain(queue)
    assert any(e == "egress.resolved" for e, _f in frames)
    assert any(e == "egress.rules" for e, _f in frames)
    # A second resolve finds the hold gone.
    assert (
        await app.state.consent.resolve(
            requests[0]["request"]["id"], DECISION_ALLOWED, "token", "once"
        )
        is None
    )


async def test_rate_limit_and_duplicate_deny(engine_app) -> None:
    app, _frames, _queue = engine_app
    # The fixture's short timeout is for the expiry tests; this one
    # asserts holds stay pending, so give them room under load.
    app.state.settings.net.consent_timeout_s = 30.0
    app.state.deciders.register(1, "ws-interactive")
    holds = [
        await app.state.consent.hold("ws-interactive", f"h{i}.example", 443)
        for i in range(2)
    ]
    # A duplicate SYN for a held destination is denied (no second hold);
    # the dedup answers while the cap still has room.
    dup = await app.state.consent.hold("ws-interactive", "h0.example", 443)
    assert (await verdict_of(dup))["reason"] == "duplicate"
    third = await app.state.consent.hold("ws-interactive", "h2.example", 443)
    # Past the cap (3), new holds are refused outright.
    capped = await app.state.consent.hold("ws-interactive", "h9.example", 443)
    assert (await verdict_of(capped))["reason"] == "rate_limited"
    assert not holds[0].done() and not third.done()


async def test_timeout_expires_the_hold(engine_app) -> None:
    app, frames, _queue = engine_app
    app.state.deciders.register(1, "ws-interactive")
    future = await app.state.consent.hold(
        "ws-interactive", "slow.example", 443
    )
    verdict = await verdict_of(future)
    assert verdict["decision"] == "deny"
    assert verdict["reason"] == "timeout"
    rows = await app.state.model.egress_consent.list_requests(
        "ws-interactive", decision="expired"
    )
    assert rows and rows[0]["dest_host"] == "slow.example"
    assert any(e == "egress.resolved" for e, _f in frames)


async def test_stop_and_workspace_stop_fail_close(engine_app) -> None:
    app, _frames, _queue = engine_app
    app.state.settings.net.consent_timeout_s = 30.0
    app.state.deciders.register(1, "ws-interactive")
    future = await app.state.consent.hold("ws-interactive", "a.example", 443)
    other = await app.state.consent.hold("ws-interactive", "b.example", 443)
    await app.state.consent.on_workspace_stop("ws-interactive")
    assert (await verdict_of(future))["reason"] == "stopped"
    assert (await verdict_of(other))["reason"] == "stopped"
    # tilrestart rows cleared, session memory gone.
    assert (
        app.state.consent.session.allow_ttl("ws-interactive", "a.example", 443)
        is None
    )
    # A fresh hold after stop still works; engine.stop fails it closed.
    app.state.deciders.register(2, "ws-interactive")
    held = await app.state.consent.hold("ws-interactive", "c.example", 443)
    await app.state.consent.stop()
    assert (await verdict_of(held))["reason"] == "shutdown"


async def test_start_reaps_orphaned_pending(engine_app) -> None:
    app, _frames, _queue = engine_app
    model = app.state.model.egress_consent
    row = await model.create_request("ws-interactive", "old.example", 443)
    assert row is not None
    await app.state.consent.start()
    fresh = await model.get_request(row["id"])
    assert fresh["decision"] == "expired"


async def test_revoke_clears_enforcement_and_memory(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")
    future = await engine.hold("ws-interactive", "api.example", 443)
    request_id = None
    rows = await app.state.model.egress_consent.list_requests(
        "ws-interactive", decision="pending"
    )
    request_id = rows[0]["id"]
    await engine.resolve(request_id, DECISION_ALLOWED, "token", "forever")
    assert (await verdict_of(future))["decision"] == "allow"
    assert engine.session.allow_ttl("ws-interactive", "api.example", 443)
    revoked = await engine.revoke(request_id, "token")
    assert revoked is not None and revoked["decision"] == "revoked"
    assert app.state.net.calls == [
        ("clear", "ws-interactive", "api.example", 443)
    ]
    assert (
        engine.session.allow_ttl("ws-interactive", "api.example", 443) is None
    )
    # Idempotent: a second revoke reports the already-revoked row.
    again = await engine.revoke(request_id, "token")
    assert again is not None and again["decision"] == "revoked"
    # Not-a-verdict rows are refused.
    assert await engine.revoke("missing", "token") is None


async def test_resolve_fail_closes_on_a_decide_error(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")

    async def explode(*_args, **_kw):
        raise RuntimeError("db down")

    engine.model.decide = explode
    await engine.hold("ws-interactive", "boom.example", 443)
    verdict = await engine.resolve(
        (
            await app.state.model.egress_consent.list_requests(
                "ws-interactive", decision="pending"
            )
        )[0]["id"],
        DECISION_ALLOWED,
        "token",
        "once",
    )
    assert verdict["decision"] == "deny"
    assert verdict["reason"] == "gone"


async def test_hold_fail_closes_on_a_model_error(engine_app) -> None:
    app, _frames, _queue = engine_app
    app.state.consent.app.state.model.get_workspace = None
    future = await app.state.consent.hold("ws-interactive", "x.example", 443)
    assert (await verdict_of(future))["reason"] == "error"


async def test_rules_frame_and_snapshot(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    assert await engine.rules_frame("missing-ws") is None
    frame = await engine.rules_frame("ws-interactive")
    assert frame["mode"] == MODE_INTERACTIVE
    assert frame["allow_list"] == []
    assert frame["allowed"] == [] and frame["denied"] == []
    # A pending hold snapshots only while held.
    app.state.deciders.register(1, "ws-interactive")
    future = await engine.hold("ws-interactive", "api.example", 443)
    snap = await engine.snapshot("ws-interactive")
    assert [s["request"]["dest_host"] for s in snap] == ["api.example"]
    rows = await app.state.model.egress_consent.list_requests(
        "ws-interactive", decision="pending"
    )
    engine.pop_hold(rows[0]["id"])  # a verdict in flight
    assert await engine.snapshot("ws-interactive") == []
    future.cancel()


def test_duration_ttl_mapping() -> None:
    assert duration_ttl("once") is None
    assert duration_ttl("5m") == 300.0
    assert duration_ttl("15m") == 900.0
    assert duration_ttl("tilrestart") == duration_ttl("forever")
    assert duration_ttl("nonsense") is None


async def test_session_memory_lifecycle(engine_app) -> None:
    app, _frames, _queue = engine_app
    session = app.state.consent.session
    session.allow("ws", "h.example", None, 10.0)
    session.deny("ws", "h.example", 443, 10.0)
    assert session.allow_ttl("ws", "h.example", 0) is not None
    assert session.deny_ttl("ws", "h.example", 443) is not None
    assert session.deny_ttl("ws", "h.example", 0) is None  # port-scoped
    assert session.allow_ports("ws", "h.example") is None  # all-ports
    session.allow("ws", "p.example", 8443, 10.0)
    assert session.allow_ports("ws", "p.example") == {8443}
    assert session.allow_ports("ws", "none.example") == set()
    session.forget("ws", "h.example")
    assert session.allow_ttl("ws", "h.example", 0) is None
    assert session.deny_ttl("ws", "h.example", 443) is None
    # Expired entries stop matching.
    session.allow("ws", "gone.example", None, -1.0)
    assert session.allow_ttl("ws", "gone.example", 0) is None
    assert session.min_allow_ttl("ws", "gone.example") is None
    session.clear("ws")
    assert session.allow_ttl("ws", "p.example", 8443) is None


async def test_remember_verdict_denies_and_portless(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    await engine.remember_verdict(
        "ws-interactive", "d.example", 443, "denied", "5m"
    )
    assert engine.session.deny_ttl("ws-interactive", "d.example", 443) > 0
    # A portless destination keys all-ports.
    await engine.remember_verdict("ws-interactive", "raw", 0, "allowed", "5m")
    assert engine.session.allow_ttl("ws-interactive", "raw", 0) > 0
    # once adds nothing.
    await engine.remember_verdict(
        "ws-interactive", "o.example", 443, "allowed", "once"
    )
    assert engine.session.allow_ttl("ws-interactive", "o.example", 443) is None


async def test_revoke_without_an_enforcement_seam(engine_app) -> None:
    """A net without the consent seam (a test double, or a backend
    that enforces elsewhere) still flips the row."""
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")
    await engine.hold("ws-interactive", "api.example", 443)
    rows = await app.state.model.egress_consent.list_requests(
        "ws-interactive", decision="pending"
    )
    await engine.resolve(rows[0]["id"], "allowed", "token", "forever")
    app.state.net.calls.clear()
    app.state.net.clear_consent_dest = None  # the seam is gone
    revoked = await engine.revoke(rows[0]["id"], "token")
    assert revoked["decision"] == "revoked"
    assert app.state.net.calls == []


async def test_fail_close_survives_an_expire_error(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent

    async def explode(*_args, **_kw):
        raise RuntimeError("db down")

    engine.model.expire_pending = explode
    app.state.deciders.register(1, "ws-interactive")
    future = await engine.hold("ws-interactive", "x.example", 443)
    await engine.fail_close(
        (
            await app.state.model.egress_consent.list_requests(
                "ws-interactive", decision="pending"
            )
        )[0]["id"],
        reason="timeout",
    )
    assert (await verdict_of(future))["reason"] == "timeout"


async def test_resolve_fail_closes_when_cancelled_midflight(
    engine_app,
) -> None:
    """A cancellation landing after the pop fail-closes the hold
    (the consumer's await would hang otherwise)."""
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")
    future = await engine.hold("ws-interactive", "y.example", 443)
    request_id = (
        await app.state.model.egress_consent.list_requests(
            "ws-interactive", decision="pending"
        )
    )[0]["id"]

    async def on_remember(*_args, **_kw):
        raise asyncio.CancelledError

    engine.remember_verdict = on_remember  # lands before the finish
    with pytest.raises(asyncio.CancelledError):
        await engine.resolve(request_id, "allowed", "token", "5m")
    # The decide committed before the cancellation: the verdict on
    # the wire keeps it (the row and the future agree), and only
    # the session-memory tail is lost to the cancel.
    assert future.result()["decision"] == "allow"
    row = await app.state.model.egress_consent.get_request(request_id)
    assert row["decision"] == "allowed"
    assert engine.session.allow_ttl("ws-interactive", "y.example", 443) is None


async def test_cancel_hold_task_reaps_exceptions(engine_app) -> None:
    app, _frames, _queue = engine_app

    async def broken():
        raise RuntimeError("timeout path bug")

    task = asyncio.create_task(broken())
    await app.state.consent.cancel_hold_task(task)


async def test_publish_without_a_hub_and_with_a_failing_hub(
    engine_app,
) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent

    class Boom:
        async def publish(self, event, data):
            raise RuntimeError("subscriber gone")

    hub = app.state.hub
    app.state.hub = None
    engine.publish("egress.rules", {})  # no hub: a no-op
    app.state.hub = Boom()
    engine.publish("egress.rules", {})  # the failure logs, not raises
    await asyncio.sleep(0.01)  # let the fire-and-forget task land
    app.state.hub = hub


async def test_broadcast_rules_survives_a_read_error(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent

    async def explode(_workspace_id):
        raise RuntimeError("db down")

    engine.rules_frame = explode
    await engine.broadcast_rules("ws-interactive")


async def test_cancel_hold_task_names_a_real_failure(engine_app) -> None:
    """A timeout task that died of a bug is reaped with the failure
    logged, not silently swallowed."""
    app, _frames, _queue = engine_app

    async def broken():
        await asyncio.sleep(0)  # let the exception land first
        raise RuntimeError("timeout path bug")

    task = asyncio.create_task(broken())
    await asyncio.sleep(0.01)
    await app.state.consent.cancel_hold_task(task)


async def test_fail_close_without_a_hold_is_a_no_op(engine_app) -> None:
    app, _frames, _queue = engine_app
    await app.state.consent.fail_close("missing", reason="timeout")


async def test_start_with_nothing_to_reap_is_quiet(engine_app) -> None:
    app, _frames, _queue = engine_app
    await app.state.consent.start()  # zero reaped: no log branch


async def test_stop_skips_other_workspaces_holds(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")
    mine = await engine.hold("ws-interactive", "mine.example", 443)
    theirs = object()  # a hold registered for a workspace not stopped
    engine._holds["foreign"] = {
        "future": asyncio.get_running_loop().create_future(),
        "workspace_id": "ws-elsewhere",
        "task": asyncio.create_task(asyncio.sleep(3600)),
    }
    await engine.on_workspace_stop("ws-interactive")
    assert (await verdict_of(mine))["reason"] == "stopped"
    assert "foreign" in engine._holds
    engine._holds["foreign"]["task"].cancel()
    del engine._holds["foreign"]
    del theirs


async def test_pop_hold_of_an_unknown_request(engine_app) -> None:
    app, _frames, _queue = engine_app
    assert app.state.consent.pop_hold("missing") is None


async def test_snapshot_skips_rows_no_longer_held(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")
    await engine.hold("ws-interactive", "gone.example", 443)
    rows = await app.state.model.egress_consent.list_requests(
        "ws-interactive", decision="pending"
    )
    engine.pop_hold(rows[0]["id"])  # resolved in flight
    assert await engine.snapshot("ws-interactive") == []


async def test_stop_tolerates_a_vanished_hold(engine_app) -> None:
    """A hold that a racing timeout popped between the stop's
    snapshot and its iteration is skipped, not crashed on."""
    app, _frames, _queue = engine_app
    engine = app.state.consent
    app.state.deciders.register(1, "ws-interactive")
    first = await engine.hold("ws-interactive", "a.example", 443)
    second = await engine.hold("ws-interactive", "b.example", 443)
    request_ids = list(engine._holds)
    real_cancel = engine.cancel_hold_task

    async def cancel_and_steal(task):
        # The racing timeout: the OTHER hold pops while the first is
        # being reaped.
        engine._holds.pop(request_ids[1], None)
        await real_cancel(task)

    engine.cancel_hold_task = cancel_and_steal
    await engine.stop()
    assert (await verdict_of(first))["reason"] == "shutdown"
    assert not second.done()  # stolen by the racing timeout


async def test_finish_twice_keeps_the_first_verdict(engine_app) -> None:
    app, _frames, _queue = engine_app
    engine = app.state.consent
    hold = {
        "future": asyncio.get_running_loop().create_future(),
        "workspace_id": "ws",
        "task": asyncio.create_task(asyncio.sleep(3600)),
    }
    engine.finish(hold, {"decision": "allow"})
    engine.finish(hold, {"decision": "deny"})  # already done: no-op
    assert hold["future"].result()["decision"] == "allow"
    hold["task"].cancel()


async def test_broadcast_rules_skips_missing_workspaces(engine_app) -> None:
    app, _frames, _queue = engine_app
    await app.state.consent.broadcast_rules("missing-ws")

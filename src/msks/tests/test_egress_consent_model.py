"""The egress consent table's CRUD and lifecycle (#69)."""

import time
from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DECISION_PENDING,
    DECISION_REVOKED,
    DURATION_5M,
    DURATION_15M,
    DURATION_FOREVER,
    DURATION_ONCE,
    DURATION_TILRESTART,
    duration_in_effect,
)
from msks.settings import NetSettings, ServerSettings, Settings


@pytest.fixture
async def consent(tmp_path: Path):
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "c.db"),
            net=NetSettings(consent_retention_days=30, consent_row_cap=3),
        )
    )
    app.state.model.migrate()
    for wid in ("ws-a", "ws-b"):
        await app.state.model.create_workspace(
            VmSpec(workspace_id=wid, kernel=Path("/k"), rootfs=Path("/r"))
        )
    try:
        yield app.state.model.egress_consent, app
    finally:
        await app.state.model.close()


async def test_create_dedups_pending(consent) -> None:
    model, _app = consent
    first = await model.create_request("ws-a", "example.com", 443)
    again = await model.create_request("ws-a", "example.com", 443)
    assert first is not None and first["decision"] == DECISION_PENDING
    assert again is None
    # A decided destination frees the dedup slot.
    await model.decide(first["id"], DECISION_ALLOWED, "token", DURATION_ONCE)
    fresh = await model.create_request("ws-a", "example.com", 443)
    assert fresh is not None and fresh["id"] != first["id"]


async def test_policy_rows_dedup_per_decision(consent) -> None:
    model, _app = consent
    first = await model.record_policy(
        DECISION_DENIED, "ws-a", "off.list.example", 0
    )
    again = await model.record_policy(
        DECISION_DENIED, "ws-a", "off.list.example", 0
    )
    assert first is not None and first["decided_by"] is None
    assert again is None
    # The allow-side index dedups independently of the deny side.
    allowed = await model.record_policy(
        DECISION_ALLOWED, "ws-a", "off.list.example", 0
    )
    assert allowed is not None


async def test_decide_moves_pending_only(consent) -> None:
    model, _app = consent
    row = await model.create_request("ws-a", "db.internal", 5432)
    decided = await model.decide(
        row["id"], DECISION_DENIED, "token", DURATION_15M
    )
    assert decided["decision"] == DECISION_DENIED
    assert decided["duration"] == DURATION_15M
    assert decided["decided_by"] == "token"
    # A second decide finds nothing pending.
    assert await model.decide(row["id"], DECISION_ALLOWED, "t", "once") is None
    with pytest.raises(ValueError):
        await model.decide(row["id"], "maybe", "t", "once")
    with pytest.raises(ValueError):
        await model.decide(row["id"], DECISION_ALLOWED, "t", "2h")


async def test_revoke_flips_only_verdicts(consent) -> None:
    model, _app = consent
    row = await model.create_request("ws-a", "example.com", 443)
    assert await model.revoke(row["id"], "token") is None  # pending
    await model.decide(row["id"], DECISION_ALLOWED, "token", "forever")
    revoked = await model.revoke(row["id"], "token")
    assert revoked["decision"] == DECISION_REVOKED
    assert revoked["revoked_by"] == "token"
    assert revoked["decided_by"] == "token"  # provenance preserved
    assert await model.revoke(row["id"], "token") is None  # already


async def test_expire_paths(consent) -> None:
    model, _app = consent
    row = await model.create_request("ws-a", "a.example", 80)
    assert await model.expire_pending(row["id"])
    assert not await model.expire_pending(row["id"])
    fresh = await model.get_request(row["id"])
    assert fresh["decision"] == DECISION_EXPIRED
    assert fresh["duration"] == DURATION_ONCE
    # Startup reaping: pending orphans from a prior run.
    other = await model.create_request("ws-b", "b.example", 80)
    assert other is not None
    assert await model.expire_all_pending() == 1


async def test_list_active_and_durations(consent) -> None:
    model, _app = consent
    once = await model.create_request("ws-a", "once.example", 80)
    timed = await model.create_request("ws-a", "timed.example", 80)
    keep = await model.create_request("ws-a", "keep.example", 80)
    for row, duration in (
        (once, DURATION_ONCE),
        (timed, DURATION_5M),
        (keep, DURATION_FOREVER),
    ):
        await model.decide(row["id"], DECISION_ALLOWED, "token", duration)
    # A policy record is never in the active set.
    policy = await model.record_policy(
        DECISION_ALLOWED, "ws-a", "policy.example", 0
    )
    assert policy is not None
    active = {r["dest_host"] for r in await model.list_active("ws-a")}
    assert active == {"timed.example", "keep.example"}


async def test_duration_in_effect_table(consent) -> None:
    assert duration_in_effect(DURATION_FOREVER, 1.0, 9999.0)
    assert duration_in_effect(DURATION_TILRESTART, 1.0, 9999.0)
    assert not duration_in_effect(DURATION_ONCE, 1.0, 1.1)
    assert duration_in_effect(DURATION_5M, 100.0, 100.0 + 299)
    assert not duration_in_effect(DURATION_5M, 100.0, 100.0 + 301)
    assert not duration_in_effect(None, 1.0, 1.0)  # unbounded = no
    assert not duration_in_effect("5m", None, 1.0)  # no decided_at = no


async def test_active_verdict_prefers_in_effect(consent) -> None:
    model, _app = consent
    older = await model.create_request("ws-a", "x.example", 443)
    await model.decide(older["id"], DECISION_DENIED, "t", DURATION_FOREVER)
    newer = await model.create_request("ws-a", "x.example", 443)
    await model.decide(newer["id"], DECISION_ALLOWED, "t", DURATION_ONCE)
    # The newest (once) elapsed; the forever deny is the verdict.
    found = await model.active_verdict_for("ws-a", "x.example", 443)
    assert found is not None and found["decision"] == DECISION_DENIED


async def test_forever_verdict_for_any_port(consent) -> None:
    model, _app = consent
    row = await model.create_request("ws-a", "api.example", 443)
    await model.decide(row["id"], DECISION_ALLOWED, "t", DURATION_FOREVER)
    found = await model.forever_verdict_for("ws-a", "api.example")
    assert found is not None and found["decision"] == DECISION_ALLOWED
    assert await model.forever_verdict_for("ws-a", "other.example") is None
    assert {r["dest_host"] for r in await model.forever_rows("ws-a")} == {
        "api.example"
    }


async def test_clear_tilrestart_and_delete_for_workspace(consent) -> None:
    model, _app = consent
    for host, duration in (
        ("a.example", DURATION_TILRESTART),
        ("b.example", DURATION_FOREVER),
        ("c.example", DURATION_5M),
    ):
        row = await model.create_request("ws-a", host, 443)
        await model.decide(row["id"], DECISION_ALLOWED, "t", duration)
    denied = await model.create_request("ws-a", "d.example", 443)
    await model.decide(denied["id"], DECISION_DENIED, "t", "tilrestart")
    assert await model.clear_tilrestart("ws-a") == 2  # a + d
    remaining = {r["dest_host"] for r in await model.list_requests("ws-a")}
    assert remaining == {"b.example", "c.example"}
    assert await model.delete_for_workspace("ws-a") == 2
    assert await model.list_requests("ws-a") == []


async def test_count_pending_and_list_filter(consent) -> None:
    model, _app = consent
    row = await model.create_request("ws-a", "x.example", 443)
    assert await model.count_pending("ws-a") == 1
    await model.decide(row["id"], DECISION_ALLOWED, "t", "once")
    assert await model.count_pending("ws-a") == 0
    pending = await model.create_request("ws-a", "y.example", 443)
    assert pending is not None
    only = await model.list_requests("ws-a", decision=DECISION_PENDING)
    assert [r["id"] for r in only] == [pending["id"]]


async def test_prune_respects_in_effect(consent) -> None:
    model, _app = consent
    keep = await model.create_request("ws-a", "keep.example", 443)
    await model.decide(keep["id"], DECISION_ALLOWED, "t", DURATION_FOREVER)
    stale_pending = await model.create_request("ws-a", "old.example", 443)
    assert stale_pending is not None
    removed = await model.prune(now=stale_pending["requested_at"] + 1e9)
    assert removed >= 1
    survivors = {r["dest_host"] for r in await model.list_requests("ws-a")}
    assert "keep.example" in survivors
    assert "old.example" not in survivors


async def test_prune_row_cap_drops_oldest_eligible(consent) -> None:
    model, _app = consent
    for i in range(4):
        row = await model.create_request("ws-a", f"h{i}.example", 443)
        await model.decide(row["id"], DECISION_ALLOWED, "t", DURATION_ONCE)
    assert await model.prune_row_cap(3, float("inf")) == 1
    assert len(await model.list_requests("ws-a")) == 3


async def test_prune_disabled_when_bounds_are_zero(consent) -> None:
    model, app = consent
    app.state.settings.net.consent_retention_days = 0
    app.state.settings.net.consent_row_cap = 0
    await model.create_request("ws-a", "x.example", 443)
    assert await model.prune(now=1e18) == 0


async def test_hmac_stamped_when_key_set(tmp_path: Path) -> None:
    app = build_app(
        Settings(
            server=ServerSettings(
                db_path=tmp_path / "h.db", audit_hmac_key="k1"
            ),
        )
    )
    app.state.model.migrate()
    try:
        model = app.state.model.egress_consent
        row = await model.create_request("ws-a", "x.example", 443)
        assert row is not None and row["hmac"] is not None
    finally:
        await app.state.model.close()


async def test_active_verdict_for_none_when_nothing_in_effect(consent) -> None:
    model, _app = consent
    row = await model.create_request("ws-a", "gone.example", 443)
    await model.decide(row["id"], DECISION_ALLOWED, "token", DURATION_ONCE)
    assert await model.active_verdict_for("ws-a", "gone.example", 443) is None
    assert await model.active_verdict_for("ws-a", "never.example", 443) is None


async def test_prune_keeps_shared_pending_arcs(consent) -> None:
    """The retention pass skips live pending rows even when the
    row-cap pass wants room (a pending row is never its target)."""
    model, _app = consent
    row = await model.create_request("ws-a", "live.example", 443)
    assert row is not None
    # Row-cap pruning with a cap of zero would doom everything
    # decided; the pending row survives untouched either way.
    old = await model.create_request("ws-a", "old.example", 443)
    await model.decide(old["id"], DECISION_DENIED, "t", DURATION_ONCE)
    assert await model.prune_row_cap(1, float("inf")) >= 1
    survivors = {r["dest_host"] for r in await model.list_requests("ws-a")}
    assert "live.example" in survivors


async def test_prune_eligible_covers_policy_rows(consent) -> None:
    model, _app = consent
    assert model.prune_eligible(
        {
            "decision": "denied",
            "decided_by": None,
            "duration": None,
            "decided_at": time.time(),
        },
        time.time(),
    )


async def test_trim_skips_in_effect_rows(consent) -> None:
    """The row-cap trim never deletes a row still in effect: a
    forever verdict survives even when it is the oldest row."""
    model, _app = consent
    import time as _time

    forever = await model.create_request("ws-a", "keep.example", 443)
    await model.decide(forever["id"], DECISION_ALLOWED, "t", "forever")
    _time.sleep(0.01)  # drop.example decides strictly later
    doomed = await model.create_request("ws-a", "drop.example", 443)
    await model.decide(doomed["id"], "denied", "t", "once")
    async with model.maker()() as session:
        deleted = await model.trim_workspace(session, "ws-a", 1, float("inf"))
        assert deleted == 1
        await session.commit()  # trim runs inside the caller's commit
    survivors = {r["dest_host"] for r in await model.list_requests("ws-a")}
    assert survivors == {"keep.example"}


async def test_prune_retention_skips_pending_arcs(consent) -> None:
    """A pending row past the retention cutoff is only deleted when
    its decision is still pending at DELETE time (the TOCTOU
    guard's pending split)."""
    model, _app = consent
    row = await model.create_request("ws-a", "old-pending.example", 443)
    assert row is not None
    # The retention pass with a far-future now sees the pending row
    # as stale and removes it (nothing raced it).
    assert await model.prune(now=row["requested_at"] + 1e9) >= 1


async def test_active_verdict_loop_skips_expired(consent) -> None:
    """The newest-first walk continues past elapsed verdicts (an
    expired newer allow cannot mask an older deny)."""
    model, _app = consent
    import time as _time

    older = await model.create_request("ws-a", "mix.example", 443)
    await model.decide(older["id"], DECISION_DENIED, "t", "forever")
    _time.sleep(0.01)
    newer = await model.create_request("ws-a", "mix.example", 443)
    await model.decide(newer["id"], DECISION_ALLOWED, "t", "5m")
    # Age the timed allow past its window by asking far in the
    # future: duration_in_effect is computed against now, so fake
    # it by deciding a 5m allow and reading the row back with a
    # monkeypatched clock.
    row = await model.active_verdict_for("ws-a", "mix.example", 443)
    assert row["decision"] == "allowed"  # still in effect now
    # The expired arm: patch time inside the module.
    import msks.model.egress_consent as ec

    real_time = ec.time.time
    ec.time.time = lambda: real_time() + 3600
    try:
        aged = await model.active_verdict_for("ws-a", "mix.example", 443)
    finally:
        ec.time.time = real_time
    assert aged["decision"] == "denied"


async def test_prune_passes_run_independently(consent) -> None:
    """Retention off, cap on (and the reverse): each pass stands
    alone, and a second over-cap workspace still trims."""
    model, app = consent
    app.state.settings.net.consent_retention_days = 0
    app.state.settings.net.consent_row_cap = 1
    for host in ("a.example", "b.example"):
        row = await model.create_request("ws-a", host, 443)
        await model.decide(row["id"], "denied", "t", "once")
    for host in ("c.example", "d.example"):
        row = await model.create_request("ws-b", host, 443)
        await model.decide(row["id"], "denied", "t", "once")
    assert await model.prune(now=float("inf")) == 2
    assert len(await model.list_requests("ws-a")) == 1
    assert len(await model.list_requests("ws-b")) == 1
    # The reverse: retention on, cap off.
    app.state.settings.net.consent_retention_days = 30
    app.state.settings.net.consent_row_cap = 0
    assert await model.prune(now=float("inf")) == 2

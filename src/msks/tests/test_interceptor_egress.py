"""The web-egress gate's decision table (#452), against a stub
app: the posture split, the coverage precedence, and the fail
closed on every failure."""

import asyncio
from types import SimpleNamespace

from msks.consent.coordinator import SessionMemory
from msks.interceptor import egress

WS = "ws-gate"
HOST = "api.example.com"
ADDR = "198.51.100.7"


def row(mode="interactive", allowlist=()):
    return {
        "id": WS,
        "egress_mode": mode,
        "egress_allowlist": list(allowlist),
    }


class FakeModel:
    def __init__(self, rows, forever=None):
        self.rows = rows
        self.forever = forever
        self.policy_rows: list[tuple] = []
        # The consent submodel's shape: the gate reads its verdict
        # and policy surfaces through ``model.egress_consent``.
        self.egress_consent = self

    async def get_workspace(self, workspace_id):
        return self.rows.get(workspace_id)

    async def forever_verdict_for(self, workspace_id, host):
        return self.forever

    async def record_policy(self, decision, workspace_id, host, port):
        self.policy_rows.append((decision, workspace_id, host, port))


class FakeEngine:
    """The consent engine surface the gate reads: a real session
    memory plus a hold stub that records the call."""

    def __init__(self, decided=None):
        self.session = SessionMemory()
        self.decided = decided or {"decision": "allow", "reason": "decided"}
        self.holds: list[tuple] = []

    async def hold(self, workspace_id, host, port):
        self.holds.append((workspace_id, host, port))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(self.decided)
        return fut


def app(model, engine):
    return SimpleNamespace(state=SimpleNamespace(model=model, consent=engine))


async def decide(model, engine, host=HOST, port=443, address=ADDR):
    return await egress.decide(app(model, engine), WS, host, port, address)


# --- the posture split -------------------------------------------------------


async def test_allow_mode_passes_without_rows() -> None:
    model = FakeModel({WS: row(mode="allow")})
    verdict = await decide(model, FakeEngine())
    assert verdict == egress.WebVerdict(True, "ungated")
    assert model.policy_rows == []


async def test_a_missing_workspace_denies() -> None:
    verdict = await decide(FakeModel({}), FakeEngine())
    assert verdict == egress.WebVerdict(False, "gone")


async def test_an_unnamed_flow_denies() -> None:
    model = FakeModel({WS: row()})
    verdict = await egress.decide(app(model, FakeEngine()), WS, "", 443, "")
    assert verdict == egress.WebVerdict(False, "unnamed")


async def test_any_failure_answers_deny() -> None:
    class Broken:
        async def get_workspace(self, workspace_id):
            raise RuntimeError("model down")

    verdict = await egress.decide(
        SimpleNamespace(
            state=SimpleNamespace(model=Broken(), consent=FakeEngine())
        ),
        WS,
        HOST,
        443,
        ADDR,
    )
    assert verdict == egress.WebVerdict(False, "error")


# --- durable and session coverage -------------------------------------


async def test_a_session_deny_wins_over_the_allowlist() -> None:
    model = FakeModel({WS: row(allowlist=(HOST,))})
    engine = FakeEngine()
    engine.session.deny(WS, HOST, None, 300.0)
    verdict = await decide(model, engine)
    assert verdict == egress.WebVerdict(False, "verdict")
    assert engine.holds == []


async def test_a_forever_deny_row_wins_over_the_allowlist() -> None:
    model = FakeModel(
        {WS: row(allowlist=(HOST,))},
        forever={"decision": "denied"},
    )
    verdict = await decide(model, FakeEngine())
    assert verdict == egress.WebVerdict(False, "verdict")


async def test_a_session_allow_covers_the_name() -> None:
    model = FakeModel({WS: row()})
    engine = FakeEngine()
    engine.session.allow(WS, HOST, 443, 300.0)
    verdict = await decide(model, engine)
    assert verdict == egress.WebVerdict(True, "verdict")
    assert engine.holds == []


async def test_a_port_scoped_session_allow_covers_only_its_port() -> None:
    """A port-scoped session allow covers its own port only: the
    other port still gates (the safe direction)."""
    model = FakeModel({WS: row()})
    engine = FakeEngine()
    engine.session.allow(WS, HOST, 8443, 300.0)
    verdict = await decide(model, engine)
    assert verdict == egress.WebVerdict(True, "decided")
    assert engine.holds == [(WS, HOST, 443)]


async def test_a_forever_allow_row_covers_the_name() -> None:
    model = FakeModel({WS: row()}, forever={"decision": "allowed"})
    verdict = await decide(model, FakeEngine())
    assert verdict == egress.WebVerdict(True, "verdict")


# --- the static allowlist ----------------------------------------------


async def test_a_name_spec_allows_its_port() -> None:
    model = FakeModel({WS: row(allowlist=(f"{HOST}:443",))})
    verdict = await decide(model, FakeEngine())
    assert verdict == egress.WebVerdict(True, "allowlist")


async def test_a_port_scoped_name_spec_denies_the_other_port() -> None:
    model = FakeModel({WS: row(mode="static", allowlist=(f"{HOST}:443",))})
    verdict = await decide(model, FakeEngine(), port=80)
    assert verdict == egress.WebVerdict(False, "static")


async def test_an_inclusive_suffix_spec_allows_subdomains() -> None:
    model = FakeModel({WS: row(allowlist=(".example.com",))})
    verdict = await decide(model, FakeEngine(), host=f"api.{HOST}")
    assert verdict == egress.WebVerdict(True, "allowlist")


async def test_an_address_spec_allows_the_original_destination() -> None:
    model = FakeModel({WS: row(allowlist=(f"{ADDR}/32:443",))})
    verdict = await decide(model, FakeEngine(), host="")
    assert verdict == egress.WebVerdict(True, "allowlist")


async def test_an_address_spec_port_scopes() -> None:
    model = FakeModel({WS: row(mode="static", allowlist=(f"{ADDR}/32:443",))})
    verdict = await decide(model, FakeEngine(), host="", port=80)
    assert verdict == egress.WebVerdict(False, "static")


async def test_an_address_spec_denies_a_foreign_address() -> None:
    model = FakeModel({WS: row(mode="static", allowlist=("203.0.113.0/24",))})
    verdict = await decide(model, FakeEngine())
    assert verdict == egress.WebVerdict(False, "static")


# --- the mode tail -----------------------------------------------------


async def test_static_records_the_denial_and_denies() -> None:
    model = FakeModel({WS: row(mode="static")})
    verdict = await decide(model, FakeEngine(), port=80)
    assert verdict == egress.WebVerdict(False, "static")
    assert model.policy_rows == [("denied", WS, HOST, 80)]


async def test_a_failed_policy_record_still_denies() -> None:
    class Broken(FakeModel):
        async def record_policy(self, *args):
            raise RuntimeError("write failed")

    model = Broken({WS: row(mode="static")})
    verdict = await decide(model, FakeEngine())
    assert verdict == egress.WebVerdict(False, "static")


async def test_interactive_holds_through_the_engine() -> None:
    model = FakeModel({WS: row()})
    engine = FakeEngine(decided={"decision": "deny", "reason": "decided"})
    verdict = await decide(model, engine)
    assert verdict == egress.WebVerdict(False, "decided")
    assert engine.holds == [(WS, HOST, 443)]


async def test_the_gate_passes_the_address_as_the_name_when_nameless() -> None:
    """A nameless flow (no SNI, no Host) keys the hold by the
    original destination address — the kernel path's raw-IP shape."""
    model = FakeModel({WS: row()})
    engine = FakeEngine()
    verdict = await egress.decide(app(model, engine), WS, ADDR, 443, ADDR)
    assert verdict == egress.WebVerdict(True, "decided")
    assert engine.holds == [(WS, ADDR, 443)]

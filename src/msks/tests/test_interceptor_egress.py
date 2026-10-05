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


async def decide(model, engine, host=HOST, port=443, address=ADDR, named=HOST):
    return await egress.decide(
        app(model, engine), WS, host, port, address, named
    )


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
    verdict = await egress.decide(
        app(model, FakeEngine()), WS, "", 443, "", None
    )
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
        None,
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
    verdict = await decide(model, FakeEngine(), host="", named=None)
    assert verdict == egress.WebVerdict(True, "allowlist")


async def test_an_address_spec_port_scopes() -> None:
    model = FakeModel({WS: row(mode="static", allowlist=(f"{ADDR}/32:443",))})
    verdict = await decide(model, FakeEngine(), host="", port=80, named=None)
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
    verdict = await egress.decide(
        app(model, engine), WS, ADDR, 443, ADDR, None
    )
    assert verdict == egress.WebVerdict(True, "decided")
    assert engine.holds == [(WS, ADDR, 443)]


# --- the gate key (#452 review: binding and case) ----------------------------


def test_gate_key_lowercases_the_wire_name() -> None:
    """DNS names compare case-insensitively: the wire's case must
    not fork the verdict rows (a mixed-case SNI cannot slip a
    standing deny)."""
    assert egress.gate_key("API.Example.COM", ADDR, None) == ADDR
    assert (
        egress.gate_key("API.Example.COM", ADDR, "api.example.com")
        == "api.example.com"
    )


async def test_a_mixed_case_sni_hits_the_standing_deny() -> None:
    """The deny-beats-allowlist precedence survives the wire's
    case: the key lowercases before every lookup."""
    model = FakeModel(
        {WS: row(allowlist=(HOST,))},
        forever={"decision": "denied"},
    )
    verdict = await decide(model, FakeEngine(), host=HOST.upper())
    assert verdict == egress.WebVerdict(False, "verdict")


async def test_a_mixed_case_name_matches_the_allowlist() -> None:
    model = FakeModel({WS: row(mode="static", allowlist=(HOST,))})
    verdict = await decide(model, FakeEngine(), host=HOST.upper())
    assert verdict == egress.WebVerdict(True, "allowlist")


async def test_an_unbound_name_keys_by_the_address() -> None:
    """A name the naming memory never bound to the connection's
    address keys the gate by the address: the allowlist's name
    specs cannot cover it (the address specs still can), and an
    interactive hold prompts with the address."""
    model = FakeModel({WS: row(mode="static", allowlist=(HOST,))})
    verdict = await decide(model, FakeEngine(), named=None)
    assert verdict == egress.WebVerdict(False, "static")


async def test_a_fronted_name_keys_by_the_memorys_name() -> None:
    """A name claiming an address the memory holds under another
    name keys by the memory's name, not the claim."""
    model = FakeModel({WS: row(mode="static", allowlist=(HOST,))})
    verdict = await decide(
        model,
        FakeEngine(),
        host="claimed.example.net",
        named="evil.example.net",
    )
    assert verdict == egress.WebVerdict(False, "static")


async def test_a_bound_name_borrows_its_verdicts() -> None:
    """The binding made the kernel path's promise true here too:
    a name-keyed verdict covers the connection once the memory
    binds the address."""
    model = FakeModel({WS: row()})
    engine = FakeEngine()
    engine.session.allow(WS, HOST, 443, 300.0)
    verdict = await decide(model, engine, named=HOST)
    assert verdict == egress.WebVerdict(True, "verdict")
    assert engine.holds == []


def test_a_non_address_original_destination_matches_no_address_spec() -> None:
    """An address spec answers nothing when the redirect's
    preserved destination is not an IPv4 literal."""
    policy = egress.EgressPolicy(WS, "static", ("203.0.113.0/24",))
    assert egress.address_allowed(policy.ip_specs, "", 443) is False

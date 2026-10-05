"""The web-egress consent gate (#452).

While the interceptor is armed, the nft prerouting redirect owns
the guest's TCP flows to ports 80/443: they become host input to
the per-tap listener and never reach the forward chain where the
NFQUEUE consent gate lives. This module is the consent gate for
exactly those flows — the Layer-7 complement of the kernel path,
keyed by the name the wire carries (SNI for TLS, the Host header
for plain HTTP) instead of the address a SYN names.

The decision table mirrors the resolver gate's precedence, with
deny winning over allow (the safe direction), and every check
keys on the destination :func:`gate_key` derives — the wire's
name only when the naming memory binds it to the connection's
address, else the address itself (a fronted or hosts-file dial
cannot borrow a name's allowlist entry or verdict):

- a name-keyed verdict already covers the destination — a session
  deny, or a durable ``forever`` deny row — deny;
- the workspace's static allowlist covers it — a name spec
  matching the wire's name on this port, or an address spec
  matching the original destination the redirect preserved —
  allow without a prompt;
- a session allow or a ``forever`` allow row covers it — allow;
- ``static`` mode records the denial and denies;
- ``interactive`` mode holds through the consent engine — the
  same decider endpoints, rows, timeouts, and session memory the
  kernel path uses, so one verdict vocabulary serves both gates.

``allow`` mode (the default-permit posture) passes without a
row per request: the naming layer already records the
destinations the guest resolves, and the kernel path records
nothing per flow in this mode either.

The gate never raises: any failure answers deny (fail-closed — a
denied web request answers locally, never forwarded).
"""

import contextlib
import logging
from dataclasses import dataclass

from ..spec.egress import (
    DECISION_DENIED,
    MODE_STATIC,
    EgressPolicy,
    covers,
    is_ipv4,
    ports_for,
)

logger = logging.getLogger(__name__)

# The verdict words the consent engine resolves its futures with
# (its own constants sit a rank above this package; the values are
# the stable contract both sides speak).
VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"


@dataclass(frozen=True)
class WebVerdict:
    """One web flow's egress decision."""

    allowed: bool
    reason: str


def allow(reason: str) -> WebVerdict:
    return WebVerdict(True, reason)


def deny(reason: str) -> WebVerdict:
    return WebVerdict(False, reason)


async def decide(
    app,
    workspace_id: str,
    host: str,
    port: int,
    address: str,
    named: str | None,
) -> WebVerdict:
    """The gate for one redirected web flow — never raises.

    ``host`` is the name the wire carried (SNI or Host header,
    falling back to the address when neither rode); ``address``
    and ``port`` are the original destination the redirect
    preserved; ``named`` is the name the daemon's naming memory
    holds for that address (None when it never resolved).
    """
    try:
        return await gate(app, workspace_id, host, port, address, named)
    except Exception:  # noqa: BLE001 - named below, fail-closed
        logger.exception(
            "interceptor: the egress gate failed for %s; answering deny",
            workspace_id,
        )
        return deny("error")


def gate_key(host: str, address: str, named: str | None) -> str:
    """The destination a web flow gates on, lowercased (DNS
    semantics — the wire's case must not fork the verdict rows).

    The wire's name counts only when the naming memory binds it
    to the connection's address — the guest resolved it through
    the daemon's resolver. A name the memory holds for the
    address under a different spelling keys by the memory's name
    (a fronted claim); an address the memory never learned keys
    by the address itself — the kernel path's keying, so a
    hosts-file or raw-IP dial cannot borrow an allowlist entry
    or a verdict granted to a name.
    """
    host = host.lower()
    if named is not None:
        return host if named == host else named
    return address or host


async def gate(
    app,
    workspace_id: str,
    host: str,
    port: int,
    address: str,
    named: str | None,
) -> WebVerdict:
    """The decision body ``decide`` guards: the posture split,
    then coverage and the mode tail — all keyed by
    :func:`gate_key`."""
    policy = await posture(app, workspace_id)
    if policy is None:
        # A workspace that vanished mid-request: a missing row
        # is not consent (the engine's own missing-row answer).
        return deny("gone")
    if not policy.gated:
        return allow("ungated")
    key = gate_key(host, address, named)
    if not key:
        return deny("unnamed")
    return await covered_or_mode(app, policy, key, port, address)


async def posture(app, workspace_id: str) -> EgressPolicy | None:
    """The workspace's parsed egress posture, None when the row
    is gone."""
    row = await app.state.model.get_workspace(workspace_id)
    return EgressPolicy.from_row(row)


async def covered_or_mode(
    app, policy: EgressPolicy, key: str, port: int, address: str
) -> WebVerdict:
    """Past the posture split: durable and session coverage first
    (deny wins), then the static allowlist, then the mode tail."""
    engine = app.state.consent
    workspace_id = policy.workspace_id
    deny_covered, allow_covered = await standing(
        app, engine, workspace_id, key, port
    )
    if deny_covered:
        return deny("verdict")
    if allowlisted(policy, key, port, address):
        return allow("allowlist")
    if (
        allow_covered
        or engine.session.allow_ttl(workspace_id, key, port) is not None
    ):
        return allow("verdict")
    return await mode_tail(app, policy, key, port)


async def standing(
    app, engine, workspace_id: str, key: str, port: int
) -> tuple[bool, bool]:
    """``(deny_covered, allow_covered)`` from one durable read:
    the session table first, then the ``forever`` row — one read
    decides both halves, so a verdict landing between two looks
    cannot be missed."""
    if engine.session.deny_ttl(workspace_id, key, port) is not None:
        return True, False
    row = await app.state.model.egress_consent.forever_verdict_for(
        workspace_id, key
    )
    denied = row is not None and row["decision"] == DECISION_DENIED
    return denied, row is not None and not denied


def allowlisted(
    policy: EgressPolicy, key: str, port: int, address: str
) -> bool:
    """The static half: name specs match the gated name on this
    port; address specs (CIDRs and literals) match the original
    destination the redirect preserved."""
    return name_allowed(policy.host_specs, key, port) or address_allowed(
        policy.ip_specs, address, port
    )


def name_allowed(specs, key: str, port: int) -> bool:
    """Whether a name spec covers the gated name on ``port``: a
    port-less spec covers every port; a port-scoped one covers
    its own."""
    ports = ports_for(key, specs)
    return covers(ports) and (ports is None or port in ports)


def address_allowed(specs, address: str, port: int) -> bool:
    """Whether an address spec (CIDR or literal) covers the
    original destination on ``port``. An address that is not an
    IPv4 literal (empty, or a form the redirect did not preserve)
    matches nothing."""
    if not is_ipv4(address):
        return False
    return any(
        spec.port in (None, port) and spec.matches(address) for spec in specs
    )


async def mode_tail(
    app, policy: EgressPolicy, key: str, port: int
) -> WebVerdict:
    """The mode tail: ``static`` records the denial and denies
    (the resolver's audit shape, port-keyed here — the wire named
    the port); ``interactive`` holds through the consent engine,
    whose future carries the decision, the reason, and the
    enforcement TTL this path has no kernel pin for."""
    workspace_id = policy.workspace_id
    if policy.mode == MODE_STATIC:
        with contextlib.suppress(Exception):
            await app.state.model.egress_consent.record_policy(
                DECISION_DENIED, workspace_id, key, port
            )
        return deny("static")
    future = await app.state.consent.hold(workspace_id, key, port)
    decided = await future
    return WebVerdict(
        decided.get("decision") == VERDICT_ALLOW,
        decided.get("reason", "decided"),
    )

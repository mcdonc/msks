"""The web-egress consent gate (#452).

While the interceptor is armed, the nft prerouting redirect owns
the guest's TCP flows to ports 80/443: they become host input to
the per-tap listener and never reach the forward chain where the
NFQUEUE consent gate lives. This module is the consent gate for
exactly those flows — the Layer-7 complement of the kernel path,
keyed by the name the wire carries (SNI for TLS, the Host header
for plain HTTP) instead of the address a SYN names.

The decision table mirrors the resolver gate's precedence, with
deny winning over allow (the safe direction):

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
    app, workspace_id: str, host: str, port: int, address: str
) -> WebVerdict:
    """The gate for one redirected web flow — never raises.

    ``host`` is the name the wire carried (SNI or Host header,
    falling back to the address when neither rode); ``address``
    and ``port`` are the original destination the redirect
    preserved.
    """
    try:
        return await gate(app, workspace_id, host, port, address)
    except Exception:  # noqa: BLE001 - named below, fail-closed
        logger.exception(
            "interceptor: the egress gate failed for %s; answering deny",
            workspace_id,
        )
        return deny("error")


async def gate(
    app, workspace_id: str, host: str, port: int, address: str
) -> WebVerdict:
    """The decision body ``decide`` guards: the posture split,
    then coverage and the mode tail."""
    policy = await posture(app, workspace_id)
    if policy is None:
        # A workspace that vanished mid-request: a missing row
        # is not consent (the engine's own missing-row answer).
        return deny("gone")
    if not policy.gated:
        return allow("ungated")
    if not host and not address:
        return deny("unnamed")
    return await covered_or_mode(app, policy, host, port, address)


async def posture(app, workspace_id: str) -> EgressPolicy | None:
    """The workspace's parsed egress posture, None when the row
    is gone."""
    row = await app.state.model.get_workspace(workspace_id)
    return EgressPolicy.from_row(row)


async def covered_or_mode(
    app, policy: EgressPolicy, host: str, port: int, address: str
) -> WebVerdict:
    """Past the posture split: durable and session coverage first
    (deny wins), then the static allowlist, then the mode tail."""
    engine = app.state.consent
    workspace_id = policy.workspace_id
    if await denied_verdict(app, engine, workspace_id, host, port):
        return deny("verdict")
    if allowlisted(policy, host, port, address):
        return allow("allowlist")
    if await allowed_verdict(app, engine, workspace_id, host, port):
        return allow("verdict")
    return await mode_tail(app, policy, host, port)


async def denied_verdict(
    app, engine, workspace_id: str, host: str, port: int
) -> bool:
    """Whether a standing deny covers the destination: the session
    table first, then the durable ``forever`` row."""
    if engine.session.deny_ttl(workspace_id, host, port) is not None:
        return True
    row = await app.state.model.egress_consent.forever_verdict_for(
        workspace_id, host
    )
    return row is not None and row["decision"] == DECISION_DENIED


async def allowed_verdict(
    app, engine, workspace_id: str, host: str, port: int
) -> bool:
    """Whether a standing allow covers the destination: the session
    table first, then the durable row — the only decision left at
    this point is an allow (the deny half answered before it)."""
    if engine.session.allow_ttl(workspace_id, host, port) is not None:
        return True
    return (
        await app.state.model.egress_consent.forever_verdict_for(
            workspace_id, host
        )
        is not None
    )


def allowlisted(
    policy: EgressPolicy, host: str, port: int, address: str
) -> bool:
    """The static half: name specs match the name the wire carried
    on this port; address specs (CIDRs and literals) match the
    original destination the redirect preserved."""
    return name_allowed(policy.host_specs, host, port) or address_allowed(
        policy.ip_specs, address, port
    )


def name_allowed(specs, host: str, port: int) -> bool:
    """Whether a name spec covers ``host`` on ``port``: a
    port-less spec covers every port; a port-scoped one covers
    its own."""
    ports = ports_for(host.lower(), specs)
    return covers(ports) and (ports is None or port in ports)


def address_allowed(specs, address: str, port: int) -> bool:
    """Whether an address spec (CIDR or literal) covers the
    original destination on ``port``."""
    return any(
        spec.port in (None, port) and spec.matches(address) for spec in specs
    )


async def mode_tail(
    app, policy: EgressPolicy, host: str, port: int
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
                DECISION_DENIED, workspace_id, host, port
            )
        return deny("static")
    future = await app.state.consent.hold(workspace_id, host, port)
    decided = await future
    return WebVerdict(
        decided.get("decision") == VERDICT_ALLOW,
        decided.get("reason", "decided"),
    )

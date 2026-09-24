"""The consent engine: holds, verdicts, decider fanout (#69).

Ported from klangk's ``consent/coordinator.py`` with the msks
enforcement seam swapped in: a held SYN is a kernel NFQUEUE hold on
the host's per-VM chain (not a sidecar relay), and verdict
enforcement (flow rules) is applied by the NFQUEUE consumer through
NetManager — this module owns only the *decision*: rows, timeouts,
session memory, and the frames deciders see.

The verdict lifecycle is klangk's — ``pending → allowed | denied |
expired | revoked``; a hold resolves through exactly one of: a
decider verdict (:meth:`resolve`), the timeout (deny, row
``expired``), or teardown (deny, fail-close). No hold is ever left
pending forever.

Session memory (name-keyed, TTL'd) mirrors klangk's
``SESSION_HOST_ALLOWS``/``_DENIES``: an allow verdict covers the
*name* for its duration (CDN-rotated IPs re-resolve and learn under
it; a rotated IP's SYN is auto-allowed at the queue), and a deny
covers the name the same way. ``forever`` verdicts additionally
persist as rows and replay at every attach.
"""

import asyncio
import contextlib
import logging
import time

from ..model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DECISION_PENDING,
    DECISION_REVOKED,
    DURATION_FOREVER,
    DURATION_ONCE,
    DURATION_SECONDS,
    DURATION_TILRESTART,
    EgressConsentModel,
    public_row,
)
from .specs import MODE_ALLOW, MODE_STATIC

logger = logging.getLogger(__name__)

# Verdicts the NFQUEUE consumer applies.
VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"

#: How long a ``once`` deny's fail-fast reject rule lives: enough to
#: catch the SYN's retransmit (one RTO), short enough that a new
#: connection to the same destination is not refused above the
#: queue (klangk's CONSENT_REJECT_TTL).
ONCE_REJECT_S = 10.0

#: The kernel's connect timeout is ~127 s (tcp_syn_retries); a hold
#: must answer inside it, and a session/flow rule standing in for
#: ``forever`` needs only outlive the table it dies with anyway.
LONG_TTL_S = 30 * 86400.0


def duration_ttl(duration: str) -> float | None:
    """Seconds a verdict's enforcement lives, or None for ``once``.

    ``tilrestart``/``forever`` map to a long TTL — the real boundary
    for the first is the per-VM table's deletion at stop, and for
    the second the row's replay at every attach."""
    if duration in DURATION_SECONDS:
        return float(DURATION_SECONDS[duration])
    if duration in (DURATION_TILRESTART, DURATION_FOREVER):
        return LONG_TTL_S
    return None


def grouped(rows: list[dict], decision: str) -> list[dict]:
    """The in-effect rows carrying one decision, hmac-stripped."""
    return [public_row(r) for r in rows if r["decision"] == decision]


def completed_verdict(verdict: dict) -> asyncio.Future:
    """A future already resolved to ``verdict`` (a no-hold answer)."""
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(verdict)
    return fut


def built_verdict(
    row: dict | None, decision: str, duration: str
) -> tuple[dict, str]:
    """``(verdict, resolved_label)`` for a decide step: a missing
    row fail-closes deny/expired; otherwise the decision maps to
    its wire verdict carrying the duration."""
    if row is None:
        return (
            {
                "decision": VERDICT_DENY,
                "reason": "gone",
                "duration": DURATION_ONCE,
            },
            DECISION_EXPIRED,
        )
    if decision == DECISION_ALLOWED:
        return (
            {
                "decision": VERDICT_ALLOW,
                "reason": "decided",
                "duration": duration,
            },
            DECISION_ALLOWED,
        )
    return (
        {
            "decision": VERDICT_DENY,
            "reason": "decided",
            "duration": duration,
        },
        DECISION_DENIED,
    )


def hold_owner(
    holds: dict, request_id: str
) -> tuple[str | None, asyncio.Task | None]:
    """``(workspace_id, timeout task)`` captured before a fail-close
    pops the hold — the pop happens inside fail_close, and the task
    must still be reaped after it. ``(None, None)`` when the hold
    vanished (a racing timeout won it)."""
    hold = holds.get(request_id)
    if hold is None:
        return None, None
    return hold["workspace_id"], hold["task"]


class SessionMemory:
    """Per-workspace name-keyed verdict memory (klangk's session
    allows/denies). Loop-only; expired entries stop matching on
    their TTL check and die wholesale with the workspace stop
    (``clear``) — the volume is one entry per consented
    destination, so lazy matching is the whole lifecycle."""

    def __init__(self) -> None:
        # (host, port-or-None) -> expire epoch, per workspace.
        self._allows: dict[str, dict[tuple[str, int | None], float]] = {}
        self._denies: dict[str, dict[tuple[str, int | None], float]] = {}

    @staticmethod
    def matches(
        entries: dict[tuple[str, int | None], float],
        host: str,
        port: int,
        now: float,
    ) -> float | None:
        """The max remaining TTL an entry covers ``host`` on
        ``port``: the host matches exactly (verdicts are exact
        hosts) and the entry's port matches or is all-ports."""
        remaining = [
            expire - now
            for (entry_host, entry_port), expire in entries.items()
            if expire > now
            and entry_host == host
            and entry_port in (None, port)
        ]
        return max(remaining, default=None)

    def allow_ttl(
        self, workspace_id: str, host: str, port: int
    ) -> float | None:
        return self.matches(
            self._allows.get(workspace_id, {}), host, port, time.time()
        )

    def deny_ttl(
        self, workspace_id: str, host: str, port: int
    ) -> float | None:
        return self.matches(
            self._denies.get(workspace_id, {}), host, port, time.time()
        )

    def min_allow_ttl(self, workspace_id: str, host: str) -> float | None:
        """The min remaining TTL across allows matching a name (the
        rule cap for DNS learns — klangk's #2465 cap: a learned rule
        must not outlive the verdict that justified it)."""
        now = time.time()
        remaining = [
            expire - now
            for (entry_host, _port), expire in self._allows.get(
                workspace_id, {}
            ).items()
            if expire > now and entry_host == host
        ]
        return min(remaining, default=None)

    def allow_ports(self, workspace_id: str, host: str) -> set[int] | None:
        """The ports session allows cover for a name: the union of
        matching entries' ports, or None when an all-ports entry
        matches. An empty union (nothing matches) is ``set()`` — the
        caller treats that as no session coverage."""
        now = time.time()
        ports: set[int] = set()
        for (entry_host, entry_port), expire in self._allows.get(
            workspace_id, {}
        ).items():
            if expire <= now or entry_host != host:
                continue
            if entry_port is None:
                return None
            ports.add(entry_port)
        return ports

    def remember(
        self,
        table: dict[str, dict[tuple[str, int | None], float]],
        workspace_id: str,
        host: str,
        port: int | None,
        ttl: float,
    ) -> None:
        """Add/refresh one entry; a re-verdict refreshes (max —
        never shortens an unexpired entry)."""
        entries = table.setdefault(workspace_id, {})
        key = (host, port)
        expire = time.time() + ttl
        entries[key] = max(entries.get(key, 0.0), expire)

    def allow(
        self, workspace_id: str, host: str, port: int | None, ttl: float
    ) -> None:
        self.remember(self._allows, workspace_id, host, port, ttl)

    def deny(
        self, workspace_id: str, host: str, port: int | None, ttl: float
    ) -> None:
        self.remember(self._denies, workspace_id, host, port, ttl)

    def forget(self, workspace_id: str, host: str) -> None:
        """Drop every entry naming ``host`` (revocation)."""
        for table in (self._allows, self._denies):
            entries = table.get(workspace_id)
            if entries is not None:
                table[workspace_id] = {
                    key: expire
                    for key, expire in entries.items()
                    if key[0] != host
                }

    def clear(self, workspace_id: str) -> None:
        """Drop the workspace entirely (stop/delete)."""
        self._allows.pop(workspace_id, None)
        self._denies.pop(workspace_id, None)


class ConsentEngine:
    """``app.state.consent``: in-process holds awaiting verdicts."""

    def __init__(self, app) -> None:
        self.app = app
        # request_id -> {"future", "workspace_id", "task"}
        self._holds: dict[str, dict] = {}
        # Strong refs for fire-and-forget publishes.
        self._publishes: set[asyncio.Task] = set()
        self.session = SessionMemory()

    # --- settings read live (SIGHUP propagates) ---------------------------

    @property
    def timeout_s(self) -> float:
        return self.app.state.settings.net.consent_timeout_s

    @property
    def rate_limit(self) -> int:
        return self.app.state.settings.net.consent_rate_limit

    @property
    def model(self) -> EgressConsentModel:
        return self.app.state.model.egress_consent

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Reap the previous run's orphaned pending rows: their
        in-memory holds died with that process, so no decider can
        resolve them — expire them so they neither replay to a
        fresh decider nor count against the pending cap."""
        reaped = await self.model.expire_all_pending()
        if reaped:
            logger.info("consent: expired %d orphaned pending row(s)", reaped)

    async def stop(self) -> None:
        """Fail-close every in-flight hold (daemon shutdown).

        The fail-close runs before the timeout task is reaped: a
        cancel landing inside the timeout's own fail_close (between
        its pop and its future resolve) would otherwise strand the
        future — pop-first here makes the timeout's arm a no-op."""
        for request_id in list(self._holds):
            _owner, task = hold_owner(self._holds, request_id)
            await self.fail_close(request_id, reason="shutdown")
            if task is not None:
                await self.cancel_hold_task(task)

    async def cancel_hold_task(self, task: asyncio.Task) -> None:
        """Cancel and reap a hold's timeout task: the cancel is
        delivered and the task's ending is retrieved here, so no
        cancelled task outlives the caller (and a bug inside the
        timeout path is logged, not lost)."""
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("consent: timeout task failed while reaping")

    async def fail_close_workspace(
        self, workspace_id: str, *, reason: str
    ) -> int:
        """Fail-close every hold one workspace still has (a stop,
        or a live mode switch away from interactive, #280): the
        rows expire, the held SYNs answer deny, and each hold's
        timeout task is cancelled and reaped. Returns how many
        holds closed. A hold that vanished mid-loop (a racing
        timeout won it) is not counted — it closed itself."""
        closed = 0
        for request_id in list(self._holds):
            owner, task = hold_owner(self._holds, request_id)
            if task is None or owner != workspace_id:
                continue  # vanished, or another workspace's hold
            await self.fail_close(request_id, reason=reason)
            await self.cancel_hold_task(task)
            closed += 1
        return closed

    async def on_workspace_stop(self, workspace_id: str) -> None:
        """Teardown for one workspace (VM stop/kill/delete): the
        flow rules died with its table, so session memory and
        ``tilrestart`` verdicts go with them, and any hold the
        workspace still has fail-closes."""
        await self.fail_close_workspace(workspace_id, reason="stopped")
        self.session.clear(workspace_id)
        await self.model.clear_tilrestart(workspace_id)

    # --- the hold gate -------------------------------------------------------

    async def hold(
        self, workspace_id: str, host: str, port: int
    ) -> asyncio.Future:
        """Gate one held SYN; return the verdict future.

        - ``allow`` mode: record the destination (audit) and allow
          at once — the default-permit posture.
        - ``static``: record a denial and deny at once (the chain
          normally never queues in static mode; this is the
          defense-in-depth answer if one arrives anyway).
        - ``interactive`` without a live decider: record a denial
          and deny at once — fail fast, no prompt, no hang (the
          kernel's own SYN retransmit timeout is the alternative).
        - the pending cap reached: deny at once (the prompt-spam
          bound; the hold is refused, not held).
        - a pending hold already exists for this destination: deny
          the duplicate.
        - otherwise: create the row, arm the timeout, fan out to
          deciders, and return the hold's future.
        """
        try:
            row = await self.app.state.model.get_workspace(workspace_id)
            if row is None:
                # The workspace vanished under the hold: fail
                # closed, not open — a missing row is not consent.
                return completed_verdict(
                    {"decision": VERDICT_DENY, "reason": "gone"}
                )
            return await self.mode_verdict(
                row.get("egress_mode") or MODE_ALLOW,
                workspace_id,
                host,
                port,
            )
        except Exception:
            # A model failure must not strand the held SYN: the
            # consumer awaits this future; answer deny.
            logger.exception("consent: hold failed; fail-closing to deny")
            return completed_verdict(
                {"decision": VERDICT_DENY, "reason": "error"}
            )

    async def mode_verdict(
        self, mode: str, workspace_id: str, host: str, port: int
    ) -> asyncio.Future:
        """The no-hold answers for ``allow``/``static`` and the
        hand-off to the interactive path (the docstring above owns
        the full decision table)."""
        if mode == MODE_ALLOW:
            await self.model.record_policy(
                DECISION_ALLOWED, workspace_id, host, port
            )
            return completed_verdict(
                {"decision": VERDICT_ALLOW, "reason": "allow_mode"}
            )
        if mode == MODE_STATIC:
            await self.model.record_policy(
                DECISION_DENIED, workspace_id, host, port
            )
            return completed_verdict(
                {"decision": VERDICT_DENY, "reason": "static"}
            )
        return await self.interactive_hold(workspace_id, host, port)

    async def interactive_hold(
        self, workspace_id: str, host: str, port: int
    ) -> asyncio.Future:
        """The interactive path: no decider means a fast static
        denial (no hold, no prompt, no hang — the kernel's SYN
        retransmit timer is the alternative), then the cap, the
        dedup, the hold."""
        if not self.app.state.deciders.has_decider(workspace_id):
            await self.model.record_policy(
                DECISION_DENIED, workspace_id, host, port
            )
            return completed_verdict(
                {"decision": VERDICT_DENY, "reason": "static"}
            )
        if self.rate_limit > 0 and (
            await self.model.count_pending(workspace_id) >= self.rate_limit
        ):
            return completed_verdict(
                {"decision": VERDICT_DENY, "reason": "rate_limited"}
            )
        request = await self.model.create_request(workspace_id, host, port)
        if request is None:
            return completed_verdict(
                {"decision": VERDICT_DENY, "reason": "duplicate"}
            )
        return self.register_hold(request)

    def register_hold(self, request: dict) -> asyncio.Future:
        """Register the hold, arm its timeout, fan out."""
        request_id = request["id"]
        fut = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self.timeout_hold(request_id))
        self._holds[request_id] = {
            "future": fut,
            "workspace_id": request["workspace_id"],
            "task": task,
        }
        frame = dict(public_row(request))
        frame["expires_at"] = request["requested_at"] + self.timeout_s
        self.publish(
            "egress.request",
            {
                "workspace_id": request["workspace_id"],
                "request": frame,
            },
        )
        logger.info(
            "consent hold: ws=%s %s:%s id=%s",
            request["workspace_id"][:8],
            request["dest_host"],
            request["dest_port"],
            request_id[:8],
        )
        return fut

    # --- verdicts -----------------------------------------------------------

    async def resolve(
        self,
        request_id: str,
        decision: str,
        decided_by: str,
        duration: str,
    ) -> dict | None:
        """Apply a decider's verdict to a held request.

        Writes the row, resolves the hold's future, records the
        session memory (before the future resolves, so a racing DNS
        query or SYN during the consumer's enforcement step already
        sees the verdict), and broadcasts. Returns the verdict dict,
        or None when the request is not currently held (already
        resolved, timed out, never held).

        Cancellation-safe: the pop is synchronous and the body is
        rescued — a cancellation landing mid-write fail-closes the
        popped hold instead of stranding its future (the consumer
        awaits it with no timeout of its own).
        """
        hold = self.pop_hold(request_id)
        if hold is None:
            return None
        try:
            hold["task"].cancel()
            row = await self.decide_or_none(
                request_id, decision, decided_by, duration
            )
            verdict, resolved = built_verdict(row, decision, duration)
            # The future resolves before the cancellable tail
            # (session memory, broadcasts): a cancellation landing
            # there keeps the committed verdict on the wire — the
            # row already says it — instead of fail-closing a
            # decision that landed.
            self.finish(hold, verdict)
            if row is None:
                # No committed decision backs this hold — the row
                # was not pending (a racing timeout won it) or the
                # decide write failed. Expire it now: with the hold
                # popped and its timeout cancelled, nothing else
                # would, and a pending row wedges the destination's
                # dedup slot until the startup reaper.
                with contextlib.suppress(Exception):
                    await self.model.expire_pending(request_id)
            else:
                await self.remember_verdict(
                    hold["workspace_id"],
                    row["dest_host"],
                    row["dest_port"],
                    decision,
                    duration,
                )
            self.publish(
                "egress.resolved",
                {
                    "workspace_id": hold["workspace_id"],
                    "request_id": request_id,
                    "decision": resolved,
                },
            )
            await self.broadcast_rules(hold["workspace_id"])
        except BaseException:
            # The hold is popped and its timeout cancelled; nothing
            # else would ever resolve the future. Fail-close now,
            # best-effort expire the row, and let the cancellation
            # propagate.
            self.fail_close_hold(request_id, hold, "error")
            with contextlib.suppress(Exception):
                await self.model.expire_pending(request_id)
            raise
        return verdict

    async def decide_or_none(
        self,
        request_id: str,
        decision: str,
        decided_by: str,
        duration: str,
    ) -> dict | None:
        """The decide write with its failure mapped to None (a
        failed write fail-closes through the gone-verdict below —
        the row stays pending only until the startup reaper)."""
        try:
            return await self.model.decide(
                request_id, decision, decided_by, duration
            )
        except Exception:
            logger.exception("consent: decide failed; fail-closing to deny")
            return None

    async def remember_verdict(
        self,
        workspace_id: str,
        host: str,
        port: int,
        decision: str,
        duration: str,
    ) -> None:
        """Record the session memory for a verdict (not ``once`` —
        a once verdict is per-connection and adds nothing; a
        portless flow (port 0) keys all-ports for the host)."""
        ttl = duration_ttl(duration)
        if ttl is None:
            return
        key_port = None if port == 0 else port
        if decision == DECISION_ALLOWED:
            self.session.allow(workspace_id, host, key_port, ttl)
        else:
            self.session.deny(workspace_id, host, key_port, ttl)

    def pop_hold(self, request_id: str) -> dict | None:
        """Pop a hold synchronously (no await between the pop and
        the timeout-task cancel — the timeout's wake guard depends
        on it)."""
        hold = self._holds.pop(request_id, None)
        if hold is None:
            return None
        hold["task"].cancel()
        return hold

    def finish(self, hold: dict, verdict: dict) -> None:
        """Resolve the hold's future (first answer wins)."""
        if not hold["future"].done():
            hold["future"].set_result(verdict)

    def fail_close_hold(
        self, request_id: str, hold: dict, reason: str
    ) -> None:
        """Resolve a popped hold deny + tell co-deciders to drop it.

        An already-done future (a racing verdict) is left untouched
        and un-broadcast — a second ``resolved`` frame after the
        real one would contradict the committed decision."""
        if hold["future"].done():
            return
        hold["future"].set_result(
            {
                "decision": VERDICT_DENY,
                "reason": reason,
                "duration": DURATION_ONCE,
            }
        )
        self.publish(
            "egress.resolved",
            {
                "workspace_id": hold["workspace_id"],
                "request_id": request_id,
                "decision": DECISION_EXPIRED,
            },
        )

    async def fail_close(self, request_id: str, *, reason: str) -> None:
        """Expire the row and resolve the hold deny (timeout or
        teardown). Pop-first: a racing resolve makes this a no-op.
        Does NOT cancel the hold's own task — the timeout task IS
        this path's caller on the timeout arm, and every other
        caller owns the cancel (klangk's ``_fail_close`` rule)."""
        hold = self._holds.pop(request_id, None)
        if hold is None:
            return
        try:
            await self.model.expire_pending(request_id)
        except Exception:
            logger.exception("consent: expiring %s failed", request_id[:8])
        self.fail_close_hold(request_id, hold, reason)

    async def timeout_hold(self, request_id: str) -> None:
        """Expire a hold after the timeout (fail-close on wake)."""
        try:
            await asyncio.sleep(self.timeout_s)
        except asyncio.CancelledError:
            return
        await self.fail_close(request_id, reason="timeout")

    # --- revocation ---------------------------------------------------------

    async def revoke(self, request_id: str, revoked_by: str) -> dict | None:
        """Revoke an in-effect verdict: flip the row, forget the
        session memory, and clear the enforcement (flow rules +
        tracked connections) for the verdict's destination. Returns
        the revoked row, or None when the row is not an in-effect
        verdict (or is already revoked — an idempotent success is
        signaled by the API layer's fresh read)."""
        row = await self.revocable_row(request_id)
        if row is None:
            return None
        updated = await self.model.revoke(request_id, revoked_by)
        if updated is None:
            # A concurrent revoke won the flip; the rule clear below
            # already happened under the winner — report success.
            return row
        self.session.forget(row["workspace_id"], row["dest_host"])
        await self.clear_enforcement(row)
        await self.broadcast_rules(row["workspace_id"])
        return updated

    async def revocable_row(self, request_id: str) -> dict | None:
        """The row a revoke may act on: an in-effect verdict. An
        already-revoked row returns as-is (the idempotent-success
        signal); anything else is None."""
        row = await self.model.get_request(request_id)
        if row is None:
            return None
        if row["decision"] == DECISION_REVOKED:
            return row
        if row["decision"] not in (DECISION_ALLOWED, DECISION_DENIED):
            return None
        return row

    async def clear_enforcement(self, row: dict) -> None:
        """Drop the flow rules and tracked connections a verdict
        installed (best-effort: a stopped workspace has no table to
        clear — its rules died with the table)."""
        net = self.app.state.net
        clear = getattr(net, "clear_consent_dest", None)
        if clear is None:
            return
        await clear(row["workspace_id"], row["dest_host"], row["dest_port"])

    # --- frames -------------------------------------------------------------

    def publish(self, event: str, data: dict) -> None:
        """Broadcast one event frame, fire-and-forget: the hub's
        delivery awaits nothing from this caller (the holds path
        must not block on a subscriber), and a failed send is
        logged, never raised into a verdict path."""
        hub = getattr(self.app.state, "hub", None)
        if hub is None:
            return
        task = asyncio.get_running_loop().create_task(hub.publish(event, data))
        self._publishes.add(task)

        def reaped(done: asyncio.Task) -> None:
            self._publishes.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("consent publish failed: %s", done.exception())

        task.add_done_callback(reaped)

    async def rules_frame(self, workspace_id: str) -> dict | None:
        """The rule-management view for a workspace: mode, static
        allowlist, and the in-effect verdicts (grouped)."""
        row = await self.app.state.model.get_workspace(workspace_id)
        if row is None:
            return None
        return await self.frame_for_row(row, workspace_id)

    async def frame_for_row(self, row: dict, workspace_id: str) -> dict:
        """The assembled frame off the fetched workspace row."""
        active = await self.model.list_active(workspace_id)
        return {
            "workspace_id": workspace_id,
            "mode": row.get("egress_mode") or MODE_ALLOW,
            "allow_list": list(row.get("egress_allowlist") or ()),
            "allowed": grouped(active, DECISION_ALLOWED),
            "denied": grouped(active, DECISION_DENIED),
        }

    async def broadcast_rules(self, workspace_id: str) -> None:
        """Push a refreshed rules frame (best-effort — a read
        failure must not break the verdict path that called this)."""
        try:
            frame = await self.rules_frame(workspace_id)
        except Exception:
            logger.exception("consent: rules refresh failed")
            return
        if frame is not None:
            self.publish("egress.rules", frame)

    async def snapshot(self, workspace_id: str) -> list[dict]:
        """Pending-request frames for a decider that just registered
        — only holds still live: a row pending in the table but
        already popped (a verdict in flight) must not replay as a
        fresh prompt."""
        rows = await self.model.list_requests(
            workspace_id, decision=DECISION_PENDING
        )
        frames = []
        for row in rows:
            if row["id"] not in self._holds:
                continue
            frame = dict(public_row(row))
            frame["expires_at"] = row["requested_at"] + self.timeout_s
            frames.append(
                {
                    "type": "egress.request",
                    "workspace_id": workspace_id,
                    "request": frame,
                }
            )
        return frames

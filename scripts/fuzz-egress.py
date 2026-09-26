#!/usr/bin/env python3
"""Interactive-egress consent fuzz harness (#286), ported from
klangk's ``smoketest_egress.py`` (#2392) onto the msks machinery.

Standalone, human-run, not in CI (no ``test_`` prefix). Invoke
through the devenv shim:

    devenv shell -- msks-fuzz-egress [--count N]

or directly (repo root, inside the devenv shell):

    python scripts/fuzz-egress.py [--count N]

Brings up a real msksd (or attaches to one with ``--url``), boots a
workspace in interactive egress mode with an allow-list, registers
a protocol-level consent decider on the events websocket (the same
wire the shipped ``msks egress watch`` decider speaks), then for N
fuzzed destinations: probes from inside the guest through the
console websocket, waits for the consent request when one is
expected, answers with a fuzzed verdict (allow/deny/timeout x
duration), and records BOTH the daemon-side outcome (request
resolved / expired / never surfaced) and the guest-side connection
result. It models verdict carryover across iterations, prints a
realtime expected-vs-actual line, and stops on the first hard
mismatch (``--continue`` defers to a summary). Genuinely
under-specified cases (raw IPs, edge-case host formatting) are
reported as findings, not halts.

A mismatch = a contradiction of a deterministic invariant: an
uncovered destination must produce a request; allow /
active-allow must release (exit 0); deny / active-deny must refuse
(nonzero); no-response must not succeed; a covered destination
must produce no request; audit rows must distinguish ``expired``
(consent timeout) from ``denied`` (a verdict).

Differences from klangk's harness, forced by the machinery:

- The probe runs in a microvm guest through the console websocket
  (``bash /dev/tcp`` under ``timeout``), not ``podman exec curl``;
  a refused connection and an NXDOMAIN both read as a fast nonzero,
  and the audit rows tell them apart.
- Verdicts travel over REST (``POST .../egress/requests/{id}``),
  the surface the shipped decider uses; a second verdict on a hold
  that already resolved answers 404 (first-decision-wins).
- The events websocket is the instance-wide channel (#8): every
  subscriber sees every workspace's ``egress.request`` frames. The
  decider-scope phase therefore scores AUTHORITY scoping (a
  cross-workspace decide must 404 and leave the hold pending),
  not frame visibility.
- Enforcement is name-keyed (the resolver's learned name->IP map).
  A controlled-DNS fixture (#289) resolves every probed hostname
  to one stable IP for the run, eliminating CDN address rotation
  as a source of non-determinism in the carryover model.
- msks ships one consent duration set (once/5m/15m/tilrestart/
  forever); the lifecycle phase exercises within/exceeding at the
  5m floor, which costs ~6 minutes per case (``--no-lifecycle``
  skips it).
- A second concurrent connection to a destination whose request
  is still pending is refused fast (the coordinator's duplicate
  gate): klangk's sidecar queued it under the same prompt.

Deferred phases (klangk features with no msks counterpart yet):

- rejected-domains pre-emptive deny (#2367) -- msks has no
  rejected-domains list.
- consent pause/resume (#2332/#2389) -- msks has no pause.
- mixed users / --as-member (#3112) -- msks auth is token-only;
  there is no second identity to act as.
- co-resident per-IP canaries (#2440) -- implemented (#292):
  two names on one address via controlled DNS (#289).

Requires: root (self-boot brings up taps and NFQUEUE), the built
guest assets (``msks-build-guest``), KVM, and a host whose FORWARD
chain the daemon's tables own. With ``--url`` the daemon must
already be running with those properties; set ``--consent-timeout``
to match its ``MSKSD_EGRESS_CONSENT_TIMEOUT_S`` so timeout waits
are sized right.

See https://github.com/mcdonc/msks/issues/286.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import random
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets
from msks.client import consoleauth
from msks.client.console import ws_url
from msks.consent.specs import EgressPolicy, ports_for
from msks.model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DURATION_5M,
    DURATION_FOREVER,
    DURATION_ONCE,
    DURATION_TILRESTART,
)
from msks.net import dnsmsg
from msks.net.dns import covers

# -- statuses and expectations ----------------------------------------------

PASS = "PASS"
MISMATCH = "MISMATCH"
FINDING = "FINDING"

EXPECT_RELEASED = "released"
EXPECT_REFUSED = "refused"
EXPECT_NOT0 = "not0"

#: The guest-side probe timeout: a held connection answers at the
#: consent timeout (deny + RST) or this clock kills bash (124) --
#: both land as "not success".
PROBE_TIMEOUT_S = 20.0

#: A once-deny / timeout / no-decider denial pins a fail-fast RST
#: for ~10s (ONCE_REJECT_S): inside that window a retry to the same
#: destination is refused above the queue with no request. The
#: model tracks the window so a fuzz recurrence inside it reads as
#: backstop coverage (soft), not a consent mismatch.
REJECT_BACKSTOP_S = 14.0

#: How long to wait for a request to surface after a probe leaves
#: the guest (DNS + SYN + hold + fanout, with margin).
REQUEST_WAIT_S = 25.0

#: The quiet window that must pass with no request before a
#: covered destination is declared prompt-free.
NO_REQUEST_WINDOW_S = 6.0


def canonical(host: str) -> str:
    """The model's key for a destination name."""
    return host.strip().lower().rstrip(".")


def settle_host(spec: str) -> str:
    """A resolvable hostname from an allow-list spec: strip port
    suffixes (``host:443`` → ``host``) and translate scope sigils
    into a name the spec actually covers so ``getent hosts`` can
    resolve through the daemon's forwarder.

    - ``*.gnu.org`` (subdomains only) → ``www.gnu.org`` (the apex
      is NOT covered, so a ``getent hosts gnu.org`` never resolves)
    - ``.debian.org`` (inclusive) → ``debian.org``
    - ``gnu.org`` (exact) → ``gnu.org``
    """
    host = spec.split(":")[0]
    if host.startswith("*."):
        return "www." + host[2:]
    host = host.lstrip(".")
    return host or ALLOW_LIST[0]


def carries(duration: str) -> bool:
    """Whether a verdict's duration covers later connections."""
    return duration != DURATION_ONCE


def exit_label(rc: int | None) -> str:
    """One guest-side probe result, as the row prints it."""
    if rc is None:
        return "no-exit"
    tag = {0: "OK", 1: "REFUSED", 6: "NORESOLVE", 124: "TIMEOUT"}.get(rc)
    return f"exit{rc}" + (f":{tag}" if tag else "")


CONN_TABLE: dict[tuple[str, int], tuple[str, str]] = {
    (EXPECT_RELEASED, 0): (PASS, ""),
    (EXPECT_RELEASED, 124): (FINDING, "expected release, probe hung"),
    (EXPECT_REFUSED, 0): (MISMATCH, "a deny let the connection through"),
    (EXPECT_REFUSED, 124): (FINDING, "expected refusal, probe hung"),
    (EXPECT_NOT0, 0): (MISMATCH, "a no-response verdict succeeded"),
}


def classify_conn(expect: str, rc: int | None) -> tuple[str, str]:
    """(status, detail) for one connection result against its
    expectation. A missing result (console wedge) is a finding, not
    a consent verdict."""
    if rc is None:
        return FINDING, "probe result never arrived (console wedge)"
    hit = CONN_TABLE.get((expect, rc))
    if hit is not None:
        return hit
    if expect == EXPECT_RELEASED and rc != 0:
        return MISMATCH, "an allow refused the connection"
    return PASS, ""


# -- fuzz corpus ------------------------------------------------------------
# Real, resolvable, :80-open destinations: an allowed probe reaches
# exit 0 deterministically and a denied one a fast nonzero. The
# allow-list seeds deb.debian.org; the rest are off-list. Raw IPs
# are exploratory (consent rows key them by the IP literal). The
# fuzz loop probes port 80 only; the port-scope phase owns :443.
ALLOW_LIST = ["deb.debian.org"]

_POOL = [
    ("deb.debian.org", "domain", False),  # on the allow-list -> covered
    ("example.com", "domain", False),
    ("cloudflare.com", "domain", False),
    ("github.com", "domain", False),
    ("1.1.1.1", "ip", True),  # raw IP -> exploratory
]
_EDGE_VARIANTS = [  # exploratory: canonicalization edges
    ("CLOUDFLARE.COM", "domain", True),
    ("cloudflare.com.", "domain", True),
    ("www.Google.com", "domain", True),
]
# Fresh off-list hosts for phases; disjoint from the pool and from
# each other so every phase starts from a clean consent state.
# Names the static/scope phases resolve never appear in the fuzz
# pool: those workspaces carry their own allow-lists and their own
# per-workspace resolver caches.
FRESH = {
    "multi_x": "stackoverflow.com",
    "multi_y": "reddit.com",
    "snap_a": "sqlite.org",
    "snap_b": "gnu.org",
    "failclosed": "en.wikipedia.org",
    "scope_b": "openjdk.org",
    "audit_e": "ietf.org",
    "audit_d": "python.org",
    "revoke": "openssl.org",
    "fanouts": [
        "rust-lang.org",
        "gnupg.org",
        "isc.org",
        "w3.org",
        "iana.org",
    ],
    "dedupe": "mozilla.org",
    "static_off": ["duckduckgo.com", "news.ycombinator.com"],
    "lifecycle_allow": "kernel.org",
    "lifecycle_deny": "debian.org",
    "restart_forever_allow": "cloudflare.com",
    "restart_forever_deny": "github.com",
    "restart_tilrestart": "wikipedia.org",
    "allow_off": "debian.org",
    "allow_off2": "mozilla.org",
    "scope_incl_sub": "www.debian.org",
    "scope_incl_apex": "debian.org",
    "scope_incl_off": "gnu.org",
    "scope_exact_on": "gnu.org",
    "scope_exact_off": "www.gnu.org",
    "scope_star_on": "www.gnu.org",
    "scope_star_off": "gnu.org",
    "port80_host": "ietf.org",
    "port443_host": "sqlite.org",
    "coresident_a": "httpbin.org",
    "coresident_b": "archive.org",
}
_FUZZ_DURATIONS = [
    DURATION_ONCE,
    DURATION_5M,
    "15m",
    DURATION_TILRESTART,
    DURATION_FOREVER,
]
_ACTIONS = ["allow", "deny", "none"]  # 'none' = no response -> timeout
_EDGE_META = {h: (k, e) for h, k, e in (*_POOL, *_EDGE_VARIANTS)}


@dataclass
class Verdict:
    """One in-effect verdict, as the model holds it."""

    decision: str
    duration: str
    decided_at: float


@dataclass
class Step:
    """One fuzz iteration's drawn plan."""

    idx: int
    host: str
    kind: str
    exploratory: bool
    action: str
    duration: str


def gen_plan(seed: int, count: int) -> list[Step]:
    """Deterministic fuzz plan, biased to repeat recent dests
    (carryover) and toward carrying durations."""
    rng = random.Random(seed)
    steps: list[Step] = []
    recent: list[str] = []
    for i in range(count):
        host, kind, expl = draw_dest(rng, recent, None)
        recent.append(host)
        recent = recent[-6:]
        steps.append(
            Step(
                i,
                host,
                kind,
                expl,
                rng.choice(_ACTIONS),
                rng.choice(_FUZZ_DURATIONS),
            )
        )
    return steps


def draw_dest(
    rng: random.Random, recent: list[str], meta: dict
) -> tuple[str, str, bool]:
    """One drawn destination: a fresh pool host, a recent repeat
    (the carryover bias), the raw IP, or an edge variant."""
    roll = rng.random()
    if roll < 0.75:
        return draw_pool_dest(rng, roll, recent)
    if roll < 0.85:
        return _POOL[-1]
    return rng.choice(_EDGE_VARIANTS)


def draw_pool_dest(
    rng: random.Random, roll: float, recent: list[str]
) -> tuple[str, str, bool]:
    """The pool half of the draw: fresh below 0.55, a recent
    repeat up to 0.75."""
    if roll < 0.55:
        return rng.choice([p for p in _POOL if not p[2]])
    host = rng.choice(recent) if recent else rng.choice(_POOL)[0]
    kind, expl = _EDGE_META.get(host, ("domain", False))
    return host, kind, expl


class EgressModel:
    """Mirror of the daemon's in-effect coverage so the expected
    outcome for iteration N folds in verdicts from iterations <N on
    recurring dests.

    The fuzz loop probes port 80 only, so verdict entries key by
    host alone (the daemon's session memory is (host, port)-keyed;
    a verdict on :80 never covers :443 and the loop never asks).
    ``restart`` folds a workspace stop: tilrestart verdicts and the
    reject backstop die with the per-VM table, forever verdicts
    survive (their rows replay at the next attach).
    """

    def __init__(self, allow_list: list[str]) -> None:
        self.policy = EgressPolicy("", "interactive", tuple(allow_list))
        self.verdicts: dict[str, Verdict] = {}
        self.reject_until: dict[str, float] = {}
        self.touched: set[str] = set()

    def active(self, host: str, now: float) -> Verdict | None:
        """The in-effect verdict entry, expiry-aware."""
        v = self.verdicts.get(host)
        if v is None:
            return None
        return v if now - v.decided_at <= duration_secs(v.duration) else None

    def covers(
        self, host: str, now: float
    ) -> tuple[bool, str | None, str | None]:
        """(covered, decision, source); source is 'allowlist',
        'verdict', or 'backstop'. The allow-list is a deterministic
        invariant (the resolver gate matches the name before any
        SYN can queue); verdict carryover is best-effort (it races
        with TTL lapses on the learned pins), so a caller
        hard-fails only on allow-list coverage."""
        if self.allowlisted(host):
            return True, DECISION_ALLOWED, "allowlist"
        if now < self.reject_until.get(host, 0.0):
            return True, DECISION_DENIED, "backstop"
        v = self.active(host, now)
        if v is not None:
            return True, v.decision, "verdict"
        return False, None, None

    def allowlisted(self, host: str) -> bool:
        """Whether an allow-list spec covers the name on :80 (the
        gate's own matcher, scope sigils included)."""
        return covers(ports_for(canonical(host), self.policy.host_specs))

    def expect_request(self, host: str, now: float) -> bool:
        return not self.covers(host, now)[0]

    def record(
        self, host: str, decision: str, duration: str, now: float
    ) -> None:
        """Fold one verdict (or denial) into the model."""
        self.touched.add(host)
        if carries(duration):
            self.verdicts[host] = Verdict(decision, duration, now)
        if decision == DECISION_DENIED:
            self.reject_until[host] = now + REJECT_BACKSTOP_S

    def forget(self, host: str) -> None:
        """A revoke: the destination gates again at the next SYN."""
        self.verdicts.pop(host, None)
        self.reject_until.pop(host, None)

    def restart(self) -> None:
        """A workspace stop/start: tilrestart verdicts and the RST
        backstop die with the per-VM table; forever survives."""
        self.verdicts = {
            h: v
            for h, v in self.verdicts.items()
            if v.duration == DURATION_FOREVER
        }
        self.reject_until.clear()


def duration_secs(duration: str) -> float:
    """The modeled lifetime of one duration (tilrestart and forever
    outlive the run; the restart phase folds their boundary)."""
    table = {DURATION_5M: 300.0, "15m": 900.0}
    return table.get(duration, 1e9)


# -- outcome names ----------------------------------------------------------
# A stable NAME per observable phenomenon, printed on each result
# line and tallied in the summary so a repeat in a later run maps
# straight to the explanation. Keyed on the result's detail prefix;
# the classify_conn mismatches carry no detail, so the fallback
# derives from the expectation. PASS rows get no name.
_DETAIL_PREFIX_NAMES: list[tuple[str, str]] = [
    ("expected a request, none arrived", "NO-EXPECTED-REQUEST"),
    ("expected a request; connection hung", "HUNG-PROBE"),
    ("expected a re-prompt, none arrived", "NO-EXPECTED-REQUEST"),
    ("expected no request (covered:", "CARRYOVER-SURPRISE"),
    ("an allow refused the connection", "ALLOW-REFUSED"),
    ("a deny let the connection through", "DENY-RELEASED"),
    ("a no-response verdict succeeded", "NORESPONSE-OK"),
    ("expected release, probe hung", "HUNG-PROBE"),
    ("expected refusal, probe hung", "HUNG-PROBE"),
    ("probe result never arrived", "HUNG-PROBE"),
    ("probe hung after disconnect", "HUNG-PROBE"),
    ("the late verdict was accepted", "FIRST-DECISION-VIOLATION"),
    ("late allow let it through", "FIRST-DECISION-VIOLATION"),
    ("a peer's hold resolved without a verdict", "FIRST-DECISION-VIOLATION"),
    ("not seen by BOTH deciders", "NO-EXPECTED-REQUEST"),
    ("the other decider never saw the resolve", "HUNG-PROBE"),
    ("still-held row missing from snapshot", "SNAPSHOT-REPLAY"),
    ("resolved-while-away row replayed", "SNAPSHOT-REPLAY"),
    ("concurrent distinct hosts did not", "FANOUT-SERIALIZED"),
    ("a second prompt arrived", "RETRANSMIT-DUPLICATED"),
    ("both connections released under one once verdict", "ONCE-CROSS-CONN"),
    ("no re-prompt after revoke", "NO-REPROMPT-REVOKE"),
    ("succeeded after the decider disconnected", "FAILCLOSED-LEAK"),
    ("resolved as something else", "AUDIT-MISLABELED"),
    ("cross-workspace decide was accepted", "ISOLATION-BROKEN"),
    ("the foreign hold resolved", "ISOLATION-BROKEN"),
    ("timeout audited as", "AUDIT-MISLABELED"),
    ("human deny audited as", "AUDIT-MISLABELED"),
    ("decided_by=", "AUDIT-MISATTRIBUTED"),
    ("policy row carries decided_by", "AUDIT-MISATTRIBUTED"),
    ("no policy-allow row recorded", "AUDIT-MISSING"),
    ("off-list succeeded with no decider", "STATIC-LEAK"),
    ("off-list hung with no decider", "STATIC-HELD"),
    ("allow-list host should connect", "ALLOW-REFUSED"),
    ("static denial left no audit row", "AUDIT-MISSING"),
    ("allow-mode off-list was refused", "ALLOWMODE-REFUSED"),
    ("a request surfaced in allow mode", "ALLOWMODE-REQUEST"),
    ("forever verdict did not survive", "RESTART-FOREVER-NOT-DURABLE"),
    ("forever deny leaked after restart", "RESTART-FOREVER-NOT-DURABLE"),
    ("tilrestart verdict survived restart", "RESTART-TILRESTART-SURVIVED"),
    ("host scope: ", "HOST-SCOPE-VIOLATION"),
    ("port scope: ", "PORT-SCOPE-VIOLATION"),
    ("co-resident allow leaked", "CORESIDENT-LEAK"),
    ("co-resident deny leaked", "CORESIDENT-LEAK"),
    ("phase bring-up failed", "UNEXPECTED-ERROR"),
    ("step raised", "UNEXPECTED-ERROR"),
]

OUTCOME_NAMES: dict[str, str] = {
    "NO-EXPECTED-REQUEST": (
        "an expected consent request never arrived (off-list / post-expiry "
        "/ post-revoke); fail-closed or hung."
    ),
    "CARRYOVER-SURPRISE": (
        "an in-effect verdict did not cover a retry (TTL/per-IP rotation); "
        "scored a FINDING, matching klangk #2399/#2419."
    ),
    "ALLOW-REFUSED": "an allow / allow-list / active-allow was refused.",
    "DENY-RELEASED": "a deny / active-deny let the connection through.",
    "NORESPONSE-OK": "a no-response (timeout) verdict succeeded.",
    "HUNG-PROBE": (
        "the probe hung or its result never arrived (console/NFQUEUE/DNS), "
        "not a consent-semantics failure."
    ),
    "FIRST-DECISION-VIOLATION": (
        "first-decision-wins broken (a late or second verdict applied)."
    ),
    "SNAPSHOT-REPLAY": (
        "a reconnect snapshot replayed a resolved row or dropped a held one."
    ),
    "FANOUT-SERIALIZED": (
        "N concurrent off-list connects did not all surface requests "
        "(the NFQUEUE consumer serialized them)."
    ),
    "RETRANSMIT-DUPLICATED": (
        "a held destination produced a second prompt (dedup broken)."
    ),
    "NO-REPROMPT-REVOKE": (
        "no re-prompt after a revoke (the enforcement stayed)."
    ),
    "FAILCLOSED-LEAK": (
        "a connection succeeded after the decider disconnected (the "
        "fail-closed guarantee broke)."
    ),
    "ISOLATION-BROKEN": (
        "cross-workspace authority: a decide for another workspace's hold "
        "was accepted or resolved it."
    ),
    "AUDIT-MISLABELED": (
        "expired/denied audit distinction broken (a timeout audited as a "
        "deny, or vice-versa)."
    ),
    "AUDIT-MISATTRIBUTED": (
        "a verdict row's decided_by names no one (a human verdict) or "
        "names a human (a policy row)."
    ),
    "AUDIT-MISSING": (
        "a static denial recorded no audit row (the resolver gate's "
        "policy write is missing)."
    ),
    "STATIC-LEAK": "off-list succeeded with no decider registered.",
    "STATIC-HELD": "off-list hung with no decider (not a clean denial).",
    "ALLOWMODE-REFUSED": (
        "an allow-mode (default-permit) off-list host was refused."
    ),
    "ALLOWMODE-REQUEST": (
        "a consent request surfaced in allow mode (the default-permit "
        "posture must hold nothing)."
    ),
    "ONCE-CROSS-CONN": (
        "a once verdict released a separate connection (the "
        "per-connection verdict leaked across connections)."
    ),
    "RESTART-FOREVER-NOT-DURABLE": (
        "a forever verdict did not survive a workspace stop/start "
        "(allow must reconnect, deny must re-deny)."
    ),
    "RESTART-TILRESTART-SURVIVED": (
        "a tilrestart verdict survived a workspace stop/start; it must "
        "be reaped with the per-VM table."
    ),
    "HOST-SCOPE-VIOLATION": (
        "an nginx-style host scope (exact/inclusive/subdomains) let the "
        "wrong name through or blocked the right one."
    ),
    "PORT-SCOPE-VIOLATION": (
        "a port-scoped allow permitted a different port, or refused its own."
    ),
    "CORESIDENT-LEAK": (
        "a verdict on one hostname leaked to a co-resident hostname "
        "(same IP, different name): consent is IP-scoped, not name-scoped."
    ),
    "UNEXPECTED-ERROR": "a phase bring-up or per-step exception.",
}

_EXPECT_CONN_NAMES = {
    EXPECT_RELEASED: "ALLOW-REFUSED",
    EXPECT_REFUSED: "DENY-RELEASED",
    EXPECT_NOT0: "NORESPONSE-OK",
}

assert all(name in OUTCOME_NAMES for _, name in _DETAIL_PREFIX_NAMES), (
    "unregistered outcome name in _DETAIL_PREFIX_NAMES"
)
assert all(name in OUTCOME_NAMES for name in _EXPECT_CONN_NAMES.values()), (
    "unregistered outcome name in _EXPECT_CONN_NAMES"
)


def outcome_name(res: Result) -> str:
    """The stable outcome name for a result ("" for PASS); an
    unmapped detail returns the loud "??" sentinel."""
    if res.status == PASS:
        return ""
    prefix_hit = prefix_outcome(res.detail or "")
    if prefix_hit:
        return prefix_hit
    if res.detail:
        return "??"
    return fallback_outcome(res)


def prefix_outcome(detail: str) -> str:
    """The first registered prefix the detail matches, if any."""
    for prefix, name in _DETAIL_PREFIX_NAMES:
        if detail.startswith(prefix):
            return name
    return ""


def fallback_outcome(res: Result) -> str:
    """A detail-less row derives from its expectation."""
    if res.status == MISMATCH:
        return _EXPECT_CONN_NAMES.get(res.expect_conn, "??")
    return "HUNG-PROBE"


@dataclass
class Result:
    """One scored observation (a fuzz step or a phase probe)."""

    label: str
    host: str
    expect_request: bool
    expect_conn: str
    action: str
    server: str
    rc: int | None
    status: str
    detail: str = ""
    outcome: str = ""

    @property
    def mark(self) -> str:
        return {PASS: "ok", MISMATCH: "XX MISMATCH", FINDING: "? finding"}[
            self.status
        ]


@dataclass
class Summary:
    """The run's tally."""

    total: int = 0
    passed: int = 0
    findings: int = 0
    mismatches: int = 0
    rows: list[Result] = field(default_factory=list)


def snapshot_outcome(has_a: bool, has_b: bool) -> tuple[str, str]:
    """Score the reconnect snapshot: the held row replays, the
    resolved-while-away row does not."""
    if has_b and not has_a:
        return PASS, "snapshot has the still-held B, not the resolved A"
    if has_b and has_a:
        return MISMATCH, "resolved-while-away row replayed"
    return MISMATCH, "still-held row missing from snapshot"


def fail_closed_outcome(rc: int | None) -> tuple[str, str]:
    """Score the disconnect-mid-hold observation."""
    if rc == 0:
        return MISMATCH, (
            "connection succeeded after the decider disconnected "
            "(silent allow -- fail-closed violation)"
        )
    if rc is None:
        return FINDING, "probe hung after disconnect (not clean fail-closed)"
    return PASS, f"connection failed after disconnect (exit {rc})"


def fanout_release_outcome(
    rc0: int | None, others_held: bool
) -> tuple[str, str]:
    """Score the once-allow release among held peers."""
    if rc0 != 0:
        status, detail = classify_conn(EXPECT_RELEASED, rc0)
        return status, detail or "the once-allowed connection"
    if not others_held:
        return MISMATCH, "a peer's hold resolved without a verdict"
    return PASS, "own connection released; peers stayed held"


def dedupe_outcome(
    count: int, released: list[bool], results: dict, tags: list[str]
) -> tuple[str, str]:
    """Score the same-host concurrent case: exactly one prompt, at
    most one release (the duplicate is refused while the first is
    pending), no second prompt over the retransmit window."""
    if count != 1:
        return MISMATCH, f"a second prompt arrived ({count} rows)"
    if all(released):
        return MISMATCH, "both connections released under one once verdict"
    if all(results.get(t) in (None, 124) for t in tags):
        return FINDING, "probe result never arrived (duplicate connection)"
    return PASS, "one prompt; the duplicate refused fast"


# -- daemon bring-up --------------------------------------------------------


def free_port() -> int:
    """An unused TCP port for the self-booted daemon."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def default_route_iface() -> str:
    """The default route's device (the egress uplink) — parsed the
    way the smoke suite parses it: the token after ``dev``."""
    out = subprocess.run(
        ["ip", "-4", "route", "show", "default"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    parts = out.split()
    for i, part in enumerate(parts):
        if part == "dev" and i + 1 < len(parts):
            return parts[i + 1]
    raise SystemExit(f"no default route to NAT behind: {out!r}")


def guest_archive() -> Path:
    """The newest built workspace image."""
    guest_dir = Path(
        os.environ.get("GUEST_DIR", ".devenv/state/guest")
    ).resolve()
    archives = sorted(guest_dir.glob("workspace-*.tar"))
    if not archives:
        raise SystemExit(
            f"no workspace-*.tar under {guest_dir} -- run msks-build-guest "
            "first"
        )
    return archives[-1]


# -- host-process hygiene (#307) --------------------------------------------
#
# The daemon's SIGTERM shutdown stops nft tables and taps but not
# the VMMs: a workspace's cloud-hypervisor runs in its own session
# (#141) so daemon death must not kill it -- only the API path
# (stop + delete) retires one, and only while msksd lives.  A
# teardown that dies partway, or a fuzzer crash, strands VMM
# processes on the host.  The helpers below find and reap them.

VMM_BASENAME = "cloud-hypervisor"
DAEMON_PID = "daemon.pid"
IP_FORWARD_WAS = "ip_forward.was"
REAP_SENTINEL = "reap.done"
REAP_POLL_S = 1.0


def pid_alive(pid: int) -> bool:
    """Whether /proc still lists the pid (zombies included)."""
    return Path(f"/proc/{pid}").exists()


def alive_pids(pids: list[int]) -> list[int]:
    """Which of the scanned pids still live."""
    alive = []
    for pid in pids:
        if pid_alive(pid):
            alive.append(pid)
    return alive


def read_cmdline(pid: int) -> list[bytes]:
    """A /proc cmdline's non-empty NUL-separated fields."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [f for f in raw.split(b"\0") if f]


def proc_pids() -> list[int]:
    """Every numeric /proc entry (the live pids)."""
    return [
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit()
    ]


def socket_of(fields: list[bytes]) -> bytes | None:
    """The --api-socket value, in either = or separate-field
    form."""
    for index, item in enumerate(fields):
        if item == b"--api-socket" and index + 1 < len(fields):
            return fields[index + 1]
        if item.startswith(b"--api-socket="):
            return item.split(b"=", 1)[1]
    return None


def is_vmm_field(field: bytes) -> bool:
    """Whether one argv field names the cloud-hypervisor binary
    (a path or a bare name, exact basename match)."""
    return os.path.basename(os.fsdecode(field)) == VMM_BASENAME


def has_vmm_field(fields: list[bytes]) -> bool:
    """Whether any argv field names the cloud-hypervisor binary."""
    for item in fields:
        if is_vmm_field(item):
            return True
    return False


def owned_vmm(fields: list[bytes], root, ws_ids) -> bool:
    """Whether a /proc cmdline is a VMM this run owns: the binary
    is cloud-hypervisor and its --api-socket lives under our state
    dir (self-boot) or in a workspace directory we created
    (attach mode, where the daemon's state dir is the
    operator's)."""
    sock = socket_of(fields)
    if sock is None:
        return False
    if not has_vmm_field(fields):
        return False
    path = Path(os.fsdecode(sock))
    under_root = root is not None and root in path.parents
    return under_root or path.parent.name in ws_ids


def scan_vmm_pids(root, ws_ids) -> list[int]:
    """Every live process that reads as an owned VMM."""
    owned = []
    for pid in proc_pids():
        fields = read_cmdline(pid)
        if fields and owned_vmm(fields, root, ws_ids):
            owned.append(pid)
    return owned


def kill_verified(pid: int, sig: int, root, ws_ids) -> bool:
    """Signal one owned VMM.  The cmdline is re-read first, so a
    pid recycled between scan and kill is never signaled."""
    if not owned_vmm(read_cmdline(pid), root, ws_ids):
        return False
    with contextlib.suppress(OSError):
        os.kill(pid, sig)
    return True


def read_pidfile(path: Path) -> int | None:
    """The pid in a pidfile, or None when absent or unparsable."""
    try:
        return int(path.read_text().strip())
    except OSError, ValueError:
        return None


def cmdline_is_msksd(fields: list[bytes]) -> bool:
    """Whether a /proc cmdline is the msksd we booted -- the exact
    msksd binary name or the -m module field (the pidfile pid is
    verified before any signal, so a recycled pid reads as not
    ours)."""
    if any(os.path.basename(os.fsdecode(item)) == "msksd" for item in fields):
        return True
    return b"msks.server.main" in fields


def term_then_kill(pid: int) -> None:
    """SIGTERM the daemon, wait out its graceful window, then
    SIGKILL.  The cmdline is re-read every poll, so an exited or
    recycled pid ends the wait instead of taking the kill."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 90.0
    while cmdline_is_msksd(read_cmdline(pid)):
        if time.monotonic() > deadline:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            return
        time.sleep(REAP_POLL_S)


def restore_forwarding_file(path: Path) -> None:
    """Put ip_forward back where boot found it (the crash path's
    copy of Daemon.restore_forwarding)."""
    try:
        saved = path.read_text()
    except OSError:
        return
    with contextlib.suppress(OSError):
        Path("/proc/sys/net/ipv4/ip_forward").write_text(saved)


def reap_active(fuzzer_pid: int, state_dir: Path) -> bool:
    """Whether the watchdog must keep watching: the fuzzer lives,
    the state dir exists, and no sentinel says teardown ran."""
    if not pid_alive(fuzzer_pid) or not state_dir.exists():
        return False
    return not (state_dir / REAP_SENTINEL).exists()


def reap_daemon_stack(state_dir: Path) -> None:
    """The crash-path cleanup: the daemon (graceful window first),
    its state dir's VMMs, then ip_forward."""
    daemon_pid = read_pidfile(state_dir / DAEMON_PID)
    if daemon_pid is not None and cmdline_is_msksd(read_cmdline(daemon_pid)):
        term_then_kill(daemon_pid)
    for pid in scan_vmm_pids(state_dir, set()):
        kill_verified(pid, signal.SIGKILL, state_dir, set())
    restore_forwarding_file(state_dir / IP_FORWARD_WAS)


def run_reaper(fuzzer_pid: int, state_dir: Path) -> int:
    """Watchdog for the crash path (#307): runs detached (own
    session, null stdio), polls the fuzzer, and on its death
    reaps the stack it booted.  Stands down without touching
    anything when the sentinel or a vanished state dir says the
    teardown completed."""
    while reap_active(fuzzer_pid, state_dir):
        time.sleep(REAP_POLL_S)
    if pid_alive(fuzzer_pid):
        return 0  # stood down: the fuzzer's teardown completed
    reap_daemon_stack(state_dir)
    return 0


def reap_result(label: str, killed: list[int], alive: list[int]) -> Result:
    """The row for a sweep that had to kill VMMs by hand: a FINDING
    when the kills land (the run cleaned up after itself), a
    MISMATCH when processes survive SIGKILL."""
    pid_list = ", ".join(str(pid) for pid in killed)
    if alive:
        return Result(
            label,
            "(host)",
            True,
            "",
            "?",
            "host",
            None,
            MISMATCH,
            detail=f"{len(alive)} VMM(s) survived SIGKILL: {pid_list}",
        )
    return Result(
        label,
        "(host)",
        True,
        "",
        "?",
        "host",
        None,
        FINDING,
        detail=f"reaped {len(killed)} leftover VMM(s): {pid_list}",
    )


class Daemon:
    """A self-booted msksd (the daemon-e2e smoke's shape) or an
    attached one (``--url``)."""

    def __init__(self, args) -> None:
        self.args = args
        self.proc: subprocess.Popen | None = None
        self.state_dir: Path | None = None
        self.forwarding_was: str | None = None
        self.out_file = None
        self.err_file = None
        self.url = args.url
        self.token = args.token
        self.cafile = args.cafile

    def log_tail(self, limit: int = 40) -> str:
        """The daemon's captured output -- the failure evidence a
        self-booted run owns."""
        if self.state_dir is None:
            return ""
        parts = []
        for name in ("daemon.out", "daemon.err"):
            path = self.state_dir / name
            if path.exists():
                lines = path.read_text(errors="replace").splitlines()
                parts.append(f"--- {name} (last {limit}) ---")
                parts.extend(lines[-limit:])
        return "\n".join(parts)

    def boot(self, dns_upstream: str | None = None) -> None:
        """Start msksd with its own state dir, token, and TLS.
        When *dns_upstream* is given (``host:port`` or just ``host``),
        ``MSKSD_EGRESS_DNS_UPSTREAM`` is set so the daemon's forwarder
        relays through the controlled DNS server."""
        check_root()
        self.state_dir = Path(f"/tmp/msks-fuzz-{uuid.uuid4().hex[:8]}")
        self.state_dir.mkdir(parents=True)
        self.token = (
            base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=\n")
        )
        (self.state_dir / "bootstrap-token").write_text(self.token + "\n")
        port = free_port()
        self.url = f"https://127.0.0.1:{port}"
        env = dict(os.environ)
        env.update(
            MSKSD_STATE_DIR=str(self.state_dir),
            MSKSD_BOOTSTRAP_TOKEN=self.token,
            MSKSD_HOST="127.0.0.1",
            MSKSD_PORT=str(port),
            MSKSD_EGRESS_ENABLED="true",
            MSKSD_EGRESS_UPLINK=default_route_iface(),
            MSKSD_DEFAULT_IMAGE=str(guest_archive()),
            MSKSD_EGRESS_CONSENT_TIMEOUT_S=str(self.args.consent_timeout),
        )
        if dns_upstream is not None:
            env["MSKSD_EGRESS_DNS_UPSTREAM"] = dns_upstream
        msksd = shutil.which("msksd")
        command = (
            [msksd, "--config=none"]
            if msksd
            else [sys.executable, "-m", "msks.server.main", "--config=none"]
        )
        self.out_file = open(self.state_dir / "daemon.out", "wb")  # noqa: SIM115
        self.err_file = open(self.state_dir / "daemon.err", "wb")  # noqa: SIM115
        self.proc = subprocess.Popen(
            command, env=env, stdout=self.out_file, stderr=self.err_file
        )
        # The crash-path watchdog's inputs (#307): the daemon's pid
        # and the pre-boot ip_forward value, both written here,
        # before anything can strand a process on the host.
        (self.state_dir / DAEMON_PID).write_text(f"{self.proc.pid}\n")
        self.forwarding_was = Path("/proc/sys/net/ipv4/ip_forward").read_text()
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
        (self.state_dir / IP_FORWARD_WAS).write_text(self.forwarding_was)

    async def wait_ready(self) -> None:
        """The CA file, then /health, then the client context."""
        if self.proc is not None:
            await self.wait_file(
                self.state_dir / "msks-ca.pem", "write its CA"
            )
        if self.cafile is None:
            self.cafile = str(self.state_dir / "msks-ca.pem")
        client = self.client()
        await self.wait_health(client)
        await client.aclose()

    async def wait_file(self, path: Path, doing: str) -> None:
        """Wait for a startup artifact, failing loudly if the
        daemon died trying."""
        deadline = time.monotonic() + 180.0
        while not path.exists():
            self.check_died(doing)
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"msksd did not {doing} within 180s\n{self.log_tail()}"
                )
            await asyncio.sleep(0.25)

    def check_died(self, doing: str) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            raise SystemExit(
                f"msksd exited ({self.proc.returncode}) before it could "
                f"{doing}\n{self.log_tail()}"
            )

    def client(self) -> httpx.AsyncClient:
        """One authenticated REST client against the daemon."""
        return httpx.AsyncClient(
            base_url=self.url,
            verify=ssl.create_default_context(cafile=self.cafile),
            timeout=httpx.Timeout(
                connect=10.0, read=300.0, write=10.0, pool=10.0
            ),
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def wait_health(self, client: httpx.AsyncClient) -> None:
        """Poll /health until the daemon answers."""
        deadline = time.monotonic() + 180.0
        while True:
            self.check_died("serve /health")
            try:
                code = (await client.get("/api/v1/health")).status_code
            except httpx.HTTPError, ssl.SSLError:
                code = None
            if code == 200:
                return
            if time.monotonic() > deadline:
                raise SystemExit(
                    "msksd did not serve /health within 180s\n"
                    + self.log_tail()
                )
            await asyncio.sleep(0.5)

    async def stop(self) -> str | None:
        """Terminate the daemon, restore forwarding, drop the state.

        Returns a problem string instead of raising (#307): the
        teardown's other steps must run even when the daemon
        misbehaves, and a second call is a no-op (terminate nulls
        self.proc)."""
        problem = None
        if self.proc is not None:
            problem = await self.terminate()
        self.restore_forwarding()
        self.cleanup_files()
        return problem

    def restore_forwarding(self) -> None:
        """Put ip_forward back where boot found it (idempotent)."""
        if self.forwarding_was is None:
            return
        with contextlib.suppress(OSError):
            Path("/proc/sys/net/ipv4/ip_forward").write_text(
                self.forwarding_was
            )
        self.forwarding_was = None

    def cleanup_files(self) -> None:
        """Close the captured output and drop the state dir."""
        for handle in (self.out_file, self.err_file):
            if handle is not None:
                handle.close()
        if self.state_dir is not None:
            shutil.rmtree(self.state_dir, ignore_errors=True)

    async def terminate(self) -> str | None:
        """SIGTERM -> exit, with the graceful-path check.  A daemon
        that will not exit is SIGKILLed and the problem returned,
        not raised, so teardown continues (#307)."""
        proc, self.proc = self.proc, None
        proc.terminate()
        try:
            rc = await asyncio.to_thread(proc.wait, 90)
        except subprocess.TimeoutExpired:
            proc.kill()
            return (
                "msksd did not exit on SIGTERM within 90s\n" + self.log_tail()
            )
        if rc not in (0, -signal.SIGTERM):
            return (
                f"msksd exited {rc} on SIGTERM (not the graceful path)\n"
                + self.log_tail()
            )
        return None


def check_root() -> None:
    """Self-boot needs the caps the egress stack uses."""
    if os.geteuid() != 0:
        raise SystemExit(
            "self-boot needs root (taps, NFQUEUE, ip_forward); use "
            "--url/--token/--cafile to attach to a running daemon"
        )


# -- controlled DNS (#289) --------------------------------------------------
# A lightweight UDP DNS server that resolves every harness hostname
# to a single frozen IP for the run, eliminating CDN address
# rotation as a source of non-determinism.  The daemon's forwarder
# relays through this server (MSKSD_EGRESS_DNS_UPSTREAM), so the
# guest's view of every probed name is stable.


DNS_CONTROLLED_TTL = 3600
DNS_CONTROLLED_ADDR = "127.0.0.54"


def fresh_hostnames() -> list[str]:
    """Every hostname in the FRESH map, flattened."""
    hosts: list[str] = []
    for val in FRESH.values():
        if isinstance(val, list):
            hosts.extend(val)
        else:
            hosts.append(val)
    return hosts


def all_harness_hostnames() -> list[str]:
    """Every hostname the harness probes (pool + edges + FRESH),
    deduplicated and lowered.  Raw IPs are excluded — they bypass
    DNS."""
    names: set[str] = set()
    for host, kind, _expl in (*_POOL, *_EDGE_VARIANTS):
        if kind == "domain":
            names.add(canonical(host))
    names.update(canonical(h) for h in fresh_hostnames())
    names.update(canonical(settle_host(s)) for s in ALLOW_LIST)
    return sorted(names)


def resolve_hostnames(hostnames: list[str]) -> dict[str, str]:
    """Resolve each hostname once via the system resolver and return
    a frozen ``{name: ipv4}`` map.  A name that fails to resolve is
    a hard error — the harness cannot score probes against it."""
    mapping: dict[str, str] = {}
    for host in hostnames:
        try:
            results = socket.getaddrinfo(host, 80, socket.AF_INET)
        except socket.gaierror as exc:
            raise SystemExit(
                f"controlled DNS: cannot resolve {host!r}: {exc}"
            ) from exc
        if not results:
            raise SystemExit(f"controlled DNS: no A record for {host!r}")
        mapping[host] = results[0][4][0]
    return mapping


def encode_dns_name(name: str) -> bytes:
    """Encode a domain name as DNS wire format labels."""
    parts = []
    for label in name.split("."):
        encoded = label.encode("ascii")
        parts.append(bytes([len(encoded)]) + encoded)
    parts.append(b"\x00")
    return b"".join(parts)


def a_response(query: bytes, ip: str) -> bytes:
    """Build an A-record response for *query* returning *ip* with
    the controlled TTL."""
    question = dnsmsg.parse_query(query)
    if question is None:
        return dnsmsg.nxdomain_for(query)
    addr = socket.inet_aton(ip)
    # Header: QR + RD + RA, RCODE 0, 1 question, 1 answer.
    header = struct.pack(
        "!HHHHHH",
        question.id,
        0x8180,
        1,
        1,
        0,
        0,
    )
    # The question section is the query's own question verbatim.
    qsection = question.wire[dnsmsg.HEADER_LEN :]
    # Answer: owner name (full labels), type A, class IN, TTL,
    # rdlength 4, rdata.
    answer = struct.pack(
        "!HHIH",
        1,
        1,
        DNS_CONTROLLED_TTL,
        4,
    )
    name_wire = encode_dns_name(question.name)
    return header + qsection + name_wire + answer + addr


class ControlledDNS:
    """A UDP DNS server serving frozen A records for harness
    hostnames.  Queries for names not in the map are forwarded to
    the real upstream resolver so non-harness DNS still works."""

    def __init__(
        self,
        mapping: dict[str, str],
        upstream: tuple[str, int],
        bind: str = DNS_CONTROLLED_ADDR,
    ) -> None:
        self.mapping = mapping
        self.upstream = upstream
        self.bind = bind
        self.port = 53
        self.sock: socket.socket | None = None

    def start(self) -> None:
        """Bind the UDP socket."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.bind, self.port))
        except OSError as exc:
            raise SystemExit(
                f"controlled DNS: cannot bind {self.bind}:{self.port}: {exc}"
            ) from exc
        self.sock.setblocking(False)

    async def serve(self) -> None:
        """Serve queries until cancelled or the socket closes."""
        loop = asyncio.get_running_loop()
        while True:
            data, addr = await loop.sock_recvfrom(self.sock, 65535)
            try:
                reply = await self._handle(data)
            except Exception:  # noqa: BLE001
                continue
            if reply is not None:
                await loop.sock_sendto(self.sock, reply, addr)

    async def _handle(self, query: bytes) -> bytes | None:
        """Resolve from the map or forward to upstream."""
        question = dnsmsg.parse_query(query)
        if question is None:
            return None
        name = question.name.lower().rstrip(".")
        ip = self.mapping.get(name)
        if ip is not None and question.qtype == 1:  # A record
            return a_response(query, ip)
        return await self._forward(query)

    async def _forward(self, query: bytes) -> bytes | None:
        """Relay to the real upstream and return its answer."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            await loop.sock_sendto(sock, query, self.upstream)
            # 2s timeout for upstream response.
            data = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 65535),
                2.0,
            )
            return data[0]
        except TimeoutError, OSError:
            return None
        finally:
            sock.close()

    def stop(self) -> None:
        """Close the socket; the serve task dies on the next recv."""
        if self.sock is not None:
            self.sock.close()
            self.sock = None


# -- the decider client -----------------------------------------------------


class RawDecider:
    """A protocol-level consent decider: the events websocket plus
    the decide/revoke REST surface -- the same wire the shipped
    ``msks egress watch`` decider speaks, with the prompts and
    verdicts observable programmatically instead of on a TTY."""

    def __init__(self, daemon: Daemon, workspace_id: str) -> None:
        self.daemon = daemon
        self.workspace_id = workspace_id
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.requests: list[dict] = []  # every egress.request frame's row
        self.resolutions: dict[str, str] = {}  # rid -> decision
        self.rules_frames = 0
        self._mark = 0  # requests[] index at the last connect()

    async def connect(self) -> None:
        """Open the socket and announce as this workspace's
        decider; remember where the snapshot slice begins."""
        scheme = self.daemon.url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        uri = f"{scheme}/api/v1/events"
        self.ws = await websockets.connect(
            uri,
            subprotocols=["bearer", self.daemon.token],
            ssl=None
            if self.daemon.url.startswith("http://")
            else ssl.create_default_context(cafile=self.daemon.cafile),
            max_size=2**22,
        )
        await self.ws.send(
            json.dumps(
                {"type": "egress.decider", "workspace": self.workspace_id}
            )
        )
        assert self.ws.subprotocol == "bearer", (
            "daemon did not echo the websocket auth subprotocol"
        )
        self._mark = len(self.requests)
        self.reader = asyncio.create_task(self.recv_loop())

    async def recv_loop(self) -> None:
        """File every frame until the socket closes."""
        try:
            async for raw in self.ws:
                self.file_frame(json.loads(raw))
        except websockets.ConnectionClosed, asyncio.CancelledError:
            return

    def file_frame(self, frame: dict) -> None:
        """One frame onto the right observation list."""
        event = frame.get("event")
        data = frame.get("data") or {}
        if event == "egress.request":
            self.file_request(data)
        elif event == "egress.resolved":
            self.file_resolution(data)
        elif event == "egress.rules":
            self.rules_frames += 1

    def file_request(self, data: dict) -> None:
        """One request frame's row onto the observation list."""
        row = data.get("request")
        if row is not None:
            self.requests.append(row)

    def file_resolution(self, data: dict) -> None:
        """One resolved frame onto the resolutions map."""
        decision = data.get("decision")
        if isinstance(decision, str):
            self.resolutions[data.get("request_id", "")] = decision

    def rows_for(self, host: str) -> list[dict]:
        """Every request row this decider saw for one destination
        in its own workspace."""
        want = canonical(host)
        return [
            r
            for r in self.requests
            if canonical(r.get("dest_host") or "") == want
            and r.get("workspace_id", self.workspace_id) == self.workspace_id
        ]

    async def wait_for(self, host: str, timeout: float) -> str | None:
        """The request id for one destination that has not yet been
        resolved, or None.  A prior iteration's resolved request
        must not satisfy a later probe's wait."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.rows_for(host)
            for row in reversed(rows):
                rid = row["id"]
                if rid not in self.resolutions:
                    return rid
            await asyncio.sleep(0.05)
        return None

    async def wait_no_request(self, host: str, window: float) -> str | None:
        """The id of a request that must NOT arrive, or None when
        the window stayed quiet.  Only unresolved requests count:
        a prior iteration's resolved request is not a new hold."""
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            for row in reversed(self.rows_for(host)):
                rid = row["id"]
                if rid not in self.resolutions:
                    return rid
            await asyncio.sleep(0.05)
        return None

    async def wait_resolution(self, rid: str, timeout: float) -> str | None:
        """The decision a hold resolved under, or None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if rid in self.resolutions:
                return self.resolutions[rid]
            await asyncio.sleep(0.05)
        return None

    async def settle(self, timeout: float = 4.0) -> None:
        """Let the registration snapshot and rules land."""
        await asyncio.sleep(timeout)

    async def verdict(
        self, rid: str, decision: str, duration: str
    ) -> tuple[int, dict | None]:
        """POST one verdict; returns (status, reply body)."""
        async with self.daemon.client() as client:
            response = await client.post(
                f"/api/v1/workspaces/{self.workspace_id}"
                f"/egress/requests/{rid}",
                json={"decision": decision, "duration": duration},
            )
            body = None
            with contextlib.suppress(Exception):
                body = response.json()
            return response.status_code, body

    async def revoke(self, rid: str) -> int:
        """DELETE one in-effect verdict."""
        async with self.daemon.client() as client:
            response = await client.delete(
                f"/api/v1/workspaces/{self.workspace_id}/egress/requests/{rid}"
            )
            return response.status_code

    async def rows(self, decision: str | None = None) -> list[dict]:
        """The workspace's audit rows over REST."""
        path = f"/api/v1/workspaces/{self.workspace_id}/egress/requests"
        if decision is not None:
            path += f"?decision={decision}"
        async with self.daemon.client() as client:
            return (await client.get(path)).json()

    async def close(self) -> None:
        """End this decider's authority (the socket is the
        liveness)."""
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


# -- the guest console ------------------------------------------------------


PROMPT_NEEDLE = b"root@msks-guest:~#"


class Console:
    """One workspace's console websocket: the probe surface.

    A fresh session per attempt (the #75 rule): a wedged session
    must not fail the probe -- the next attempt opens a fresh shell
    on a further-along guest. Markers are guest-computed (the
    ``$?`` expansion), so the pty's echo of the command text never
    satisfies a marker.
    """

    def __init__(self, daemon: Daemon, workspace_id: str) -> None:
        self.daemon = daemon
        self.workspace_id = workspace_id
        self.attempts = 3

    def connect(self):
        """The console websocket, with the client's own TLS. The
        token rides the handshake's auth subprotocol offer (#116)."""
        return websockets.connect(
            ws_url(self.daemon.url, self.workspace_id),
            subprotocols=["bearer", self.daemon.token],
            ssl=ssl.create_default_context(cafile=self.daemon.cafile),
            max_size=2**22,
        )

    def ctx(self):
        return ssl.create_default_context(cafile=self.daemon.cafile)

    async def run(
        self, command: str, marker: str, timeout: float
    ) -> str | None:
        """Run one command; return the marker it printed (its
        ``TAG-RC-<n>`` tail), or None when no marker arrived."""
        want = marker.encode()
        for _ in range(self.attempts):
            got = await self.try_once(command, want, timeout)
            if got is not None:
                return got.decode(errors="replace")
        return None

    async def run_multi(
        self, command: str, markers: list[str], timeout: float
    ) -> dict[str, str]:
        """Run one command; collect each marker prefix's full text
        (``{prefix: prefix-RC-<n>}``)."""
        wants = [m.encode() for m in markers]
        for _ in range(self.attempts):
            got = await self.try_once_multi(command, wants, timeout)
            if got is not None:
                return self.multi_text(markers, got)
        return {m: "" for m in markers}

    @staticmethod
    def multi_text(markers: list[str], got: list[bytes]) -> dict[str, str]:
        """The markers mapped to their decoded texts."""
        return {
            m: hit.decode(errors="replace")
            for m, hit in zip(markers, got, strict=True)
        }

    async def try_once(
        self, command: str, want: bytes, timeout: float
    ) -> bytes | None:
        """One session: auth, wait for the prompt, send, await the
        marker.  The prompt wait is critical (#75): the pty's
        readline discards typeahead that arrives before it starts,
        so a command sent before the prompt lands is swallowed."""
        try:
            async with self.connect() as ws:
                buf = await self.authed_lead(ws)
                buf = await self.wait_prompt(ws, buf)
                if want in buf:
                    return want
                await ws.send(command.encode() + b"\n")
                hits = await self.drain_until(ws, buf, [want], timeout)
                if hits is None:
                    print(
                        f"     (console session wedged; tail: {buf[-200:]!r})",
                        flush=True,
                    )
                return None if hits is None else hits[0]
        except websockets.ConnectionClosed, TimeoutError, OSError:
            return None

    async def try_once_multi(
        self, command: str, wants: list[bytes], timeout: float
    ) -> list[bytes] | None:
        """One session collecting several markers."""
        try:
            async with self.connect() as ws:
                buf = await self.authed_lead(ws)
                buf = await self.wait_prompt(ws, buf)
                await ws.send(command.encode() + b"\n")
                return await self.drain_until(ws, buf, wants, timeout)
        except websockets.ConnectionClosed, TimeoutError, OSError:
            return None

    async def authed_lead(self, ws) -> bytes:
        """The console's own #123 auth exchange, and the bytes it
        left on the wire."""
        lead = await consoleauth.auth_exchange(
            ws,
            self.workspace_id,
            self.daemon.url,
            self.daemon.token,
            self.ctx(),
        )
        return lead if isinstance(lead, bytes) else lead.encode()

    async def wait_prompt(self, ws, buf: bytes) -> bytes:
        """Drain until the shell prompt arrives (#75): readline
        discards typeahead that precedes its first prompt, so a
        command sent too early is swallowed silently."""
        deadline = time.monotonic() + 30.0
        while PROMPT_NEEDLE not in buf:
            if time.monotonic() > deadline:
                return buf  # give up waiting; the caller retries
            try:
                buf += await self.next_chunk(ws)
            except TimeoutError:
                return buf
        return buf

    async def drain_until(
        self, ws, buf: bytes, wants: list[bytes], timeout: float
    ) -> list[bytes] | None:
        """Drain the console until every marker lands (each with
        its ``-RC-<n>`` tail) or the clock runs out."""
        deadline = time.monotonic() + timeout
        found: dict[int, bytes] = {}
        while time.monotonic() < deadline and len(found) < len(wants):
            buf += await self.next_chunk(ws)
            scan_markers(buf, wants, found)
        if len(found) < len(wants):
            return None
        return [found[i] for i in range(len(wants))]

    @staticmethod
    async def next_chunk(ws, timeout: float = 30.0) -> bytes:
        """One console chunk, as bytes."""
        chunk = await asyncio.wait_for(ws.recv(), timeout)
        return chunk if isinstance(chunk, bytes) else chunk.encode()


def locate_marker(buf: bytes, want: bytes) -> bytes | None:
    """The probe's marker (``want`` + its numeric tail) once it
    has arrived. The pty echoes the sent command, and that echo
    carries the prefix with the literal ``$?`` tail, so an
    occurrence counts only when its tail is delimited and all
    digits; the echo's occurrence keeps the search moving."""
    start = 0
    while (hit := scan_marker(buf, want, start)) is not None:
        at, end = hit
        tail = buf[at + len(want) : end]
        if tail.isdigit():
            return buf[at:end]
        start = at + 1
    return None


def scan_marker(buf: bytes, want: bytes, start: int) -> tuple[int, int] | None:
    """The next prefix occurrence with its tail's delimiter index
    (None when the prefix is absent or its tail is still open --
    an open tail may yet prove to be the answer, so it waits)."""
    at = buf.find(want, start)
    if at < 0:
        return None
    end = delimiter_at(buf, at + len(want))
    return None if end is None else (at, end)


def delimiter_at(buf: bytes, pos: int) -> int | None:
    """The first output delimiter at or after ``pos``."""
    for i in range(pos, len(buf)):
        if buf[i] in b" \r\n\x00":
            return i
    return None


def scan_markers(buf: bytes, wants: list[bytes], found: dict) -> None:
    """Fold every newly-satisfied marker into ``found``."""
    for i, want in enumerate(wants):
        if i not in found:
            hit = locate_marker(buf, want)
            if hit is not None:
                found[i] = hit


def probe_command(tag: str, host: str, port: int) -> str:
    """One TCP probe inside the guest: ``bash /dev/tcp`` under
    ``timeout``; the echo line carries the exit status in the
    marker (``TAG-RC-<n>``, guest-computed)."""
    return (
        f"timeout {int(PROBE_TIMEOUT_S)} "
        f"bash -c '</dev/tcp/{host}/{port}' "
        f">/dev/null 2>&1; echo {tag}-RC-$?"
    )


def fanout_command(pairs: list[tuple[str, str]], port: int) -> str:
    """N probes at once, each in its own subshell with its own
    marker; the command returns when the last one finishes."""
    parts = [f"( {probe_command(tag, host, port)} ) &" for tag, host in pairs]
    return " ".join(parts) + " wait"


def parse_rc(marker_text: str | None) -> int | None:
    """The exit status a probe's marker carried."""
    if not marker_text:
        return None
    with contextlib.suppress(ValueError):
        return int(marker_text.rsplit("-", 1)[1])
    return None


# -- the harness ------------------------------------------------------------


class Harness:
    """The run: setup, the fuzz loop, the phases, teardown."""

    def __init__(self, args) -> None:
        self.args = args
        self.daemon = Daemon(args)
        self.summary = Summary()
        self.abort = False
        self.client: httpx.AsyncClient | None = None
        self.ws_id = ""
        self.extra_ws: list[str] = []
        self.consoles: dict[str, Console] = {}
        self.console: Console | None = None
        self.decider: RawDecider | None = None
        self.model = EgressModel(ALLOW_LIST)
        self.tag_seq = 0
        self.dns: ControlledDNS | None = None
        self._dns_task: asyncio.Task | None = None
        self.reaper: subprocess.Popen | None = None

    def tag(self) -> str:
        """A fresh probe marker tag."""
        self.tag_seq += 1
        return f"T{self.tag_seq}"

    # -- scoring ------------------------------------------------------------

    def record(self, res: Result) -> Result:
        """Tally, name, and print one result."""
        res.outcome = outcome_name(res)
        self.summary.total += 1
        self.summary.rows.append(res)
        if res.status == PASS:
            self.summary.passed += 1
        elif res.status == FINDING:
            self.summary.findings += 1
        else:
            self.summary.mismatches += 1
        self.print_row(res)
        if res.status == MISMATCH and not self.args.continue_run:
            self.abort = True
        return res

    def print_row(self, r: Result) -> None:
        """The realtime expected-vs-actual line."""
        print(
            f"[{self.summary.total:>4}] {r.label:<30} host={r.host:<22} "
            f"server={r.server:<13} conn={exit_label(r.rc):<14} {r.mark}"
            + (f" [{r.outcome}]" if r.outcome else "")
        )
        if r.detail:
            print(f"       ... {r.detail}")

    def phase_step(self, label: str, host: str) -> Step:
        """A synthetic step for a phase probe's row."""
        return Step(0, host, "domain", False, label, "-")

    # -- REST surface -------------------------------------------------------

    async def create_workspace(
        self, name: str, mode: str, allow: list[str] | None = None
    ) -> str:
        """Create + start one workspace with a consent posture, and
        settle its guest: the console answers before DHCP and the
        resolver do, so the first scored probe waits for a name to
        resolve before anything is scored."""
        body: dict = {"name": name, "egress_mode": mode}
        if allow is not None:
            body["egress_allowlist"] = allow
        response = await self.client.post("/api/v1/workspaces", json=body)
        assert response.status_code == 201, response.text
        wid = response.json()["id"]
        response = await self.client.post(f"/api/v1/workspaces/{wid}/start")
        assert response.status_code == 200, response.text
        host = allow[0] if allow else ALLOW_LIST[0]
        await self.settle_guest(wid, settle_host(host))
        return wid

    async def settle_guest(self, wid: str, host: str) -> None:
        """Wait for the guest's networking: a name resolving through
        the daemon's forwarder (pure DNS -- no SYN, so it gates
        nothing and records nothing in interactive mode)."""
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            tag = self.tag()
            marker = await self.console_for(wid).run(
                f"getent hosts {host} >/dev/null 2>&1; echo {tag}-RC-$?",
                f"{tag}-RC-",
                30.0,
            )
            if parse_rc(marker) == 0:
                return
            await asyncio.sleep(2.0)
        raise SystemExit(
            f"workspace {wid}: no DNS through the daemon within 120s; "
            "the guest's egress path is not up"
        )

    async def delete_workspace(self, wid: str) -> None:
        """Stop + delete one workspace (best-effort, skipped when
        setup died before the client existed)."""
        if self.client is None:
            return
        with contextlib.suppress(Exception):
            await self.client.post(f"/api/v1/workspaces/{wid}/stop")
        with contextlib.suppress(Exception):
            await self.client.delete(f"/api/v1/workspaces/{wid}")

    async def set_policy(self, wid: str, mode: str, allow: list[str]) -> None:
        """Swap a workspace's posture live."""
        response = await self.client.put(
            f"/api/v1/workspaces/{wid}/egress/policy",
            json={"mode": mode, "allow_list": allow},
        )
        assert response.status_code == 200, response.text

    def console_for(self, wid: str) -> Console:
        """The (cached) console for one workspace."""
        if wid not in self.consoles:
            self.consoles[wid] = Console(self.daemon, wid)
        return self.consoles[wid]

    # -- probes --------------------------------------------------------------

    async def probe(self, host: str, port: int = 80) -> int | None:
        """One guest-side TCP probe; returns its exit status."""
        tag = self.tag()
        marker = await self.console.run(
            probe_command(tag, host, port), f"{tag}-RC-", PROBE_TIMEOUT_S + 15
        )
        return parse_rc(marker)

    async def probe_ws(
        self, wid: str, host: str, port: int = 80
    ) -> int | None:
        """One probe inside another workspace's guest."""
        tag = self.tag()
        marker = await self.console_for(wid).run(
            probe_command(tag, host, port), f"{tag}-RC-", PROBE_TIMEOUT_S + 15
        )
        return parse_rc(marker)

    async def probe_batch(
        self, pairs: list[tuple[str, str]], port: int = 80
    ) -> dict[str, int | None]:
        """N probes at once; returns tag -> exit status."""
        markers = [f"{tag}-RC-" for tag, _host in pairs]
        found = await self.console.run_multi(
            fanout_command(pairs, port), markers, 120.0
        )
        return {tag: parse_rc(text) for tag, text in found.items()}

    # -- lifecycle -----------------------------------------------------------

    async def start_controlled_dns(self) -> str:
        """Resolve every harness hostname once, start the controlled
        DNS server, and return the upstream address for the daemon."""
        from msks.net.dns import upstream_from_resolv

        hostnames = all_harness_hostnames()
        print(f"controlled DNS: resolving {len(hostnames)} hostnames...")
        mapping = resolve_hostnames(hostnames)
        # Co-resident canaries: alias host B to host A's address so
        # both names resolve to the same IP (#292).
        host_a = canonical(FRESH["coresident_a"])
        host_b = canonical(FRESH["coresident_b"])
        mapping[host_b] = mapping[host_a]
        for name, ip in sorted(mapping.items()):
            print(f"  {name:<28} -> {ip}")
        upstream = upstream_from_resolv()
        if upstream is None:
            raise SystemExit(
                "controlled DNS: no system resolver in /etc/resolv.conf"
            )
        self.dns = ControlledDNS(mapping, upstream)
        self.dns.start()
        self._dns_task = asyncio.create_task(self.dns.serve())
        return DNS_CONTROLLED_ADDR

    async def setup(self) -> None:
        """Controlled DNS, daemon, workspace, console, decider."""
        if self.args.url is None:
            dns_upstream = await self.start_controlled_dns()
            self.daemon.boot(dns_upstream=dns_upstream)
            self.arm_reaper()
        elif not (self.args.token and self.args.cafile):
            raise SystemExit(
                "--url needs --token and --cafile (the dev daemon writes "
                "them under .devenv/state/msksd)"
            )
        await self.daemon.wait_ready()
        self.client = self.daemon.client()
        self.ws_id = await self.create_workspace(
            f"fuzz-{uuid.uuid4().hex[:6]}", "interactive", ALLOW_LIST
        )
        print(
            f"workspace {self.ws_id} (interactive, allow-list: {ALLOW_LIST})"
        )
        self.console = self.console_for(self.ws_id)
        self.decider = RawDecider(self.daemon, self.ws_id)
        await self.decider.connect()
        await self.decider.settle()
        await self.readiness_probe()

    async def readiness_probe(self) -> None:
        """One allow-list connection with the decider attached: the
        learned pin carries it (no request) under controlled DNS
        (the address is stable).  If the SYN lands before the
        resolver's pin is populated a re-hold surfaces and the probe
        settles it — either path proves the guest's data path out.

        Retried: the first probe races with the resolver's allow-
        list pin learning (the SYN lands before the name→IP map is
        populated and the NFQUEUE hold outlasts the probe timeout).
        A second probe usually finds the pin in place."""
        for attempt in range(3):
            rc = await self.readiness_attempt()
            if rc == 0:
                return
            if attempt < 2:
                print(
                    f"     (readiness attempt {attempt + 1}/3: "
                    f"rc={rc}; retrying)",
                    flush=True,
                )
                await asyncio.sleep(3.0)
        raise SystemExit(
            f"allow-list readiness probe failed (rc={rc}); the guest's "
            f"egress path is not up\n{self.daemon.log_tail()}"
        )

    async def readiness_attempt(self) -> int | None:
        """One readiness probe attempt."""
        task = asyncio.create_task(self.probe(ALLOW_LIST[0]))
        rid = await self.decider.wait_for(ALLOW_LIST[0], 30.0)
        if rid is not None:
            await self.decider.verdict(rid, "allow", DURATION_ONCE)
            await self.decider.wait_resolution(rid, 15.0)
        return await task

    async def teardown(self) -> None:
        """Teardown as independent steps (#307): each step's failure
        is suppressed on its own, so one failure cannot strand the
        rest, and every exit path retires the whole stack."""
        for step in self.teardown_steps():
            with contextlib.suppress(Exception):
                await step()

    def teardown_steps(self) -> list:
        """The teardown steps in order: the sockets first (they need
        a live daemon), the daemon and any leftover VMMs next, the
        watchdog and the DNS server last."""
        steps = []
        if self.decider is not None:
            steps.append(self.decider.close)
        steps.append(self.teardown_workspaces)
        if self.client is not None:
            steps.append(self.client.aclose)
        steps.append(self.stop_daemon)
        steps.append(self.stand_down_reaper)
        steps.append(self.stop_dns)
        return steps

    async def teardown_workspaces(self) -> None:
        """Delete every workspace and verify its VMM retired; a
        --keep-workspace run leaves the workspaces to the operator
        (so no verification and no sweep either)."""
        if self.args.keep_workspace:
            return
        for wid in sorted(self.workspace_ids()):
            await self.delete_workspace(wid)
            await self.verify_workspace_gone(wid)

    def workspace_ids(self) -> set[str]:
        """Every workspace this run created."""
        ids = set(self.extra_ws)
        if self.ws_id:
            ids.add(self.ws_id)
        return ids

    async def verify_workspace_gone(self, wid: str) -> None:
        """The API said gone; make sure the host agrees.  A delete
        can return while the VMM is still dying, so the check
        retries the API once before reaping by hand (#307)."""
        root = self.daemon.state_dir
        for _ in range(2):
            if not scan_vmm_pids(root, {wid}):
                return
            await self.delete_workspace(wid)
            await asyncio.sleep(2.0)
        await self.reap_strays(f"workspace {wid}", {wid})

    async def reap_strays(self, label: str, ids: set[str]) -> None:
        """SIGKILL owned VMMs still on the host and record the
        residue (#307)."""
        if self.args.keep_workspace:
            return
        root = self.daemon.state_dir
        strays = scan_vmm_pids(root, ids)
        if not strays:
            return
        for pid in strays:
            kill_verified(pid, signal.SIGKILL, root, ids)
        await asyncio.sleep(1.0)
        alive = alive_pids(strays)
        self.record(reap_result(label, strays, alive))

    async def stop_daemon(self) -> None:
        """Stop msksd -- recording, not raising, a grace failure --
        then sweep the host for leftover VMMs (#307)."""
        problem = await self.daemon.stop()
        if problem is not None:
            self.record(
                Result(
                    "daemon stop",
                    "(daemon)",
                    True,
                    "",
                    "?",
                    "error",
                    None,
                    MISMATCH,
                    detail=problem,
                )
            )
        await self.reap_strays("host sweep", self.workspace_ids())

    def arm_reaper(self) -> None:
        """Spawn the detached watchdog that cleans up the stack if
        this process dies without a teardown (#307).  Attach mode
        has no self-booted daemon, so there is nothing to watch."""
        if self.daemon.state_dir is None:
            return
        self.reaper = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--reap-for",
                str(os.getpid()),
                "--state-dir",
                str(self.daemon.state_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stand_down_reaper(self) -> None:
        """Tell the watchdog the teardown completed (#307): the
        sentinel, or a state dir the daemon's stop already dropped,
        ends its poll without a reap."""
        if self.reaper is None:
            return
        with contextlib.suppress(OSError):
            (self.daemon.state_dir / REAP_SENTINEL).touch()
        self.reaper = None

    async def stop_dns(self) -> None:
        """Stop the controlled DNS server and its serve task."""
        if self._dns_task is not None:
            self._dns_task.cancel()
            self._dns_task = None
        if self.dns is not None:
            self.dns.stop()
            self.dns = None

    # -- the fuzz loop -------------------------------------------------------

    async def run_step(self, step: Step) -> Result:
        """One fuzzed destination: model the expectation, probe,
        answer (or let it time out), score both sides."""
        canon = canonical(step.host)
        covered, cov_decision, cov_src = self.model.covers(canon, time.time())
        self.model.touched.add(canon)
        if covered:
            return await self.covered_step(step, cov_decision, cov_src)
        expect_conn = {
            "allow": EXPECT_RELEASED,
            "deny": EXPECT_REFUSED,
            "none": EXPECT_NOT0,
        }[step.action]
        task = asyncio.create_task(self.probe(step.host))
        rid = await self.decider.wait_for(canon, REQUEST_WAIT_S)
        if rid is None:
            rc = await task
            return self.no_request_result(step, expect_conn, rc)
        return await self.answer_step(step, canon, rid, expect_conn, task)

    async def covered_step(
        self, step: Step, cov_decision: str, cov_src: str
    ) -> Result:
        """A covered destination: no request may surface; the
        connection must land as the coverage says."""
        canon = canonical(step.host)
        expect_conn = (
            EXPECT_RELEASED
            if cov_decision == DECISION_ALLOWED
            else EXPECT_REFUSED
        )
        task = asyncio.create_task(self.probe(step.host))
        intruder = await self.decider.wait_no_request(
            canon, NO_REQUEST_WINDOW_S
        )
        rc = await task
        if intruder is not None:
            soft = step.exploratory or cov_src != "allowlist"
            return self.record(
                Result(
                    "covered step",
                    step.host,
                    False,
                    expect_conn,
                    "-",
                    "request(!)",
                    rc,
                    FINDING if soft else MISMATCH,
                    detail=(
                        f"expected no request (covered:{cov_decision}:"
                        f"{cov_src}), one arrived"
                    ),
                )
            )
        status, detail = classify_conn(expect_conn, rc)
        return self.record(
            Result(
                "covered step",
                step.host,
                False,
                expect_conn,
                "-",
                "no-req",
                rc,
                status,
                detail=detail,
            )
        )

    def no_request_result(
        self, step: Step, expect_conn: str, rc: int | None
    ) -> Result:
        """An uncovered destination produced no request: a hang is
        a finding (console/NFQUEUE), anything else a hard
        mismatch."""
        if rc is None:
            return self.record(
                Result(
                    "fuzz step",
                    step.host,
                    True,
                    expect_conn,
                    step.action,
                    "no-request(!)",
                    None,
                    FINDING,
                    "expected a request; connection hung",
                )
            )
        return self.record(
            Result(
                "fuzz step",
                step.host,
                True,
                expect_conn,
                step.action,
                "no-request(!)",
                rc,
                FINDING if step.exploratory else MISMATCH,
                f"expected a request, none arrived ({exit_label(rc)})",
            )
        )

    async def answer_step(
        self,
        step: Step,
        canon: str,
        rid: str,
        expect_conn: str,
        task: asyncio.Task,
    ) -> Result:
        """The request arrived: answer per the plan and score the
        connection result against the expectation."""
        if step.action == "none":
            return await self.timeout_step(step, canon, rid, expect_conn, task)
        decision = (
            DECISION_ALLOWED if step.action == "allow" else DECISION_DENIED
        )
        decide_status, _body = await self.decider.verdict(
            rid, step.action, step.duration
        )
        resolved = await self.decider.wait_resolution(rid, 20.0)
        self.model.record(canon, decision, step.duration, time.time())
        rc = await task
        res = self.finish_step(step, expect_conn, rc, resolved, decide_status)
        await self.retry_after_verdict(step, canon)
        return res

    async def timeout_step(
        self,
        step: Step,
        canon: str,
        rid: str,
        expect_conn: str,
        task: asyncio.Task,
    ) -> Result:
        """No verdict: the hold expires at the consent timeout; the
        connection must not succeed."""
        resolved = await self.decider.wait_resolution(
            rid, self.args.consent_timeout + 25.0
        )
        self.model.record(canon, DECISION_DENIED, DURATION_ONCE, time.time())
        rc = await task
        return self.finish_step(step, expect_conn, rc, resolved, 200)

    def finish_step(
        self,
        step: Step,
        expect_conn: str,
        rc: int | None,
        resolved: str | None,
        decide_status: int,
    ) -> Result:
        """Score one answered step (server side + connection)."""
        status, detail = classify_conn(expect_conn, rc)
        if decide_status != 200:
            status, detail = (
                MISMATCH,
                (f"step raised: decide answered {decide_status}"),
            )
        return self.record(
            Result(
                "fuzz step",
                step.host,
                True,
                expect_conn,
                f"{step.action}/{step.duration}",
                "resolved" if resolved else "held(!)",
                rc,
                status,
                detail=detail,
            )
        )

    async def retry_after_verdict(self, step: Step, canon: str) -> None:
        """Reconnect after a verdict to probe the duration's effect,
        with the expectation taken from the model's coverage: a
        carrying verdict stays in effect (no re-prompt, same
        enforcement); a once-allow has been consumed (fresh
        re-prompt); a once-deny's fail-fast RST still covers the
        destination (refused, no re-prompt) until it lapses. A
        retry right after a timeout ('none') is confounded by that
        same backstop and adds nothing, so it is skipped."""
        if not self.args.retries or step.action == "none":
            return
        if step.duration == DURATION_ONCE:
            # A once verdict is consumed by its connection: the
            # retry must re-prompt. For a deny the fail-fast RST
            # pin covers the first seconds; the guest's SYN
            # retransmit passes the lapsed pin and re-queues, so
            # the request arrives inside the wait window.
            await self.sub_probe(
                step, canon, "exceeding-retry(once)", True, EXPECT_NOT0
            )
            return
        await asyncio.sleep(1.5)
        await self.within_retry(step, canon)

    async def within_retry(self, step: Step, canon: str) -> None:
        """The carrying-verdict reconnect: covered (no re-prompt,
        same enforcement) with the expectation from the model."""
        covered, cov_decision, _src = self.model.covers(canon, time.time())
        within = (
            EXPECT_RELEASED
            if cov_decision == DECISION_ALLOWED
            else EXPECT_REFUSED
        )
        await self.sub_probe(step, canon, "within-retry", not covered, within)

    async def sub_probe(
        self,
        parent: Step,
        canon: str,
        label: str,
        expect_request: bool,
        expect_conn: str,
    ) -> str:
        """One reconnect probe; records an indented sub-line."""
        task = asyncio.create_task(self.probe(parent.host))
        if not expect_request:
            return await self.covered_sub_probe(
                parent, canon, label, task, expect_conn
            )
        rid = await self.decider.wait_for(canon, REQUEST_WAIT_S)
        if rid is None:
            rc = await task
            status = FINDING if rc is None else MISMATCH
            return self.record_sub(
                parent,
                label,
                "no-request(!)",
                rc,
                status,
                "expected a re-prompt; none arrived"
                + ("" if rc is None else f" ({exit_label(rc)})"),
            )
        await self.decider.verdict(rid, "allow", DURATION_ONCE)
        await self.decider.wait_resolution(rid, 15.0)
        rc = await task
        return self.record_sub(
            parent, label, "re-prompt", rc, PASS, "request arrived as expected"
        )

    async def covered_sub_probe(
        self,
        parent: Step,
        canon: str,
        label: str,
        task: asyncio.Task,
        expect_conn: str,
    ) -> str:
        """The no-re-prompt half of a reconnect probe."""
        intruder = await self.decider.wait_no_request(
            canon, NO_REQUEST_WINDOW_S
        )
        rc = await task
        if intruder is not None:
            return self.record_sub(
                parent,
                label,
                "request(!)",
                rc,
                FINDING,
                "expected no request (covered:verdict), one arrived",
            )
        status, detail = classify_conn(expect_conn, rc)
        return self.record_sub(parent, label, "no-req", rc, status, detail)

    def record_sub(
        self,
        parent: Step,
        label: str,
        server: str,
        rc: int | None,
        status: str,
        detail: str,
    ) -> str:
        """Tally and print one sub-probe row."""
        self.record(
            Result(
                label,
                parent.host,
                True,
                "",
                label,
                server,
                rc,
                status,
                detail=detail,
            )
        )
        return status

    # -- phases --------------------------------------------------------------

    async def run_phases(self) -> None:
        """Every phase, in order, each skipped on abort. The
        no-decider phase runs last: it closes the decider."""
        phases = [
            ("lifecycle", self.args.lifecycle, self.run_lifecycle_phase),
            (
                "multiple deciders",
                self.args.multi_decider,
                self.run_multi_decider_phase,
            ),
            (
                "reconnect snapshot",
                self.args.snapshot,
                self.run_snapshot_phase,
            ),
            ("fan-out + dedupe", self.args.fanout, self.run_fanout_phase),
            ("revoke", self.args.revoke, self.run_revoke_phase),
            (
                "decider disconnects (fail-closed)",
                self.args.fail_closed,
                self.run_fail_closed_phase,
            ),
            ("decider scope", self.args.decider_scope, self.run_scope_phase),
            (
                "audit distinction",
                self.args.audit_distinction,
                self.run_audit_phase,
            ),
            ("allow mode", self.args.allow_phase, self.run_allow_phase),
            (
                "restart semantics",
                self.args.restart_phase,
                self.run_restart_phase,
            ),
            ("host scope", self.args.host_scope, self.run_host_scope_phase),
            ("port scope", self.args.port_scope, self.run_port_scope_phase),
            (
                "co-resident canaries",
                self.args.coresident_phase,
                self.run_coresident_phase,
            ),
            ("no decider", self.args.static_phase, self.run_no_decider_phase),
        ]
        for name, enabled, phase in phases:
            if self.abort:
                return
            if enabled:
                print(f"\n--- {name} ---")
                await phase()

    async def fresh_hold(self, host: str) -> tuple[str | None, asyncio.Task]:
        """Start a probe and wait for its request; returns
        (rid, probe task)."""
        task = asyncio.create_task(self.probe(host))
        rid = await self.decider.wait_for(canonical(host), REQUEST_WAIT_S)
        return rid, task

    def hold_failed(self, label: str, host: str, task: asyncio.Task) -> None:
        """Record a missing hold and cancel its probe."""
        task.cancel()
        self.record(
            Result(
                label,
                host,
                True,
                "",
                "-",
                "no-request(!)",
                None,
                MISMATCH,
                "expected a request, none arrived (phase bring-up)",
            )
        )

    async def run_lifecycle_phase(self) -> None:
        """Within vs exceeding a 5m verdict, for an allow and a
        deny: decide 5m -> within-retry (in effect) -> wait past
        the window -> exceeding-retry (fresh re-prompt)."""
        cases = [
            (FRESH["lifecycle_allow"], "allow", EXPECT_RELEASED),
            (FRESH["lifecycle_deny"], "deny", EXPECT_REFUSED),
        ]
        for host, action, main_conn in cases:
            if self.abort:
                return
            await self.lifecycle_case(host, action, main_conn)

    async def lifecycle_case(
        self, host: str, action: str, main_conn: str
    ) -> None:
        """One lifecycle case at the 5m floor."""
        step = self.phase_step("lifecycle", host)
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("lifecycle hold", host, task)
            return
        await self.decider.verdict(rid, action, DURATION_5M)
        await self.decider.wait_resolution(rid, 20.0)
        self.model.record(canonical(host), action, DURATION_5M, time.time())
        rc = await task
        status, detail = classify_conn(main_conn, rc)
        self.record(
            Result(
                "lifecycle decide",
                host,
                True,
                main_conn,
                action,
                "resolved",
                rc,
                status,
                detail=detail,
            )
        )
        await self.sub_probe(
            step, canonical(host), "within-retry", False, main_conn
        )
        print(
            f"     (waiting {self.args.expiry_wait:.0f}s past the 5m "
            "window ...)"
        )
        await asyncio.sleep(self.args.expiry_wait)
        self.model.forget(canonical(host))
        await self.sub_probe(
            step, canonical(host), "exceeding-retry", True, EXPECT_NOT0
        )

    async def run_multi_decider_phase(self) -> None:
        """Two deciders on one workspace: both see the request,
        the first verdict wins, the second answers 404 and changes
        nothing."""
        d2 = RawDecider(self.daemon, self.ws_id)
        await d2.connect()
        await d2.settle()
        await self.multi_sync_case(d2)
        if not self.abort:
            await self.multi_first_wins_case(d2)
        await d2.close()

    async def multi_sync_case(self, d2: RawDecider) -> None:
        """d2 denies; the primary decider saw the same request and
        the resolve frame."""
        host = FRESH["multi_x"]
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("multi hold X", host, task)
            return
        seen_by_d1 = self.decider.rows_for(canonical(host)) != []
        await d2.verdict(rid, "deny", DURATION_ONCE)
        resolved = await self.decider.wait_resolution(rid, 15.0)
        rc = await task
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
        )
        status, detail = self.verdict_outcome(resolved, rc, seen_by_d1)
        self.record(
            Result(
                "d2 denies -> d1 synced",
                host,
                True,
                EXPECT_REFUSED,
                "deny",
                "resolved" if resolved else "held(!)",
                rc,
                status,
                detail=detail,
            )
        )

    def verdict_outcome(
        self, resolved: str | None, rc: int | None, seen_by_d1: bool
    ) -> tuple[str, str]:
        """Score one denied-and-refused observation."""
        if not seen_by_d1:
            return MISMATCH, "not seen by BOTH deciders"
        if resolved is None:
            return FINDING, "the other decider never saw the resolve"
        if rc == 0:
            return MISMATCH, "a deny let the connection through"
        if rc is None:
            return FINDING, "expected refusal, probe hung"
        return PASS, ""

    async def multi_first_wins_case(self, d2: RawDecider) -> None:
        """d2's deny wins; the primary's late allow must 404 and
        change nothing."""
        host = FRESH["multi_y"]
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("multi hold Y", host, task)
            return
        await d2.verdict(rid, "deny", DURATION_ONCE)
        await asyncio.sleep(0.5)
        late_status, _body = await self.decider.verdict(
            rid, "allow", DURATION_ONCE
        )
        rc = await task
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
        )
        status, detail = self.late_verdict_outcome(late_status, rc)
        self.record(
            Result(
                "first(deny) wins, late allow 404s",
                host,
                True,
                EXPECT_REFUSED,
                "deny",
                "resolved",
                rc,
                status,
                detail=detail,
            )
        )

    def late_verdict_outcome(
        self, late_status: int, rc: int | None
    ) -> tuple[str, str]:
        """Score the first-decision-wins observation."""
        if late_status == 200:
            return MISMATCH, "the late verdict was accepted, not 404"
        if rc == 0:
            return MISMATCH, "late allow let it through"
        if rc is None:
            return FINDING, "expected refusal, probe hung"
        return PASS, ""

    async def run_snapshot_phase(self) -> None:
        """Reconnect snapshot: a request resolved while the decider
        was away does not replay; a still-held one does."""
        host_a, host_b = FRESH["snap_a"], FRESH["snap_b"]
        task_a = asyncio.create_task(self.probe(host_a))
        task_b = asyncio.create_task(self.probe(host_b))
        rid_a = await self.decider.wait_for(canonical(host_a), REQUEST_WAIT_S)
        rid_b = await self.decider.wait_for(canonical(host_b), REQUEST_WAIT_S)
        if not (rid_a and rid_b):
            await self.snapshot_bail(
                host_a, host_b, rid_a, rid_b, task_a, task_b
            )
            return
        await self.decider.close()
        await self.decider.verdict(rid_a, "deny", DURATION_ONCE)
        await asyncio.sleep(1.0)
        await self.decider.connect()
        await self.decider.settle(2.0)
        await self.snapshot_score(host_a, host_b, rid_b, task_a, task_b)

    async def snapshot_score(
        self,
        host_a: str,
        host_b: str,
        rid_b: str,
        task_a: asyncio.Task,
        task_b: asyncio.Task,
    ) -> None:
        """Assert the reconnect snapshot's contents, then settle."""
        held = self.held_names()
        has_b = canonical(host_b) in held
        has_a = canonical(host_a) in held
        snap_status, detail = snapshot_outcome(has_a, has_b)
        self.record(
            Result(
                "reconnect snapshot",
                f"{host_a}+{host_b}",
                True,
                "",
                "-",
                snapshot_label(has_a, has_b),
                None,
                snap_status,
                detail=detail,
            )
        )
        await self.decider.verdict(rid_b, "deny", DURATION_ONCE)
        for host in (host_a, host_b):
            self.model.record(
                canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
            )
        await asyncio.gather(task_a, task_b, return_exceptions=True)

    def held_names(self) -> set[str]:
        """Every destination in the rows that arrived AFTER the
        last registration (the reconnect snapshot's world: a
        resolved-while-away row is absent, a still-held one
        replays)."""
        return {
            canonical(r.get("dest_host") or "")
            for r in self.decider.requests[self.decider._mark :]
        }

    async def snapshot_bail(
        self,
        host_a: str,
        host_b: str,
        rid_a: str | None,
        rid_b: str | None,
        task_a: asyncio.Task,
        task_b: asyncio.Task,
    ) -> None:
        """Settle what held and record the failure."""
        for rid in (rid_a, rid_b):
            if rid:
                await self.decider.verdict(rid, "deny", DURATION_ONCE)
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        self.record(
            Result(
                "snapshot hold A+B",
                f"{host_a}+{host_b}",
                True,
                "",
                "-",
                "no-request(!)",
                None,
                MISMATCH,
                "A/B not both held",
            )
        )

    async def run_fanout_phase(self) -> None:
        """N concurrent distinct hosts surface N distinct
        requests; a once-allow releases only its own connection.
        Then concurrent connections to one destination dedupe to
        one prompt."""
        hosts = FRESH["fanouts"]
        tags = [self.tag() for _ in hosts]
        task = asyncio.create_task(
            self.probe_batch(list(zip(tags, hosts, strict=True)))
        )
        rids = await self.collect_holds(hosts)
        if len(rids) != len(hosts):
            await self.fanout_bail(hosts, rids, task)
            return
        await self.fanout_release(tags, rids, task)
        if not self.abort:
            await self.dedupe_case()

    async def collect_holds(self, hosts: list[str]) -> list[tuple[str, str]]:
        """The (host, rid) pair for every concurrent hold."""
        rids: list[tuple[str, str]] = []
        for host in hosts:
            rid = await self.decider.wait_for(canonical(host), REQUEST_WAIT_S)
            if rid:
                rids.append((host, rid))
        return rids

    async def fanout_bail(
        self, hosts: list[str], rids: list[tuple[str, str]], task: asyncio.Task
    ) -> None:
        """Deny what held and record the serialization."""
        for _host, rid in rids:
            await self.decider.verdict(rid, "deny", DURATION_ONCE)
        task.cancel()
        self.record(
            Result(
                "fan-out holds",
                ",".join(hosts),
                True,
                "",
                "-",
                f"{len(rids)}/{len(hosts)} requests",
                None,
                MISMATCH,
                f"concurrent distinct hosts did not all surface requests "
                f"({len(rids)}/{len(hosts)})",
            )
        )

    async def fanout_release(
        self, tags: list[str], rids: list[tuple[str, str]], task: asyncio.Task
    ) -> None:
        """Allow the first held host once: its connection releases,
        the others stay held (then deny them)."""
        host0, rid0 = rids[0]
        await self.decider.verdict(rid0, "allow", DURATION_ONCE)
        results = await task
        rc0 = results.get(tags[0])
        held_list = [
            (await self.decider.wait_resolution(rid, 0.1)) is None
            for _host, rid in rids[1:]
        ]
        others_held = all(held_list)
        for host, rid in rids[1:]:
            await self.decider.verdict(rid, "deny", DURATION_ONCE)
            self.model.record(
                canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
            )
        status, detail = fanout_release_outcome(rc0, others_held)
        self.record(
            Result(
                "once-allow releases only its own",
                host0,
                True,
                EXPECT_RELEASED,
                "allow/once",
                "resolved",
                rc0,
                status,
                detail=detail,
            )
        )

    async def dedupe_case(self) -> None:
        """Two concurrent connections to one destination: exactly
        one prompt; the duplicate refuses fast while the first is
        pending; after a once-allow, a later connection
        re-prompts."""
        host = FRESH["dedupe"]
        tags = [self.tag(), self.tag()]
        task = asyncio.create_task(
            self.probe_batch([(tags[0], host), (tags[1], host)])
        )
        rid = await self.decider.wait_for(canonical(host), REQUEST_WAIT_S)
        if rid is None:
            self.hold_failed("dedupe hold", host, task)
            return
        await asyncio.sleep(3.0)  # the retransmit window: still one row
        count = len(self.decider.rows_for(canonical(host)))
        await self.decider.verdict(rid, "allow", DURATION_ONCE)
        results = await task
        released = [results.get(t) == 0 for t in tags]
        status, detail = dedupe_outcome(count, released, results, tags)
        self.record(
            Result(
                "same-host concurrent dedupes",
                host,
                True,
                "",
                "-",
                f"{count} request(s)",
                None,
                status,
                detail=detail,
            )
        )
        if count == 1 and not self.abort:
            await self.sub_probe(
                self.phase_step("dedupe", host),
                canonical(host),
                "post-once re-prompt",
                True,
                EXPECT_NOT0,
            )

    async def run_revoke_phase(self) -> None:
        """An in-effect verdict, revoked, gates again at the next
        connection: re-prompt, not silent coverage."""
        host = FRESH["revoke"]
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("revoke hold", host, task)
            return
        await self.decider.verdict(rid, "allow", DURATION_5M)
        await self.decider.wait_resolution(rid, 20.0)
        self.model.record(
            canonical(host), DECISION_ALLOWED, DURATION_5M, time.time()
        )
        rc = await task
        status, detail = classify_conn(EXPECT_RELEASED, rc)
        self.record(
            Result(
                "revoke grant",
                host,
                True,
                EXPECT_RELEASED,
                "allow/5m",
                "resolved",
                rc,
                status,
                detail=detail,
            )
        )
        status_code = await self.decider.revoke(rid)
        self.model.forget(canonical(host))
        await self.revoke_score(host, status_code)

    async def revoke_score(self, host: str, status_code: int) -> None:
        """Score the post-revoke re-prompt (the RST backstop does
        not apply: the verdict was an allow)."""
        await asyncio.sleep(1.5)
        if status_code != 200:
            self.record(
                Result(
                    "revoke",
                    host,
                    True,
                    "",
                    "-",
                    f"http {status_code}",
                    None,
                    MISMATCH,
                    "phase bring-up failed: the revoke was refused",
                )
            )
            return
        await self.sub_probe(
            self.phase_step("revoke", host),
            canonical(host),
            "post-revoke re-prompt",
            True,
            EXPECT_NOT0,
        )

    async def run_fail_closed_phase(self) -> None:
        """With a request held pending, the decider disconnects:
        the hold must not succeed -- it answers deny at the consent
        timeout, and nothing the daemon does after that reads as
        an allow."""
        host = FRESH["failclosed"]
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("fail-closed hold", host, task)
            return
        await self.decider.close()
        rc = await task
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
        )
        status, detail = fail_closed_outcome(rc)
        self.record(
            Result(
                "disconnect -> auto-deny",
                host,
                True,
                EXPECT_NOT0,
                "-",
                "auto-denied",
                rc,
                status,
                detail=detail,
            )
        )
        await self.audit_row_check(host, DECISION_EXPIRED)
        await self.decider.connect()
        await self.decider.settle()

    async def run_scope_phase(self) -> None:
        """Cross-workspace authority: workspace B's hold is decided
        by B's decider; the same decide posted from A's decider
        answers 404 and leaves B's hold pending. (Frame visibility
        is instance-wide by design -- the events channel fans out
        to every subscriber -- so the scored invariant is
        authority, not frames.)"""
        wid_b = await self.create_workspace(
            f"scope-{uuid.uuid4().hex[:6]}", "interactive", []
        )
        self.extra_ws.append(wid_b)
        d_b = RawDecider(self.daemon, wid_b)
        await d_b.connect()
        await d_b.settle()
        host = FRESH["scope_b"]
        task = asyncio.create_task(self.probe_ws(wid_b, host))
        rid = await d_b.wait_for(canonical(host), REQUEST_WAIT_S)
        if rid is None:
            task.cancel()
            await d_b.close()
            self.record(
                Result(
                    "scope hold B",
                    host,
                    True,
                    "",
                    "-",
                    "no-request(!)",
                    None,
                    MISMATCH,
                    "expected a request, none arrived (phase bring-up)",
                )
            )
            return
        seen_by_a = self.decider.rows_for(canonical(host)) != []
        if seen_by_a:
            print(
                "     (note: A's decider saw B's request frame -- the "
                "events channel is instance-wide by design, #8; authority "
                "is what scores below)"
            )
        cross_status, _body = await self.decider.verdict(
            rid, "deny", DURATION_ONCE
        )
        still_held = await d_b.wait_resolution(rid, 3.0) is None
        await d_b.verdict(rid, "deny", DURATION_ONCE)
        rc = await task
        await d_b.close()
        status, detail = scope_outcome(cross_status, still_held, rc)
        self.record(
            Result(
                "cross-workspace decide 404s",
                host,
                True,
                EXPECT_REFUSED,
                "deny(B)",
                "resolved",
                rc,
                status,
                detail=detail,
            )
        )

    async def run_audit_phase(self) -> None:
        """Audit distinction: a no-response hold expires (row
        ``expired``), a human deny lands as ``denied`` with
        ``decided_by`` set -- over REST, the operator's own read
        path."""
        await self.audit_expired_case()
        if not self.abort:
            await self.audit_denied_case()

    async def audit_expired_case(self) -> None:
        """The timeout half."""
        host = FRESH["audit_e"]
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("audit hold (expired)", host, task)
            return
        decision = await self.decider.wait_resolution(
            rid, self.args.consent_timeout + 25.0
        )
        rc = await task
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
        )
        status, detail = audit_resolution_outcome(decision, rc, "expired")
        self.record(
            Result(
                "expired (no response)",
                host,
                True,
                EXPECT_NOT0,
                "none",
                str(decision),
                rc,
                status,
                detail=detail,
            )
        )

    async def audit_denied_case(self) -> None:
        """The human-deny half."""
        host = FRESH["audit_d"]
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("audit hold (denied)", host, task)
            return
        await self.decider.verdict(rid, "deny", DURATION_ONCE)
        decision = await self.decider.wait_resolution(rid, 15.0)
        rc = await task
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
        )
        status, detail = audit_resolution_outcome(decision, rc, "denied")
        self.record(
            Result(
                "denied (human verdict)",
                host,
                True,
                EXPECT_REFUSED,
                "deny",
                str(decision),
                rc,
                status,
                detail=detail or "",
            )
        )
        await self.audit_row_check(host, DECISION_DENIED)

    async def audit_row_check(self, host: str, want: str) -> None:
        """The REST read of one row: decision label, and
        ``decided_by`` set only for human verdicts."""
        rows = await self.decider.rows(want)
        match = next(
            (
                r
                for r in rows
                if canonical(r.get("dest_host") or "") == canonical(host)
            ),
            None,
        )
        if match is None:
            self.record(
                Result(
                    "audit row",
                    host,
                    True,
                    "",
                    "-",
                    "missing",
                    None,
                    MISMATCH,
                    f"timeout audited as missing (no {want} row over REST)",
                )
            )
            return
        self.audit_by_score(host, want, match)

    def audit_by_score(self, host: str, want: str, match: dict) -> None:
        """The decided_by half of the row check."""
        by = match.get("decided_by")
        human = want == DECISION_DENIED
        status = PASS if (by == "token") == human else MISMATCH
        detail = "" if status == PASS else f"decided_by={by!r} on a {want} row"
        self.record(
            Result(
                "audit row",
                host,
                True,
                "",
                want,
                str(match.get("decision")),
                None,
                status,
                detail=detail,
            )
        )

    async def run_allow_phase(self) -> None:
        """Allow mode (default-permit): off-list connects with no
        request (nothing holds in allow mode), the rows record the
        policy allow, and a decider registered on the workspace
        stays idle."""
        wid = await self.create_workspace(
            f"allow-{uuid.uuid4().hex[:6]}", "allow", []
        )
        self.extra_ws.append(wid)
        d = RawDecider(self.daemon, wid)
        await d.connect()
        await d.settle()
        host = FRESH["allow_off"]
        rc = await self.probe_ws(wid, host)
        idle = await d.wait_no_request(host, NO_REQUEST_WINDOW_S)
        status, detail = allow_mode_outcome(rc, idle)
        self.record(
            Result(
                "allow-mode off-list connects",
                host,
                True,
                EXPECT_RELEASED,
                "-",
                "no-req",
                rc,
                status,
                detail=detail,
            )
        )
        rows = await d.rows(DECISION_ALLOWED)
        named = [
            r
            for r in rows
            if canonical(r.get("dest_host") or "") == canonical(host)
        ]
        self.allow_row_score(host, named)
        await d.close()

    def allow_row_score(self, host: str, named: list[dict]) -> None:
        """The policy-allow row: present, decided_by NULL."""
        if not named:
            self.record(
                Result(
                    "allow-mode row",
                    host,
                    True,
                    "",
                    "-",
                    "missing",
                    None,
                    FINDING,
                    "no policy-allow row recorded",
                )
            )
            return
        by = named[0].get("decided_by")
        status = PASS if by is None else MISMATCH
        detail = "" if by is None else f"policy row carries decided_by={by!r}"
        self.record(
            Result(
                "allow-mode row",
                host,
                True,
                "",
                "-",
                "present",
                None,
                status,
                detail=detail,
            )
        )

    async def run_restart_phase(self) -> None:
        """Verdict semantics across a workspace stop/start:
        forever verdicts survive (their rows replay at the next
        attach -- an allow reconnects, a deny re-denies), and a
        tilrestart verdict is reaped with the per-VM table."""
        grants = [
            (FRESH["restart_forever_allow"], "allow", DURATION_FOREVER),
            (FRESH["restart_forever_deny"], "deny", DURATION_FOREVER),
            (FRESH["restart_tilrestart"], "allow", DURATION_TILRESTART),
        ]
        for host, action, duration in grants:
            rid, task = await self.fresh_hold(host)
            if rid is None:
                self.hold_failed("restart grant", host, task)
                return
            await self.decider.verdict(rid, action, duration)
            await self.decider.wait_resolution(rid, 20.0)
            await task
        await self.sub_probe(
            self.phase_step("restart", FRESH["restart_tilrestart"]),
            canonical(FRESH["restart_tilrestart"]),
            "tilrestart pre-restart",
            False,
            EXPECT_RELEASED,
        )
        print("     (stopping + starting the workspace ...)")
        await self.client.post(f"/api/v1/workspaces/{self.ws_id}/stop")
        response = await self.client.post(
            f"/api/v1/workspaces/{self.ws_id}/start"
        )
        assert response.status_code == 200, response.text
        self.model.restart()
        await self.restart_probes()

    async def restart_probes(self) -> None:
        """The post-restart matrix."""
        forever_allow = FRESH["restart_forever_allow"]
        forever_deny = FRESH["restart_forever_deny"]
        tilrestart = FRESH["restart_tilrestart"]
        await self.sub_probe(
            self.phase_step("restart", forever_allow),
            canonical(forever_allow),
            "forever-allow survives",
            False,
            EXPECT_RELEASED,
        )
        await self.restart_deny_probe(forever_deny)
        await self.sub_probe(
            self.phase_step("restart", tilrestart),
            canonical(tilrestart),
            "tilrestart reaped",
            True,
            EXPECT_NOT0,
        )

    async def restart_deny_probe(self, host: str) -> None:
        """A forever deny after restart: refused fast (the resolver
        NXDOMAINs the name), no request."""
        task = asyncio.create_task(self.probe(host))
        intruder = await self.decider.wait_no_request(
            canonical(host), NO_REQUEST_WINDOW_S
        )
        rc = await task
        status, detail = classify_conn(EXPECT_REFUSED, rc)
        if intruder is not None:
            status, detail = (
                FINDING,
                ("expected no request (covered:verdict), one arrived"),
            )
        self.record(
            Result(
                "forever-deny survives",
                host,
                True,
                EXPECT_REFUSED,
                "-",
                "no-req",
                rc,
                status,
                detail=detail,
            )
        )

    async def run_host_scope_phase(self) -> None:
        """nginx-style host scopes (#2377) under static mode: a
        bare spec is apex-only, a leading ``.`` includes subdomains,
        ``*.`` matches subdomains only -- all enforced at the
        resolver gate, so the names themselves carry the
        determinism.

        Each sub-case gets its own workspace: the resolver's learned
        name→IP pins survive a policy swap, so a name allowed under
        one scope keeps connecting after the scope tightens (the pin
        is per-name, not per-spec)."""
        await self.host_scope_case(
            "inclusive (.debian.org)",
            [".debian.org"],
            [
                (FRESH["scope_incl_sub"], True),
                (FRESH["scope_incl_apex"], True),
            ],
        )
        await self.host_scope_case(
            "exact (gnu.org)",
            [FRESH["scope_exact_on"]],
            [
                (FRESH["scope_exact_on"], True),
                (FRESH["scope_exact_off"], False),
            ],
        )
        await self.host_scope_case(
            "subdomains (*.gnu.org)",
            [f"*.{FRESH['scope_star_off']}"],
            [(FRESH["scope_star_on"], True), (FRESH["scope_star_off"], False)],
        )

    async def host_scope_case(
        self,
        label: str,
        allow: list[str],
        cases: list[tuple[str, bool]],
    ) -> None:
        """One host-scope sub-case in its own workspace."""
        wid = await self.create_workspace(
            f"scope-h-{uuid.uuid4().hex[:6]}", "static", allow
        )
        self.extra_ws.append(wid)
        await self.scope_matrix(wid, label, cases)

    async def run_port_scope_phase(self) -> None:
        """Port-scoped allows (``host:port``): the resolver gate
        learns the name's addresses pinned to the allowed port
        only -- the other port's SYN finds no pin and the static
        chain drops it.

        Each spec gets its own workspace (same reason as host-scope:
        learned pins survive a policy swap)."""
        await self.port_scope_case(
            FRESH["port80_host"], 80, [(80, True), (443, False)]
        )
        await self.port_scope_case(
            FRESH["port443_host"], 443, [(443, True), (80, False)]
        )

    async def port_scope_case(
        self,
        host: str,
        spec_port: int,
        cases: list[tuple[int, bool]],
    ) -> None:
        """One port-scope sub-case in its own workspace; a warm-up
        probe wires the static chain before the scored probes."""
        spec = f"{host}:{spec_port}"
        wid = await self.create_workspace(
            f"scope-p-{uuid.uuid4().hex[:6]}", "static", [spec]
        )
        self.extra_ws.append(wid)
        for port, allowed in cases:
            await self.port_matrix(wid, spec, port, allowed)

    async def scope_matrix(
        self, wid: str, label: str, cases: list[tuple[str, bool]]
    ) -> None:
        """One static-policy matrix: on-scope names connect,
        off-scope names are NXDOMAIN'd fast with a denied row."""
        for host, allowed in cases:
            rc = await self.probe_ws(wid, host)
            await self.scope_case_score(wid, label, host, allowed, rc)

    async def port_matrix(
        self, wid: str, spec: str, port: int, allowed: bool
    ) -> None:
        """One port-scoped probe: the allowed port connects, the
        other finds no pin and hangs to its probe timeout."""
        host = spec.rsplit(":", 1)[0]
        rc = await self.probe_ws(wid, host, port)
        expect = EXPECT_RELEASED if allowed else EXPECT_NOT0
        status, detail = classify_conn(expect, rc)
        if status != PASS:
            detail = f"port scope: {spec} on :{port} -> {exit_label(rc)}"
        self.record(
            Result(
                f"port-scope {spec} :{port}",
                host,
                True,
                expect,
                "-",
                "static",
                rc,
                status,
                detail=detail,
            )
        )

    async def scope_case_score(
        self, wid: str, label: str, host: str, allowed: bool, rc: int | None
    ) -> None:
        """Score one host-scope case, with the audit row as the
        DNS-level marker for denials."""
        expect = EXPECT_RELEASED if allowed else EXPECT_REFUSED
        status, detail = classify_conn(expect, rc)
        if not allowed and status == PASS:
            return await self.scope_denial_row(wid, label, host, rc)
        if status != PASS:
            detail = f"host scope: {label} -> {host} {exit_label(rc)}"
        self.record(
            Result(
                f"host-scope {label}",
                host,
                True,
                expect,
                "-",
                "static",
                rc,
                status,
                detail=detail,
            )
        )

    async def scope_denial_row(
        self, wid: str, label: str, host: str, rc: int | None
    ) -> None:
        """An off-scope denial must carry a ``denied`` policy row
        (the resolver gate's write) with ``decided_by`` NULL."""
        path = f"/api/v1/workspaces/{wid}/egress/requests?decision=denied"
        rows = (await self.client.get(path)).json()
        named = [
            r
            for r in rows
            if canonical(r.get("dest_host") or "") == canonical(host)
        ]
        if not named:
            self.record(
                Result(
                    f"host-scope {label}",
                    host,
                    True,
                    EXPECT_REFUSED,
                    "-",
                    "static",
                    rc,
                    MISMATCH,
                    "static denial left no audit row",
                )
            )
            return
        self.row_by_score(f"host-scope {label}", host, named[0], "static+row")

    def row_by_score(
        self, label: str, host: str, row: dict, server: str
    ) -> None:
        """A policy row's decided_by must be NULL (no human)."""
        by = row.get("decided_by")
        status = PASS if by is None else MISMATCH
        detail = "" if by is None else f"policy row carries decided_by={by!r}"
        self.record(
            Result(
                label,
                host,
                True,
                EXPECT_REFUSED,
                "-",
                server,
                None,
                status,
                detail=detail,
            )
        )

    async def run_coresident_phase(self) -> None:
        """Two hostnames on one IP (#292): a verdict on host A must
        not leak to host B, and vice-versa.  Controlled DNS maps
        both names to the same address; the enforcement layer must
        key by name, not IP."""
        if self.dns is None:
            print("  (skipped: co-resident canaries need controlled DNS)")
            return
        host_a = FRESH["coresident_a"]
        host_b = FRESH["coresident_b"]
        if not await self.coresident_allow_a(host_a):
            return
        if not await self.coresident_deny_b(host_b):
            return
        await self.coresident_verify(
            "re-A",
            host_a,
            canonical(host_a),
            EXPECT_RELEASED,
            "co-resident deny leaked to the allowed hostname",
        )
        if not self.abort:
            await self.coresident_verify(
                "re-B",
                host_b,
                canonical(host_b),
                EXPECT_REFUSED,
                "co-resident allow leaked to the denied hostname",
            )

    async def coresident_allow_a(self, host: str) -> bool:
        """Allow host A (5m) and score the connection."""
        rid, task = await self.fresh_hold(host)
        if rid is None:
            self.hold_failed("coresident hold A", host, task)
            return False
        await self.decider.verdict(rid, "allow", DURATION_5M)
        await self.decider.wait_resolution(rid, 20.0)
        self.model.record(
            canonical(host), DECISION_ALLOWED, DURATION_5M, time.time()
        )
        rc = await task
        status, detail = classify_conn(EXPECT_RELEASED, rc)
        self.record(
            Result(
                "coresident allow A",
                host,
                True,
                EXPECT_RELEASED,
                "allow/5m",
                "resolved",
                rc,
                status,
                detail=detail,
            )
        )
        return not self.abort

    async def coresident_deny_b(self, host: str) -> bool:
        """Probe host B (same IP, different name) — it must gate
        independently and accept a deny."""
        rid, task = await self.fresh_hold(host)
        if rid is None:
            rc = await task
            self.record(
                Result(
                    "coresident gate B",
                    host,
                    True,
                    EXPECT_REFUSED,
                    "-",
                    "no-request(!)",
                    rc,
                    MISMATCH,
                    "co-resident allow leaked to a different hostname",
                )
            )
            return False
        await self.decider.verdict(rid, "deny", DURATION_5M)
        await self.decider.wait_resolution(rid, 20.0)
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_5M, time.time()
        )
        rc = await task
        status, detail = classify_conn(EXPECT_REFUSED, rc)
        self.record(
            Result(
                "coresident deny B",
                host,
                True,
                EXPECT_REFUSED,
                "deny/5m",
                "resolved",
                rc,
                status,
                detail=detail,
            )
        )
        return not self.abort

    async def coresident_verify(
        self,
        tag: str,
        host: str,
        canon: str,
        expect_conn: str,
        leak_detail: str,
    ) -> None:
        """Re-probe a canary host: its own verdict must still hold."""
        label = f"coresident {tag}"
        task = asyncio.create_task(self.probe(host))
        intruder = await self.decider.wait_no_request(
            canon, NO_REQUEST_WINDOW_S
        )
        rc = await task
        if intruder is not None:
            self.record(
                Result(
                    label,
                    host,
                    False,
                    expect_conn,
                    "-",
                    "request(!)",
                    rc,
                    MISMATCH,
                    leak_detail,
                )
            )
            return
        status, detail = classify_conn(expect_conn, rc)
        self.record(
            Result(
                label,
                host,
                False,
                expect_conn,
                "-",
                "no-req",
                rc,
                status,
                detail=detail,
            )
        )

    async def run_no_decider_phase(self) -> None:
        """With no decider registered, interactive mode denies
        off-list fast (the no-hold static answer) while the
        allow-list still connects -- the fail-closed posture."""
        await self.decider.close()
        await asyncio.sleep(3.0)
        rc = await self.probe(ALLOW_LIST[0])
        status, detail = classify_conn(EXPECT_RELEASED, rc)
        if status != PASS and not detail:
            detail = "allow-list host should connect"
        self.record(
            Result(
                "no decider: allow-list connects",
                ALLOW_LIST[0],
                True,
                EXPECT_RELEASED,
                "-",
                "allowed",
                rc,
                status,
                detail=detail,
            )
        )
        for host in FRESH["static_off"]:
            if self.abort:
                return
            await self.no_decider_case(host)

    async def no_decider_case(self, host: str) -> None:
        """One off-list probe with no decider anywhere."""
        rc = await self.probe(host)
        self.model.record(
            canonical(host), DECISION_DENIED, DURATION_ONCE, time.time()
        )
        if rc == 0:
            status, detail = MISMATCH, "off-list succeeded with no decider"
        elif rc is None or rc == 124:
            status, detail = MISMATCH, "off-list hung with no decider"
        else:
            status, detail = (
                PASS,
                (f"denied fast with no decider ({exit_label(rc)})"),
            )
        self.record(
            Result(
                "no decider: off-list denied",
                host,
                True,
                EXPECT_REFUSED,
                "-",
                "denied",
                rc,
                status,
                detail=detail,
            )
        )

    # -- run / summary -------------------------------------------------------

    async def run(self) -> int:
        """Setup, the fuzz loop, the phases, teardown, summary.

        A setup failure still tears down (#307): the daemon and its
        workspaces are booted partway through setup, and skipping
        teardown there strands them on the host."""
        try:
            await self.setup()
        except SystemExit:
            await self.teardown()
            raise
        except Exception as exc:  # noqa: BLE001
            self.setup_failure(exc)
            await self.teardown()
            self.print_summary(self.abort)
            return 1
        try:
            await self.run_plan()
            await self.run_phases()
        except Exception as exc:  # noqa: BLE001
            self.record(
                Result(
                    "run",
                    "(run)",
                    True,
                    "",
                    "?",
                    "error",
                    None,
                    MISMATCH,
                    detail=f"step raised: uncaught {exc!r}",
                )
            )
        finally:
            await self.teardown()
        self.print_summary(self.abort)
        return 1 if self.summary.mismatches else 0

    def setup_failure(self, exc: Exception) -> None:
        """A setup crash, recorded so the summary shows why."""
        self.record(
            Result(
                "setup",
                "(setup)",
                True,
                "",
                "?",
                "error",
                None,
                MISMATCH,
                detail=f"setup raised: {exc!r}",
            )
        )

    async def run_plan(self) -> None:
        """The fuzz loop, one guarded step at a time."""
        plan = gen_plan(self.args.seed, self.args.count)
        print(
            f"\n--- fuzz loop: {self.args.count} steps "
            f"(seed {self.args.seed}) ---"
        )
        for step in plan:
            if self.abort:
                return
            await self.guarded_step(step)

    async def guarded_step(self, step: Step) -> None:
        """One step, with its exception recorded instead of
        aborting the run with a traceback."""
        try:
            await self.run_step(step)
        except Exception as exc:  # noqa: BLE001
            self.record(
                Result(
                    "fuzz step",
                    step.host,
                    True,
                    "",
                    "?",
                    "error",
                    None,
                    MISMATCH,
                    detail=f"step raised: {exc!r}",
                )
            )

    def print_summary(self, stopped: bool) -> None:
        """The tally, the outcome histogram, and the mismatches."""
        s = self.summary
        print(
            f"\n=== {s.total} scored: {s.passed} pass, "
            f"{s.findings} findings, {s.mismatches} mismatches ==="
        )
        if stopped:
            print("(stopped early: a mismatch halts without --continue)")
        self.print_outcome_tally()
        for row in s.rows:
            if row.status == MISMATCH:
                print(f"  MISMATCH [{row.outcome}] {row.label}: {row.detail}")

    def print_outcome_tally(self) -> None:
        """The histogram of non-PASS outcome names."""
        names: dict[str, int] = {}
        for row in self.summary.rows:
            if row.outcome:
                names[row.outcome] = names.get(row.outcome, 0) + 1
        for name, count in sorted(names.items()):
            desc = OUTCOME_NAMES.get(name, "(unmapped outcome)")
            print(f"  {name:<32} x{count}  {desc}")


def snapshot_label(has_a: bool, has_b: bool) -> str:
    """The snapshot row's B=/A= presence summary."""
    b = "y" if has_b else "n"
    a = "y" if has_a else "n"
    return f"B={b},A={a}"


def scope_outcome(
    cross_status: int, still_held: bool, rc: int | None
) -> tuple[str, str]:
    """Score the cross-workspace authority observation."""
    if cross_status != 404:
        return MISMATCH, (
            f"cross-workspace decide was accepted ({cross_status})"
        )
    if not still_held:
        return MISMATCH, "the foreign hold resolved"
    if rc == 0:
        return MISMATCH, "a deny let the connection through"
    if rc is None:
        return FINDING, "expected refusal, probe hung"
    return PASS, ""


def audit_resolution_outcome(
    decision: str | None, rc: int | None, want: str
) -> tuple[str, str]:
    """Score one audit-distinction half against its resolved
    frame."""
    if decision != want:
        return MISMATCH, audit_mislabel(decision, want)
    return audit_conn_outcome(rc, want)


def audit_mislabel(decision: str | None, want: str) -> str:
    """The mislabeled-resolution detail."""
    if want == DECISION_EXPIRED:
        return f"timeout audited as {decision!r}, not {want!r}"
    return f"human deny audited as {decision!r}, not {want!r}"


def audit_conn_outcome(rc: int | None, want: str) -> tuple[str, str]:
    """The resolution was labeled right; the connection must land
    as the label says."""
    if rc == 0:
        if want == DECISION_EXPIRED:
            return MISMATCH, "a no-response verdict succeeded"
        return MISMATCH, "a deny let the connection through"
    return PASS, ""


def allow_mode_outcome(rc: int | None, idle: str | None) -> tuple[str, str]:
    """Score the allow-mode probe."""
    if rc != 0:
        return MISMATCH, "allow-mode off-list was refused (or hung)"
    if idle is not None:
        return FINDING, "a request surfaced in allow mode"
    return PASS, ""


def build_parser() -> argparse.ArgumentParser:
    """The CLI."""
    p = argparse.ArgumentParser(
        description="Interactive-egress consent fuzz harness (#286)"
    )
    p.add_argument("--count", type=int, default=50, help="fuzz iterations")
    p.add_argument("--seed", type=int, default=None, help="PRNG seed")
    p.add_argument(
        "--consent-timeout",
        type=int,
        default=8,
        help="the daemon's consent timeout, seconds "
        "(self-boot sets it; with --url, match the daemon's "
        "MSKSD_EGRESS_CONSENT_TIMEOUT_S so timeout waits are sized)",
    )
    p.add_argument("--url", default=None, help="attach to this daemon URL")
    p.add_argument("--token", default=None, help="its bootstrap token")
    p.add_argument("--cafile", default=None, help="its CA file")
    p.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help="keep going past a mismatch; summarize at the end",
    )
    p.add_argument(
        "--no-retries",
        dest="retries",
        action="store_false",
        help="skip within/exceeding reconnect probes after a verdict",
    )
    p.add_argument(
        "--no-lifecycle",
        dest="lifecycle",
        action="store_false",
        help="skip the within/exceeding 5m lifecycle phase (~12 min)",
    )
    p.add_argument(
        "--no-fail-closed",
        dest="fail_closed",
        action="store_false",
        help="skip the decider-disconnects-mid-hold phase",
    )
    p.add_argument(
        "--no-multi-decider",
        dest="multi_decider",
        action="store_false",
        help="skip the multiple-deciders (first-wins + sync) phase",
    )
    p.add_argument(
        "--no-snapshot",
        dest="snapshot",
        action="store_false",
        help="skip the reconnect snapshot-replay phase",
    )
    p.add_argument(
        "--no-fanout",
        dest="fanout",
        action="store_false",
        help="skip the N-way fan-out + same-host dedupe phase",
    )
    p.add_argument(
        "--no-revoke",
        dest="revoke",
        action="store_false",
        help="skip the revoke-verdict phase",
    )
    p.add_argument(
        "--no-decider-scope",
        dest="decider_scope",
        action="store_false",
        help="skip the cross-workspace authority phase",
    )
    p.add_argument(
        "--no-audit-distinction",
        dest="audit_distinction",
        action="store_false",
        help="skip the expired-vs-denied audit-distinction phase",
    )
    p.add_argument(
        "--no-allow-phase",
        dest="allow_phase",
        action="store_false",
        help="skip the allow-egress-mode phase",
    )
    p.add_argument(
        "--no-restart-phase",
        dest="restart_phase",
        action="store_false",
        help="skip the restart verdict-semantics phase "
        "(forever survives, tilrestart reaped)",
    )
    p.add_argument(
        "--no-host-scope",
        dest="host_scope",
        action="store_false",
        help="skip the host-scope (exact/inclusive/subdomains) phase",
    )
    p.add_argument(
        "--no-port-scope",
        dest="port_scope",
        action="store_false",
        help="skip the port-scope (host:443 vs :80) phase",
    )
    p.add_argument(
        "--no-coresident",
        dest="coresident_phase",
        action="store_false",
        help="skip the co-resident per-IP canary phase",
    )
    p.add_argument(
        "--no-static-phase",
        dest="static_phase",
        action="store_false",
        help="skip the no-decider static-denial phase",
    )
    p.add_argument(
        "--expiry-wait",
        type=float,
        default=320.0,
        help="seconds to wait past a 5m verdict before the exceeding "
        "probe (default 320)",
    )
    p.add_argument(
        "--keep-workspace",
        dest="keep_workspace",
        action="store_true",
        help="do not delete the workspaces on exit",
    )
    # The crash-path watchdog's own invocation (#307): the fuzzer
    # respawns this script detached with these two flags.
    p.add_argument("--reap-for", type=int, help=argparse.SUPPRESS)
    p.add_argument("--state-dir", help=argparse.SUPPRESS)
    return p


def main() -> int:
    """Entry point: parse, run, exit (or serve as the detached
    crash-path watchdog, #307)."""
    args = build_parser().parse_args()
    if args.reap_for is not None:
        assert args.state_dir is not None
        return run_reaper(args.reap_for, Path(args.state_dir))
    if args.seed is None:
        args.seed = random.randrange(2**32)
    harness = Harness(args)
    try:
        return asyncio.run(harness.run())
    except KeyboardInterrupt:
        print("\ninterrupted")
        with contextlib.suppress(Exception):
            asyncio.run(harness.teardown())
        return 130


if __name__ == "__main__":
    sys.exit(main())

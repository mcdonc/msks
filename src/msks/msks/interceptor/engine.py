"""The interceptor addon: dispatch, splice, leaf, rewrite (#199).

One addon instance rides the embedded mitmproxy master and answers
three hooks; every decision keys off the **accepting listener**,
recovered from the connection's proxy mode (``transparent@<tap
ip>:<port>`` — one mode spec per armed workspace), so a guest that
spoofs another workspace's source address still lands on its own
tap's entries. Transparent mode rewrites ``client.sockname`` to the
original destination, which is why the listener address must come
from the mode spec instead.

The integration facts the #194 spike paid for in debugging rounds:
the addon registers **before** mitmproxy's default addons (hook
dispatch follows registration order, and the default TLS context
lands first otherwise); the ``tls_start_client`` hook calls
``set_accept_state()`` itself; and the client-facing context takes
mitmproxy's own ``_default_ciphers()`` list — an empty tuple is a
hard OpenSSL error, and the FIPS posture keeps cipher overrides in
settings for the certification effort, never in code.

Fail-closed on the swap path: a secret the store cannot serve (or a
sentinel whose row cannot be read) answers the guest with a local
502 rather than forwarding a request that still carries the
sentinel.

The consent tier (#452): while the interceptor is armed, the nft
prerouting redirect owns the guest's TCP flows to ports 80/443 —
they become host input to this listener and never reach the
forward chain's NFQUEUE gate. The hooks below run the web-egress
gate (:mod:`msks.interceptor.egress`) before anything else moves:

- ``tls_clienthello`` gates every TLS connection on its SNI (the
  cryptographic destination name). An allowed connection takes
  today's path (decrypt when a placeholder covers the SNI, relay
  undecrypted otherwise). A denied connection takes the decrypt
  path with a deny marker — the guest's client completes its
  handshake against the workspace CA and the request hook answers
  locally, so no byte forwards. A pinned client fails the
  handshake instead; fail-closed either way.
- ``request`` gates plain HTTP (no ClientHello ever gated it) on
  the Host header, and answers denied requests — including the
  marked TLS ones — with a local 403 whose body names what happened
  (#472): a verdict refused the destination, its decision is still
  pending (the duplicate rule refused this newcomer's re-ask), no
  decider answered in time, or the request ended without a decision.
  TLS connections skip the re-gate: the hello already gated the
  connection, and a ``once`` verdict must not re-prompt per request
  on a kept-alive connection — except a duplicate-race denial, which
  is not final and re-gates until the destination's decision lands.
"""

import logging
import weakref

from mitmproxy import http, tls
from mitmproxy.addons.tlsconfig import _default_ciphers
from mitmproxy.net import tls as net_tls
from mitmproxy.net.http import url
from OpenSSL import SSL

logger = logging.getLogger(__name__)


def host_matches(host: str, dests: tuple[str, ...]) -> bool:
    """The allowlist grammar (#194): an exact hostname, or a
    label-anchored suffix — ``.example.com`` matches every host
    under example.com and never ``notexample.com``. Names compare
    case-insensitively (DNS semantics), both sides — the mint path
    lowercases, and the wire does not have to."""
    host = host.lower()
    for dest in dests:
        dest = dest.lower()
        if host == dest or (dest.startswith(".") and host.endswith(dest)):
            return True
    return False


def destination_name(flow: http.HTTPFlow) -> str:
    """The name the guest aimed this request at: the TLS handshake's
    SNI where the connection is TLS (cryptographic — the handshake
    names the server), else the Host header (plain HTTP's only
    name); with no Host header at all, the address form mitmproxy
    derived — which never matches the name grammar."""
    if flow.client_conn.sni:
        return flow.client_conn.sni
    return flow.request.pretty_host


def sentinel_in_headers(sentinel: str, flow: http.HTTPFlow) -> bool:
    """Whether the sentinel rides any header field — name or value."""
    needle = sentinel.encode()
    return any(
        needle in name or needle in value
        for name, value in flow.request.headers.fields
    )


def sentinel_in_query(sentinel: str, flow: http.HTTPFlow) -> bool:
    """Whether the sentinel rides any query pair — key or value."""
    return any(
        sentinel in key or sentinel in value
        for key, value in flow.request.query.items(multi=True)
    )


def carried_entry(entries: dict, flow: http.HTTPFlow):
    """The first entry whose sentinel rides this request — in any
    header or query field — or None."""
    for sentinel, entry in entries.items():
        if sentinel_in_headers(sentinel, flow) or sentinel_in_query(
            sentinel, flow
        ):
            return entry
    return None


def rewrite_request(flow: http.HTTPFlow, sentinel: str, secret: str) -> None:
    """Swap the sentinel for the secret everywhere it can ride:
    every header field (duplicates and order preserved through the
    raw fields) and every query pair (the pair-list assignment
    URL-encodes properly — a plain string assignment is not accepted
    here, and duplicate keys survive)."""
    needle, replacement = sentinel.encode(), secret.encode()
    flow.request.headers.fields = tuple(
        (name.replace(needle, replacement), value.replace(needle, replacement))
        for name, value in flow.request.headers.fields
    )
    flow.request.query = [
        (key.replace(sentinel, secret), value.replace(sentinel, secret))
        for key, value in flow.request.query.items(multi=True)
    ]


def pin_destination(flow: http.HTTPFlow, host: str) -> None:
    """Bind the swapped request's routing to the matched name.

    TLS connections already carry the binding in their handshake:
    the upstream dial was verified for the SNI. What a verified
    connection does **not** pin is the request's Host header — a
    guest can send ``SNI: api.example.com`` with ``Host:
    attacker-vhost`` toward shared fronting infrastructure, and
    the infrastructure routes by Host. The swap pins the header to
    the matched name, so the request lands on the allowlisted
    service's own vhost, and a fronting guest's request is
    rerouted rather than its secret leaked.

    Plain HTTP carries no SNI to bind anything: the swap pins the
    upstream dial to the matched name instead — the daemon
    resolves it, and the secret only reaches the server the
    allowlist names. (The TLS branch's ``hostport`` carries the
    connection's real port whenever it is not the scheme default —
    TLS toward a non-443 port is redirected like any other.)"""
    if flow.client_conn.tls:
        flow.request.host_header = url.hostport(
            flow.request.scheme, host, flow.request.port
        )
        return
    flow.request.host = host


async def fetch_secret(owner, entry) -> str | None:
    """The swap's two halves: the sentinel's row must be live and
    the store must serve the value. None means pass through
    (revoked or expired); a raising store surfaces to the caller."""
    if not await owner.sentinel_live(entry.sentinel):
        return None
    return await owner.secret_for(entry)


def original_destination(client) -> tuple[str, int]:
    """``(address, port)`` the guest aimed at — transparent mode
    rewrites ``sockname`` to the nft redirect's original
    destination, which is what the egress gate keys its address
    half on."""
    sock = getattr(client, "sockname", None) or ("", 0)
    return sock[0], sock[1]


# The refusal bodies, by the verdict's reason (#472): the guest
# reads one line in its shell, and the line must say what happened
# — a flow the duplicate rule refused is not a verdict on its
# destination, and a prompt nobody answered refused no destination
# either.
BODY_DENIED = (
    "msks: egress consent denied this destination; "
    "the request was not forwarded."
)
BODY_PENDING = (
    "msks: egress consent for this destination is already pending; "
    "answer the consent prompt; "
    "the request was not forwarded."
)
BODY_UNANSWERED = (
    "msks: no decider answered the egress consent request "
    "(no decider was connected, or the request expired undecided); "
    "the request was not forwarded."
)
BODY_UNDECIDED = (
    "msks: the egress consent request ended without a decision; "
    "the request was not forwarded."
)


def refusal_body(reason: str) -> str:
    """The 403 body one denial reason answers with: a destination
    an actual verdict or the static posture refused, a destination
    whose decision is still pending (the duplicate rule refused
    this newcomer while the first hold waits), a prompt no decider
    answered (none registered, or the hold expired undecided), or
    a request that ended without a decision (the gate raised, the
    workspace vanished or stopped, the mode switched mid-hold) —
    never a verdict on the destination."""
    if reason == "duplicate":
        return BODY_PENDING
    if reason in ("no_decider", "timeout"):
        return BODY_UNANSWERED
    if reason in ("error", "gone", "stopped", "shutdown", "mode switch"):
        return BODY_UNDECIDED
    return BODY_DENIED


def refuse(flow: http.HTTPFlow, reason: str) -> None:
    """Answer a consent-denied request locally: nothing forwards,
    and the sentinel a denied request may carry never rides the
    wire (the swap path's fail-closed rule). The body names what
    happened, per ``reason``."""
    flow.response = http.Response.make(
        403,
        refusal_body(reason),
        {"content-type": "text/plain"},
    )


def fail_closed(flow: http.HTTPFlow) -> None:
    """Answer locally instead of forwarding: a swap-path failure must
    not leak the request the sentinel still rides."""
    flow.response = http.Response.make(
        502,
        "msks: the secret service is unavailable; "
        "the request was not forwarded.",
        {"content-type": "text/plain"},
    )


async def swap_or_announce(owner, workspace: str, flow) -> None:
    """The swap tail: a carried sentinel whose own allowlist covers
    the destination swaps; one whose allowlist misses it is the
    off-allowlist sighting — decrypted, unrewritten, announced;
    a sentinelless request passes."""
    entry = carried_entry(owner.entries_for(workspace), flow)
    if entry is None:
        return
    host = destination_name(flow)
    if not host_matches(host, entry.dests):
        await owner.publish_sighting(workspace, entry, host)
        return
    await swap_on_flow(owner, workspace, entry, host, flow)


async def swap_on_flow(owner, workspace, entry, host, flow) -> None:
    """The swap's tail: gate, fetch, rewrite, announce. A store or
    row failure answers the guest locally (fail-closed); a dead
    sentinel passes through decrypted and unrewritten."""
    try:
        secret = await fetch_secret(owner, entry)
    except Exception:  # noqa: BLE001 - named below, fail-closed
        logger.exception(
            "interceptor: swap for %s/%s failed; answered 502",
            workspace,
            entry.name,
        )
        fail_closed(flow)
        return
    if secret is None:
        return  # revoked or expired: decrypted, unrewritten
    rewrite_request(flow, entry.sentinel, secret)
    pin_destination(flow, host)
    await owner.publish_swap(workspace, entry, host)


class InterceptorAddon:
    """The hooks: consent, splice, leaf, rewrite."""

    def __init__(self, owner) -> None:
        # The interceptor manager: dispatch, entries, CAs, events.
        self.owner = owner
        # Connections the hello-time gate denied, by identity: the
        # request hook answers them locally with the denial's
        # reason — the 403 body names what happened (#472). Weak,
        # so a closed connection's marker dies with the connection
        # object.
        self._denied: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        # One connection's own gate answers, per destination —
        # plain HTTP gates per request, and a ``once`` verdict must
        # not re-prompt for the next request on the same kept-alive
        # connection. The cached value is the verdict itself, so
        # the refusal keeps its reason. Weak, so a closed
        # connection's answers die with it.
        self._per_connection: weakref.WeakKeyDictionary = (
            weakref.WeakKeyDictionary()
        )

    def workspace(self, client) -> str | None:
        """The workspace whose tap accepted this connection — the
        listener address off the connection's proxy mode, never the
        client-asserted peer address."""
        return self.owner.workspace_for_tap(
            client.proxy_mode.custom_listen_host
        )

    def denial(self, client) -> str | None:
        """The reason the hello-time gate denied this connection,
        None when it allowed the connection. A stored reason that
        reads falsy answers ``error`` — a denial must never vanish
        from the marker (the request hook treats None as
        allowed)."""
        if client not in self._denied:
            return None
        return self._denied[client] or "error"

    async def gate(self, workspace: str, host: str, port: int, address: str):
        """The egress gate for one web destination (#452), keyed by
        the name the daemon's naming memory binds to the address
        (the gate falls back to the address when nothing binds)."""
        named = self.owner.host_for(workspace, address)
        return await self.owner.web_verdict(
            workspace, host, port, address, named
        )

    async def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        """The consent tier, then the splice tier. The gate first:
        a gated workspace's TLS connections hold here until a
        verdict — the Layer-7 answer to the SYN hold the redirect
        made impossible. Allowed connections splice as before (a
        ClientHello whose SNI no active placeholder of this
        workspace covers is relayed undecrypted,
        ``ignore_connection``; a covered one decrypts for the
        swap). A denied connection is marked and left on the
        decrypt path: the handshake answers from the workspace CA
        and the request hook refuses locally."""
        workspace = self.workspace(data.context.client)
        if workspace is None:
            return
        client = data.context.client
        sni = data.client_hello.sni or ""
        if await self.refuse_client(client, workspace, sni):
            return
        if self.owner.matching_entry(workspace, sni) is None:
            data.ignore_connection = True

    async def refuse_client(self, client, workspace: str, sni: str) -> bool:
        """Gate one connection at its hello: True when the gate
        denied it — the connection takes the decrypt path with a
        deny marker, never a relay; False when allowed (the splice
        tier then decides decrypt or relay)."""
        address, port = original_destination(client)
        verdict = await self.gate(workspace, sni or address, port, address)
        if verdict.allowed:
            return False
        self._denied[client] = verdict.reason
        return True

    def tls_start_client(self, data: tls.TlsData) -> None:
        """Serve the client-facing TLS from this workspace's CA: a
        leaf minted for the connection's SNI, on a context built
        here so the cipher list is mitmproxy's own default (never an
        empty tuple — a hard OpenSSL error). The proxy layer never
        calls ``set_accept_state()``; the hook does."""
        workspace = self.workspace(data.context.client)
        if workspace is None:
            return
        ca = self.owner.ca_for(workspace)
        if ca is None:
            return  # disarmed mid-handshake: the default context fails visibly
        leaf_key, leaf_cert = self.owner.mint_leaf(
            workspace, data.context.client.sni
        )
        context = net_tls.create_client_proxy_context(
            method=net_tls.Method.TLS_SERVER_METHOD,
            min_version=net_tls.Version.TLS1_2,
            max_version=net_tls.Version.UNBOUNDED,
            cipher_list=_default_ciphers(net_tls.Version.TLS1_2),
            ecdh_curve=None,
            chain_file=ca.chain_file,
            request_client_cert=False,
            alpn_select_callback=None,
            extra_chain_certs=(),
            dhparams=None,
        )
        data.ssl_conn = SSL.Connection(context)
        data.ssl_conn.use_certificate(leaf_cert)
        data.ssl_conn.use_privatekey(leaf_key)
        data.ssl_conn.set_accept_state()

    async def request(self, flow: http.HTTPFlow) -> None:
        """The consent tier, then the swap. A connection the hello
        gate denied with a final reason answers locally (no swap
        machinery runs — a sentinel on a denied request never rides
        the wire). Plain HTTP gated here on its Host header — no
        ClientHello gated the connection. TLS connections skip the
        re-gate: the hello gated the connection, and per-request
        re-gating would re-prompt a ``once`` verdict on every
        kept-alive request."""
        workspace = self.workspace(flow.client_conn)
        if workspace is None:
            return
        reason = await self.refused(flow, workspace)
        if reason is not None:
            refuse(flow, reason)
            return
        await swap_or_announce(self.owner, workspace, flow)

    async def refused(self, flow: http.HTTPFlow, workspace: str) -> str | None:
        """The denial reason when this request is consent-denied
        (None when it passes): a connection the hello gate denied
        with a final reason, or a fresh gate whose verdict denies
        now. A duplicate-race denial is not final — the
        destination's pending decision may land at any time — so
        that one re-gates per request until a final answer covers
        the destination (#472)."""
        marked = self.denial(flow.client_conn)
        if marked is not None and marked != "duplicate":
            return marked
        if flow.client_conn.tls and marked is None:
            # Allowed at the hello: the connection's own verdict
            # stands (re-gating would re-prompt a ``once`` verdict
            # per kept-alive request).
            return None
        return await self.gated(flow, workspace)

    async def gated(self, flow: http.HTTPFlow, workspace: str) -> str | None:
        """The egress gate's denial reason for one request (None
        when it allows), cached per connection once the answer is
        final: a ``once`` verdict covers the connection's own
        kept-alive requests, and a different Host gates fresh (a
        different key, lowercased). A duplicate answer caches
        nothing — it is the pending decision's placeholder, so the
        next request re-gates: while the decision waits, the
        engine's dedup answers fast; once it lands, session or
        standing coverage answers for real."""
        client = flow.client_conn
        address, port = original_destination(client)
        host = destination_name(flow)
        key = (host.lower(), port)
        cached = self._per_connection.get(client, {}).get(key)
        if cached is not None:
            return None if cached.allowed else cached.reason
        verdict = await self.gate(workspace, host, port, address)
        if verdict.reason != "duplicate":
            self._per_connection.setdefault(client, {})[key] = verdict
        return None if verdict.allowed else verdict.reason


class LogBridge:
    """mitmproxy's log events into the daemon's logging (the master
    runs without a termlog addon)."""

    LEVELS = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
        "alert": logging.ERROR,
    }

    def log(self, event) -> None:
        level = self.LEVELS.get(event.level, logging.INFO)
        logger.log(level, "mitmproxy: %s", event.msg)

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
"""

import logging

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
    case-insensitively (DNS semantics; the mint path lowercases
    the stored entries)."""
    host = host.lower()
    for dest in dests:
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
    allowlist names."""
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


def fail_closed(flow: http.HTTPFlow) -> None:
    """Answer locally instead of forwarding: a swap-path failure must
    not leak the request the sentinel still rides."""
    flow.response = http.Response.make(
        502,
        "msks: the secret service is unavailable; "
        "the request was not forwarded.",
        {"content-type": "text/plain"},
    )


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
    """The three hooks: splice, leaf, rewrite."""

    def __init__(self, owner) -> None:
        # The interceptor manager: dispatch, entries, CAs, events.
        self.owner = owner

    def workspace(self, client) -> str | None:
        """The workspace whose tap accepted this connection — the
        listener address off the connection's proxy mode, never the
        client-asserted peer address."""
        return self.owner.workspace_for_tap(
            client.proxy_mode.custom_listen_host
        )

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        """The splice tier: a ClientHello whose SNI no active
        placeholder of this workspace covers is relayed undecrypted
        (``ignore_connection``), end-to-end TLS preserved — pinned
        clients keep working, and detection of off-allowlist
        sightings stays limited to decrypted flows, exactly as #194
        recorded."""
        workspace = self.workspace(data.context.client)
        if workspace is None:
            return
        sni = data.client_hello.sni or ""
        if self.owner.matching_entry(workspace, sni) is None:
            data.ignore_connection = True

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
        """The swap: a carried sentinel whose own allowlist covers
        the destination is exchanged for the secret; a carried
        sentinel whose allowlist misses it is the off-allowlist
        sighting — decrypted, unrewritten, announced. A sentinel
        whose row is gone (revoked, expired) passes through
        decrypted and unrewritten."""
        workspace = self.workspace(flow.client_conn)
        if workspace is None:
            return
        entry = carried_entry(self.owner.entries_for(workspace), flow)
        if entry is None:
            return
        host = destination_name(flow)
        if not host_matches(host, entry.dests):
            await self.owner.publish_sighting(workspace, entry, host)
            return
        await swap_on_flow(self.owner, workspace, entry, host, flow)


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

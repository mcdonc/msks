"""The interceptor addon's decisions, against stub flows (#199)."""

import logging
from types import SimpleNamespace

import pytest
from msks.interceptor import PlaceholderEntry, ca, engine, host_matches
from msks.interceptor.egress import WebVerdict

TAP = "172.31.0.2"
OTHER_TAP = "172.31.0.6"
SNI = "api.example.com"


class FakeHeaders:
    """The raw-fields surface rewrite_request touches."""

    def __init__(self, **values: str) -> None:
        self.fields = tuple(
            (name.encode(), value.encode()) for name, value in values.items()
        )

    def get(self, name: str) -> str:
        for key, value in self.fields:
            if key.decode().lower() == name.lower():
                return value.decode()
        return ""


class FakeQuery:
    """The multi-pair query surface (duplicate keys preserved)."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = pairs
        self.assigned: list[tuple[str, str]] | None = None

    def items(self, multi: bool = False) -> list[tuple[str, str]]:
        assert multi
        return self.pairs


class FakeRequest:
    def __init__(self, headers, query, pretty_host) -> None:
        self.headers = headers
        self._query = query
        self.pretty_host = pretty_host
        self.host = "198.51.100.7"  # the dialed address (transparent)
        self.host_header = pretty_host
        self.scheme = "https"
        self.port = 443

    @property
    def query(self):
        return self._query

    @query.setter
    def query(self, pairs) -> None:
        self._query.assigned = list(pairs)


class FakeClient:
    def __init__(
        self, tap=TAP, sni=SNI, tls=True, dest=("198.51.100.7", 443)
    ) -> None:
        self.proxy_mode = SimpleNamespace(custom_listen_host=tap)
        self.sni = sni if tls else None
        self.tls = tls
        self.sockname = dest


class FakeFlow:
    def __init__(self, client, headers, query, pretty_host=SNI) -> None:
        self.client_conn = client
        self.request = FakeRequest(headers, query, pretty_host)
        self.response = None


class FakeOwner:
    """The manager surface the addon reads."""

    def __init__(
        self,
        entries,
        authority,
        live=True,
        secret="real-one",
        verdict=None,
    ):
        self.entries = entries
        self.authority = authority
        self.live = live
        self.secret = secret
        self.verdict = verdict or WebVerdict(True, "ungated")
        self.swaps: list[tuple] = []
        self.sightings: list[tuple] = []
        self.leaf_snis: list[str] = []
        self.gated: list[tuple] = []

    def workspace_for_tap(self, tap_ip):
        return "ws-a" if tap_ip == TAP else None

    async def web_verdict(self, workspace_id, host, port, address):
        self.gated.append((workspace_id, host, port, address))
        return self.verdict

    def entries_for(self, workspace_id):
        return self.entries

    def matching_entry(self, workspace_id, host):
        for entry in self.entries.values():
            if host_matches(host, entry.dests):
                return entry
        return None

    def ca_for(self, workspace_id):
        return self.authority

    def mint_leaf(self, workspace_id, sni):
        self.leaf_snis.append(sni)
        return ca.mint_leaf(self.authority, sni)

    async def sentinel_live(self, sentinel):
        return self.live

    async def secret_for(self, entry):
        if isinstance(self.secret, Exception):
            raise self.secret
        return self.secret

    async def publish_swap(self, workspace_id, entry, host):
        self.swaps.append((workspace_id, entry.name, host))

    async def publish_sighting(self, workspace_id, entry, host):
        self.sightings.append((workspace_id, entry.name, host))


def entry(
    sentinel="mskssec1_" + "a" * 43,
    dests=(SNI,),
    name="api",
) -> PlaceholderEntry:
    return PlaceholderEntry(
        sentinel=sentinel,
        name=name,
        placeholder_id=7,
        dests=dests,
        backend_ref="MSKSWS_WS_A_API",
    )


@pytest.fixture
def authority(tmp_path):
    return ca.load_or_mint(tmp_path, "ws-a")


def hello_data(client, sni):
    return SimpleNamespace(
        context=SimpleNamespace(client=client),
        client_hello=SimpleNamespace(sni=sni),
        ignore_connection=False,
    )


def tls_data(client):
    return SimpleNamespace(
        context=SimpleNamespace(client=client), ssl_conn=None
    )


def swap_flow(sentinel, *, tap=TAP, tls=True, pretty=SNI, sni=SNI):
    return FakeFlow(
        FakeClient(tap=tap, sni=sni if tls else None, tls=tls),
        FakeHeaders(authorization=f"Bearer {sentinel}", x_custom=sentinel),
        FakeQuery([("api_key", sentinel), ("api_key", "other")]),
        pretty_host=pretty,
    )


# --- the allowlist grammar ---------------------------------------------------


def test_host_matches_exact_and_label_anchored_suffix() -> None:
    assert host_matches("api.example.com", ("api.example.com",))
    assert host_matches("deep.api.example.com", (".example.com",))
    assert not host_matches("apiexample.com", (".example.com",))
    assert not host_matches("api.example.com", ("other.example.com",))
    assert not host_matches("api.example.com", ())


# --- the splice tier ---------------------------------------------------------


async def test_hello_splices_when_no_entry_covers_the_sni(authority) -> None:
    addon = engine.InterceptorAddon(FakeOwner({}, authority))
    data = hello_data(FakeClient(), "unknown.example.com")
    await addon.tls_clienthello(data)
    assert data.ignore_connection is True


async def test_hello_intercepts_when_an_entry_covers_the_sni(
    authority,
) -> None:
    addon = engine.InterceptorAddon(
        FakeOwner({entry().sentinel: entry()}, authority)
    )
    data = hello_data(FakeClient(), SNI)
    await addon.tls_clienthello(data)
    assert data.ignore_connection is False


async def test_hello_ignores_connections_from_foreign_listeners(
    authority,
) -> None:
    addon = engine.InterceptorAddon(FakeOwner({}, authority))
    data = hello_data(FakeClient(tap=OTHER_TAP), SNI)
    await addon.tls_clienthello(data)
    assert data.ignore_connection is False
    assert addon.owner.gated == []


async def test_hello_splices_a_sentinelless_sni(authority) -> None:
    """No SNI carries no name to match: the flow relays undecrypted."""
    addon = engine.InterceptorAddon(
        FakeOwner({entry().sentinel: entry()}, authority)
    )
    data = hello_data(FakeClient(), None)
    await addon.tls_clienthello(data)
    assert data.ignore_connection is True
    # The address the redirect preserved named the flow instead.
    assert addon.owner.gated == [("ws-a", "198.51.100.7", 443, "198.51.100.7")]


# --- the consent tier (#452) -------------------------------------------------


async def test_hello_gates_the_sni_before_the_splice(authority) -> None:
    """The gate runs before the splice decision and keys on the
    SNI with the original destination beside it."""
    owner = FakeOwner({entry().sentinel: entry()}, authority)
    data = hello_data(FakeClient(), SNI)
    await engine.InterceptorAddon(owner).tls_clienthello(data)
    assert owner.gated == [("ws-a", SNI, 443, "198.51.100.7")]


async def test_hello_marks_a_denied_connection(authority) -> None:
    """A denied SNI stays on the decrypt path (never relayed) and
    the connection carries the deny marker the request hook
    answers."""
    owner = FakeOwner({}, authority, verdict=WebVerdict(False, "static"))
    addon = engine.InterceptorAddon(owner)
    client = FakeClient()
    data = hello_data(client, "denied.example.com")
    await addon.tls_clienthello(data)
    assert data.ignore_connection is False
    assert addon.denied(client)
    # A covered SNI would have decrypted anyway; the marker, not
    # the coverage, owns the refusal.
    assert owner.leaf_snis == []


async def test_a_marked_connection_refuses_locally(authority) -> None:
    addon = engine.InterceptorAddon(
        FakeOwner({}, authority, verdict=WebVerdict(False, "verdict"))
    )
    client = FakeClient(sni="denied.example.com")
    data = hello_data(client, "denied.example.com")
    await addon.tls_clienthello(data)
    flow = swap_flow(entry().sentinel, sni="denied.example.com")
    flow.client_conn = client
    await addon.request(flow)
    assert flow.response is not None
    assert flow.response.status_code == 403
    assert addon.denied(client)


async def test_request_gates_plain_http(authority) -> None:
    """Plain HTTP never had a ClientHello: the request hook gates
    it on the Host header and the original destination."""
    owner = FakeOwner(
        {entry().sentinel: entry()},
        authority,
        verdict=WebVerdict(False, "static"),
    )
    flow = swap_flow(entry().sentinel, tls=False, pretty="plain.example.net")
    flow.client_conn.sockname = ("198.51.100.9", 80)
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.response is not None
    assert flow.response.status_code == 403
    assert owner.gated == [("ws-a", "plain.example.net", 80, "198.51.100.9")]
    assert owner.swaps == []


async def test_request_skips_the_gate_for_allowed_tls(authority) -> None:
    """TLS connections gated at their hello: the request hook runs
    the swap without re-gating (a once verdict must not
    re-prompt per kept-alive request)."""
    sentinel = entry().sentinel
    owner = FakeOwner({sentinel: entry()}, authority)
    flow = swap_flow(sentinel)
    await engine.InterceptorAddon(owner).request(flow)
    assert owner.gated == []
    assert owner.swaps == [("ws-a", "api", SNI)]


async def test_a_denied_plain_request_never_swaps(authority) -> None:
    """The refusal precedes the swap machinery: a sentinel on a
    denied request never rides the wire."""
    sentinel = entry().sentinel
    owner = FakeOwner(
        {sentinel: entry()}, authority, verdict=WebVerdict(False, "verdict")
    )
    flow = swap_flow(sentinel, tls=False, pretty="plain.example.net")
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.response is not None
    assert flow.response.status_code == 403
    assert owner.swaps == []
    assert owner.sightings == []


# --- the leaf ----------------------------------------------------------------


def test_tls_start_client_serves_a_leaf_from_the_workspace_ca(
    authority,
) -> None:
    owner = FakeOwner({entry().sentinel: entry()}, authority)
    data = tls_data(FakeClient())
    engine.InterceptorAddon(owner).tls_start_client(data)
    assert data.ssl_conn is not None
    assert owner.leaf_snis == [SNI]


def test_tls_start_client_ignores_a_foreign_listener(authority) -> None:
    addon = engine.InterceptorAddon(FakeOwner({}, authority))
    data = tls_data(FakeClient(tap=OTHER_TAP))
    addon.tls_start_client(data)
    assert data.ssl_conn is None


def test_tls_start_client_skips_a_disarmed_workspace(authority) -> None:
    owner = FakeOwner({entry().sentinel: entry()}, authority)
    owner.authority = None
    data = tls_data(FakeClient())
    engine.InterceptorAddon(owner).tls_start_client(data)
    assert data.ssl_conn is None


# --- the swap ----------------------------------------------------------------


async def test_request_swaps_headers_and_query(authority) -> None:
    sentinel = entry().sentinel
    owner = FakeOwner({sentinel: entry()}, authority)
    flow = swap_flow(sentinel)
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.request.headers.get("authorization") == "Bearer real-one"
    assert flow.request.headers.get("x_custom") == "real-one"
    # Duplicate query keys survive the pair-list rewrite.
    assert flow.request.query.assigned == [
        ("api_key", "real-one"),
        ("api_key", "other"),
    ]
    assert owner.swaps == [("ws-a", "api", SNI)]
    assert owner.sightings == []
    # HTTPS keeps the dialed address: the SNI already bound the
    # connection's name.
    assert flow.request.host == "198.51.100.7"


async def test_request_pins_a_plain_http_swap_to_the_name(authority) -> None:
    sentinel = entry().sentinel
    owner = FakeOwner({sentinel: entry()}, authority)
    flow = swap_flow(sentinel, tls=False, pretty="api.example.com")
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.request.host == "api.example.com"


async def test_request_sights_an_off_allowlist_carrier(authority) -> None:
    """The entry exists, its own allowlist misses the host: decrypted,
    unrewritten, announced — even though the sentinel rode along."""
    sentinel = entry(dests=("api.example.com",)).sentinel
    owner = FakeOwner({sentinel: entry(dests=("api.example.com",))}, authority)
    flow = swap_flow(sentinel, sni="elsewhere.example.net")
    await engine.InterceptorAddon(owner).request(flow)
    assert owner.sightings == [("ws-a", "api", "elsewhere.example.net")]
    assert owner.swaps == []
    assert flow.request.headers.get("authorization").startswith("Bearer msks")


async def test_request_sights_a_detection_only_entry(authority) -> None:
    """#339: a sentinel a workspace holds no covering row for — a
    daemon-wide sentinel from a scoped workspace, another
    workspace's scoped sentinel — arrives as a detection-only
    entry with empty dests: every destination misses it, so the
    carrier publishes an off-allowlist sighting even toward the
    host its own row would have covered, and nothing swaps."""
    daemon_sentinel = "mskssec2_" + "b" * 43
    detection = entry(sentinel=daemon_sentinel, dests=())
    owner = FakeOwner({daemon_sentinel: detection}, authority)
    flow = swap_flow(daemon_sentinel)  # toward SNI, its row's dest
    await engine.InterceptorAddon(owner).request(flow)
    assert owner.sightings == [("ws-a", "api", SNI)]
    assert owner.swaps == []
    assert flow.request.headers.get("authorization").startswith(
        f"Bearer {daemon_sentinel[:9]}"
    )


async def test_request_passes_a_revoked_sentinel_through(authority) -> None:
    sentinel = entry().sentinel
    owner = FakeOwner({sentinel: entry()}, authority, live=False)
    flow = swap_flow(sentinel)
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.response is None
    assert owner.swaps == []
    assert sentinel in flow.request.headers.get("authorization")


async def test_request_fails_closed_when_the_store_cannot_serve(
    authority,
) -> None:
    sentinel = entry().sentinel
    owner = FakeOwner(
        {sentinel: entry()}, authority, secret=RuntimeError("store down")
    )
    flow = swap_flow(sentinel)
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.response is not None
    assert flow.response.status_code == 502
    assert owner.swaps == []


async def test_request_ignores_a_foreign_listener(authority) -> None:
    owner = FakeOwner({entry().sentinel: entry()}, authority)
    flow = swap_flow(entry().sentinel, tap=OTHER_TAP)
    await engine.InterceptorAddon(owner).request(flow)
    assert owner.swaps == []


async def test_request_ignores_a_sentinelless_request(authority) -> None:
    owner = FakeOwner({entry().sentinel: entry()}, authority)
    flow = FakeFlow(
        FakeClient(),
        FakeHeaders(authorization="Bearer something-else"),
        FakeQuery([("k", "v")]),
    )
    await engine.InterceptorAddon(owner).request(flow)
    assert owner.swaps == []


def test_destination_name_prefers_the_sni() -> None:
    flow = FakeFlow(
        FakeClient(sni="sni.example.com", tls=True),
        FakeHeaders(host="header.example.com"),
        FakeQuery([]),
        pretty_host="header.example.com",
    )
    assert engine.destination_name(flow) == "sni.example.com"
    plain = FakeFlow(
        FakeClient(tls=False),
        FakeHeaders(host="header.example.com"),
        FakeQuery([]),
        pretty_host="header.example.com",
    )
    assert engine.destination_name(plain) == "header.example.com"


def test_carried_entry_finds_a_sentinel_in_a_query_key(authority) -> None:
    sentinel = entry().sentinel
    flow = FakeFlow(
        FakeClient(),
        FakeHeaders(authorization="Bearer none"),
        FakeQuery([(f"{sentinel}-x", "v")]),
    )
    assert engine.carried_entry({sentinel: entry()}, flow) is not None


def test_log_bridge_maps_levels(caplog) -> None:
    bridge = engine.LogBridge()
    with caplog.at_level(logging.DEBUG, logger="msks.interceptor.engine"):
        bridge.log(SimpleNamespace(level="warn", msg="careful"))
        bridge.log(SimpleNamespace(level="mystery", msg="huh"))
    assert "careful" in caplog.text
    assert "huh" in caplog.text


async def test_a_swapped_https_request_pins_its_host_header(authority) -> None:
    """Fronting exfiltration (#260 review): a guest may send an
    allowlisted SNI with a Host header naming its own vhost on
    shared infrastructure. The swap pins the header to the matched
    name, so the request routes to the allowlisted service's vhost —
    the dialed, SNI-verified connection is untouched."""
    sentinel = entry().sentinel
    owner = FakeOwner({sentinel: entry()}, authority)
    flow = swap_flow(sentinel)
    flow.request.host_header = "attacker-vhost.example.net"
    await engine.InterceptorAddon(owner).request(flow)
    assert flow.request.host_header == "api.example.com"
    assert flow.request.host == "198.51.100.7"  # the dial is pinned by SNI


async def test_matching_is_case_insensitive(authority) -> None:
    """DNS names compare case-insensitively: a mixed-case Host or
    SNI still swaps (lowercase stays the stored form)."""
    assert engine.host_matches("API.Example.COM", ("api.example.com",))
    sentinel = entry().sentinel
    owner = FakeOwner({sentinel: entry()}, authority)
    flow = swap_flow(sentinel, tls=False, pretty="API.Example.COM")
    await engine.InterceptorAddon(owner).request(flow)
    assert owner.swaps == [("ws-a", "api", "API.Example.COM")]
    assert flow.request.host == "API.Example.COM"

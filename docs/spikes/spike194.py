"""Spike harness for #194: mitmproxy in-process, per-workspace CAs.

Proves, with printed evidence:
  1. One master, per-workspace CA dispatched by client address.
  2. Sentinel swap in the Authorization header and the query string,
     only when the destination matches the allowlist (exact host for
     ws-a, label-anchored suffix for ws-b).
  3. Splice tier: destinations outside every allowlist are relayed
     undecrypted (the origin's real cert reaches the client).
  4. Off-allowlist sighting (entry exists, its allowlist misses the
     host while another placeholder covers it): decrypted, unrewritten,
     audit event. Revocation with the workspace still armed by a
     second mapping: the revoked sentinel passes through decrypted but
     unrewritten.
  5. Benchmark: direct vs spliced vs MITM (10 MiB, best of 3).

Run in ONE devenv shell invocation (the uv-sync task strips
mitmproxy from the venv on every shell entry, and public DNS is
required proxy-side — the origins are reached through
*.127.0.0.1.sslip.io):

    devenv shell -- bash -c \
      'uv pip install -q --python .devenv/state/venv/bin/python \
       mitmproxy && python docs/spikes/spike194.py'
"""

import asyncio
import inspect
import json
import os
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mitmproxy import certs, options, tls
from mitmproxy.master import Master
from mitmproxy.net import tls as net_tls
from OpenSSL import SSL

SENTINEL = "mskssec1_" + secrets.token_urlsafe(32)
SECRET = "real-secret-" + secrets.token_urlsafe(12)
# A second, independent placeholder for ws-a so test 4 can revoke the
# first while the workspace stays armed — proving the decrypted-but-
# unrewritten path the production semantics call for.
SENTINEL2 = "mskssec1_" + secrets.token_urlsafe(32)
SECRET2 = "real-secret-2-" + secrets.token_urlsafe(12)
BIG = 10 * 1024 * 1024

API = "api.127.0.0.1.sslip.io"  # allowlisted destination
OTHER = "other.127.0.0.1.sslip.io"  # not allowlisted
# A second host on origin-b, covered ONLY by ws-a's second placeholder
# — makes the off-allowlist sighting reachable: SENTINEL (dests=API)
# toward OTHER2 is intercepted (SENTINEL2 covers it) yet never swapped.
OTHER2 = "other2.127.0.0.1.sslip.io"
ORIGIN_A_PORT = 19443
ORIGIN_B_PORT = 19444
PROXY_PORT = 19800
WS = {"127.0.0.2": "ws-a", "127.0.0.3": "ws-b"}
AUDIT: list[tuple] = []


class Interceptor:
    """The #194 addon: dispatch by client address, swap by allowlist."""

    def __init__(self, stores, mappings):
        self.stores = stores  # client addr -> certs.CertStore
        self.mappings = mappings  # client addr -> {sentinel: entry}

    def tls_clienthello(self, data: tls.ClientHelloData):
        addr = data.context.client.peername[0]
        if addr not in self.mappings:
            return
        sni = data.client_hello.sni or ""
        if self.match(addr, sni) is None:
            data.ignore_connection = True  # splice tier
            AUDIT.append(("splice", addr, sni))

    def tls_start_client(self, data: tls.TlsData):
        addr = data.conn.peername[0]
        store = self.stores.get(addr)
        if store is None:
            return  # fall through to the default addon
        sni = data.context.client.sni or "localhost"
        entry = store.get_cert(sni, [x509.DNSName(sni)], None, None)
        # cipher_list=None keeps the platform (OpenSSL) defaults — the
        # set_cipher_list call is skipped entirely. An EMPTY tuple is a
        # hard OpenSSL error ("no cipher match"), and mitmproxy's own
        # contexts never take the None path — _default_ciphers() always
        # returns its curated list. FIPS posture note in the doc.
        ssl_ctx = net_tls.create_client_proxy_context(
            method=net_tls.Method.TLS_SERVER_METHOD,
            min_version=net_tls.Version.TLS1_2,
            max_version=net_tls.Version.UNBOUNDED,
            cipher_list=None,
            ecdh_curve=None,
            chain_file=entry.chain_file,
            request_client_cert=False,
            alpn_select_callback=None,
            extra_chain_certs=(),
            dhparams=store.dhparams,
        )
        data.ssl_conn = SSL.Connection(ssl_ctx)
        data.ssl_conn.use_certificate(entry.cert.to_cryptography())
        data.ssl_conn.use_privatekey(entry.privatekey)
        data.ssl_conn.set_accept_state()  # the layer never sets it

    def request(self, flow):
        addr = flow.client_conn.peername[0]
        auth = flow.request.headers.get("authorization", "")
        # Detection must see duplicate query keys: dict() collapses
        # ?k=1&k=<sentinel> to the first value and the sentinel leaks
        # raw through a decrypted, allowlisted flow.
        items = list(flow.request.query.items(multi=True))
        qs = "".join(f"{k}{v}" for k, v in items)
        host = flow.request.host  # hostname without port
        # Swap binding is PER PLACEHOLDER: the sentinel being carried
        # must be its own entry whose allowlist covers this host. A
        # revoked sentinel (no entry) passes decrypted and unrewritten;
        # a carried sentinel whose own allowlist misses the host is the
        # off-allowlist sighting, even when another placeholder covers
        # the destination.
        for sentinel, entry in self.mappings.get(addr, {}).items():
            if sentinel not in auth + qs:
                continue
            if not self.host_matches(host, entry["dests"]):
                AUDIT.append(("off-allowlist-sighting", addr, host))
                return
            if sentinel in auth:
                flow.request.headers["authorization"] = auth.replace(
                    sentinel, entry["secret"]
                )
            if any(sentinel in k or sentinel in v for k, v in items):
                # Assign the full pair list (duplicates preserved); a
                # plain string assignment is not accepted here. Values
                # are URL-safe in this harness; production encodes
                # properly (#199).
                flow.request.query = [
                    (
                        k.replace(sentinel, entry["secret"]),
                        v.replace(sentinel, entry["secret"]),
                    )
                    for k, v in items
                ]
            AUDIT.append(("swap", addr, host))
            return

    def match(self, addr, host):
        """Any placeholder of this workspace covering the host? (hello)"""
        for entry in self.mappings.get(addr, {}).values():
            if self.host_matches(host, entry["dests"]):
                return entry
        return None

    @staticmethod
    def host_matches(host, dests):
        for dest in dests:
            if host == dest or (dest.startswith(".") and host.endswith(dest)):
                return True
        return False


async def handle_origin(reader, writer):
    """Tiny TLS origin: /echo reports what it received; /big is 10 MB."""
    try:
        line = await reader.readline()
        headers = {}
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode().partition(":")
            headers[k.strip().lower()] = v.strip()
        path = line.decode().split()[1]
        path, _, qstr = path.partition("?")
        if path == "/big":
            body = b"x" * BIG
        else:
            body = json.dumps(
                {"saw-auth": headers.get("authorization"), "qs": qstr}
            ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + (
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
    except Exception as e:  # noqa: BLE001
        print("origin error:", e, file=sys.stderr)
    finally:
        writer.close()


def mint_origin_cert(path_pem, path_key, hosts):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    host = hosts[0]
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, host)])
        )
        .issuer_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, host)])
        )
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    path_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    path_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def curl(args):
    """Return (exit, body) — body captured for echo checks."""
    r = subprocess.run(
        ["curl", "-sS", "-m", "30"] + args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()[:400]


async def get_w(url, *extra):
    """Benchmark variant: report code+time+size, discard body."""
    args = [
        "-o",
        os.devnull,
        "-w",
        "%{http_code} %{time_total} %{size_download}",
    ]
    return await asyncio.to_thread(curl, args + list(extra) + [url])


async def get(url, *extra, body=True):
    args = ["-w", "\n%{http_code} %{time_total}s"]
    if not body:
        args += ["-o", os.devnull]
    # curl MUST run in a worker thread: the origin and the proxy live
    # in THIS process's event loop, and a blocking subprocess.run()
    # starves them while curl waits on them (self-deadlock).
    return await asyncio.to_thread(curl, args + list(extra) + [url])


async def amain(tmp):
    # Origins (real TLS, self-signed per host).
    servers = []
    for port, name, hosts in [
        (ORIGIN_A_PORT, "origin-a", [API]),
        (ORIGIN_B_PORT, "origin-b", [OTHER, OTHER2]),
    ]:
        pem, key = tmp / f"{name}.pem", tmp / f"{name}.key"
        mint_origin_cert(pem, key, hosts)
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(pem, key)
        servers.append(
            await asyncio.start_server(
                handle_origin, "127.0.0.1", port, ssl=sctx
            )
        )

    # Per-workspace cert stores + placeholder mappings.
    stores, mappings = {}, {}
    for addr, name in WS.items():
        d = tmp / name
        d.mkdir()
        stores[addr] = certs.CertStore.from_store(str(d), "msks-" + name, 2048)
        mappings[addr] = {SENTINEL: {"secret": SECRET, "dests": [API]}}
    # ws-b matches by label-anchored suffix instead of exact host —
    # exercising the second half of the allowlist grammar.
    mappings["127.0.0.3"][SENTINEL]["dests"] = [".127.0.0.1.sslip.io"]
    mappings["127.0.0.2"][SENTINEL2] = {
        "secret": SECRET2,
        "dests": [API, OTHER2],
    }

    opts = options.Options(
        listen_host="127.0.0.1",
        listen_port=PROXY_PORT,
        mode=["regular"],
        # mitmproxy's own store lives under confdir — pointed at the
        # scratch dir so the harness never creates ~/.mitmproxy.
        confdir=str(tmp / "mitmproxy-conf"),
    )
    master = Master(opts)
    from mitmproxy.addons import default_addons
    from mitmproxy.addons.termlog import TermLog

    master.addons.add(
        TermLog(), Interceptor(stores, mappings), *default_addons()
    )
    master.options.update(
        connection_strategy="lazy",
        ssl_insecure=True,
        keep_host_header=True,
        termlog_verbosity="debug",
    )
    proxy = asyncio.create_task(master.run())
    await asyncio.sleep(1.5)

    prox = ["-x", f"http://127.0.0.1:{PROXY_PORT}"]
    ca_a = tmp / "ws-a" / "msks-ws-a-ca-cert.pem"
    ca_b = tmp / "ws-b" / "msks-ws-b-ca-cert.pem"
    ob = tmp / "origin-b.pem"
    auth = ["-H", f"Authorization: Bearer {SENTINEL}"]

    print("=== 1. swap (ws-a -> allowlisted", API + ")")
    print(
        await get(
            f"https://{API}:{ORIGIN_A_PORT}/echo?api_key={SENTINEL}",
            "--interface",
            "127.0.0.2",
            "--cacert",
            str(ca_a),
            *auth,
            *prox,
        )
    )

    print("\n=== 2. CA separation: ws-b client (trusts ca-b only)")
    print(
        await get(
            f"https://{API}:{ORIGIN_A_PORT}/echo",
            "--interface",
            "127.0.0.3",
            "--cacert",
            str(ca_b),
            *auth,
            *prox,
        )
    )
    print(
        "    expected: 200 — ws-b's allowlist is the suffix form;"
        " the leaf is signed by ws-b's own CA"
    )

    print("\n=== 3. splice (ws-a -> " + OTHER + ", not allowlisted)")
    print(
        await get(
            f"https://{OTHER}:{ORIGIN_B_PORT}/echo",
            "--interface",
            "127.0.0.2",
            "--cacert",
            str(ob),
            *auth,
            *prox,
        )
    )
    print("    expected: 200 with origin-b's REAL cert (undecrypted relay)")

    print(
        "\n=== 4a. off-allowlist sighting (entry exists, host outside"
        " ITS allowlist)"
    )
    print(
        await get(
            f"https://{OTHER2}:{ORIGIN_B_PORT}/echo",
            "--interface",
            "127.0.0.2",
            "--cacert",
            str(ca_a),
            *auth,
            *prox,
        )
    )
    print(
        "    expected: 200, decrypted (SENTINEL2 covers OTHER2), raw"
        " sentinel in the echo; audit shows off-allowlist-sighting"
    )

    print("\n=== 4b. revocation (ws-a stays armed via the 2nd placeholder)")
    mappings["127.0.0.2"].pop(SENTINEL, None)
    print(
        await get(
            f"https://{API}:{ORIGIN_A_PORT}/echo",
            "--interface",
            "127.0.0.2",
            "--cacert",
            str(ca_a),
            *auth,
            *prox,
        )
    )
    print(
        "    expected: 200, decrypted, and saw-auth still carries the"
        " raw sentinel (its own entry is gone; SENTINEL2 still arms"
        " the workspace, so the flow is intercepted, not spliced)"
    )

    print("\n=== 5. audit log")
    for e in AUDIT:
        print("  ", e)

    print("\n=== 6. benchmark: 10 MiB, best of 3")
    # Echo latency baselines (best of 3, ms) so the holdback verdict
    # has a direct leg to compare against.
    for nm, url, extra in [
        (
            "direct",
            f"https://{API}:{ORIGIN_A_PORT}/echo",
            ["--cacert", str(tmp / "origin-a.pem")],
        ),
        (
            "spliced",
            f"https://{OTHER}:{ORIGIN_B_PORT}/echo",
            ["--interface", "127.0.0.2", "--cacert", str(ob), *prox],
        ),
        (
            "mitm",
            f"https://{API}:{ORIGIN_A_PORT}/echo",
            ["--interface", "127.0.0.2", "--cacert", str(ca_a), *prox],
        ),
    ]:
        lat = []
        for _ in range(3):
            t0 = time.monotonic()
            await get(url, *extra)
            lat.append((time.monotonic() - t0) * 1000)
        print(f"  echo {nm:7s} best {min(lat):5.0f} ms")
    # Test 4 revoked ws-a's mapping; re-arm it for the MITM leg.
    mappings["127.0.0.2"][SENTINEL] = {"secret": SECRET, "dests": [API]}
    bench = {
        "direct": (
            f"https://{API}:{ORIGIN_A_PORT}/big",
            ["--cacert", str(tmp / "origin-a.pem")],
        ),
        "spliced": (
            f"https://{OTHER}:{ORIGIN_B_PORT}/big",
            ["--interface", "127.0.0.2", "--cacert", str(ob), *prox],
        ),
        # The sentinel header rides along so this row measures the
        # full decrypt + rewrite + re-encrypt path, not decrypt alone.
        "mitm": (
            f"https://{API}:{ORIGIN_A_PORT}/big",
            [
                "--interface",
                "127.0.0.2",
                "--cacert",
                str(ca_a),
                *auth,
                *prox,
            ],
        ),
    }
    for name, (url, extra) in bench.items():
        times = []
        sizes = []
        for _ in range(3):
            t0 = time.monotonic()
            rc, out = await get_w(url, *extra)
            times.append(time.monotonic() - t0)
            sizes.append(out)
        best = min(times)
        print(
            f"  {name:8s} best {best:6.2f}s = {BIG / best / 2**20:6.1f}"
            f" MiB/s  sizes={sizes} runs={[f'{t:.2f}' for t in times]}"
        )

    proxy.cancel()
    for s in servers:
        s.close()


def main():
    print("mitmproxy CA-store API probe:")
    print("  get_cert", inspect.signature(certs.CertStore.get_cert))
    print("  from_store", inspect.signature(certs.CertStore.from_store))
    tmp = Path(tempfile.mkdtemp(prefix="spike194-"))
    asyncio.run(amain(tmp))
    print(f"\ntmpdir: {tmp}")


if __name__ == "__main__":
    main()

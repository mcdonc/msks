"""The proxy's tap gate under the real net stack (#483).

The two halves of the gate, exercised as the daemon ships them.
The base table's loopback guard (one static drop: ``lo`` traffic
to the tap pool on the proxy port) answers a host process, which
neither the device pin nor the per-VM chains could — the kernel's
weak-host delivery hands loopback-routed connections to the
listener through any device pin, and every per-VM chain's final
drop keys to its own tap. The listener's ``SO_BINDTODEVICE`` pin
answers a second guest reaching across taps, exactly in the
degraded state the criterion names: workspace B's per-VM table is
deliberately absent (a failed or flushed apply — every per-VM call
is absent_ok), so nothing but the pin stands between B and
workspace A's proxy. Veth pairs stand in for taps: each host end
is a tap with its own /30, and a network namespace holding the
peer end is the guest.
"""

import asyncio
import socket
import subprocess

from msks.llm import TapListener
from msks.net import nft
from msks.settings import LlmSettings, NetSettings, Settings

from test_smoke import needs_egress

A_NETNS = "mskstest-a"
B_NETNS = "mskstest-b"
A_VETH = "mskstest-ta"
B_VETH = "mskstest-tb"
# Two /30s inside 172.31.255.0/24 (a range the egress allocator
# never carves — the pool sits at the subnet's start).
A_HOST = "172.31.255.1"
A_GUEST = "172.31.255.2"
B_HOST = "172.31.255.5"
B_GUEST = "172.31.255.6"
PORT = 48770


def ip(*args: str) -> None:
    subprocess.run(["ip", *args], check=True, capture_output=True)


def cleanup() -> None:
    for netns in (A_NETNS, B_NETNS):
        subprocess.run(["ip", "netns", "del", netns], capture_output=True)
    for veth in (A_VETH, B_VETH):
        subprocess.run(["ip", "link", "del", veth], capture_output=True)


class ServeApp:
    """The smallest thing TapListener can serve: it never answers
    HTTP, but the TCP handshake is the gate under test."""

    async def __call__(self, scope, receive, send) -> None:
        await receive()


@needs_egress
async def test_the_tap_gate_holds_without_per_vm_tables() -> None:
    cleanup()
    try:
        # Two taps, two guests — a fresh namespace each, the host
        # end addressed on its own /30.
        for netns, veth, host, guest in (
            (A_NETNS, A_VETH, A_HOST, A_GUEST),
            (B_NETNS, B_VETH, B_HOST, B_GUEST),
        ):
            ip("netns", "add", netns)
            ip(
                "link",
                "add",
                veth,
                "type",
                "veth",
                "peer",
                "name",
                f"{veth}-g",
            )
            ip("link", "set", f"{veth}-g", "netns", netns)
            ip("addr", "add", f"{host}/30", "dev", veth)
            ip("link", "set", veth, "up")
            ip("-n", netns, "addr", "add", f"{guest}/30", "dev", f"{veth}-g")
            ip("-n", netns, "link", "set", f"{veth}-g", "up")
            ip("-n", netns, "link", "set", "lo", "up")
            ip("-n", netns, "route", "add", "default", "via", host)

        # The base table exactly as the daemon ships it: the
        # masquerade plus the loopback guard for the pool and port.
        # Workspace A's per-VM table is absent too — nothing
        # admits A's guest, so only the pin and the guard speak.
        settings = Settings(
            net=NetSettings(enabled=True),
            llm=LlmSettings(port=PORT, models=("*:http://up.stream/v1:sk-x",)),
        )
        await nft.apply_base(settings)

        # The proxy listener for workspace A: bound to A's tap
        # address and pinned to A's tap device — exactly what
        # listener_for builds for an attachment.
        listener = TapListener(
            ServeApp(), tap=A_VETH, tap_ip=A_HOST, port=PORT
        )
        listener.bind()
        serve = asyncio.create_task(listener.start())
        await asyncio.sleep(0.2)

        # From guest A, over A's own link: the handshake completes.
        a_probe = await asyncio.to_thread(probe, A_NETNS, A_HOST)
        assert a_probe, "the tap's own guest must reach the proxy"

        # From guest B, routed through its tap to a local
        # destination — the kernel's weak-host delivery is what the
        # missing table would have allowed: the socket refuses it.
        b_probe = await asyncio.to_thread(probe, B_NETNS, A_HOST)
        assert not b_probe, "another guest must not reach the proxy"

        # From the host itself, through lo: the base table's
        # loopback guard drops it.
        lo_probe = await asyncio.to_thread(host_probe, A_HOST)
        assert not lo_probe, "a host process must not reach the proxy"

        serve.cancel()
        await listener.stop()
    finally:
        cleanup()
        subprocess.run(
            ["nft", "delete", "table", "inet", nft.BASE_TABLE],
            capture_output=True,
        )


def probe(netns: str, target: str) -> bool:
    """One TCP connect from inside a guest namespace, with a short
    bound: the pin drops the SYN, so the attempt times out rather
    than refusing fast — a False answer takes the full timeout."""
    return (
        subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                netns,
                "python3",
                "-c",
                "import socket, sys\n"
                "s = socket.socket(); s.settimeout(2)\n"
                "try:\n"
                "    s.connect((sys.argv[1], int(sys.argv[2]))); "
                "print(1)\n"
                "except OSError:\n"
                "    print(0)\n",
                target,
                str(PORT),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        == "1"
    )


def host_probe(target: str) -> bool:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((target, PORT))
        return True
    except OSError:
        return False

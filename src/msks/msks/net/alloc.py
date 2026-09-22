"""Address math for the per-workspace /30 slices (#52).

Pure functions, no side effects: the manager layers allocation state
(skip-past-used collision walking) on top. A /30 gives exactly four
addresses — network, guest, tap, broadcast — one per workspace, so no
address is ever shared between workspaces.
"""

import hashlib
from ipaddress import IPv4Address, IPv4Network

# One /30 per workspace: network + guest + tap + broadcast.
SLICE_PREFIX = 30

# The interface-name budget: IFNAMSIZ counts the trailing NUL.
IFNAMSIZ = 16


def digest_of(workspace_id: str) -> str:
    """The stable hex digest names derive from (tap, chain table)."""
    return hashlib.sha256(workspace_id.encode()).hexdigest()


def slice_count(pool: IPv4Network) -> int:
    """How many /30 slices the configured pool holds."""
    return pool.num_addresses // 4


def slice_index(workspace_id: str, count: int) -> int:
    """The stable starting slice for a workspace (first probe only).

    Collisions between distinct workspaces are walked forward by the
    allocator; the digest only decides where the walk starts.
    """
    digest = hashlib.sha256(workspace_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % count


def slice_net(pool: IPv4Network, index: int) -> IPv4Network:
    """The index-th /30 of the pool (four-address aligned by
    construction: every pool network address is a multiple of its own
    size, and slicing by fours from an aligned base stays aligned)."""
    base = int(pool.network_address) + index * 4
    return IPv4Network((base, SLICE_PREFIX))


def guest_addr(net: IPv4Network) -> IPv4Address:
    """The workspace VM's address: the first host of its /30."""
    return IPv4Address(int(net.network_address) + 1)


def tap_addr(net: IPv4Network) -> IPv4Address:
    """The host-side tap address: the second host of the /30."""
    return IPv4Address(int(net.network_address) + 2)


def tap_name(workspace_id: str) -> str:
    """The deterministic tap interface name for a workspace.

    ``msks-`` + 10 hex chars = 15 bytes, exactly the IFNAMSIZ budget
    (16 including the NUL), regardless of how long the workspace id
    is.
    """
    name = "msks-" + digest_of(workspace_id)[:10]
    assert len(name) < IFNAMSIZ
    return name


def table_name(workspace_id: str) -> str:
    """The deterministic nftables table name for a workspace."""
    return "msks-e-" + digest_of(workspace_id)[:10]


def guest_mac(workspace_id: str) -> str:
    """The deterministic NIC MAC for a workspace.

    02:… is locally administered and unicast — no collision with any
    real vendor prefix, and stable across restarts so leases and
    chains keep matching the same workspace.
    """
    digest = hashlib.sha256(f"mac:{workspace_id}".encode()).digest()
    return "02:" + ":".join(f"{byte:02x}" for byte in digest[:5])

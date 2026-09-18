"""Address math for the per-workspace /30 slices (#52)."""

from ipaddress import IPv4Network

from msks.net import alloc

POOL = IPv4Network("172.31.0.0/16")


def test_slice_count_scales_with_pool() -> None:
    assert alloc.slice_count(POOL) == 16384
    assert alloc.slice_count(IPv4Network("10.0.0.0/30")) == 1


def test_slice_index_is_stable_and_in_range() -> None:
    first = alloc.slice_index("ws-a", 16384)
    assert first == alloc.slice_index("ws-a", 16384)
    assert 0 <= first < 16384
    # Distinct workspaces need not collide, but must both be in range.
    assert 0 <= alloc.slice_index("ws-b", 16384) < 16384


def test_slice_nets_walk_the_pool() -> None:
    assert alloc.slice_net(POOL, 0) == IPv4Network("172.31.0.0/30")
    assert alloc.slice_net(POOL, 1) == IPv4Network("172.31.0.4/30")
    assert alloc.slice_net(POOL, 16383) == IPv4Network("172.31.255.252/30")


def test_guest_and_tap_addresses() -> None:
    net = alloc.slice_net(POOL, 0)
    assert str(alloc.guest_addr(net)) == "172.31.0.1"
    assert str(alloc.tap_addr(net)) == "172.31.0.2"


def test_tap_name_fits_ifnamsiz_and_is_stable() -> None:
    name = alloc.tap_name("a-very-long-workspace-id-that-exceeds-limits")
    assert name.startswith("msks-")
    assert len(name) < alloc.IFNAMSIZ
    assert name == alloc.tap_name(
        "a-very-long-workspace-id-that-exceeds-limits"
    )


def test_table_name_and_mac_are_deterministic() -> None:
    assert alloc.table_name("ws-a").startswith("msks-e-")
    assert alloc.table_name("ws-a") == alloc.table_name("ws-a")
    assert alloc.table_name("ws-a") != alloc.table_name("ws-b")
    mac = alloc.guest_mac("ws-a")
    assert mac.startswith("02:")
    assert mac == alloc.guest_mac("ws-a")
    assert len(mac.split(":")) == 6

"""Allowlist spec validation and matching (#69)."""

import pytest
from msks.consent.specs import (
    MODE_ALLOW,
    MODE_INTERACTIVE,
    MODE_STATIC,
    SCOPE_EXACT,
    SCOPE_INCLUSIVE,
    SCOPE_SUBDOMAINS,
    EgressPolicy,
    allow_all_cidrs,
    host_matches,
    is_ipv4,
    parse_allowlist,
    ports_for,
    split_port,
    strip_scope_sigil,
    valid_cidr_spec,
    valid_spec,
)


def test_valid_spec_grammars() -> None:
    assert valid_spec("example.com")
    assert valid_spec("example.com:443")
    assert valid_spec(".example.com")
    assert valid_spec("*.example.com")
    assert valid_spec("10.0.0.0/8")
    assert valid_spec("10.0.0.0/8:443")
    assert valid_spec("203.0.113.7")
    assert valid_spec("203.0.113.7:5432")


def test_invalid_specs_are_named() -> None:
    for bad in (
        "",
        " ",
        "two words",
        "example.com:",
        "example.com:99999",
        ".",
        "10.0.0.0/33",
        "10.0.0.0/",
        "::1/64",
        "*.",
        "*",
        "host:١٤٤٣",  # non-ASCII digits
    ):
        assert not valid_spec(bad), bad
    with pytest.raises(ValueError, match="nope example"):
        parse_allowlist(["example.com", "nope example"])


def test_parse_allowlist_strips_and_dedups_in_order() -> None:
    assert parse_allowlist(
        [" example.com ", "example.com", "", "other.example:443"]
    ) == ("example.com", "other.example:443")


def test_scope_sigils_strip() -> None:
    assert strip_scope_sigil("*.example.com") == (
        "example.com",
        SCOPE_SUBDOMAINS,
    )
    assert strip_scope_sigil(".example.com") == (
        "example.com",
        SCOPE_INCLUSIVE,
    )
    assert strip_scope_sigil("example.com") == ("example.com", SCOPE_EXACT)
    assert strip_scope_sigil(".") == ("", SCOPE_INCLUSIVE)


def test_host_matches_under_each_scope() -> None:
    assert host_matches("example.com", "example.com", SCOPE_EXACT)
    assert not host_matches("api.example.com", "example.com", SCOPE_EXACT)
    assert host_matches("api.example.com", "example.com", SCOPE_INCLUSIVE)
    assert host_matches("example.com", "example.com", SCOPE_INCLUSIVE)
    assert host_matches("api.example.com", "example.com", SCOPE_SUBDOMAINS)
    assert not host_matches("example.com", "example.com", SCOPE_SUBDOMAINS)
    assert not host_matches("evilexample.com", "example.com", SCOPE_INCLUSIVE)
    assert not host_matches("x.example.com", "example.com", "future")


def test_ports_for_splits_the_three_answers() -> None:
    from msks.consent.specs import HostSpec

    exact443 = HostSpec("example.com", 443, SCOPE_EXACT)
    sub8443 = HostSpec("api.example", 8443, SCOPE_SUBDOMAINS)
    bare = HostSpec("bare.example", None, SCOPE_EXACT)
    assert ports_for("example.com", (exact443, sub8443)) == {443}
    assert ports_for("v2.api.example", (exact443, sub8443)) == {8443}
    assert ports_for("bare.example", (bare,)) is None
    assert ports_for("other.example", (exact443,)) == set()


def test_cidr_validation() -> None:
    assert valid_cidr_spec("192.168.0.0/16")
    assert valid_cidr_spec("192.168.0.0/16:53")
    assert not valid_cidr_spec("192.168.0.0/16:99999")
    # A CIDR spec carries a prefix length; a bare address is a
    # host spec (valid_spec routes it there).
    assert not valid_cidr_spec("192.168.0.0")
    assert not valid_cidr_spec("2001:db8::/32")


def test_policy_splits_specs_by_enforcement_point() -> None:
    policy = EgressPolicy(
        "ws-a",
        MODE_INTERACTIVE,
        (
            ".debian.org",
            "db.internal:5432",
            "10.0.0.0/8",
            "203.0.113.7:443",
        ),
    )
    hosts = policy.host_specs
    assert [h.host for h in hosts] == ["debian.org", "db.internal"]
    assert hosts[0].port is None and hosts[0].scope == SCOPE_INCLUSIVE
    assert hosts[1].port == 5432
    nets = [(str(s.network), s.port) for s in policy.ip_specs]
    assert nets == [("10.0.0.0/8", None), ("203.0.113.7/32", 443)]
    assert policy.gated
    assert policy.interactive


def test_policy_modes_gate() -> None:
    assert EgressPolicy("a", MODE_STATIC, ()).gated
    assert not EgressPolicy("a", MODE_STATIC, ()).interactive
    assert not EgressPolicy("a", MODE_ALLOW, ()).gated
    assert EgressPolicy.from_row(None) is None
    policy = EgressPolicy.from_row(
        {"id": "a", "egress_mode": "allow", "egress_allowlist": ["x.com"]}
    )
    assert policy is not None
    assert policy.specs == ("x.com",)
    # A row without the columns (pre-#69 shape) reads as allow/empty.
    legacy = EgressPolicy.from_row({"id": "b"})
    assert legacy is not None
    assert legacy.mode == MODE_ALLOW


def test_split_port_only_takes_ascii_digits() -> None:
    assert split_port("example.com:443") == ("example.com", 443)
    assert split_port("example.com") == ("example.com", None)
    assert split_port("example.com:x") == ("example.com:x", None)


def test_is_ipv4_and_the_loud_zero_cidr() -> None:
    assert is_ipv4("203.0.113.7")
    assert not is_ipv4("example.com")
    assert not is_ipv4("::1")
    import ipaddress

    from msks.consent.specs import IpSpec

    zero = (IpSpec(ipaddress.IPv4Network("0.0.0.0/0"), None),)
    wide = (IpSpec(ipaddress.IPv4Network("10.0.0.0/8"), None),)
    assert allow_all_cidrs(zero) == ("0.0.0.0/0",)
    assert allow_all_cidrs(wide) == ()


def test_ip_spec_matches_addresses() -> None:
    import ipaddress

    from msks.consent.specs import IpSpec

    spec = IpSpec(ipaddress.IPv4Network("10.0.0.0/8"), 443)
    assert spec.matches("10.1.2.3")
    assert not spec.matches("203.0.113.7")

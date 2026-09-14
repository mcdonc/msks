"""Per-workspace egress networking: taps, DHCP, DNS, NAT (#52).

Every egress workspace owns a dedicated /30 carved from the
configured pool: the guest holds the first host address, the
appliance-side tap the second. The tap, its nftables chain, its DHCP
service, and its DNS forwarder all live inside the appliance —
enforcement the guest cannot reach. A workspace without egress gets
none of it: no NIC, no tap, no rules.
"""

from .manager import NetAttachment, NetManager

__all__ = [
    "NetAttachment",
    "NetManager",
]

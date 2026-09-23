"""The egress interceptor (#199): secrets that never ride the wire.

Placeholder sentinel swapping over an embedded mitmproxy — the
working reference is the #194 spike
(``docs/spikes/194-egress-interceptor.md``).
"""

from .ca import WorkspaceCA, load_or_mint, mint_ca, mint_leaf
from .engine import InterceptorAddon, LogBridge, host_matches
from .manager import Armed, Interceptor, PlaceholderEntry

__all__ = [
    "Armed",
    "Interceptor",
    "InterceptorAddon",
    "LogBridge",
    "PlaceholderEntry",
    "WorkspaceCA",
    "host_matches",
    "load_or_mint",
    "mint_ca",
    "mint_leaf",
]

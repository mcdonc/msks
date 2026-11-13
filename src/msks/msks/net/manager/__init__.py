"""The egress attachment lifecycle: one tap + services per workspace (#52).

NetManager is the state object on ``app.state.net``. Enabled and
privileged, ``start()`` arms the shared plumbing once (the NAT
base table, after verifying the kernel's ip_forward sysctl — the
deployment ships it as a boot-time setting, and the daemon never
writes it); every egress workspace boot then ``attach()``s
— tap, per-VM chain, DHCP service, DNS forwarder — and every stop,
kill, or delete ``detach()``es all of it.

Fail-closed: a daemon that cannot arm the plumbing records itself
unavailable, and each egress workspace boot then refuses with a
named cause instead of running with a half-open path. Workspaces
without egress never touch any of this.
"""

import sys

# The nft leaf the suites patch through this package.
from .. import nft  # noqa: F401 — suite seam
from . import consent, forward, helpers, switch
from .core import NetManager as NetManager
from .helpers import (
    FORWARDING as FORWARDING,
)
from .helpers import (
    NetAttachment as NetAttachment,
)
from .helpers import (
    NetServices as NetServices,
)
from .helpers import (
    WorkspaceGuard as WorkspaceGuard,
)
from .helpers import (
    dial_with_retry as dial_with_retry,
)
from .helpers import (
    reject_is_per_flow as reject_is_per_flow,
)
from .helpers import (
    stop_services as stop_services,
)
from .helpers import (
    verify_forwarding as verify_forwarding,
)

_core = sys.modules[__name__ + ".core"]

SUBMODULES = (consent, forward, helpers, switch, _core)

__all__ = [
    "FORWARDING",
    "NetAttachment",
    "NetManager",
    "NetServices",
    "WorkspaceGuard",
    "dial_with_retry",
    "reject_is_per_flow",
    "stop_services",
    "verify_forwarding",
]

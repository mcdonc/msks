"""The secrets surfaces (#510): the placeholder list, the audit
trail, the mint form, and the sentinel panel — one package, the
screens the main screen reaches.

The names importers use re-export from here.
"""

from . import audit, cells, mint, screen, sentinel
from .audit import SecretAuditScreen
from .cells import (
    COVERAGE_CHOICES as COVERAGE_CHOICES,
)
from .cells import (
    COVERAGE_SCOPED as COVERAGE_SCOPED,
)
from .cells import (
    COVERAGE_WIDE as COVERAGE_WIDE,
)
from .cells import (
    DEST_ENTRY as DEST_ENTRY,
)
from .cells import (
    MINT_NAME as MINT_NAME,
)
from .cells import (
    MINT_NOTE as MINT_NOTE,
)
from .cells import (
    MINT_TTL_CHOICES as MINT_TTL_CHOICES,
)
from .cells import (
    MINT_TTLS as MINT_TTLS,
)
from .cells import (
    SCOPED_SENTINEL as SCOPED_SENTINEL,
)
from .cells import (
    SECRET_COLUMNS as SECRET_COLUMNS,
)
from .cells import (
    SECRET_GROWING_LABELS as SECRET_GROWING_LABELS,
)
from .cells import (
    SECRET_TTLS as SECRET_TTLS,
)
from .cells import (
    WIDE_SENTINEL as WIDE_SENTINEL,
)
from .cells import (
    coverage_text as coverage_text,
)
from .cells import (
    dests_cell as dests_cell,
)
from .cells import (
    minted_note as minted_note,
)
from .cells import (
    osc52_copy as osc52_copy,
)
from .cells import (
    osc52_sequence as osc52_sequence,
)
from .cells import (
    refused_dest as refused_dest,
)
from .cells import (
    renew_note as renew_note,
)
from .cells import (
    revoke_note as revoke_note,
)
from .cells import (
    secret_row_cells as secret_row_cells,
)
from .cells import (
    secret_row_text as secret_row_text,
)
from .cells import (
    sentinel_reach as sentinel_reach,
)
from .cells import (
    split_entries as split_entries,
)
from .cells import (
    ttl_default as ttl_default,
)
from .cells import (
    ttl_text as ttl_text,
)
from .mint import MintScreen, WorkspacePicker
from .screen import SecretsScreen
from .sentinel import SentinelPanel, masked_sentinel

__all__ = [
    "MINT_NOTE",
    "SCOPED_SENTINEL",
    "SECRET_TTLS",
    "WIDE_SENTINEL",
    "MintScreen",
    "SecretAuditScreen",
    "SecretsScreen",
    "SentinelPanel",
    "WorkspacePicker",
    "coverage_text",
    "masked_sentinel",
    "osc52_copy",
    "osc52_sequence",
    "secret_row_cells",
    "sentinel_reach",
    "split_entries",
    "ttl_default",
    "ttl_text",
]

SUBMODULES = (audit, cells, mint, screen, sentinel)

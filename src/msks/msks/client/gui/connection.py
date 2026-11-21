"""The connection picker's presenter (phase 5 of #924, #1009):
the presets the window picks its daemon from, and the
environment application a pick makes. Plain data over the
client's own config layer (:mod:`msks.client.config`, #314) —
the file's ``daemons:`` aliases resolve through that layer's
own :func:`~msks.client.config.resolve`, never a parallel set
of readers, so a pick dials exactly what ``msks --daemon
<alias>`` would.

The three preset kinds the plan names: the file's aliases with
``active_daemon`` marking the file's own pick, the environment
the shell exported (the values a worktree's devenv presets
write), and the dev daemon's state-dir preset (``port``,
``bootstrap-token``, ``msks-ca.pem`` under the state dir
``MSKSD_STATE_DIR`` names, else ``.devenv/state/msksd`` below
the working directory — the same files the devenv shell seeds
its exports from, derived here so a window opened outside that
shell still finds the dev daemon).

A pick applies its values to the environment exclusively: the
winner's variable carries the address and the loser's goes
empty (the config layer's own ``filed_address`` rule for an
alias; the dev and environment kinds write the same shape
directly). The environment preset restores the snapshot taken
at the window's open, so a later pick returns the dial to the
shell's own exports, not to a previous pick's writes.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import NO_CONFIG, load_config, resolve, resolve_config_path
from ..config import apply as apply_config

#: The connection-shaping variables (#1009): the ones a pick
#: writes and the snapshot keeps — address (the url or the local
#: socket), the bearer token, the CA file, and the expected
#: image. The rest of the ``MSKSC_*`` family (state roots, the
#: identity) belongs to the operator, not to one daemon, and a
#: connection pick never moves them.
SHAPING_VARS = (
    "MSKSC_URL",
    "MSKSC_SOCKET",
    "MSKSC_TOKEN",
    "MSKSC_CAFILE",
    "MSKSC_EXPECTED_IMAGE",
)

#: The dev daemon's files under its state dir (#231): the port
#: the daemon serves, the bootstrap token it minted, and the CA
#: it wrote on first serve. A dial needs the first two; the CA
#: pins the connection when it stands.
DEV_PORT_FILE = "port"
DEV_TOKEN_FILE = "bootstrap-token"
DEV_CA_FILE = "msks-ca.pem"

#: The dev state dir's default, below the working directory
#: (#1009): the devenv tree's own ``.devenv/state/msksd``, the
#: same default ``MSKSD_STATE_DIR`` relocates everywhere else.
DEV_STATE_DEFAULT = Path(".devenv/state/msksd")

#: The preset kinds' names (#1009): an alias the config file
#: defines, the environment the shell exported, and the dev
#: daemon's state dir.
ALIAS = "alias"
ENVIRONMENT = "environment"
DEV = "dev"


@dataclass(frozen=True)
class ConnectionPreset:
    """One preset the picker offers (#1009): its name for the
    menu, its kind (the pick's application reads it), its
    address for the menu's beside-name display, and whether it
    is the standing connection. Plain data with nothing
    Qt-bound in it."""

    name: str
    kind: str
    address: str
    active: bool = False


def dev_state_dir() -> Path:
    """The dev daemon's state dir (#1009): ``MSKSD_STATE_DIR``
    when the environment names one, else the working
    directory's ``.devenv/state/msksd`` — the tree a dev runs
    the window from."""
    named = os.environ.get("MSKSD_STATE_DIR")
    if named:
        return Path(named)
    return DEV_STATE_DEFAULT


def dev_values(state: Path) -> tuple[str, str, str] | None:
    """The dev daemon's dial (#1009) as ``(url, token,
    cafile)`` — or None when the state dir cannot name one: a
    port or token file missing, empty, or unreadable leaves
    nothing honest to dial. The values read at call time, the
    menu's display and the pick alike: the daemon's port and
    token rotate across its restarts, and a stale menu row
    must not dial a gone daemon."""
    try:
        port = (state / DEV_PORT_FILE).read_text().strip()
        token = (state / DEV_TOKEN_FILE).read_text().strip()
    except OSError:
        return None
    if not port or not token:
        return None
    ca_path = state / DEV_CA_FILE
    ca = str(ca_path) if ca_path.is_file() else ""
    return f"https://127.0.0.1:{port}", token, ca


def snapshot() -> dict[str, str]:
    """The connection-shaping variables as the window found them
    (#1009): the environment preset's own values, taken once at
    the open — a pick that returns to the environment restores
    these, not whatever a later pick wrote."""
    return {name: os.environ.get(name, "") for name in SHAPING_VARS}


def restore(values: dict[str, str]) -> None:
    """The snapshot back into the environment (#1009): a
    variable the shell exported gets its value back, one a pick
    added leaves (removed, not blanked — the readers treat an
    empty export as unset either way, and the environment the
    operator hands the next launch stays clean)."""
    for name in SHAPING_VARS:
        if values.get(name):
            os.environ[name] = values[name]
        else:
            os.environ.pop(name, None)


def read_doc(config: str | None) -> dict:
    """The config file's document (#1009), an empty one for the
    env-only opt-out. An unusable file at menu-build time reads
    empty — the window already runs on what bootstrap resolved,
    and a broken file names itself on the CLI where the
    operator can fix it."""
    try:
        path = resolve_config_path(config)
        return {} if path == NO_CONFIG else load_config(path)
    except ValueError:
        return {}


def entry_address(entry: dict) -> str:
    """One alias entry's address (#1009): its url, else its
    local socket, else the honest empty display."""
    url = entry.get("url")
    if url:
        return str(url)
    return str(entry.get("socket") or "")


def alias_entries(doc: dict) -> list[tuple[str, str]]:
    """The document's ``daemons:`` entries as ``(name,
    address)`` (#1009), the file's own order."""
    entries = doc.get("daemons")
    if not isinstance(entries, dict):
        return []
    rows = []
    for name, entry in entries.items():
        if isinstance(entry, dict):
            rows.append((name, entry_address(entry)))
    return rows


def standing_address(conf, startup: dict[str, str]) -> str:
    """The standing connection's address (#1009): the
    invocation's own resolution when it named one, else the
    environment's export, else empty (the local socket default
    the readers fall to)."""
    for value in (getattr(conf, "url", ""), getattr(conf, "socket", "")):
        if value:
            return value
    for name in ("MSKSC_URL", "MSKSC_SOCKET"):
        if startup.get(name):
            return startup[name]
    return ""


def alias_rows(doc: dict, active_alias: str | None) -> list[ConnectionPreset]:
    """The file's aliases as preset rows (#1009), the
    invocation's own alias pick marked."""
    return [
        ConnectionPreset(name, ALIAS, address, active=name == active_alias)
        for name, address in alias_entries(doc)
    ]


def dev_row(
    standing: str, active_alias: str | None
) -> ConnectionPreset | None:
    """The dev daemon's preset row (#1009), or None when its
    state dir cannot name a dial. The row stands active when
    the standing address is the dev dial itself and no alias
    pick holds the invocation."""
    dev = dev_values(dev_state_dir())
    if dev is None:
        return None
    url = dev[0]
    return ConnectionPreset(
        "dev daemon",
        DEV,
        url,
        active=standing == url and active_alias is None,
    )


def environment_row(
    startup: dict[str, str], standing: str, rows: list[ConnectionPreset]
) -> ConnectionPreset | None:
    """The environment's preset row (#1009), or None when the
    shell exported no address. The row stands active when no
    other row already holds the standing connection."""
    if not (startup.get("MSKSC_URL") or startup.get("MSKSC_SOCKET")):
        return None
    return ConnectionPreset(
        "environment",
        ENVIRONMENT,
        standing,
        active=not any(row.active for row in rows),
    )


def build_presets(
    conf, startup: dict[str, str], config: str | None
) -> list[ConnectionPreset]:
    """The menu's presets (#1009): the file's aliases in the
    file's own order, the dev daemon when its state dir names a
    dial, and the environment when the shell exported an
    address."""
    active_alias = getattr(conf, "daemon", None)
    standing = standing_address(conf, startup)
    rows = alias_rows(read_doc(config), active_alias)
    dev = dev_row(standing, active_alias)
    if dev is not None:
        rows.append(dev)
    env = environment_row(startup, standing, rows)
    if env is not None:
        rows.append(env)
    return rows


def apply_alias(name: str, config: str | None) -> bool:
    """One alias pick through the config layer's own resolution
    (#1009): exactly what ``msks --daemon <name>`` materializes.
    A file that can no longer resolve the name (it changed under
    the menu, its token file left) answers False with the
    environment untouched — the config layer's own refusal
    naming nothing to re-dial."""
    try:
        apply_config(resolve(name, config))
    except ValueError:
        return False
    return True


def apply_dev() -> bool:
    """The dev pick (#1009): the url, token, and CA from the
    state dir as it stands at the pick — the values re-read, a
    rotated port or token honored — with the socket variable
    emptied beside them. A state dir that cannot name a dial
    answers False and touches nothing."""
    dev = dev_values(dev_state_dir())
    if dev is None:
        return False
    url, token, ca = dev
    os.environ["MSKSC_URL"] = url
    os.environ["MSKSC_SOCKET"] = ""
    os.environ["MSKSC_TOKEN"] = token
    if ca:
        os.environ["MSKSC_CAFILE"] = ca
    return True


class Connections:
    """The picker's state and application seam (#1009): the
    preset list the menu builds from, and the pick that applies
    one to the environment. The config layer owns every
    resolution — the aliases through its own ``resolve``, the
    address materialization through its own ``apply``."""

    def __init__(self, conf=None, *, config: str | None = None) -> None:
        self.conf = conf
        self.config = config
        self.startup = snapshot()

    def presets(self) -> list[ConnectionPreset]:
        """The menu's rows, built over the open's own
        snapshot."""
        return build_presets(self.conf, self.startup, self.config)

    def pick(self, preset: ConnectionPreset) -> bool:
        """Apply one preset to the environment (#1009), the
        answer naming whether the pick applied."""
        if preset.kind == ALIAS:
            return apply_alias(preset.name, self.config)
        if preset.kind == DEV:
            return apply_dev()
        restore(self.startup)
        return True

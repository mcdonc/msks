"""The msksd YAML configuration file (#46), modeled on klangkd's.

A YAML file is the primary substrate for a deployment's durable
settings; ``MSKSD_*`` environment variables override file values,
and the settings dataclass defaults are the floor — precedence
**env > config file > built-in defaults**.

Mechanics: the file is parsed into a flat ``MSKSD_*`` variable layer
(:data:`CONFIG_ENV_VARS` is the single key↔variable table) and folded
under the live environment as :class:`LayeredEnv`, which the settings
parsers read like any env mapping. Native YAML scalars keep their
meaning — ``port: 8660`` and ``access_log: true`` arrive at the
parsers as ``"8660"`` and ``"true"`` — so both sources share one
validation path and an invalid value fails identically wherever it
came from, with the error naming the ``MSKSD_*`` variable.

The file is located through three ``--config`` modes (klangkd's):

- bare ``msksd`` → ``$MSKSD_CONFIG_DIR/msksd.yaml`` (default
  ``~/.config/msksd/msksd.yaml``); a missing file is generated as a
  commented template pointing at the docs.
- ``msksd --config /path/to/msksd.yaml`` → exactly that file; a
  missing file is a startup error. Explicit paths are never
  auto-generated.
- ``msksd --config=none`` → environment variables and built-in
  defaults only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import yaml

from .settings import Settings

# The ``--config=none`` sentinel: env vars + built-in defaults only.
NO_CONFIG = "none"

# The filename inside the config directory.
CONFIG_FILENAME = "msksd.yaml"

# The single key↔variable table (#46): each config-file key is a
# settings dataclass field name (snake_case), nested under its
# section, and each maps to the ``MSKSD_*`` variable that overrides
# it. ``state_dir`` lives under ``vmm:`` and feeds both consumers of
# ``MSKSD_STATE_DIR``: the VMM driver's artifacts and the server's
# sqlite database path (``<state_dir>/msks.db`` — there is no
# separate ``db_path`` key, matching the env var).
CONFIG_ENV_VARS: dict[str, dict[str, str]] = {
    "vmm": {
        "driver": "MSKSD_VMM_DRIVER",
        "cloud_hypervisor": "MSKSD_CLOUD_HYPERVISOR",
        "state_dir": "MSKSD_STATE_DIR",
        "socket_wait_timeout_s": "MSKSD_SOCKET_WAIT_TIMEOUT_S",
        "request_timeout_s": "MSKSD_REQUEST_TIMEOUT_S",
        "shutdown_timeout_s": "MSKSD_SHUTDOWN_TIMEOUT_S",
        "vsock_shell_port": "MSKSD_VSOCK_SHELL_PORT",
        "vsock_wait_timeout_s": "MSKSD_VSOCK_WAIT_TIMEOUT_S",
        "default_image": "MSKSD_DEFAULT_IMAGE",
        "qemu_img": "MSKSD_QEMU_IMG",
        "mkfs_ext4": "MSKSD_MKFS_EXT4",
        "mkisofs": "MSKSD_MKISOFS",
        "host_name": "MSKSD_HOST_NAME",
        "root_mib": "MSKSD_ROOT_MIB",
        "home_mib": "MSKSD_HOME_MIB",
    },
    "server": {
        "host": "MSKSD_HOST",
        "port": "MSKSD_PORT",
        "tls_cert": "MSKSD_TLS_CERT",
        "tls_key": "MSKSD_TLS_KEY",
        "event_poll_s": "MSKSD_EVENT_POLL_S",
        "bootstrap_token": "MSKSD_BOOTSTRAP_TOKEN",
        "access_log": "MSKSD_ACCESS_LOG",
    },
    "k8s": {
        "namespace": "MSKSD_K8S_NAMESPACE",
        "runner_image": "MSKSD_K8S_RUNNER_IMAGE",
        "kubeconfig": "MSKSD_KUBECONFIG",
        "api_timeout_s": "MSKSD_K8S_API_TIMEOUT_S",
        "storage_class": "MSKSD_K8S_STORAGE_CLASS",
        "workspace_storage_gib": "MSKSD_K8S_WORKSPACE_STORAGE_GIB",
    },
    "net": {
        "enabled": "MSKSD_EGRESS_ENABLED",
        "pool": "MSKSD_EGRESS_SUBNET",
        "uplink": "MSKSD_EGRESS_UPLINK",
        "dns_upstream": "MSKSD_EGRESS_DNS_UPSTREAM",
        "ip_tool": "MSKSD_IP_TOOL",
        "nft_tool": "MSKSD_NFT_TOOL",
        "lease_s": "MSKSD_EGRESS_LEASE_S",
        "dns_timeout_s": "MSKSD_EGRESS_DNS_TIMEOUT_S",
    },
}


def config_dir() -> str:
    """The config-tree root: ``$MSKSD_CONFIG_DIR``, else
    ``$XDG_CONFIG_HOME/msksd`` (XDG fallback ``~/.config/msksd``).

    Resolved purely from the environment — ``msksd.yaml`` cannot
    relocate the config tree it lives in, so the root must be
    computable before the file is located (klangkd's bootstrap rule).
    """
    override = os.environ.get("MSKSD_CONFIG_DIR")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return str(Path(xdg).expanduser() / "msksd")


def default_config_path() -> str:
    """The path a bare ``msksd`` resolves: ``<config_dir>/msksd.yaml``."""
    return os.path.join(config_dir(), CONFIG_FILENAME)


def scalar_to_str(key: str, value: object) -> str:
    """A YAML scalar as the env-var string the settings parsers read.

    Native YAML scalars keep their meaning: a bare int/float arrives
    as its digits, a bare bool as ``true``/``false``, a quoted string
    as itself. Anything else (a list or mapping — no setting is one
    today) is rejected so a misplaced block fails at startup instead
    of stringifying into garbage.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    kind = type(value).__name__
    raise ValueError(
        f"config key {key!r} must be a number, boolean, or string, got {kind}"
    )


class UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses duplicate mapping keys.

    PyYAML keeps the last of duplicate keys silently; the config file
    fails fast instead — an operator appending a second ``vmm:``
    block to a long file gets an error naming the key, not a silent
    override of everything above it.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError(f"duplicate config key {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep)


def parse_config_doc(text: str, path: str) -> dict[str, str]:
    """Parse config-file text into an ``MSKSD_*`` env-var layer.

    Unknown sections and keys are errors — a typo'd key fails fast at
    startup naming the key and the valid ones. A null value (``key:``
    with nothing after it) is the unset form: the default (or the
    environment) applies, exactly as an unset variable would.
    """
    try:
        doc = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        kind = type(doc).__name__
        raise ValueError(
            f"{path}: the config file must be a mapping of sections, got {kind}"
        )
    return section_layer(doc, path)


def section_layer(doc: dict, path: str) -> dict[str, str]:
    """The validated section→key walk of a parsed config document."""
    layer: dict[str, str] = {}
    for section, keys in doc.items():
        table = CONFIG_ENV_VARS.get(section)
        if table is None:
            valid = ", ".join(sorted(CONFIG_ENV_VARS))
            raise ValueError(
                f"{path}: unknown config section {section!r} (valid sections: {valid})"
            )
        if not isinstance(keys, dict):
            kind = type(keys).__name__
            raise ValueError(
                f"{path}: section {section!r} must be a mapping of keys, got {kind}"
            )
        key_layer(layer, section, keys, table, path)
    return layer


def key_layer(
    layer: dict[str, str],
    section: str,
    keys: dict,
    table: dict[str, str],
    path: str,
) -> None:
    """Fold one section's keys into the env-var layer in place."""
    for key, value in keys.items():
        if key not in table:
            valid = ", ".join(sorted(table))
            raise ValueError(
                f"{path}: unknown config key {section}.{key} (valid keys: {valid})"
            )
        if value is None:
            continue
        layer[table[key]] = scalar_to_str(f"{section}.{key}", value)


def file_env_overrides(path: str) -> dict[str, str]:
    """Read the config file at *path* into an ``MSKSD_*`` env-var layer.

    An unreadable file raises ``OSError``; a malformed document or an
    unknown section/key raises ``ValueError`` — both are startup
    errors the caller reports.
    """
    return parse_config_doc(Path(path).read_text(encoding="utf-8"), path)


class LayeredEnv(Mapping):
    """The live environment over the config-file layer (#46).

    Lookup order: ``os.environ`` first, then the file — so a variable
    set in the process overrides the same key in the file. A variable
    set to an empty string is the unset form and falls through to the
    file, matching the settings parsers' empty-means-default rule.
    """

    def __init__(self, overrides: Mapping[str, str]) -> None:
        self._overrides = dict(overrides)

    def __getitem__(self, name: str) -> str:
        value = os.environ.get(name)
        if value:
            return value
        return self._overrides[name]

    def __iter__(self):
        merged = dict(self._overrides)
        merged.update(os.environ)
        return iter(merged)

    def __len__(self) -> int:
        merged = dict(self._overrides)
        merged.update(os.environ)
        return len(merged)


def load_settings(config: str | None, *, generate: bool = True) -> Settings:
    """Settings from the resolved config file + environment (#46).

    *config* is the ``--config`` argument: ``None`` resolves the
    default path (generating the template on first run), ``"none"``
    reads env vars and defaults only, and a path reads exactly that
    file. Precedence env > file > defaults holds for every key.
    *generate* is ``False`` on the SIGHUP reload path: a missing
    default file is refused there instead of regenerated.
    """
    path = resolve_config_path(config, generate=generate)
    if path == NO_CONFIG:
        return Settings.from_env()
    overrides = file_env_overrides(path)
    if not overrides:
        return Settings.from_env()
    return Settings.from_env(LayeredEnv(overrides))


def resolve_config_path(config: str | None, *, generate: bool = True) -> str:
    """Resolve the ``--config`` value into a path or the "none" sentinel.

    Three modes (klangkd's, #46):

    - ``None`` (bare ``msksd``, no ``--config``) → the default path
      at ``$MSKSD_CONFIG_DIR/msksd.yaml`` (default
      ``~/.config/msksd/msksd.yaml``), **generated as a near-empty
      template on first run** when missing.
    - ``"none"`` → the explicit env-only opt-out (no config file).
    - a path → that path, required to exist; missing raises
      ``ValueError``. Explicit paths are never auto-generated.

    *generate* arms first-run generation for the default path only;
    the SIGHUP reload passes ``generate=False`` so a deleted default
    file is refused ("config file not found") instead of silently
    regenerating the template and reverting every file-set value —
    a reload is not a first run.
    """
    if config is None:
        return default_path_or_error(generate)
    if config == NO_CONFIG:
        return NO_CONFIG
    if not Path(config).is_file():
        raise ValueError(f"config file not found: {config}")
    return config


def default_path_or_error(generate: bool) -> str:
    """The default path: generated on first run, or required to exist."""
    if generate:
        return ensure_default_config()
    path = default_config_path()
    if not os.path.isfile(path):
        raise ValueError(f"config file not found: {path}")
    return path


def ensure_default_config() -> str:
    """The default config path, generating the template when missing."""
    path = default_config_path()
    if os.path.isfile(path):
        return path
    try:
        generate_template(path)
    except FileExistsError:
        # A concurrent msksd (e.g. a systemd restart overlap)
        # generated the file between our isfile check and the open.
        # Treat it as "the file is there now" and proceed.
        pass
    return path


def render_template() -> str:
    """The generated ``msksd.yaml`` body: a commented near-empty file.

    The template's purpose is discoverability — this is where the
    daemon's config lives — plus a commented example of every section
    carrying its defaults. The settings themselves come from the
    built-in defaults until the operator edits the file.
    """
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"""\
# msksd configuration — generated on first run ({timestamp}).
#
# msksd looked here because it was started without a --config
# argument and found no file at
# ${{MSKSD_CONFIG_DIR:-$XDG_CONFIG_HOME/msksd}}/msksd.yaml.
#
# This file is the durable home for msksd's settings. Every key here
# also exists as an MSKSD_* environment variable; a variable set in
# the process overrides the same key in this file, and a key set
# nowhere uses the built-in default. Precedence:
#   environment > this file > built-in defaults
#
# The four sections mirror the settings tree, and keys are the
# settings field names in snake_case:
#   vmm:    the local cloud-hypervisor driver (paths, timeouts,
#           artifact sizes)
#   server: the HTTPS + WSS API listener (bind address, TLS,
#           database, tokens)
#   k8s:    the Kubernetes runner driver (namespace, images,
#           storage)
#   net:    per-workspace egress networking (pool, uplink, tools)
#
# Values may be written as native YAML scalars — port: 8660,
# access_log: true, socket_wait_timeout_s: 12.5 — or as quoted
# strings; both parse identically.
#
# SIGHUP re-reads this file: changed values apply to everything that
# reads settings live (the API listener's address, port, and TLS
# material are bound at startup and keep their startup values until
# a restart).
#
# The full key-by-key reference — each key with its environment
# variable, type, default, and meaning — is docs/config.md in the
# msks repository: https://github.com/mcdonc/msks
#
# --- Example (every line commented; the values shown are the
# --- built-in defaults) ---
#
# server:
#   host: 127.0.0.1          # the API listener's bind address
#   port: 8660               # the API listener's port
#   tls_cert: /etc/msksd/tls.crt   # operator-provided TLS material;
#   tls_key: /etc/msksd/tls.key    # both unset -> a self-signed CA is
#                            # generated on first run and its
#                            # fingerprint printed for pinning
#   event_poll_s: 1.0        # seconds between workspace status scans
#   bootstrap_token: secret  # seeds the first bearer token at first
#                            # boot
#   access_log: false        # uvicorn access logging; the events
#                            # websocket carries its token in the
#                            # query string, which the access log
#                            # would persist
# vmm:
#   driver: local            # local | k8s
#   state_dir: ~/.local/state/msksd  # the daemon's state: the sqlite
#                            # database (<state_dir>/msks.db) and
#                            # per-workspace artifacts
#   cloud_hypervisor: cloud-hypervisor  # the VMM binary the local
#                            # driver execs
#   vsock_shell_port: 1023   # the vsock port the guest console
#                            # listens on
#   vsock_wait_timeout_s: 15.0   # seconds to wait for the console at
#                            # boot
#   socket_wait_timeout_s: 10.0   # seconds to wait for the VMM API
#                            # socket at start
#   request_timeout_s: 5.0   # seconds per VMM API request
#   shutdown_timeout_s: 20.0 # seconds a stop waits for guest poweroff
#   default_image: ""        # a container-image tar imported and
#                            # designated default on first boot
#   qemu_img: qemu-img       # builds the root overlay
#   mkfs_ext4: mkfs.ext4     # builds the /home volume
#   mkisofs: mkisofs         # builds the user_data seed disk
#   host_name: ""            # the host recorded as owning created
#                            # workspaces; empty -> the hostname
#   root_mib: 10240          # default root overlay size (MiB)
#   home_mib: 2048           # default /home volume size (MiB)
# k8s:
#   namespace: msks          # the namespace workspaces run in
#   runner_image: registry.k8s.io/pause:3.10
#   kubeconfig: ""           # a kubeconfig path; empty -> the
#                            # cluster's ambient configuration
#   api_timeout_s: 30.0      # seconds per Kubernetes API request
#   storage_class: ""        # the PVC storage class; empty -> the
#                            # cluster's default
#   workspace_storage_gib: "" # fixed PVC size (GiB); empty ->
#                            # derived from the workspace's disks
# net:
#   enabled: false           # arm per-workspace NICs, DHCP, NAT, and
#                            # the DNS forwarder
#   pool: 172.31.0.0/16      # the IPv4 pool per-workspace /30s are
#                            # carved from
#   uplink: eth0             # the interface egress is NATed out of
#   dns_upstream: ""         # the resolver to relay DNS to; empty ->
#                            # the appliance's /etc/resolv.conf
#   ip_tool: ip              # the ip binary
#   nft_tool: nft            # the nft binary
#   lease_s: 3600            # DHCP lease seconds
#   dns_timeout_s: 3.0       # seconds waiting on the upstream
#                            # resolver
"""


def generate_template(path: str) -> None:
    """Write the first-run ``msksd.yaml`` template at *path*.

    The parent directory is created (0700) when missing. An existing
    file is the operator's config: ``open("x")`` refuses to overwrite
    it, failing loudly if the file appeared between the caller's
    existence check and now (a concurrent ``msksd``).
    """
    body = render_template()
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "x", encoding="utf-8") as f:
        f.write(body)

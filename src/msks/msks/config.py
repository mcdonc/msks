"""The msksd YAML configuration file (#46), modeled on klangkd's.

A YAML file is the primary substrate for a deployment's durable
settings; ``MSKSD_*`` environment variables override file values,
and the settings dataclass defaults are the floor — precedence
**env > config file > built-in defaults**.

Key mapping (klangkd's convention): a config key is its ``MSKSD_*``
variable with the prefix stripped and lowercased — ``MSKSD_PORT`` →
``port``, ``MSKSD_EGRESS_SUBNET`` → ``egress_subnet``. The mapping
is derived from :data:`SETTING_ENV_VARS` by that one rule, so the
file spelling and the variable spelling cannot drift apart, and
either is recoverable from the other without a lookup table.

Mechanics: the file is parsed into a flat ``MSKSD_*`` variable layer
and folded under the live environment as :class:`LayeredEnv`, which
the settings parsers read like any env mapping. Native YAML scalars
keep their meaning — ``port: 8660`` and ``access_log: true`` arrive
at the parsers as ``"8660"`` and ``"true"`` — so both sources share
one validation path and an invalid value fails identically wherever
it came from, with the error naming the ``MSKSD_*`` variable.

The file is located through three ``--config`` modes (klangkd's):

- bare ``msksd`` → ``$MSKSD_CONFIG_DIR/msksd.yaml`` (default
  ``~/.config/msksd/msksd.yaml``); a missing file is generated as a
  commented template pointing at the docs.
- ``msksd --config /path/to/msksd.yaml`` → exactly that file; a
  missing file is a startup error. Explicit paths are never
  auto-generated.
- ``msksd --config=none`` → environment variables and built-in
  defaults only.

``MSKSD_CONFIG_DIR`` is deliberately not a config key: the file
cannot relocate the config tree it lives in, so the tree root must
be resolvable from the environment before the file is located
(klangkd's bootstrap rule).
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

# Every settings variable (#46), grouped by the settings class that
# reads it. The config-key form is derived by the one rule — strip
# ``MSKSD_``, lowercase — so each group here reads as the file's
# documentation order, not a separate naming scheme.
SETTING_ENV_VARS: tuple[str, ...] = (
    # VmmSettings — the local cloud-hypervisor driver.
    "MSKSD_VMM_DRIVER",
    "MSKSD_CLOUD_HYPERVISOR",
    "MSKSD_STATE_DIR",
    "MSKSD_SOCKET_WAIT_TIMEOUT_S",
    "MSKSD_REQUEST_TIMEOUT_S",
    "MSKSD_SHUTDOWN_TIMEOUT_S",
    "MSKSD_VSOCK_SHELL_PORT",
    "MSKSD_VSOCK_WAIT_TIMEOUT_S",
    "MSKSD_FORWARD_WAIT_TIMEOUT_S",
    "MSKSD_CONSOLE_STALL_TIMEOUT_S",
    "MSKSD_MOVE_WAIT_TIMEOUT_S",
    "MSKSD_DEFAULT_IMAGE",
    "MSKSD_QEMU_IMG",
    "MSKSD_MKFS_EXT4",
    "MSKSD_MKISOFS",
    "MSKSD_HOST_NAME",
    "MSKSD_ROOT_MIB",
    "MSKSD_HOME_MIB",
    "MSKSD_SSH_KEY_TYPE",
    # ServerSettings — the API listener.
    "MSKSD_HOST",
    "MSKSD_PORT",
    "MSKSD_TLS_CERT",
    "MSKSD_TLS_KEY",
    "MSKSD_EVENT_POLL_S",
    "MSKSD_BOOTSTRAP_TOKEN",
    "MSKSD_ACCESS_LOG",
    # K8sSettings — the Kubernetes runner driver.
    "MSKSD_K8S_NAMESPACE",
    "MSKSD_K8S_RUNNER_IMAGE",
    "MSKSD_KUBECONFIG",
    "MSKSD_K8S_API_TIMEOUT_S",
    "MSKSD_K8S_STORAGE_CLASS",
    "MSKSD_K8S_WORKSPACE_STORAGE_GIB",
    # NetSettings — per-workspace egress networking.
    "MSKSD_EGRESS_ENABLED",
    "MSKSD_EGRESS_SUBNET",
    "MSKSD_EGRESS_UPLINK",
    "MSKSD_EGRESS_DNS_UPSTREAM",
    "MSKSD_IP_TOOL",
    "MSKSD_NFT_TOOL",
    "MSKSD_EGRESS_LEASE_S",
    "MSKSD_EGRESS_DNS_TIMEOUT_S",
)

# The key↔variable mapping, derived by the one rule. ``state_dir``
# (``MSKSD_STATE_DIR``) feeds both consumers of the variable: the
# VMM driver's artifacts and the server's sqlite database path
# (``<state_dir>/msks.db`` — there is no separate ``db_path`` key,
# matching the environment variable).
CONFIG_ENV_VARS: dict[str, str] = {
    var.removeprefix("MSKSD_").lower(): var for var in SETTING_ENV_VARS
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
    """A safe loader that refuses duplicate mapping keys and merge keys.

    PyYAML keeps the last of duplicate keys silently; the config file
    fails fast instead — an operator appending a second block to a
    long file gets an error naming the key, not a silent override of
    everything above it. Merge keys (``<<: *anchor``) are refused
    with their own message: the file is flat and every key is spelled
    out, so an anchored base has nothing to merge into — and every
    practical merge carries a mapping-valued anchor carrier, which
    is itself not a config key.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            note_key(self, key_node, seen, deep)
        return super().construct_mapping(node, deep)


def note_key(loader, key_node, seen: set, deep: bool) -> None:
    """Validate one mapping key in place: refuse merge keys and
    complex (non-scalar) keys, and duplicate keys."""
    if key_node.tag == "tag:yaml.org,2002:merge":
        raise ValueError(
            "merge keys (<<) are not supported by the msksd "
            "config file; write each key out"
        )
    key = loader.construct_object(key_node, deep=deep)
    try:
        duplicate = key in seen
    except TypeError:
        raise ValueError("config keys must be scalars, not lists or mappings") from None
    if duplicate:
        raise ValueError(f"duplicate config key {key!r}")
    seen.add(key)


def parse_config_doc(text: str, path: str) -> dict[str, str]:
    """Parse config-file text into an ``MSKSD_*`` env-var layer.

    Unknown keys are errors — a typo'd key fails fast at startup
    naming the key and the valid ones. A null value (``key:`` with
    nothing after it) is the unset form: the default (or the
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
            f"{path}: the config file must be a mapping of keys, got {kind}"
        )
    return key_layer(doc, path)


def key_layer(doc: dict, path: str) -> dict[str, str]:
    """The validated key walk of a parsed config document."""
    layer: dict[str, str] = {}
    for key, value in doc.items():
        if not isinstance(key, str):
            raise ValueError(f"{path}: config keys must be strings, got {key!r}")
        var = CONFIG_ENV_VARS.get(key)
        if var is None:
            valid = ", ".join(sorted(CONFIG_ENV_VARS))
            raise ValueError(
                f"{path}: unknown config key {key!r} (valid keys: {valid})"
            )
        if value is None:
            continue
        layer[var] = scalar_to_str(key, value)
    return layer


def file_env_overrides(path: str) -> dict[str, str]:
    """Read the config file at *path* into an ``MSKSD_*`` env-var layer.

    Raises on anything the operator should see at startup: an
    unreadable file, a malformed document, an unknown key — the
    callers' guards report all of them as one clean line.
    """
    return parse_config_doc(Path(path).read_text(encoding="utf-8"), path)


class LayeredEnv(Mapping):
    """The live environment over the config-file layer (#46).

    Lookup order: ``os.environ`` first, then the file — so a variable
    set in the process overrides the same key in the file. A variable
    set to an empty string is the unset form and falls through to the
    file, matching the settings parsers' empty-means-default rule.
    Iteration applies the same rule: an empty-string environment
    entry does not shadow the file's value.
    """

    def __init__(self, overrides: Mapping[str, str]) -> None:
        self._overrides = dict(overrides)

    def _merged(self) -> dict[str, str]:
        merged = dict(self._overrides)
        merged.update({name: value for name, value in os.environ.items() if value})
        return merged

    def __getitem__(self, name: str) -> str:
        value = os.environ.get(name)
        if value:
            return value
        return self._overrides[name]

    def __iter__(self):
        return iter(self._merged())

    def __len__(self) -> int:
        return len(self._merged())


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
    if Path(config).is_dir():
        raise ValueError(f"config path is a directory: {config}")
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
    daemon's config lives — plus a commented example of every key
    carrying its default. The settings themselves come from the
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
# also exists as an MSKSD_* environment variable, spelled the same
# with the prefix stripped and lowercased (MSKSD_PORT -> port,
# MSKSD_EGRESS_SUBNET -> egress_subnet). A variable set in the
# process overrides the same key in this file, and a key set nowhere
# uses the built-in default. Precedence:
#   environment > this file > built-in defaults
#
# The file is flat — one key per setting, no sections. The comments
# below group the keys by the subsystem that reads them.
#
# Values may be written as native YAML scalars — port: 8660,
# access_log: true, socket_wait_timeout_s: 12.5 — or as quoted
# strings; both parse identically.
#
# SIGHUP re-reads this file: changed values apply to everything that
# reads settings live. The API listener's address, port, TLS
# material, and access logging are bound at startup and keep their
# startup values until a restart; so are the database path, the
# workspace status scan's interval, the egress pool's NAT base
# table, and the egress-enabled switch itself.
#
# The full key-by-key reference — each key with its environment
# variable, type, default, and meaning — is docs/config.md in the
# msks repository: https://github.com/mcdonc/msks
#
# --- Example (every line commented; the values shown are the
# --- built-in defaults) ---
#
# --- The API listener ---
# host: 127.0.0.1           # the listener's bind address
# port: 8660                # the listener's port
# tls_cert: /etc/msksd/tls.crt  # operator-provided TLS material;
# tls_key: /etc/msksd/tls.key   # both unset -> a self-signed CA is
#                           # generated on first run and its
#                           # fingerprint printed for pinning
# event_poll_s: 1.0         # seconds between workspace status
#                           # scans (applies at startup)
# bootstrap_token: secret   # seeds the first bearer token at first
#                           # boot
# access_log: false         # uvicorn access logging; the events
#                           # websocket carries its token in the
#                           # query string, which the access log
#                           # would persist
#
# --- The local cloud-hypervisor driver ---
# vmm_driver: local         # local | k8s
# state_dir: ~/.local/state/msksd  # the daemon's state: the sqlite
#                           # database (<state_dir>/msks.db) and
#                           # per-workspace artifacts
# cloud_hypervisor: cloud-hypervisor  # the VMM binary the local
#                           # driver execs
# vsock_shell_port: 1023    # the vsock port the guest console
#                           # listens on
# vsock_wait_timeout_s: 15.0    # seconds to wait for the console at
#                           # boot
# console_stall_timeout_s: 60.0  # seconds of input-unanswered
#                           # silence before a console session is
#                           # closed as stalled (4502); 0 disables
# socket_wait_timeout_s: 10.0   # seconds to wait for the VMM API
#                           # socket at start
# request_timeout_s: 5.0    # seconds per VMM API request
# shutdown_timeout_s: 20.0  # seconds a stop waits for guest poweroff
# default_image: ""         # a container-image tar imported and
#                           # designated default on first boot
# qemu_img: qemu-img        # builds the root overlay
# mkfs_ext4: mkfs.ext4      # builds the /home volume
# mkisofs: mkisofs          # builds the user_data seed disk
# host_name: ""             # the host recorded as owning created
#                           # workspaces; empty -> the hostname
# root_mib: 10240           # default root overlay size (MiB)
# home_mib: 2048            # default /home volume size (MiB)
# ssh_key_type: ed25519     # the identity type minted at create
#                           # (#111): ed25519 (the FIPS-approvable
#                           # default, #138; accepted by ssh clients
#                           # restricted to the common
#                           # ssh-ed25519,ssh-rsa set), ecdsa
#                           # (P-256), or rsa
#
# --- The Kubernetes runner driver ---
# k8s_namespace: msks       # the namespace workspaces run in
# k8s_runner_image: registry.k8s.io/pause:3.10
# kubeconfig: ""            # a kubeconfig path; empty -> the
#                           # cluster's ambient configuration
# k8s_api_timeout_s: 30.0   # seconds per Kubernetes API request
# k8s_storage_class: ""     # the PVC storage class; empty -> the
#                           # cluster's default
# k8s_workspace_storage_gib: ""  # fixed PVC size (GiB); empty ->
#                           # derived from the workspace's disks
#
# --- Per-workspace egress networking ---
# egress_enabled: false     # arm per-workspace NICs, DHCP, NAT, and
#                           # the DNS forwarder (applies at startup;
#                           # the appliance turns this on)
# egress_subnet: 172.31.0.0/16  # the IPv4 pool per-workspace /30s
#                           # are carved from
# egress_uplink: eth0       # the interface egress is NATed out of
#                           # (the base NAT table applies at startup)
# egress_dns_upstream: ""   # the resolver to relay DNS to; empty ->
#                           # the appliance's /etc/resolv.conf
# ip_tool: ip               # the ip binary
# nft_tool: nft             # the nft binary
# egress_lease_s: 3600      # DHCP lease seconds
# egress_dns_timeout_s: 3.0 # seconds waiting on the upstream
#                           # resolver
"""


def generate_template(path: str) -> None:
    """Write the first-run ``msksd.yaml`` template at *path*.

    The parent directory is created (0700) when missing, and the
    file is written 0600 — the template's examples name credentials
    (``bootstrap_token``), so the file joins the house pattern of
    secret-bearing artifacts readable only by the daemon's user. An
    existing file is the operator's config: the exclusive create
    refuses to overwrite it, failing loudly if the file appeared
    between the caller's existence check and now (a concurrent
    ``msksd``).
    """
    body = render_template()
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)

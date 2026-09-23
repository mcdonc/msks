"""Settings for msksd, loaded from ``MSKSD_*`` environment variables.

Env naming follows the house rule: the category word ``MSKSD``
(daemon) concatenated onto the prefix with no underscore before it,
then a single underscore before the field (``MSKSD_STATE_DIR``,
``MSKSD_PORT``). All values are read live off
``app.state.settings`` — never materialized onto subsystems — so a
runtime settings swap (SIGHUP) propagates without per-module
``reconfigure()`` calls.

The parsers read through a mapping that defaults to the live
``os.environ``. :mod:`msks.config` layers a parsed YAML config file
under the environment — precedence **env > config file > built-in
defaults** (#46) — so both sources share one validation path: an
invalid value fails the same way wherever it came from, and the
error names the ``MSKSD_*`` variable either way.
"""

import ipaddress
import math
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from pathlib import Path

from .consent.specs import EGRESS_MODES, MODE_ALLOW
from .identity import KEY_TYPES

VALID_DRIVERS = ("local",)

#: The secret-store providers v1 wires (#198): the SecretSpec CLI
#: URIs the daemon knows how to build from settings.
VALID_SECRET_PROVIDERS = ("file", "age", "awssm", "bws")


def live_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """The env to read: an explicit mapping, or the live environment."""
    return os.environ if env is None else env


def _env(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    return default if value in (None, "") else value


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _env(env, name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _parse_positive_int(
    env: Mapping[str, str], name: str, default: int
) -> int:
    value = _parse_int(env, name, default)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _env(env, name, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


def move_wait_seconds(env) -> float:
    """The move-wait bound from MSKSD_MOVE_WAIT_TIMEOUT_S (#80):
    zero is a valid fail-fast deadline, a negative one is a
    configuration error."""
    seconds = _env_float(env, "MSKSD_MOVE_WAIT_TIMEOUT_S", 120.0)
    if seconds < 0:
        raise ValueError(
            f"MSKSD_MOVE_WAIT_TIMEOUT_S must be zero or positive, "
            f"got {seconds}"
        )
    return seconds


@dataclass
class VmmSettings:
    """Local VMM (cloud-hypervisor) driver settings."""

    driver: str = "local"
    cloud_hypervisor: str = "cloud-hypervisor"
    state_dir: Path = field(
        default_factory=lambda: Path("~/.local/state/msksd").expanduser()
    )
    socket_wait_timeout_s: float = 10.0
    request_timeout_s: float = 5.0
    shutdown_timeout_s: float = 20.0
    vsock_shell_port: int = 1023
    # Console bring-up wait: generous by default — nested-virt guests
    # can take longer than bare metal to arm the vsock device.
    vsock_wait_timeout_s: float = 15.0
    # Forward bring-up wait (#109): a freshly booted guest races
    # DHCP against its services, so a refused dial during this window
    # retries; past the deadline the refusal names the cause.
    forward_wait_timeout_s: float = 15.0
    # Mid-session stall window (#103): the guest pty echoes every
    # input byte, so input that draws zero guest bytes for this long
    # names a wedged stream, and the console websocket closes with
    # 4502 instead of hanging open and silent. An idle session never
    # trips it — the clock only runs after client input.
    console_stall_timeout_s: float = 60.0
    # How long a boot or volume move waits for the workspace's other
    # volume move to finish (#80): a stalled reader holds an export's
    # lock as long as its connection lives, and the waiter answers a
    # named 409 past this bound instead of hanging with it. Zero is a
    # valid fail-fast deadline.
    move_wait_timeout_s: float = 120.0
    # A host-side container-image tar imported into the catalog on first boot
    # and designated default (the dev daemon points this at its state
    # image's store path through its cmdline bridge).
    default_image: str = ""
    # Per-workspace persistent artifacts (#14): the tools that make
    # them, the host that owns them, and their default sizes.
    qemu_img: str = "qemu-img"
    mkfs_ext4: str = "mkfs.ext4"
    # The #184 resize pair: e2fsck quiets the home volume's journal
    # before resize2fs moves it (both directions). The deployment
    # ships both (#183's state-disk grow pins them in its root
    # image); a bare-host daemon points these at its own e2fsprogs.
    resize2fs: str = "resize2fs"
    e2fsck: str = "e2fsck"
    # The tool that builds the #41 seed disk: a small iso9660 image
    # labeled ``cidata`` carrying the workspace's user_data. mkisofs
    # is genisoimage (same tool): cdrtools and every distro's
    # alternatives system serve the name.
    mkisofs: str = "mkisofs"
    # The host that owns locally-created artifacts; every instance
    # knows its name (direct constructions skip from_env).
    host_name: str = field(default_factory=socket.gethostname)
    root_mib: int = 10240
    home_mib: int = 2048
    # The state-disk pressure thresholds (#184): past the warn
    # percentage used the watcher publishes a named warning, and at
    # or below the floor's free bytes workspace creates answer 507
    # — a named refusal where #180's EIO storm used to be the first
    # signal.
    storage_warn_pct: int = 90
    storage_floor_mib: int = 512
    # The identity key type msksd mints at create (#111): Ed25519
    # is FIPS-approvable (FIPS 186-5) and accepted by ssh clients
    # restricted to the common ssh-ed25519,ssh-rsa set (#138);
    # ECDSA P-256 and RSA remain choices for validated crypto
    # modules that predate EdDSA. The type is a setting so the
    # default can move without code surgery (#115).
    ssh_key_type: str = "ed25519"
    # URL image imports (#258): the download's size ceiling (the
    # archive alone — the storage floor still counts its import
    # cost twice, like a path import) and its overall deadline.
    image_import_max_mib: int = 8192
    image_import_timeout_s: float = 600.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VmmSettings:
        return vmm_settings_from_env(cls, live_env(env))


@dataclass
class ServerSettings:
    """The API server's own settings (HTTPS + WSS on one listener)."""

    host: str = "127.0.0.1"
    port: int = 8660
    tls_cert: str | None = None
    tls_key: str | None = None
    db_path: Path = field(
        default_factory=lambda: Path(
            "~/.local/state/msksd/msks.db"
        ).expanduser()
    )
    event_poll_s: float = 1.0
    bootstrap_token: str | None = None
    # Off by default: the events websocket carries its token in the
    # query string, which uvicorn's access log would persist.
    access_log: bool = False
    # HMAC key for consent audit tags (#69): opt-in integrity
    # protection — unset stores no tags, set tags every consent
    # row at write time (read live, so a reload applies to later
    # rows only).
    audit_hmac_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        return _server_settings_from_env(cls, live_env(env))


@dataclass
class NetSettings:
    """Guest egress networking (#52).

    Disabled by default: a daemon that never enabled egress presents
    no net machinery at all, and workspaces without ``egress`` keep
    the no-NIC posture on every backend. Enabled, the settings name
    the per-workspace /30 pool, the uplink the NAT masquerade hides
    behind, and the upstream the DNS forwarder relays to (unset
    reads the host's own /etc/resolv.conf).

    The privilege contract (#101): a daemon serving egress holds
    exactly two ambient capabilities — ``CAP_NET_ADMIN`` (taps and
    their addresses, the nftables tables, and through exec
    inheritance the VMM opening its tap) and
    ``CAP_NET_BIND_SERVICE`` (DHCP 67, DNS 53) — and verifies,
    never writes, ``net.ipv4.ip_forward``: the deployment host ships it
    as a boot-time sysctl, and a daemon that reads ``0`` refuses
    egress naming the sysctl key.
    """

    enabled: bool = False
    pool: IPv4Network = field(
        default_factory=lambda: IPv4Network("172.31.0.0/16")
    )
    uplink: str = "eth0"
    dns_upstream: str | None = None
    ip_tool: str = "ip"
    nft_tool: str = "nft"
    lease_s: int = 3600
    dns_timeout_s: float = 3.0
    # --- egress consent (#69) ----------------------------------------------
    # The mode workspaces get at create when the request names none
    # (the fleet default). ``allow`` keeps #52's posture: new flows
    # pass, off-list names are recorded.
    egress_mode: str = MODE_ALLOW
    # How long a held SYN waits for a decider before the hold
    # expires to a deny. The kernel's own SYN retransmit budget is
    # ~127 s, so the default answers inside it.
    consent_timeout_s: float = 120.0
    # The per-workspace pending-hold cap (the prompt-spam bound);
    # 0 disables it (unlimited holds).
    consent_rate_limit: int = 8
    # Retention for consent rows: days and a per-workspace row cap;
    # 0 disables either bound.
    consent_retention_days: int = 30
    consent_row_cap: int = 1000
    # The base per-VM NFQUEUE numbers derive from (base + pool
    # slice); a slice past the 16-bit queue range refuses by name
    # at attach.
    queue_base: int = 1024
    # The conntrack tool revocation uses to kill a revoked
    # destination's established flows.
    conntrack_tool: str = "conntrack"
    # The port every per-tap interceptor listener binds (#199): one
    # port, one address per workspace — the listeners differ by tap
    # address, so they share the number.
    interceptor_port: int = 8643

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NetSettings:
        return _net_settings_from_env(cls, live_env(env))


@dataclass
class SecretStoreSettings:
    """The placeholder secret store (#198).

    Real secrets live behind SecretSpec; the provider is a setting,
    not code, so at-rest encryption (``age``) or a managed vault
    (``awssm``, ``bws``) is a configuration change. The store is
    driven through the ``secretspec`` CLI (its Python SDK exposes
    only the resolve path — every write lives in the CLI), with a
    generated manifest under *root*; values ride stdin, never
    argv. Provider credentials are deliberately not settings:
    each provider reads its own chain from the daemon's process
    environment (the AWS SDK chain, ``BWS_ACCESS_TOKEN``).
    """

    provider: str = "file"
    # None means <state_dir>/secrets (derived at parse time, like
    # the server's db_path).
    root: Path | None = None
    age_identity: str | None = None
    region: str | None = None
    profile: str | None = None
    prefix: str | None = None
    project: str | None = None
    cli: str = "secretspec"
    timeout_s: float = 30.0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> SecretStoreSettings:
        return secret_store_settings_from_env(cls, live_env(env))


@dataclass
class LlmSettings:
    """The workspace LLM proxy (#259).

    The model list is the switch: a daemon with no ``MSKSD_LLM_MODELS``
    presents no LLM surface at all (nothing binds on a tap, the
    per-VM input chain admits nothing), and a daemon with one serves
    the OpenAI-shaped proxy on every workspace tap at *port*.
    Entries are ``provider/model:api_base:api_key`` strings —
    comma-separated in the environment, or the config file's list,
    whose entries may also be LiteLLM-native dicts (klangk's YAML
    shape: ``model_name``/``litellm_params``, kebab- or snake-case,
    the routing knobs the string grammar cannot spell). Secret-
    bearing values carry ``file:``/``cmd:``
    indirection (resolved at configure time), and a single entry
    whose model name is ``*`` is single-upstream passthrough mode.
    The settings are read live, so a SIGHUP swap re-routes requests
    wherever a listener already serves.
    """

    port: int = 8770
    models: tuple[str | dict, ...] = ()
    api_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LlmSettings:
        return llm_settings_from_env(cls, live_env(env))


@dataclass
class Settings:
    """The live-swappable settings root msksd subsystems read."""

    vmm: VmmSettings = field(default_factory=VmmSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    net: NetSettings = field(default_factory=NetSettings)
    secret_store: SecretStoreSettings = field(
        default_factory=SecretStoreSettings
    )
    llm: LlmSettings = field(default_factory=LlmSettings)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        settings = cls(
            vmm=VmmSettings.from_env(env),
            server=ServerSettings.from_env(env),
            net=NetSettings.from_env(env),
            secret_store=SecretStoreSettings.from_env(env),
            llm=LlmSettings.from_env(env),
        )
        check_tap_port_collision(settings)
        return settings


def check_tap_port_collision(settings: Settings) -> None:
    """Refuse a daemon whose two per-tap services would bind the
    same port (#259 review): the LLM proxy binds first at attach
    and the interceptor's armed bind would then fail with an error
    naming neither setting — one comparison at load names both."""
    if (
        settings.llm.models
        and settings.llm.port == settings.net.interceptor_port
    ):
        raise ValueError(
            "MSKSD_LLM_PORT and MSKSD_INTERCEPTOR_PORT name the same "
            f"port ({settings.llm.port}); the proxy and the "
            "interceptor cannot share it"
        )


def storage_warn_pct(env: Mapping[str, str], name: str, default: int) -> int:
    """The state-disk warn line (#184): 1–99, a named error outside."""
    value = _parse_int(env, name, default)
    if not 1 <= value <= 99:
        raise ValueError(f"{name} must sit between 1 and 99, got {value}")
    return value


def vmm_settings_from_env(
    cls: type[VmmSettings], env: Mapping[str, str]
) -> VmmSettings:
    """Build VmmSettings from the environment (helper: keeps the
    class block itself at xenon rank A, like its siblings)."""
    driver = _env(env, "MSKSD_VMM_DRIVER", cls.driver)
    if driver not in VALID_DRIVERS:
        raise ValueError(
            f"MSKSD_VMM_DRIVER must be one of {VALID_DRIVERS}, got {driver!r}"
        )
    # Zero is the documented off switch for the stall close; a
    # negative window would close healthy sessions.
    stall_timeout_s = _env_float(
        env, "MSKSD_CONSOLE_STALL_TIMEOUT_S", cls.console_stall_timeout_s
    )
    if stall_timeout_s < 0:
        raise ValueError(
            "MSKSD_CONSOLE_STALL_TIMEOUT_S must be zero or positive, "
            f"got {stall_timeout_s}"
        )
    # The same shape as the stall window: zero is a valid
    # fail-fast deadline, a negative one is a configuration error.
    forward_wait_s = _env_float(
        env, "MSKSD_FORWARD_WAIT_TIMEOUT_S", cls.forward_wait_timeout_s
    )
    if forward_wait_s < 0:
        raise ValueError(
            "MSKSD_FORWARD_WAIT_TIMEOUT_S must be zero or positive, "
            f"got {forward_wait_s}"
        )
    return cls(
        driver=driver,
        cloud_hypervisor=_env(
            env, "MSKSD_CLOUD_HYPERVISOR", cls.cloud_hypervisor
        ),
        state_dir=Path(
            _env(env, "MSKSD_STATE_DIR", str(cls().state_dir))
        ).expanduser(),
        socket_wait_timeout_s=_env_float(
            env, "MSKSD_SOCKET_WAIT_TIMEOUT_S", 10.0
        ),
        request_timeout_s=_env_float(env, "MSKSD_REQUEST_TIMEOUT_S", 5.0),
        shutdown_timeout_s=_env_float(env, "MSKSD_SHUTDOWN_TIMEOUT_S", 20.0),
        vsock_shell_port=_parse_int(
            env, "MSKSD_VSOCK_SHELL_PORT", cls.vsock_shell_port
        ),
        vsock_wait_timeout_s=_env_float(
            env, "MSKSD_VSOCK_WAIT_TIMEOUT_S", cls.vsock_wait_timeout_s
        ),
        forward_wait_timeout_s=forward_wait_s,
        console_stall_timeout_s=stall_timeout_s,
        move_wait_timeout_s=move_wait_seconds(env),
        default_image=_env(env, "MSKSD_DEFAULT_IMAGE", cls.default_image),
        qemu_img=_env(env, "MSKSD_QEMU_IMG", cls.qemu_img),
        mkfs_ext4=_env(env, "MSKSD_MKFS_EXT4", cls.mkfs_ext4),
        resize2fs=_env(env, "MSKSD_RESIZE2FS", cls.resize2fs),
        e2fsck=_env(env, "MSKSD_E2FSCK", cls.e2fsck),
        mkisofs=_env(env, "MSKSD_MKISOFS", cls.mkisofs),
        host_name=_env(env, "MSKSD_HOST_NAME", cls().host_name),
        root_mib=_parse_positive_int(env, "MSKSD_ROOT_MIB", cls.root_mib),
        home_mib=_parse_positive_int(env, "MSKSD_HOME_MIB", cls.home_mib),
        storage_warn_pct=storage_warn_pct(
            env, "MSKSD_STORAGE_WARN_PCT", cls.storage_warn_pct
        ),
        storage_floor_mib=_parse_positive_int(
            env, "MSKSD_STORAGE_FLOOR_MIB", cls.storage_floor_mib
        ),
        ssh_key_type=parse_key_type(
            env, "MSKSD_SSH_KEY_TYPE", cls.ssh_key_type
        ),
        image_import_max_mib=image_import_max_mib(env),
        image_import_timeout_s=image_import_timeout_s(env),
    )


def image_import_max_mib(env: Mapping[str, str]) -> int:
    """The URL-import ceiling (#258): a positive MiB count."""
    return _parse_positive_int(env, "MSKSD_IMAGE_IMPORT_MAX_MIB", 8192)


def image_import_timeout_s(env: Mapping[str, str]) -> float:
    """The URL-import deadline (#258): a positive second count."""
    value = _env_float(env, "MSKSD_IMAGE_IMPORT_TIMEOUT_S", 600.0)
    if value <= 0:
        raise ValueError(
            f"MSKSD_IMAGE_IMPORT_TIMEOUT_S must be positive, got {value}"
        )
    return value


def parse_key_type(env: Mapping[str, str], name: str, default: str) -> str:
    """One of the mintable identity types (#115): a named error
    otherwise, so a typo fails at settings load, not at create."""
    value = _env(env, name, default)
    if value not in KEY_TYPES:
        raise ValueError(
            f"{name} must be one of {sorted(KEY_TYPES)}, got {value!r}"
        )
    return value


def secret_store_settings_from_env(
    cls: type[SecretStoreSettings], env: Mapping[str, str]
) -> SecretStoreSettings:
    """Build SecretStoreSettings from the environment (helper: keeps
    the class block itself at xenon rank A, like its siblings)."""
    provider = _env(env, "MSKSD_SECRET_STORE_PROVIDER", cls.provider)
    if provider not in VALID_SECRET_PROVIDERS:
        raise ValueError(
            f"MSKSD_SECRET_STORE_PROVIDER must be one of "
            f"{VALID_SECRET_PROVIDERS}, got {provider!r}"
        )
    # Per-provider required keys: checked in one named error at
    # load, so a typo'd or half-written config fails before the
    # first mint, not at it.
    check_provider_keys(provider, env)
    timeout = _env_float(env, "MSKSD_SECRET_STORE_TIMEOUT_S", cls.timeout_s)
    if timeout <= 0:
        raise ValueError(
            f"MSKSD_SECRET_STORE_TIMEOUT_S must be positive, got {timeout}"
        )
    state = Path(
        _env(env, "MSKSD_STATE_DIR", str(VmmSettings().state_dir))
    ).expanduser()
    root = _env(env, "MSKSD_SECRET_STORE_ROOT", "")
    return cls(
        provider=provider,
        root=Path(root).expanduser() if root else state / "secrets",
        **secret_store_options(env),
        timeout_s=timeout,
    )


def secret_store_options(env: Mapping[str, str]) -> dict:
    """The optional per-provider fields, straight off the env."""
    pairs = {
        "age_identity": "MSKSD_SECRET_STORE_AGE_IDENTITY",
        "region": "MSKSD_SECRET_STORE_REGION",
        "profile": "MSKSD_SECRET_STORE_PROFILE",
        "prefix": "MSKSD_SECRET_STORE_PREFIX",
        "project": "MSKSD_SECRET_STORE_PROJECT",
    }
    options = {
        field: _env(env, name, "") or None for field, name in pairs.items()
    }
    options["cli"] = _env(
        env, "MSKSD_SECRET_STORE_CLI", SecretStoreSettings.cli
    )
    return options


#: Per-provider required keys (#198): named at settings load so a
#: typo'd or half-written config fails before the first mint.
PROVIDER_REQUIRED_KEYS = {
    "age": "MSKSD_SECRET_STORE_AGE_IDENTITY",
    "awssm": "MSKSD_SECRET_STORE_REGION",
    "bws": "MSKSD_SECRET_STORE_PROJECT",
}


def check_provider_keys(provider: str, env: Mapping[str, str]) -> None:
    """Refuse a provider whose required key is absent."""
    name = PROVIDER_REQUIRED_KEYS.get(provider)
    if name is not None and not env.get(name):
        raise ValueError(
            f"{name} is required when "
            f"MSKSD_SECRET_STORE_PROVIDER is {provider!r}"
        )


def _parse_subnet(
    env: Mapping[str, str], name: str, default: str
) -> IPv4Network:
    """The per-workspace /30 pool: an IPv4 network of at least a /30."""
    value = _env(env, name, default)
    try:
        pool = ipaddress.IPv4Network(value)
    except ValueError:
        raise ValueError(
            f"{name} must be an IPv4 network, got {value!r}"
        ) from None
    if pool.prefixlen > 30:
        raise ValueError(f"{name} must hold at least one /30, got {value!r}")
    return pool


def _net_settings_from_env(
    cls: type[NetSettings], env: Mapping[str, str]
) -> NetSettings:
    """Build NetSettings from the environment (helper: keeps the
    class block itself at xenon rank A)."""
    default = cls()
    lease = _parse_int(env, "MSKSD_EGRESS_LEASE_S", default.lease_s)
    timeout = _env_float(
        env, "MSKSD_EGRESS_DNS_TIMEOUT_S", default.dns_timeout_s
    )
    if lease <= 0:
        raise ValueError(f"MSKSD_EGRESS_LEASE_S must be positive, got {lease}")
    if timeout <= 0:
        raise ValueError(
            f"MSKSD_EGRESS_DNS_TIMEOUT_S must be positive, got {timeout}"
        )
    mode = egress_mode(env, "MSKSD_EGRESS_MODE", default.egress_mode)
    consent_timeout = _env_float(
        env, "MSKSD_EGRESS_CONSENT_TIMEOUT_S", default.consent_timeout_s
    )
    if consent_timeout <= 0:
        raise ValueError(
            "MSKSD_EGRESS_CONSENT_TIMEOUT_S must be positive, "
            f"got {consent_timeout}"
        )
    interceptor_port = parse_interceptor_port(
        env, "MSKSD_INTERCEPTOR_PORT", default.interceptor_port
    )
    return cls(
        enabled=_env(env, "MSKSD_EGRESS_ENABLED", str(default.enabled)).lower()
        == "true",
        pool=_parse_subnet(env, "MSKSD_EGRESS_SUBNET", str(default.pool)),
        uplink=_env(env, "MSKSD_EGRESS_UPLINK", default.uplink),
        dns_upstream=_env(env, "MSKSD_EGRESS_DNS_UPSTREAM", "") or None,
        ip_tool=_env(env, "MSKSD_IP_TOOL", default.ip_tool),
        nft_tool=_env(env, "MSKSD_NFT_TOOL", default.nft_tool),
        lease_s=lease,
        dns_timeout_s=timeout,
        egress_mode=mode,
        consent_timeout_s=consent_timeout,
        consent_rate_limit=_parse_int(
            env, "MSKSD_EGRESS_CONSENT_RATE_LIMIT", default.consent_rate_limit
        ),
        consent_retention_days=_parse_int(
            env,
            "MSKSD_EGRESS_CONSENT_RETENTION_DAYS",
            default.consent_retention_days,
        ),
        consent_row_cap=_parse_int(
            env, "MSKSD_EGRESS_CONSENT_ROW_CAP", default.consent_row_cap
        ),
        queue_base=_parse_int(
            env, "MSKSD_EGRESS_QUEUE_BASE", default.queue_base
        ),
        conntrack_tool=_env(
            env, "MSKSD_CONNTRACK_TOOL", default.conntrack_tool
        ),
        interceptor_port=interceptor_port,
    )


def parse_interceptor_port(
    env: Mapping[str, str], name: str, default: int
) -> int:
    """The interceptor listeners' shared TCP port (#199): a named
    error outside the port range."""
    value = _parse_int(env, name, default)
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be a TCP port, got {value}")
    return value


def egress_mode(env: Mapping[str, str], name: str, default: str) -> str:
    """One of the three egress modes (#69): a named error
    otherwise, so a typo fails at settings load, not at first
    boot."""
    value = _env(env, name, default)
    if value not in EGRESS_MODES:
        raise ValueError(
            f"{name} must be one of {list(EGRESS_MODES)}, got {value!r}"
        )
    return value


def llm_settings_from_env(
    cls: type[LlmSettings], env: Mapping[str, str]
) -> LlmSettings:
    """Build LlmSettings from the environment (helper: keeps the
    class block itself at xenon rank A, like its siblings)."""
    default = cls()
    port = _parse_int(env, "MSKSD_LLM_PORT", default.port)
    if not 1 <= port <= 65535:
        raise ValueError(f"MSKSD_LLM_PORT must be a port, got {port}")
    return cls(
        port=port,
        models=llm_models_from(env),
        api_key=optional_env(env, "MSKSD_LLM_API_KEY"),
    )


def llm_models_from(env) -> tuple[str | dict, ...]:
    """The model list: the env's comma-separated strings, or the
    config file's list (its entries already validated as strings
    or mappings — the same shapes mix freely)."""
    raw = env.get("MSKSD_LLM_MODELS", "")
    entries = raw if isinstance(raw, list) else raw.split(",")
    return tuple(
        entry
        for entry in (model_entry(item) for item in entries)
        if entry != ""
    )


def model_entry(entry) -> str | dict:
    """One entry, kept: a mapping as-is, a stripped non-empty
    string, a blank string dropped; anything else is a named
    error (the env's string form and the file's list share this
    walk)."""
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        return entry.strip()
    raise ValueError("MSKSD_LLM_MODELS entries must be strings or mappings")


def _server_settings_from_env(
    cls: type[ServerSettings], env: Mapping[str, str]
) -> ServerSettings:
    """Build ServerSettings from the environment (helper: keeps the
    class block itself at xenon rank A)."""
    state = Path(
        _env(env, "MSKSD_STATE_DIR", str(cls().db_path.parent))
    ).expanduser()
    poll = _env_float(env, "MSKSD_EVENT_POLL_S", cls.event_poll_s)
    if poll <= 0:
        raise ValueError(f"MSKSD_EVENT_POLL_S must be positive, got {poll}")
    return cls(
        host=_env(env, "MSKSD_HOST", cls.host),
        port=_parse_int(env, "MSKSD_PORT", cls.port),
        tls_cert=optional_env(env, "MSKSD_TLS_CERT"),
        tls_key=optional_env(env, "MSKSD_TLS_KEY"),
        db_path=state / "msks.db",
        event_poll_s=poll,
        bootstrap_token=optional_env(env, "MSKSD_BOOTSTRAP_TOKEN"),
        access_log=flag_env(env, "MSKSD_ACCESS_LOG", cls.access_log),
        audit_hmac_key=optional_env(env, "MSKSD_AUDIT_HMAC_KEY"),
    )


def optional_env(env: Mapping[str, str], name: str) -> str | None:
    """An environment value that is None when unset or empty."""
    return _env(env, name, "") or None


def flag_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    """A boolean flag spelled ``true``/anything-else."""
    return _env(env, name, str(default)).lower() == "true"

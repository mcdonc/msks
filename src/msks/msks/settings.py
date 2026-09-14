"""Settings for msksd, loaded from ``MSKSD_*`` environment variables.

Env naming follows the house rule: the category word ``MSKSD``
(daemon) concatenated onto the prefix with no underscore before it,
then a single underscore before the field (``MSKSD_STATE_DIR``,
``MSKSD_K8S_NAMESPACE``). All values are read live off
``app.state.settings`` — never materialized onto subsystems — so a
future runtime settings swap (SIGHUP) propagates without per-module
``reconfigure()`` calls.
"""

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from pathlib import Path

VALID_DRIVERS = ("local", "k8s")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _parse_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _parse_positive_int(name: str, default: int) -> int:
    value = _parse_int(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _parse_optional_int(name: str, minimum: int) -> int | None:
    """A positive-when-set integer: unset means "derive it"."""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


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
    # A host-side container-image tar imported into the catalog on first boot
    # and designated default (the appliance points this at the built
    # image's store path through its cmdline bridge).
    default_image: str = ""
    # Per-workspace persistent artifacts (#14): the tools that make
    # them, the host that owns them, and their default sizes.
    qemu_img: str = "qemu-img"
    mkfs_ext4: str = "mkfs.ext4"
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

    @classmethod
    def from_env(cls) -> VmmSettings:
        driver = _env("MSKSD_VMM_DRIVER", cls.driver)
        if driver not in VALID_DRIVERS:
            raise ValueError(
                f"MSKSD_VMM_DRIVER must be one of {VALID_DRIVERS}, got {driver!r}"
            )
        return cls(
            driver=driver,
            cloud_hypervisor=_env("MSKSD_CLOUD_HYPERVISOR", cls.cloud_hypervisor),
            state_dir=Path(_env("MSKSD_STATE_DIR", str(cls().state_dir))).expanduser(),
            socket_wait_timeout_s=_env_float("MSKSD_SOCKET_WAIT_TIMEOUT_S", 10.0),
            request_timeout_s=_env_float("MSKSD_REQUEST_TIMEOUT_S", 5.0),
            shutdown_timeout_s=_env_float("MSKSD_SHUTDOWN_TIMEOUT_S", 20.0),
            vsock_shell_port=_parse_int("MSKSD_VSOCK_SHELL_PORT", cls.vsock_shell_port),
            vsock_wait_timeout_s=_env_float(
                "MSKSD_VSOCK_WAIT_TIMEOUT_S", cls.vsock_wait_timeout_s
            ),
            default_image=_env("MSKSD_DEFAULT_IMAGE", cls.default_image),
            qemu_img=_env("MSKSD_QEMU_IMG", cls.qemu_img),
            mkfs_ext4=_env("MSKSD_MKFS_EXT4", cls.mkfs_ext4),
            mkisofs=_env("MSKSD_MKISOFS", cls.mkisofs),
            host_name=_env("MSKSD_HOST_NAME", cls().host_name),
            root_mib=_parse_positive_int("MSKSD_ROOT_MIB", cls.root_mib),
            home_mib=_parse_positive_int("MSKSD_HOME_MIB", cls.home_mib),
        )


@dataclass
class ServerSettings:
    """The API server's own settings (HTTPS + WSS on one listener)."""

    host: str = "127.0.0.1"
    port: int = 8660
    tls_cert: str | None = None
    tls_key: str | None = None
    db_path: Path = field(
        default_factory=lambda: Path("~/.local/state/msksd/msks.db").expanduser()
    )
    event_poll_s: float = 1.0
    bootstrap_token: str | None = None
    # Off by default: the events websocket carries its token in the
    # query string, which uvicorn's access log would persist.
    access_log: bool = False

    @classmethod
    def from_env(cls) -> ServerSettings:
        return _server_settings_from_env(cls)


@dataclass
class K8sSettings:
    """Kubernetes runner-driver settings."""

    namespace: str = "msks"
    runner_image: str = "registry.k8s.io/pause:3.10"
    kubeconfig: str | None = None
    api_timeout_s: float = 30.0
    # Per-workspace claims (#14): the storage class the admin's
    # cluster offers (unset asks the cluster's default) and each
    # claim's size — one PVC holds the workspace's overlay and home
    # volume files, so it needs room for both. Unset derives the
    # size from the workspace's root_mib + home_mib at create.
    storage_class: str | None = None
    workspace_storage_gib: int | None = None

    @classmethod
    def from_env(cls) -> K8sSettings:
        return cls(
            namespace=_env("MSKSD_K8S_NAMESPACE", cls.namespace),
            runner_image=_env("MSKSD_K8S_RUNNER_IMAGE", cls.runner_image),
            kubeconfig=_env("MSKSD_KUBECONFIG", "") or None,
            api_timeout_s=_env_float("MSKSD_K8S_API_TIMEOUT_S", 30.0),
            storage_class=_env("MSKSD_K8S_STORAGE_CLASS", "") or None,
            workspace_storage_gib=_parse_optional_int(
                "MSKSD_K8S_WORKSPACE_STORAGE_GIB", minimum=1
            ),
        )


@dataclass
class NetSettings:
    """Guest egress networking (#52).

    Disabled by default: a daemon that never enabled egress presents
    no net machinery at all, and workspaces without ``egress`` keep
    the no-NIC posture on every backend. Enabled, the settings name
    the per-workspace /30 pool, the appliance uplink the NAT
    masquerade hides behind, and the upstream the DNS forwarder
    relays to (unset reads the appliance's own /etc/resolv.conf).
    """

    enabled: bool = False
    pool: IPv4Network = field(default_factory=lambda: IPv4Network("172.31.0.0/16"))
    uplink: str = "eth0"
    dns_upstream: str | None = None
    ip_tool: str = "ip"
    nft_tool: str = "nft"
    lease_s: int = 3600
    dns_timeout_s: float = 3.0

    @classmethod
    def from_env(cls) -> NetSettings:
        return _net_settings_from_env(cls)


@dataclass
class Settings:
    """The live-swappable settings root msksd subsystems read."""

    vmm: VmmSettings = field(default_factory=VmmSettings)
    k8s: K8sSettings = field(default_factory=K8sSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    net: NetSettings = field(default_factory=NetSettings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            vmm=VmmSettings.from_env(),
            k8s=K8sSettings.from_env(),
            server=ServerSettings.from_env(),
            net=NetSettings.from_env(),
        )


def _parse_subnet(name: str, default: str) -> IPv4Network:
    """The per-workspace /30 pool: an IPv4 network of at least a /30."""
    value = _env(name, default)
    try:
        pool = ipaddress.IPv4Network(value)
    except ValueError:
        raise ValueError(f"{name} must be an IPv4 network, got {value!r}") from None
    if pool.prefixlen > 30:
        raise ValueError(f"{name} must hold at least one /30, got {value!r}")
    return pool


def _net_settings_from_env(cls: type[NetSettings]) -> NetSettings:
    """Build NetSettings from the environment (helper: keeps the
    class block itself at xenon rank A)."""
    default = cls()
    lease = _parse_int("MSKSD_EGRESS_LEASE_S", default.lease_s)
    timeout = _env_float("MSKSD_EGRESS_DNS_TIMEOUT_S", default.dns_timeout_s)
    if lease <= 0:
        raise ValueError(f"MSKSD_EGRESS_LEASE_S must be positive, got {lease}")
    if timeout <= 0:
        raise ValueError(f"MSKSD_EGRESS_DNS_TIMEOUT_S must be positive, got {timeout}")
    return cls(
        enabled=_env("MSKSD_EGRESS_ENABLED", "false").lower() == "true",
        pool=_parse_subnet("MSKSD_EGRESS_SUBNET", str(default.pool)),
        uplink=_env("MSKSD_EGRESS_UPLINK", default.uplink),
        dns_upstream=_env("MSKSD_EGRESS_DNS_UPSTREAM", "") or None,
        ip_tool=_env("MSKSD_IP_TOOL", default.ip_tool),
        nft_tool=_env("MSKSD_NFT_TOOL", default.nft_tool),
        lease_s=lease,
        dns_timeout_s=timeout,
    )


def _server_settings_from_env(cls: type[ServerSettings]) -> ServerSettings:
    """Build ServerSettings from the environment (helper: keeps the
    class block itself at xenon rank A)."""
    state = Path(_env("MSKSD_STATE_DIR", "~/.local/state/msksd")).expanduser()
    poll = _env_float("MSKSD_EVENT_POLL_S", cls.event_poll_s)
    if poll <= 0:
        raise ValueError(f"MSKSD_EVENT_POLL_S must be positive, got {poll}")
    return cls(
        host=_env("MSKSD_HOST", cls.host),
        port=_parse_int("MSKSD_PORT", cls.port),
        tls_cert=_env("MSKSD_TLS_CERT", "") or None,
        tls_key=_env("MSKSD_TLS_KEY", "") or None,
        db_path=state / "msks.db",
        event_poll_s=poll,
        bootstrap_token=_env("MSKSD_BOOTSTRAP_TOKEN", "") or None,
        access_log=_env("MSKSD_ACCESS_LOG", "false").lower() == "true",
    )

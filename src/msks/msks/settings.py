"""Settings for msksd, loaded from ``MSKSD_*`` environment variables.

Env naming follows the house rule: the category word ``MSKSD``
(daemon) concatenated onto the prefix with no underscore before it,
then a single underscore before the field (``MSKSD_STATE_DIR``,
``MSKSD_K8S_NAMESPACE``). All values are read live off
``app.state.settings`` — never materialized onto subsystems — so a
future runtime settings swap (SIGHUP) propagates without per-module
``reconfigure()`` calls.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_DRIVERS = ("local", "k8s")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value in (None, "") else value


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
        )


@dataclass
class K8sSettings:
    """Kubernetes runner-driver settings."""

    namespace: str = "msks"
    runner_image: str = "registry.k8s.io/pause:3.10"
    kubeconfig: str | None = None
    api_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> K8sSettings:
        return cls(
            namespace=_env("MSKSD_K8S_NAMESPACE", cls.namespace),
            runner_image=_env("MSKSD_K8S_RUNNER_IMAGE", cls.runner_image),
            kubeconfig=_env("MSKSD_KUBECONFIG", "") or None,
            api_timeout_s=_env_float("MSKSD_K8S_API_TIMEOUT_S", 30.0),
        )


@dataclass
class Settings:
    """The live-swappable settings root msksd subsystems read."""

    vmm: VmmSettings = field(default_factory=VmmSettings)
    k8s: K8sSettings = field(default_factory=K8sSettings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(vmm=VmmSettings.from_env(), k8s=K8sSettings.from_env())

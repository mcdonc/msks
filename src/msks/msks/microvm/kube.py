"""Kubeconfig loading for the k8s backend (#1).

Builds an ``httpx.AsyncClient`` straight from a kubeconfig file — no
kubectl shell-outs. Supported credentials: bearer-token users; cluster
verification via ``insecure-skip-tls-verify`` or a CA file path.
Client-certificate users raise :class:`MicrovmError` (token-based
ServiceAccounts are the supported shape for msksd).
"""

import os
from pathlib import Path

import httpx
import yaml

from ..settings import K8sSettings
from .errors import MicrovmError


def kubeconfig_path(settings: K8sSettings) -> Path:
    """Resolve which kubeconfig to read: setting, KUBECONFIG, default."""
    if settings.kubeconfig:
        return Path(settings.kubeconfig).expanduser()
    env = os.environ.get("KUBECONFIG")
    if env:
        return Path(env.split(os.pathsep)[0]).expanduser()
    return Path("~/.kube/config").expanduser()


def _find_named(entries: list | None, name: str | None) -> dict:
    """The ``{"name": ..., ...}`` entry whose name matches."""
    for entry in entries or []:
        if entry.get("name") == name:
            return entry
    raise MicrovmError(f"kubeconfig has no entry named {name!r}")


def load_context(path: Path) -> tuple[dict, dict]:
    """Return ``(cluster, user)`` dicts for the current context."""
    try:
        document = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise MicrovmError(f"cannot read kubeconfig {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise MicrovmError(
            f"kubeconfig {path} is not valid YAML: {exc}"
        ) from exc
    current = document.get("current-context")
    if not current:
        raise MicrovmError(f"kubeconfig {path} has no current-context")
    context = _find_named(document.get("contexts"), current).get("context", {})
    cluster = _find_named(document.get("clusters"), context.get("cluster"))
    user = _find_named(document.get("users"), context.get("user"))
    return cluster.get("cluster", {}), user.get("user", {})


def verify_setting(cluster: dict) -> str | bool:
    """The ``httpx`` verify argument a cluster section implies."""
    if cluster.get("insecure-skip-tls-verify"):
        return False
    if cluster.get("certificate-authority-data"):
        raise MicrovmError(
            "kubeconfig certificate-authority-data is not supported; "
            "export the CA to a file and point certificate-authority at it"
        )
    ca = cluster.get("certificate-authority")
    if ca:
        return str(Path(ca).expanduser())
    return True


UNSUPPORTED_USER_KEYS = (
    "client-certificate",
    "client-certificate-data",
    "client-key",
    "client-key-data",
    "exec",
    "auth-provider",
)


def auth_header(user: dict) -> dict[str, str]:
    """The Authorization headers a user section implies.

    Credential shapes msksd cannot speak raise instead of silently
    producing an unauthenticated client: an empty header set is only
    correct when nothing was configured at all, which is itself
    rejected below so every failure names its cause.
    """
    unsupported = [key for key in UNSUPPORTED_USER_KEYS if key in user]
    if unsupported:
        raise MicrovmError(
            "kubeconfig user uses unsupported credential fields: "
            + ", ".join(unsupported)
            + "; use a token-based ServiceAccount kubeconfig"
        )
    token = user.get("token")
    if not token:
        raise MicrovmError(
            "kubeconfig user has no token; "
            "use a token-based ServiceAccount kubeconfig"
        )
    return {"Authorization": f"Bearer {token}"}


def kube_client(settings: K8sSettings) -> httpx.AsyncClient:
    """The Kubernetes API client described by the resolved kubeconfig."""
    cluster, user = load_context(kubeconfig_path(settings))
    server = cluster.get("server")
    if not server:
        raise MicrovmError("kubeconfig cluster has no server URL")
    return httpx.AsyncClient(
        base_url=server,
        verify=verify_setting(cluster),
        headers=auth_header(user),
        timeout=settings.api_timeout_s,
    )

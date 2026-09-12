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
        raise MicrovmError(f"kubeconfig {path} is not valid YAML: {exc}") from exc
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
    ca = cluster.get("certificate-authority")
    if ca:
        return str(Path(ca).expanduser())
    return True


def auth_header(user: dict) -> dict[str, str]:
    """The Authorization headers a user section implies."""
    token = user.get("token")
    if not token:
        return {}
    if user.get("client-certificate"):
        raise MicrovmError(
            "client-certificate kubeconfig users are not supported; "
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

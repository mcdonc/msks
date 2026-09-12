"""K8s backend: one vm-runner pod per workspace VM (#1).

msksd stays the control plane; each VM is a pod named
``msks-vm-<workspace_id>`` in the configured namespace, carrying the
VMM artifacts as environment variables for the runner image to
consume, with ``/dev/kvm`` exposed through a hostPath CharDevice
volume (works on any cluster; the device-plugin extended-resource
idiom is the hardened alternative once a cluster runs the KVM device
plugin). Lifecycle calls map onto plain Kubernetes API requests; no
CLI parsing anywhere. Shutdown/kill request a pod deletion with the
given grace period and do not wait for deletion to complete.
"""

import httpx

from ..settings import K8sSettings, Settings
from . import kube
from .driver import MicrovmDriver
from .errors import MicrovmError
from .spec import VmInfo, VmSpec, VmStatus

LABEL_WORKSPACE_ID = "msks.io/workspace-id"

POD_PHASE_TO_STATUS = {
    "Pending": VmStatus.STARTING,
    "Running": VmStatus.RUNNING,
    "Succeeded": VmStatus.STOPPED,
    "Failed": VmStatus.STOPPED,
}


def pod_name(workspace_id: str) -> str:
    """The deterministic pod name for one workspace."""
    return f"msks-vm-{workspace_id}"


def map_phase(phase: str | None) -> VmStatus:
    """Translate a pod phase into the shared status enum."""
    if phase is None:
        return VmStatus.UNKNOWN
    return POD_PHASE_TO_STATUS.get(phase, VmStatus.UNKNOWN)


def spec_env(spec: VmSpec) -> list[dict]:
    """The VM sizing, passed to the runner container as env vars.

    Artifact paths and cmdline stay in the image: the runner image
    carries the guest it boots (kernel, initrd, rootfs, and a cmdline
    that matches them), so host-side spec paths are meaningless inside
    the container. Per-workspace artifacts reach the pod through
    volumes when workspace images land, at which point the paths
    become container-visible and can ride these env vars.
    """
    return [
        {"name": "MSKSD_CPUS", "value": str(spec.cpus)},
        {"name": "MSKSD_MEM_MIB", "value": str(spec.mem_mib)},
    ]


def pod_manifest(spec: VmSpec, settings: K8sSettings) -> dict:
    """The pod object msksd creates for one workspace VM."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name(spec.workspace_id),
            "namespace": settings.namespace,
            "labels": {
                "app": "msks-vm",
                LABEL_WORKSPACE_ID: spec.workspace_id,
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "volumes": [
                {
                    "name": "kvm",
                    "hostPath": {"path": "/dev/kvm", "type": "CharDevice"},
                }
            ],
            "containers": [
                {
                    "name": "runner",
                    "image": settings.runner_image,
                    "env": spec_env(spec),
                    "volumeMounts": [{"name": "kvm", "mountPath": "/dev/kvm"}],
                }
            ],
        },
    }


def _pod_url(settings: K8sSettings, workspace_id: str) -> str:
    return f"/api/v1/namespaces/{settings.namespace}/pods/{pod_name(workspace_id)}"


def _pods_url(settings: K8sSettings) -> str:
    return f"/api/v1/namespaces/{settings.namespace}/pods"


class KubernetesRunner(MicrovmDriver):
    """Drives VMs as pods through the Kubernetes API."""

    def __init__(self, app) -> None:
        self.app = app

    def _settings(self) -> Settings:
        return self.app.state.settings

    async def _client(self) -> httpx.AsyncClient:
        return kube.kube_client(self._settings().k8s)

    async def launch(self, spec: VmSpec) -> None:
        client = await self._client()
        try:
            response = await client.post(
                _pods_url(self._settings().k8s),
                json=pod_manifest(spec, self._settings().k8s),
            )
        finally:
            await client.aclose()
        if response.status_code >= 400:
            raise MicrovmError(
                f"k8s pod create failed: {response.status_code}"
                f" {response.text.strip()}",
                status=response.status_code,
            )

    async def info(self, workspace_id: str) -> VmInfo:
        client = await self._client()
        try:
            response = await client.get(_pod_url(self._settings().k8s, workspace_id))
        finally:
            await client.aclose()
        if response.status_code == 404:
            return VmInfo(workspace_id, VmStatus.ABSENT)
        if response.status_code >= 400:
            raise MicrovmError(
                f"k8s pod get failed: {response.status_code} {response.text.strip()}",
                status=response.status_code,
            )
        phase = response.json().get("status", {}).get("phase")
        return VmInfo(workspace_id, map_phase(phase))

    async def shutdown(self, workspace_id: str, timeout_s: float | None = None) -> None:
        """Delete with the graceful termination period."""
        grace = 30 if timeout_s is None else int(timeout_s)
        await self._delete(workspace_id, grace)

    async def kill(self, workspace_id: str) -> None:
        await self._delete(workspace_id, 0)

    async def cleanup(self, workspace_id: str) -> None:
        await self._delete(workspace_id, 0, tolerate_missing=True)

    async def _delete(
        self, workspace_id: str, grace_s: int, tolerate_missing: bool = False
    ) -> None:
        client = await self._client()
        try:
            response = await client.request(
                "DELETE",
                _pod_url(self._settings().k8s, workspace_id),
                json={"gracePeriodSeconds": grace_s},
            )
        finally:
            await client.aclose()
        if response.status_code == 404 and tolerate_missing:
            return
        if response.status_code >= 400:
            raise MicrovmError(
                f"k8s pod delete failed: {response.status_code}"
                f" {response.text.strip()}",
                status=response.status_code,
            )

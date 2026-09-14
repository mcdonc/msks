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

from pathlib import Path

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

#: Where the runner pod mounts each workspace's PVC: the container's
#: analogue of the local backend's ``<state_dir>/vms/<workspace_id>/``.
WORKSPACE_STATE_MOUNT = "/var/lib/msks/workspaces"


def pod_name(workspace_id: str) -> str:
    """The deterministic pod name for one workspace."""
    return f"msks-vm-{workspace_id}"


def pvc_name(workspace_id: str) -> str:
    """The deterministic claim name for one workspace's artifacts."""
    return f"msks-ws-{workspace_id}"


def map_phase(phase: str | None) -> VmStatus:
    """Translate a pod phase into the shared status enum."""
    if phase is None:
        return VmStatus.UNKNOWN
    return POD_PHASE_TO_STATUS.get(phase, VmStatus.UNKNOWN)


def spec_env(spec: VmSpec) -> list[dict]:
    """The VM's sizing and artifact locations, as runner env vars.

    Kernel/initrd/cmdline stay in the image: the runner image carries
    the guest it boots. The per-workspace persistent artifacts (#14)
    live on the PVC mounted at ``WORKSPACE_STATE_MOUNT`` — the env
    vars name the overlay and home-volume files inside it and the
    sizes to create them at when the runner finds them absent.
    """
    state_dir = Path(WORKSPACE_STATE_MOUNT) / spec.workspace_id
    return [
        {"name": "MSKSD_CPUS", "value": str(spec.cpus)},
        {"name": "MSKSD_MEM_MIB", "value": str(spec.mem_mib)},
        {"name": "MSKSD_ROOT_OVERLAY", "value": str(state_dir / "root.qcow2")},
        {"name": "MSKSD_HOME_VOLUME", "value": str(state_dir / "home.ext4")},
        {"name": "MSKSD_ROOT_MIB", "value": str(spec.root_mib)},
        {"name": "MSKSD_HOME_MIB", "value": str(spec.home_mib)},
    ]


def pod_manifest(spec: VmSpec, settings: K8sSettings) -> dict:
    """The pod object msksd creates for one workspace VM."""
    state_mount = str(Path(WORKSPACE_STATE_MOUNT) / spec.workspace_id)
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
                },
                {
                    "name": "workspace-state",
                    "persistentVolumeClaim": {"claimName": pvc_name(spec.workspace_id)},
                },
            ],
            "containers": [
                {
                    "name": "runner",
                    "image": settings.runner_image,
                    "env": spec_env(spec),
                    "volumeMounts": [
                        {"name": "kvm", "mountPath": "/dev/kvm"},
                        {"name": "workspace-state", "mountPath": state_mount},
                    ],
                }
            ],
        },
    }


def storage_gib(spec: VmSpec, settings: K8sSettings) -> int:
    """The claim size in GiB: room for both artifacts (#14).

    The explicit setting wins; unset derives from the sizes create
    actually requests, rounded up — a claim smaller than the overlay
    plus the volume fails the guest with late ENOSPC on its root.
    """
    if settings.workspace_storage_gib is not None:
        return settings.workspace_storage_gib
    return -(-(spec.root_mib + spec.home_mib) // 1024)


def pvc_manifest(spec: VmSpec, settings: K8sSettings) -> dict:
    """The per-workspace claim (#14): one RWO volume owning the
    workspace's persistent artifacts (root overlay + home volume).

    ``ReadWriteOnce`` is the placement rule: the volume attaches to
    one node, and the pod follows it — a workspace runs where its
    artifacts live, at most once at a time.
    """
    claim: dict = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": f"{storage_gib(spec, settings)}Gi"}},
    }
    if settings.storage_class:
        claim["storageClassName"] = settings.storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name(spec.workspace_id),
            "namespace": settings.namespace,
            "labels": {
                "app": "msks-vm",
                LABEL_WORKSPACE_ID: spec.workspace_id,
            },
        },
        "spec": claim,
    }


def _pod_url(settings: K8sSettings, workspace_id: str) -> str:
    return f"/api/v1/namespaces/{settings.namespace}/pods/{pod_name(workspace_id)}"


def _pods_url(settings: K8sSettings) -> str:
    return f"/api/v1/namespaces/{settings.namespace}/pods"


def _pvcs_url(settings: K8sSettings) -> str:
    return f"/api/v1/namespaces/{settings.namespace}/persistentvolumeclaims"


def _pvc_url(settings: K8sSettings, workspace_id: str) -> str:
    return (
        f"/api/v1/namespaces/{settings.namespace}"
        f"/persistentvolumeclaims/{pvc_name(workspace_id)}"
    )


def claim_gib(manifest: dict) -> int | None:
    """The claim's requested size in GiB (None when unparseable)."""
    raw = (
        manifest.get("spec", {})
        .get("resources", {})
        .get("requests", {})
        .get("storage", "")
    )
    if raw.endswith("Gi") and raw[:-2].isdigit():
        return int(raw[:-2])
    return None


class KubernetesRunner(MicrovmDriver):
    """Drives VMs as pods through the Kubernetes API."""

    def __init__(self, app) -> None:
        self.app = app

    def _settings(self) -> Settings:
        return self.app.state.settings

    async def _client(self) -> httpx.AsyncClient:
        return kube.kube_client(self._settings().k8s)

    async def prepare(self, spec: VmSpec) -> None:
        """Create the workspace's PVC (#14); an existing claim is kept.

        Claim reuse is the same id's own persistence (the claim name
        is derived from the workspace id); the files inside it are the
        runner agent's domain, so a stale claim's contents are never
        inspected or replaced here. A reused claim that is smaller
        than this workspace's artifacts is refused by name — the
        guest would hit late ENOSPC on its root otherwise.
        """
        settings = self._settings().k8s
        client = await self._client()
        try:
            response = await client.post(
                _pvcs_url(settings), json=pvc_manifest(spec, settings)
            )
            if response.status_code == 409:
                await self._check_claim_size(spec, client)
                return
        finally:
            await client.aclose()
        if response.status_code in (200, 201):
            return
        raise MicrovmError(
            f"k8s pvc create failed: {response.status_code} {response.text.strip()}",
            status=response.status_code,
        )

    async def _check_claim_size(self, spec: VmSpec, client) -> None:
        """Refuse a reused claim too small for the new workspace."""
        response = await client.get(_pvc_url(self._settings().k8s, spec.workspace_id))
        if response.status_code != 200:
            return
        existing = claim_gib(response.json())
        wanted = storage_gib(spec, self._settings().k8s)
        if existing is not None and existing < wanted:
            raise MicrovmError(
                f"claim {pvc_name(spec.workspace_id)} holds {existing}Gi; "
                f"this workspace needs {wanted}Gi — delete the workspace "
                f"(dropping the claim) and create it again"
            )

    async def launch(self, spec: VmSpec) -> None:
        # Boots heal their claim, same as the local backend heals its
        # artifact files: a pre-#14 row (or a manually deleted PVC)
        # gets the claim back before the pod pends on a missing one.
        await self.prepare(spec)
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
        """Delete with the graceful termination period.

        A missing pod is success, not an error: stopping a workspace
        that was never started (or already died) must not wedge the
        caller — the absent-VM contract every backend honors.
        """
        grace = 30 if timeout_s is None else int(timeout_s)
        await self._delete(workspace_id, grace, tolerate_missing=True)

    async def kill(self, workspace_id: str) -> None:
        await self._delete(workspace_id, 0, tolerate_missing=True)

    async def cleanup(self, workspace_id: str) -> None:
        """Remove the pod and the PVC together (#14): the artifacts
        die with the workspace, never with a stop."""
        await self._delete(workspace_id, 0, tolerate_missing=True)
        await self._delete_pvc(workspace_id)

    async def _delete_pvc(self, workspace_id: str) -> None:
        client = await self._client()
        try:
            response = await client.request(
                "DELETE", _pvc_url(self._settings().k8s, workspace_id)
            )
        finally:
            await client.aclose()
        if response.status_code in (200, 404):
            return
        raise MicrovmError(
            f"k8s pvc delete failed: {response.status_code} {response.text.strip()}",
            status=response.status_code,
        )

    async def reset(self, workspace_id: str) -> None:
        """Factory reset needs the runner agent: the overlay file
        lives inside the PVC mount, which only the pod's container
        can reach. Until the runner implements it, name the gap
        instead of silently deleting the whole claim (that would take
        /home with it)."""
        raise MicrovmError(
            f"factory reset for k8s workspace {workspace_id} needs the "
            "runner agent to delete the overlay file inside the PVC; "
            "recreate the workspace to start over (its /home data "
            "goes with the claim)"
        )

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

"""K8s backend unit tests against a mocked Kubernetes API transport."""

import httpx
import pytest
import yaml
from msks.app import build_app
from msks.microvm import MicrovmError, VmSpec
from msks.microvm.k8s import LABEL_WORKSPACE_ID, map_phase, pod_manifest
from msks.microvm.kube import (
    auth_header,
    kube_client,
    kubeconfig_path,
    load_context,
    verify_setting,
)
from msks.microvm.spec import VmStatus
from msks.settings import K8sSettings, Settings, VmmSettings

WID = "ws-k8s"

KUBECONFIG = """
current-context: dev
clusters:
  - name: dev
    cluster:
      server: https://127.0.0.1:6443
contexts:
  - name: dev
    context:
      cluster: dev
      user: msksd
users:
  - name: msksd
    user:
      token: sekrit
"""


def app_with_k8s(tmp_path, monkeypatch, handler) -> object:
    """An app whose k8s client uses a MockTransport with ``handler``."""
    settings = Settings(
        vmm=VmmSettings(driver="k8s"),
        k8s=K8sSettings(kubeconfig=str(tmp_path / "kubeconfig")),
    )
    (tmp_path / "kubeconfig").write_text(KUBECONFIG)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://127.0.0.1:6443")

    def fake_client(_settings):
        return client

    monkeypatch.setattr("msks.microvm.kube.kube_client", fake_client)
    return build_app(settings)


def spec(tmp_path) -> VmSpec:
    return VmSpec(
        workspace_id=WID, kernel=tmp_path / "vmlinux", rootfs=tmp_path / "rootfs.ext4"
    )


def pod(status_code=200, phase="Running"):
    return httpx.Response(
        status_code,
        json={"status": {"phase": phase}},
    )


def test_pod_manifest_shape(tmp_path) -> None:
    settings = K8sSettings(namespace="ns1", runner_image="example/runner:1")
    manifest = pod_manifest(spec(tmp_path), settings)
    assert manifest["metadata"]["name"] == f"msks-vm-{WID}"
    assert manifest["metadata"]["labels"][LABEL_WORKSPACE_ID] == WID
    container = manifest["spec"]["containers"][0]
    assert container["image"] == "example/runner:1"
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["MSKSD_VMLINUX"] == str(tmp_path / "vmlinux")
    assert container["volumeMounts"] == [{"name": "kvm", "mountPath": "/dev/kvm"}]
    assert manifest["spec"]["volumes"] == [
        {"name": "kvm", "hostPath": {"path": "/dev/kvm", "type": "CharDevice"}}
    ]


def test_map_phase() -> None:
    assert map_phase("Running") == VmStatus.RUNNING
    assert map_phase("Pending") == VmStatus.STARTING
    assert map_phase("Succeeded") == VmStatus.STOPPED
    assert map_phase("Failed") == VmStatus.STOPPED
    assert map_phase("WeirdPhase") == VmStatus.UNKNOWN
    assert map_phase(None) == VmStatus.UNKNOWN


async def test_launch_posts_pod(tmp_path, monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = httpx.Response(200).json() if False else None
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201)

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    await app.state.microvm.launch(spec(tmp_path))
    assert seen["path"] == "/api/v1/namespaces/msks/pods"
    assert seen["body"]["kind"] == "Pod"


async def test_launch_conflict_maps_to_error(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="already exists")

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    with pytest.raises(MicrovmError) as excinfo:
        await app.state.microvm.launch(spec(tmp_path))
    assert excinfo.value.status == 409


async def test_info_maps_phases(tmp_path, monkeypatch) -> None:
    cases = {"Running": "running", "Pending": "starting", "Succeeded": "stopped"}
    for phase, expected in cases.items():

        def handler(request: httpx.Request, phase=phase) -> httpx.Response:
            return pod(phase=phase)

        app = app_with_k8s(tmp_path, monkeypatch, handler)
        info = await app.state.microvm.info(WID)
        assert info.status.value == expected


async def test_info_absent_on_404(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    info = await app.state.microvm.info(WID)
    assert info.status.value == "absent"


async def test_info_error_maps(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    with pytest.raises(MicrovmError) as excinfo:
        await app.state.microvm.info(WID)
    assert excinfo.value.status == 403


async def test_shutdown_uses_grace_period(tmp_path, monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["grace"] = json.loads(request.content)["gracePeriodSeconds"]
        return httpx.Response(200)

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    await app.state.microvm.shutdown(WID, timeout_s=7)
    assert seen["grace"] == 7


async def test_kill_uses_zero_grace(tmp_path, monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["grace"] = json.loads(request.content)["gracePeriodSeconds"]
        return httpx.Response(200)

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    await app.state.microvm.kill(WID)
    assert seen["grace"] == 0


async def test_cleanup_tolerates_404(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    await app.state.microvm.cleanup(WID)


async def test_delete_error_maps(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="api down")

    app = app_with_k8s(tmp_path, monkeypatch, handler)
    with pytest.raises(MicrovmError):
        await app.state.microvm.kill(WID)


def test_kubeconfig_path_precedence(tmp_path, monkeypatch) -> None:
    explicit = K8sSettings(kubeconfig=str(tmp_path / "a"))
    assert kubeconfig_path(explicit) == tmp_path / "a"
    monkeypatch.setenv("KUBECONFIG", f"{tmp_path / 'b'}:{tmp_path / 'c'}")
    assert kubeconfig_path(K8sSettings()) == tmp_path / "b"
    monkeypatch.delenv("KUBECONFIG")
    assert kubeconfig_path(K8sSettings()) is not None


def test_load_context_and_auth(tmp_path) -> None:
    path = tmp_path / "kubeconfig"
    path.write_text(KUBECONFIG)
    cluster, user = load_context(path)
    assert cluster["server"] == "https://127.0.0.1:6443"
    assert auth_header(user) == {"Authorization": "Bearer sekrit"}


def test_load_context_missing_entry(tmp_path) -> None:
    path = tmp_path / "kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "current-context": "dev",
                "clusters": [{"name": "other", "cluster": {}}],
                "contexts": [{"name": "elsewhere", "context": {}}],
                "users": [],
            }
        )
    )
    with pytest.raises(MicrovmError, match="no entry named"):
        load_context(path)


def test_load_context_no_current(tmp_path) -> None:
    path = tmp_path / "kubeconfig"
    path.write_text("clusters: []\n")
    with pytest.raises(MicrovmError, match="current-context"):
        load_context(path)


def test_load_context_unreadable(tmp_path) -> None:
    with pytest.raises(MicrovmError, match="cannot read"):
        load_context(tmp_path / "missing")


def test_load_context_bad_yaml(tmp_path) -> None:
    path = tmp_path / "kubeconfig"
    path.write_text("{not: yaml: at all")
    with pytest.raises(MicrovmError, match="not valid YAML"):
        load_context(path)


def test_verify_setting_variants(tmp_path) -> None:
    assert verify_setting({"insecure-skip-tls-verify": True}) is False
    assert verify_setting(
        {"certificate-authority": "/etc/ssl/certs/ca-bundle.crt"}
    ) == ("/etc/ssl/certs/ca-bundle.crt")
    assert verify_setting({}) is True


def test_auth_header_rejects_client_certs() -> None:
    with pytest.raises(MicrovmError, match="unsupported credential fields"):
        auth_header({"client-certificate": "/x", "client-key": "/y"})


def test_auth_header_rejects_missing_token() -> None:
    with pytest.raises(MicrovmError, match="no token"):
        auth_header({})


def test_verify_setting_rejects_inline_ca() -> None:
    with pytest.raises(MicrovmError, match="certificate-authority-data"):
        verify_setting({"certificate-authority-data": "aGk="})


def test_kube_client_builds(tmp_path) -> None:
    path = tmp_path / "kubeconfig"
    path.write_text(KUBECONFIG)
    client = kube_client(K8sSettings(kubeconfig=str(path)))
    assert str(client.base_url).startswith("https://127.0.0.1:6443")
    assert client.headers["Authorization"] == "Bearer sekrit"


def test_kube_client_requires_server(tmp_path) -> None:
    path = tmp_path / "kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "current-context": "d",
                "clusters": [{"name": "d", "cluster": {}}],
                "contexts": [{"name": "d", "context": {"cluster": "d", "user": "u"}}],
                "users": [{"name": "u", "user": {}}],
            }
        )
    )
    with pytest.raises(MicrovmError, match="no server"):
        kube_client(K8sSettings(kubeconfig=str(path)))

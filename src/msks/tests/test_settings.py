"""Settings loading: defaults, env overrides, validation."""

import pytest
from msks.settings import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MSKSD_VMM_DRIVER",
        "MSKSD_K8S_NAMESPACE",
        "MSKSD_SHUTDOWN_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.vmm.driver == "local"
    assert settings.k8s.namespace == "msks"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_VMM_DRIVER", "k8s")
    monkeypatch.setenv("MSKSD_K8S_NAMESPACE", "sandboxes")
    monkeypatch.setenv("MSKSD_SHUTDOWN_TIMEOUT_S", "3.5")
    settings = Settings.from_env()
    assert settings.vmm.driver == "k8s"
    assert settings.k8s.namespace == "sandboxes"
    assert settings.vmm.shutdown_timeout_s == 3.5


def test_invalid_driver_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_VMM_DRIVER", "firecracker")
    with pytest.raises(ValueError, match="MSKSD_VMM_DRIVER"):
        Settings.from_env()


def test_non_numeric_float_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_REQUEST_TIMEOUT_S", "soon")
    with pytest.raises(ValueError, match="MSKSD_REQUEST_TIMEOUT_S"):
        Settings.from_env()


def test_bad_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_PORT", "https")
    with pytest.raises(ValueError, match="MSKSD_PORT"):
        Settings.from_env()


def test_nonpositive_poll_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_EVENT_POLL_S", "0")
    with pytest.raises(ValueError, match="MSKSD_EVENT_POLL_S"):
        Settings.from_env()


def test_nonpositive_artifact_sizes_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact sizes are positive integers (#14); the claim size is
    at least 1 GiB and unset (None) by default — derived at create."""
    monkeypatch.setenv("MSKSD_ROOT_MIB", "0")
    with pytest.raises(ValueError, match="MSKSD_ROOT_MIB"):
        Settings.from_env()
    monkeypatch.delenv("MSKSD_ROOT_MIB")
    monkeypatch.setenv("MSKSD_HOME_MIB", "-5")
    with pytest.raises(ValueError, match="MSKSD_HOME_MIB"):
        Settings.from_env()
    monkeypatch.delenv("MSKSD_HOME_MIB")
    monkeypatch.setenv("MSKSD_K8S_WORKSPACE_STORAGE_GIB", "0")
    with pytest.raises(ValueError, match="MSKSD_K8S_WORKSPACE_STORAGE_GIB"):
        Settings.from_env()
    monkeypatch.setenv("MSKSD_K8S_WORKSPACE_STORAGE_GIB", "soon")
    with pytest.raises(ValueError, match="MSKSD_K8S_WORKSPACE_STORAGE_GIB"):
        Settings.from_env()
    monkeypatch.setenv("MSKSD_K8S_WORKSPACE_STORAGE_GIB", "7")
    settings = Settings.from_env()
    assert settings.k8s.workspace_storage_gib == 7
    monkeypatch.delenv("MSKSD_K8S_WORKSPACE_STORAGE_GIB")
    assert Settings.from_env().k8s.workspace_storage_gib is None


def test_access_log_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_ACCESS_LOG", "true")
    assert Settings.from_env().server.access_log is True

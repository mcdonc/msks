"""Settings loading: defaults, env overrides, validation."""

import pytest
from msks.settings import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MSKSD_VMM_DRIVER",
        "MSKSD_SHUTDOWN_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.vmm.driver == "local"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_VMM_DRIVER", "local")
    monkeypatch.setenv("MSKSD_SHUTDOWN_TIMEOUT_S", "3.5")
    monkeypatch.setenv("MSKSD_CONSOLE_STALL_TIMEOUT_S", "0")
    settings = Settings.from_env()
    assert settings.vmm.driver == "local"
    assert settings.vmm.shutdown_timeout_s == 3.5
    # Zero is the documented off switch for the stall close (#103).
    assert settings.vmm.console_stall_timeout_s == 0


def test_negative_forward_wait_timeout_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative dial deadline is a configuration error, not a
    fail-fast zero (#109)."""
    monkeypatch.setenv("MSKSD_FORWARD_WAIT_TIMEOUT_S", "-1")
    with pytest.raises(ValueError, match="MSKSD_FORWARD_WAIT_TIMEOUT_S"):
        Settings.from_env()


def test_negative_move_wait_timeout_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative move-wait bound is a configuration error (#80)."""
    monkeypatch.setenv("MSKSD_MOVE_WAIT_TIMEOUT_S", "-1")
    with pytest.raises(ValueError, match="MSKSD_MOVE_WAIT_TIMEOUT_S"):
        Settings.from_env()


def test_negative_stall_timeout_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative stall window would close healthy sessions (#103)."""
    monkeypatch.setenv("MSKSD_CONSOLE_STALL_TIMEOUT_S", "-1")
    with pytest.raises(ValueError, match="MSKSD_CONSOLE_STALL_TIMEOUT_S"):
        Settings.from_env()


def test_invalid_driver_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_VMM_DRIVER", "firecracker")
    with pytest.raises(ValueError, match="MSKSD_VMM_DRIVER"):
        Settings.from_env()


def test_ssh_key_type_valid_and_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mint type is a setting (#115): a known name loads, an
    unknown one is a named error at settings load, not at create."""
    monkeypatch.setenv("MSKSD_SSH_KEY_TYPE", "ecdsa")
    assert Settings.from_env().vmm.ssh_key_type == "ecdsa"
    monkeypatch.setenv("MSKSD_SSH_KEY_TYPE", "bogus")
    with pytest.raises(ValueError, match="MSKSD_SSH_KEY_TYPE"):
        Settings.from_env()
    monkeypatch.delenv("MSKSD_SSH_KEY_TYPE")
    assert Settings.from_env().vmm.ssh_key_type == "ed25519"


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
    """Artifact sizes are positive integers (#14)."""
    monkeypatch.setenv("MSKSD_ROOT_MIB", "0")
    with pytest.raises(ValueError, match="MSKSD_ROOT_MIB"):
        Settings.from_env()
    monkeypatch.delenv("MSKSD_ROOT_MIB")
    monkeypatch.setenv("MSKSD_HOME_MIB", "-5")
    with pytest.raises(ValueError, match="MSKSD_HOME_MIB"):
        Settings.from_env()
    monkeypatch.delenv("MSKSD_HOME_MIB")


def test_access_log_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_ACCESS_LOG", "true")
    assert Settings.from_env().server.access_log is True


def test_net_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MSKSD_EGRESS_ENABLED",
        "MSKSD_EGRESS_SUBNET",
        "MSKSD_EGRESS_UPLINK",
        "MSKSD_EGRESS_DNS_UPSTREAM",
        "MSKSD_EGRESS_LEASE_S",
        "MSKSD_EGRESS_DNS_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.net.enabled is False
    assert str(settings.net.pool) == "172.31.0.0/16"
    assert settings.net.uplink == "eth0"
    assert settings.net.dns_upstream is None
    assert settings.net.lease_s == 3600
    assert settings.net.dns_timeout_s == 3.0


def test_net_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_EGRESS_ENABLED", "true")
    monkeypatch.setenv("MSKSD_EGRESS_SUBNET", "10.200.0.0/24")
    monkeypatch.setenv("MSKSD_EGRESS_UPLINK", "enp3s0")
    monkeypatch.setenv("MSKSD_EGRESS_DNS_UPSTREAM", "192.168.4.1")
    monkeypatch.setenv("MSKSD_EGRESS_LEASE_S", "600")
    monkeypatch.setenv("MSKSD_EGRESS_DNS_TIMEOUT_S", "1.5")
    settings = Settings.from_env()
    assert settings.net.enabled is True
    assert str(settings.net.pool) == "10.200.0.0/24"
    assert settings.net.uplink == "enp3s0"
    assert settings.net.dns_upstream == "192.168.4.1"
    assert settings.net.lease_s == 600
    assert settings.net.dns_timeout_s == 1.5


def test_net_subnet_must_be_an_ipv4_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_EGRESS_SUBNET", "banana")
    with pytest.raises(ValueError, match="MSKSD_EGRESS_SUBNET"):
        Settings.from_env()


def test_net_subnet_must_hold_a_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_EGRESS_SUBNET", "10.0.0.0/31")
    with pytest.raises(ValueError, match="MSKSD_EGRESS_SUBNET"):
        Settings.from_env()


def test_net_lease_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_EGRESS_LEASE_S", "0")
    with pytest.raises(ValueError, match="MSKSD_EGRESS_LEASE_S"):
        Settings.from_env()


def test_net_dns_timeout_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_EGRESS_DNS_TIMEOUT_S", "0")
    with pytest.raises(ValueError, match="MSKSD_EGRESS_DNS_TIMEOUT_S"):
        Settings.from_env()


def test_mkisofs_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_MKISOFS", "/opt/tools/mkisofs")
    settings = Settings.from_env()
    assert settings.vmm.mkisofs == "/opt/tools/mkisofs"


def test_storage_threshold_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_STORAGE_WARN_PCT", "80")
    monkeypatch.setenv("MSKSD_STORAGE_FLOOR_MIB", "1024")
    settings = Settings.from_env()
    assert settings.vmm.storage_warn_pct == 80
    assert settings.vmm.storage_floor_mib == 1024


def test_storage_warn_pct_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_STORAGE_WARN_PCT", "100")
    with pytest.raises(ValueError, match="MSKSD_STORAGE_WARN_PCT"):
        Settings.from_env()


def test_storage_floor_mib_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSD_STORAGE_FLOOR_MIB", "0")
    with pytest.raises(ValueError, match="MSKSD_STORAGE_FLOOR_MIB"):
        Settings.from_env()


def test_resize_tool_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSD_RESIZE2FS", "/opt/resize2fs")
    monkeypatch.setenv("MSKSD_E2FSCK", "/opt/e2fsck")
    settings = Settings.from_env()
    assert settings.vmm.resize2fs == "/opt/resize2fs"
    assert settings.vmm.e2fsck == "/opt/e2fsck"


# --- egress consent settings (#69) ------------------------------------


def test_consent_settings_defaults_and_env() -> None:
    from msks.settings import NetSettings

    default = NetSettings()
    assert default.egress_mode == "allow"
    assert default.consent_timeout_s == 120.0
    assert default.consent_rate_limit == 8
    assert default.consent_retention_days == 30
    assert default.consent_row_cap == 1000
    assert default.queue_base == 1024
    assert default.conntrack_tool == "conntrack"

    from msks.settings import Settings

    tuned = Settings.from_env(
        {
            "MSKSD_EGRESS_MODE": "interactive",
            "MSKSD_EGRESS_CONSENT_TIMEOUT_S": "45",
            "MSKSD_EGRESS_CONSENT_RATE_LIMIT": "0",
            "MSKSD_EGRESS_CONSENT_RETENTION_DAYS": "7",
            "MSKSD_EGRESS_CONSENT_ROW_CAP": "50",
            "MSKSD_EGRESS_QUEUE_BASE": "2048",
            "MSKSD_CONNTRACK_TOOL": "/usr/sbin/conntrack",
            "MSKSD_AUDIT_HMAC_KEY": "k1",
        }
    )
    assert tuned.net.egress_mode == "interactive"
    assert tuned.net.consent_timeout_s == 45.0
    assert tuned.net.consent_rate_limit == 0  # the cap's off switch
    assert tuned.net.consent_retention_days == 7
    assert tuned.net.consent_row_cap == 50
    assert tuned.net.queue_base == 2048
    assert tuned.net.conntrack_tool == "/usr/sbin/conntrack"
    assert tuned.server.audit_hmac_key == "k1"


def test_consent_settings_name_bad_values() -> None:
    import pytest
    from msks.settings import Settings

    with pytest.raises(ValueError, match="MSKSD_EGRESS_MODE"):
        Settings.from_env({"MSKSD_EGRESS_MODE": "sloppy"})
    with pytest.raises(ValueError, match="MSKSD_EGRESS_CONSENT_TIMEOUT_S"):
        Settings.from_env({"MSKSD_EGRESS_CONSENT_TIMEOUT_S": "0"})

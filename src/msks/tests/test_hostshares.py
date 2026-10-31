"""The daemon-side host-share gates (#667, part #672).

Covers the mounted-roots enumeration (the mount gate's ground
truth), the two gates and their refusal taxonomy through both
host modes (not-mounted names VM recreation; outside-roots names
the config edit), the symlink-escape refusal, the host-form row
grammar and its derived tag, and the settings keys that feed the
gates.
"""

from pathlib import Path

import pytest
from msks.hostshares import (
    check_host_share,
    mounted_share_roots,
)
from msks.settings import MountSettings, host_mode_from, host_roots_from
from msks.spec.mounts import (
    HOST_MODE_IDENTITY,
    HOST_MODE_PREFIX,
    HOST_TAG_PREFIX,
    SHARE_ROOT_PREFIX,
    MountSpec,
    canonical_host_path,
    dump_mounts,
    mount_tag,
    mount_unit,
    mounts_fingerprint,
    parse_mounts,
    validate_mounts,
)

# --- the mounted-roots enumeration ---------------------------------


def mounts_text(*points_fstypes: tuple[str, str]) -> str:
    """A /proc/mounts-shaped document: device, mount point,
    fstype — the three fields the enumeration reads."""
    return "".join(
        f"/dev/fs{i} {point} {fstype} rw 0 0\n"
        for i, (point, fstype) in enumerate(points_fstypes)
    )


def test_mounted_roots_read_virtiofs_under_the_prefix(tmp_path: Path) -> None:
    table = tmp_path / "mounts"
    table.write_text(
        mounts_text(
            (f"{SHARE_ROOT_PREFIX}/Users/probe", "virtiofs"),
            ("/boot/efi", "vfat"),
            (f"{SHARE_ROOT_PREFIX}/Volumes/data", "virtiofs"),
            ("/srv/elsewhere", "virtiofs"),
        )
    )
    assert mounted_share_roots(table) == (
        f"{SHARE_ROOT_PREFIX}/Users/probe",
        f"{SHARE_ROOT_PREFIX}/Volumes/data",
    )


def test_mounted_roots_decode_escaped_fields(tmp_path: Path) -> None:
    """A mount point carrying a space reads octal-escaped in
    /proc/mounts; the enumeration compares the real path."""
    table = tmp_path / "mounts"
    table.write_text(
        mounts_text((f"{SHARE_ROOT_PREFIX}/Users/My\\040Name", "virtiofs"))
    )
    assert mounted_share_roots(table) == (
        f"{SHARE_ROOT_PREFIX}/Users/My Name",
    )


def test_mounted_roots_empty_off_the_prefix(tmp_path: Path) -> None:
    table = tmp_path / "mounts"
    table.write_text(mounts_text(("/boot", "ext4")))
    assert mounted_share_roots(table) == ()


# --- the gates, identity mode ----------------------------------------


def test_identity_mode_allows_under_the_roots(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    served = check_host_share(HOST_MODE_IDENTITY, (str(tmp_path),), str(code))
    assert served == str(code)


def test_identity_mode_refuses_outside_the_roots(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    with pytest.raises(ValueError, match="outside the allowed"):
        check_host_share(HOST_MODE_IDENTITY, (str(code),), str(tmp_path))


def test_empty_roots_allow_nothing(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    with pytest.raises(ValueError, match="outside the allowed"):
        check_host_share(HOST_MODE_IDENTITY, (), str(code))


def test_a_missing_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        check_host_share(
            HOST_MODE_IDENTITY, (str(tmp_path),), str(tmp_path / "nope")
        )


def test_a_symlink_escape_is_refused(tmp_path: Path) -> None:
    """A path inside an allowed root that resolves outside it —
    the #667 escape: the realpath happens before the prefix
    check, so the share cannot walk out of what the operator
    allowed."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (allowed / "escape").symlink_to(secret)
    with pytest.raises(ValueError, match="outside the allowed"):
        check_host_share(
            HOST_MODE_IDENTITY, (str(allowed),), str(allowed / "escape")
        )


# --- the gates, prefix mode ------------------------------------------


def prefix_table(*roots: str) -> str:
    """A mount table whose virtiofs entries are exactly the
    given roots under the prefix."""
    return mounts_text(*((root, "virtiofs") for root in roots))


def prefix_fixture(monkeypatch, tmp_path: Path) -> Path:
    """A prefix-mode daemon on this test host: the share roots the
    table reports live under the reserved prefix (the daemon VM's
    view), while the directories live under the throwaway root —
    the resolve seam maps one onto the other, the same mapping a
    real daemon gets from its own mount table."""
    home = tmp_path / "v" / "Users" / "probe"
    (home / "code").mkdir(parents=True)
    (home / "projects").mkdir()
    table = tmp_path / "mounts"
    table.write_text(prefix_table(f"{SHARE_ROOT_PREFIX}/Users/probe"))

    def fake_resolve(path: str) -> str:
        # String concat, not a pathlib join: the suffix is
        # absolute, and joining an absolute path onto a base
        # replaces the base outright.
        if path.startswith(SHARE_ROOT_PREFIX):
            return str(tmp_path / "v") + path[len(SHARE_ROOT_PREFIX) :]
        return path

    monkeypatch.setattr("msks.hostshares.resolve_path", fake_resolve)
    return table


def test_prefix_mode_translates_and_gates(monkeypatch, tmp_path: Path) -> None:
    table = prefix_fixture(monkeypatch, tmp_path)
    served = check_host_share(
        HOST_MODE_PREFIX,
        ("/Users/probe",),
        "/Users/probe/code",
        source=table,
    )
    assert served == str(tmp_path / "v" / "Users" / "probe" / "code")


def test_prefix_mode_names_vm_recreation(monkeypatch, tmp_path: Path) -> None:
    table = prefix_fixture(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="recreate the VM"):
        check_host_share(
            HOST_MODE_PREFIX,
            ("/Users/probe",),
            "/Volumes/elsewhere",
            source=table,
        )


def test_prefix_mode_policy_gate_names_the_config(
    monkeypatch, tmp_path: Path
) -> None:
    table = prefix_fixture(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="MSKSD_MOUNT_HOST_ROOTS"):
        check_host_share(
            HOST_MODE_PREFIX,
            ("/Users/probe/projects",),
            "/Users/probe/code",
            source=table,
        )


# --- the host-form row grammar ---------------------------------------


def test_host_rows_parse_with_the_rw_default() -> None:
    mounts = parse_mounts([{"host": "/Users/p/code", "guest": "/mnt/code"}])
    assert mounts == (
        MountSpec(guest="/mnt/code", mode="rw", host="/Users/p/code"),
    )
    assert dump_mounts(mounts) == (
        '[{"guest": "/mnt/code", "mode": "rw", "host": "/Users/p/code"}]'
    )


def test_host_rows_take_ro_explicitly() -> None:
    mounts = parse_mounts(
        [{"host": "/Users/p/code", "guest": "/mnt/code", "mode": "ro"}]
    )
    assert mounts[0].mode == "ro"


def test_a_row_names_one_form() -> None:
    """The one grammar (#685): the share key is an unknown key
    again, and a row without its host path is a named error."""
    with pytest.raises(ValueError, match="unknown keys.*share"):
        parse_mounts(
            [
                {
                    "share": "proj",
                    "host": "/Users/p",
                    "guest": "/mnt",
                }
            ]
        )
    with pytest.raises(ValueError, match="needs a host path"):
        parse_mounts([{"guest": "/mnt"}])


def test_the_host_tag_is_derived_and_stable() -> None:
    one = parse_mounts([{"host": "/Users/p/code", "guest": "/m"}])[0]
    two = parse_mounts([{"host": "/Users/p/code", "guest": "/m2"}])[0]
    assert mount_tag(one) == mount_tag(two)
    assert mount_tag(one).startswith(HOST_TAG_PREFIX)
    assert len(mount_tag(one)) <= 36
    assert safe_charset(mount_tag(one))


def safe_charset(tag: str) -> bool:
    return all((ch.isascii() and ch.isalnum()) or ch in "._-" for ch in tag)


def test_validate_refuses_a_repeated_host_and_tag_collisions() -> None:
    twice = (
        MountSpec(guest="/a", mode="ro", host="/Users/p/code"),
        MountSpec(guest="/b", mode="ro", host="/Users/p/code"),
    )
    with pytest.raises(ValueError, match="mounted twice"):
        validate_mounts(twice)


def test_units_and_fingerprints_key_off_the_tag() -> None:
    """The tag is the device name (#685: every mount derives one)
    — the unit's What and the fingerprint's key both ride it, and
    a different guest path under the same host keeps the tag but
    changes the fingerprint."""
    host = parse_mounts([{"host": "/Users/p/code", "guest": "/mnt/c"}])[0]
    other_guest = parse_mounts(
        [{"host": "/Users/p/code", "guest": "/mnt/other"}]
    )[0]
    assert f"What={mount_tag(host)}" in mount_unit(host)
    assert mounts_fingerprint((host,)) == mounts_fingerprint((host,))
    assert mounts_fingerprint((host,)) != mounts_fingerprint((other_guest,))


# --- the settings keys ------------------------------------------------


def test_host_mode_defaults_and_refuses(tmp_path: Path) -> None:
    assert host_mode_from({}) == HOST_MODE_IDENTITY
    assert host_mode_from({"MSKSD_MOUNT_HOST_MODE": "prefix"}) == "prefix"
    with pytest.raises(ValueError, match="MSKSD_MOUNT_HOST_MODE"):
        host_mode_from({"MSKSD_MOUNT_HOST_MODE": "bridged"})


def test_host_roots_parse_and_canonicalize(tmp_path: Path) -> None:
    assert host_roots_from({}) == ()
    roots = host_roots_from(
        {"MSKSD_MOUNT_HOST_ROOTS": '["/Users/p", "/Users/q/"]'}
    )
    assert roots == ("/Users/p", "/Users/q")
    with pytest.raises(ValueError, match="host paths"):
        host_roots_from({"MSKSD_MOUNT_HOST_ROOTS": "[5]"})
    with pytest.raises(ValueError, match="must be absolute"):
        host_roots_from({"MSKSD_MOUNT_HOST_ROOTS": '["relative"]'})
    # The raw-list walk's own refusals: malformed JSON and a
    # scalar both name the JSON-array shape.
    with pytest.raises(ValueError, match="MSKSD_MOUNT_HOST_ROOTS must be"):
        host_roots_from({"MSKSD_MOUNT_HOST_ROOTS": "not-json"})
    with pytest.raises(ValueError, match="MSKSD_MOUNT_HOST_ROOTS must be"):
        host_roots_from({"MSKSD_MOUNT_HOST_ROOTS": '"scalar"'})


def test_mount_settings_carry_the_gate_inputs(tmp_path: Path) -> None:
    settings = MountSettings.from_env(
        {
            "MSKSD_MOUNT_HOST_MODE": "prefix",
            "MSKSD_MOUNT_HOST_ROOTS": '["/Users/p"]',
        }
    )
    assert settings.host_mode == HOST_MODE_PREFIX
    assert settings.host_roots == ("/Users/p",)


# --- the remaining grammar and payload edges -------------------------


def test_control_characters_are_refused_unicode_passes() -> None:
    """A newline in a host path is legal POSIX but lands verbatim
    in the guest mount unit's Description, where it would inject
    directives — C0 and DEL are refused at parse. Unicode folder
    names pass: the macOS rationale the shape rules carry."""
    with pytest.raises(ValueError, match="control characters"):
        canonical_host_path("/Users/p/co\nde")
    with pytest.raises(ValueError, match="control characters"):
        canonical_host_path("/Users/p/ta\tb")
    with pytest.raises(ValueError, match="control characters"):
        canonical_host_path("/Users/p/del\x7f")
    assert canonical_host_path("/Users/chrism/Ünïcode") == (
        "/Users/chrism/Ünïcode"
    )


def test_a_non_string_host_field_is_a_named_refusal() -> None:
    with pytest.raises(ValueError, match="needs a host path"):
        parse_mounts([{"host": 5, "guest": "/mnt"}])


def test_an_issue_payload_carries_the_form_key() -> None:
    from msks.spec.mounts import MountIssue

    host = MountSpec(guest="/mnt/code", mode="ro", host="/Users/p/code")
    frame = MountIssue(kind="skipped", mount=host, detail="why").payload("ws")
    assert frame == {
        "id": "ws",
        "host": "/Users/p/code",
        "guest": "/mnt/code",
        "mode": "ro",
        "detail": "why",
    }


def test_a_truncated_escape_reads_as_its_characters(tmp_path: Path) -> None:
    """An escape-shaped run that is not a full octal triplet — a
    backslash at the field's end, or two digits — reads as its
    literal characters, the same decoding the kernel's writer
    guarantees never to produce."""
    table = tmp_path / "mounts"
    table.write_text(mounts_text((f"{SHARE_ROOT_PREFIX}/a\\04b", "virtiofs")))
    assert mounted_share_roots(table) == (f"{SHARE_ROOT_PREFIX}/a\\04b",)
    table.write_text(mounts_text((f"{SHARE_ROOT_PREFIX}/tail\\", "virtiofs")))
    assert mounted_share_roots(table) == (f"{SHARE_ROOT_PREFIX}/tail\\",)

"""The host-mount state and policy layer (#620, #667; #685).

Covers the retired config key's removal message, the mount
vocabulary's row rules (host-path grammar, the retired named
form dropped at read, guest-path rules, mode bound), the model
roundtrip, and the start hook: a start with a gate-refused mount
succeeds and publishes its skip event.

Also covers the host-share geometry of #667 (#671): the reserved
prefix and the host-mode translation's two spellings, the
share-root row validation (the suffix-preservation invariant and
its refusals), the template-level set rules, and the shipped
Lima template's conformance to all of it.
"""

import json
from pathlib import Path

import pytest
from msks.app import build_app
from msks.config import file_env_overrides
from msks.microvm import VmSpec
from msks.server.api.rows import starting_workspaces
from msks.settings import MountSettings, ServerSettings, Settings
from msks.spec.mounts import (
    HOST_MODE_IDENTITY,
    HOST_MODE_PREFIX,
    HOST_MODES,
    MODE_RW,
    SHARE_ROOT_PREFIX,
    MountSpec,
    ShareRoot,
    canonical_host_path,
    dump_mounts,
    mount_tag,
    parse_mounts,
    share_root_row,
    share_roots_from_template,
    translate_host_path,
    untranslate_vm_path,
    validate_mounts,
)
from test_api import auth, client  # noqa: F401 (fixture)

# --- the share config -------------------------------------------------


def test_mount_shares_config_key_names_the_removal(
    tmp_path: Path,
) -> None:
    """The removed key (#685): a config that still names
    ``mount_shares`` — in either spelling, hyphen or underscore —
    fails at load with the removal message, not the valid-keys
    list a retiree would drown in. The message points at the one
    surface that replaced it."""
    for spelling in ("mount_shares", "mount-shares"):
        config = tmp_path / "msksd.yaml"
        config.write_text(
            f"{spelling}:\n  - name: share\n    path: /srv/share\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as raised:
            file_env_overrides(str(config))
        assert "was removed (#685)" in str(raised.value)
        assert "mount_host_roots" in str(raised.value)
        assert "--share" in str(raised.value)


def test_the_removal_leaves_every_unknown_key_its_own_error(
    tmp_path: Path,
) -> None:
    """A different unknown key keeps the ordinary refusal: the
    removal message is the retiree's alone."""
    config = tmp_path / "msksd.yaml"
    config.write_text("mount_share: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config key"):
        file_env_overrides(str(config))


def test_unknown_config_key_still_refuses(tmp_path: Path) -> None:
    """The flat grammar holds: a nested ``mounts:`` block is an
    unknown key, refused at load naming the valid keys."""
    config = tmp_path / "msksd.yaml"
    config.write_text("mounts:\n  shares: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config key"):
        file_env_overrides(str(config))


# --- the mount vocabulary ---------------------------------------------


def mounts(*entries: dict) -> tuple[MountSpec, ...]:
    """Decode through the row's JSON form, the shape the model
    stores — parse and construct host one path."""
    return parse_mounts(json.dumps(list(entries)))


def test_parse_defaults_mode_to_rw() -> None:
    """A mount that names no mode reads rw — the request names the
    mode, and rw is the default it names."""
    (mount,) = mounts({"host": "/srv/projects", "guest": "/mnt/p"})
    assert mount.mode == MODE_RW
    assert parse_mounts(None) == ()
    assert parse_mounts("[]") == ()
    assert dump_mounts(()) is None


def test_parse_refusals() -> None:
    """Every malformed row shape is a named error at parse."""
    with pytest.raises(ValueError, match="unknown keys"):
        mounts({"host": "/srv/s", "guest": "/g", "extra": 1})
    with pytest.raises(ValueError, match="needs a host path"):
        mounts({"guest": "/g"})
    with pytest.raises(ValueError, match="needs a guest"):
        mounts({"host": "/srv/s"})
    with pytest.raises(ValueError, match="mode must be one of"):
        mounts({"host": "/srv/s", "guest": "/g", "mode": "rw+"})
    with pytest.raises(ValueError, match="must be mappings"):
        parse_mounts(["not-a-mapping"])
    with pytest.raises(ValueError, match="must be a JSON array"):
        parse_mounts({"host": "/s"})
    with pytest.raises(ValueError, match="must be a JSON array"):
        parse_mounts("not-json")


def test_retired_named_entries_drop_at_read() -> None:
    """The #620 named form is removed (#685): entries a pre-#685
    daemon stored drop at read — the row keeps its stored shape
    until the workspace's mounts are set again, and every read
    sees the drop. A healthy host mount beside them survives, and
    the mix that carries both keys is not the retired form: it
    fails on the unknown key, the same refusal a typo takes."""
    parsed = mounts(
        {"share": "projects", "guest": "/mnt/p"},
        {"host": "/srv/other", "guest": "/srv/other-guest"},
        {"share": "gone", "guest": "/mnt/gone", "mode": "ro"},
    )
    assert [mount.host for mount in parsed] == ["/srv/other"]
    assert [mount.guest for mount in parsed] == ["/srv/other-guest"]
    with pytest.raises(ValueError, match="unknown keys.*share"):
        mounts({"share": "s", "host": "/srv/s", "guest": "/g"})


@pytest.mark.parametrize(
    ("guest", "why"),
    [
        ("mnt/p", "must be absolute"),
        ("/mnt/../etc", "must not contain"),
        ("/", "filesystem root"),
        ("/proc/self", "at or under /proc"),
        ("/sys", "at or under /sys"),
        ("/dev/sda", "at or under /dev"),
        ("/run/sock", "at or under /run"),
        ("/home", "home volume"),
        ("/home/dev/projects", "home volume"),
        # #620 part #623: the path lands verbatim in a systemd
        # unit file — control characters would break or inject
        # the unit, non-ASCII would escape bytes the operator
        # never typed.
        ("/mnt/new\nline", "printable ASCII"),
        ("/mnt/tab\there", "printable ASCII"),
        ("/mnt/ctrl\x01x", "printable ASCII"),
        ("/mnt/café", "printable ASCII"),
    ],
)
def test_guest_path_refusals(guest: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        validate_mounts(mounts({"host": "/srv/s", "guest": guest}))


def test_guest_path_takes_spaces() -> None:
    """A space prints (#623): the path keeps it, and the unit name
    escapes it — the mount point is the one the operator typed."""
    validate_mounts(mounts({"host": "/srv/s", "guest": "/mnt/my share"}))
    parsed = parse_mounts('[{"host": "/srv/s", "guest": "/mnt/my share"}]')
    assert parsed[0].guest == "/mnt/my share"


def test_row_disjointness_refusals() -> None:
    """A share appears once, a guest path is mounted once, and no
    mount nests under another — each refusal names the pair."""
    with pytest.raises(ValueError, match="mounted twice"):
        validate_mounts(
            mounts(
                {"host": "/srv/a", "guest": "/mnt/a"},
                {"host": "/srv/a", "guest": "/mnt/b"},
            )
        )
    with pytest.raises(ValueError, match="guest path '/mnt/a' is mounted"):
        validate_mounts(
            mounts(
                {"host": "/srv/a", "guest": "/mnt/a"},
                {"host": "/srv/b", "guest": "/mnt/a"},
            )
        )
    with pytest.raises(ValueError, match="shadows"):
        validate_mounts(
            mounts(
                {"host": "/srv/a", "guest": "/mnt/a"},
                {"host": "/srv/b", "guest": "/mnt/a/deep"},
            )
        )


# --- the model roundtrip and the start hook ---------------------------


def msks_server_settings(db_path: Path) -> ServerSettings:
    """The roundtrip fixture's server settings: a throwaway
    database."""
    return ServerSettings(db_path=db_path)


async def test_set_mounts_roundtrip(tmp_path: Path) -> None:
    """set_mounts writes the whole list; the row view decodes it
    back; None clears to the mountless row."""
    settings = Settings(server=msks_server_settings(tmp_path / "roundtrip.db"))
    app = build_app(settings)
    model = app.state.model
    model.migrate()
    await model.create_workspace(
        VmSpec(workspace_id="ws-m", kernel="/k", rootfs="/r")
    )
    await model.set_mounts(
        "ws-m",
        [{"host": "/srv/projects", "guest": "/mnt/p", "mode": "rw"}],
    )
    row = await model.get_workspace("ws-m")
    assert row["mounts"] == [
        {"host": "/srv/projects", "guest": "/mnt/p", "mode": "rw"}
    ]
    await model.set_mounts("ws-m", None)
    row = await model.get_workspace("ws-m")
    assert row["mounts"] == []


async def test_start_publishes_skips_and_proceeds(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The resilient start (#620, #672): a mount whose root the
    policy gate no longer allows skips — the start still
    launches, and the skip publishes its event with the named
    reason and the host key."""
    http, app, stub = client
    proj = tmp_path / "proj"
    proj.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    app.state.settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-mounts", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid,
        [
            {"host": str(gone), "guest": "/mnt/gone", "mode": "rw"},
            {"host": str(proj), "guest": "/mnt/fine", "mode": "ro"},
        ],
    )
    # The reload narrows the allowed roots away from `gone`: the
    # mount's skip is derived state, the reload's story.
    app.state.settings.mounts = MountSettings(host_roots=(str(proj),))
    published: list[tuple[str, dict]] = []
    real_publish = app.state.hub.publish

    async def spy_publish(event: str, data: dict) -> None:
        published.append((event, data))
        await real_publish(event, data)

    app.state.hub.publish = spy_publish
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert ("launch", wid) in stub.calls
    assert published == [
        (
            "mount.skipped",
            {
                "id": wid,
                "host": str(gone),
                "guest": "/mnt/gone",
                "mode": "rw",
                "detail": (
                    f"host path {str(gone)!r} is outside the allowed "
                    "host-share roots; add it (or a parent directory) to "
                    "the daemon's config (MSKSD_MOUNT_HOST_ROOTS) and "
                    "reload"
                ),
            },
        )
    ]


def test_disjoint_mounts_validate_clean() -> None:
    """The happy path: no mounts, and two mounts on disjoint guest
    paths from two shares, pass the row rules untouched."""
    validate_mounts(())
    validate_mounts(
        mounts(
            {"host": "/srv/a", "guest": "/mnt/a"},
            {"host": "/srv/b", "guest": "/mnt/b", "mode": "ro"},
        )
    )


def test_double_slash_anchor_canonicalizes() -> None:
    """Pathlib preserves exactly two leading slashes per POSIX —
    a distinct anchor that would dodge the disjoint rules. The
    canonical form pins the anchor to one slash, so `//tmp` and
    `/tmp` collide as one guest path."""
    (mount,) = mounts({"host": "/srv/s", "guest": "//tmp"})
    assert mount.guest == "/tmp"
    with pytest.raises(ValueError, match="is mounted twice"):
        validate_mounts(
            mounts(
                {"host": "/srv/a", "guest": "/tmp"},
                {"host": "/srv/b", "guest": "//tmp"},
            )
        )
    with pytest.raises(ValueError, match="shadows"):
        validate_mounts(
            mounts(
                {"host": "/srv/a", "guest": "/mnt"},
                {"host": "/srv/b", "guest": "//mnt/a/deep"},
            )
        )


async def test_starting_workspaces_installs_when_absent(client) -> None:  # noqa: F811
    """The in-flight accessor installs its set when the app lacks
    one, so the route's add/discard always lands on stored state —
    a fresh default per call would silently drop the hold."""
    _http, app, _stub = client
    delattr(app.state, "starting_workspaces")
    installed = starting_workspaces(app)
    installed.add("ws-x")
    assert "ws-x" in starting_workspaces(app)


def mounts_for_guest_seed_tests():
    """Two mounts the seed-block tests render: one rw at a plain
    path, one ro under a deeper tree. The What= values are the
    derived tags (hs- plus hash), not the host paths — a tag
    names a virtiofs device."""
    return (
        MountSpec(host="/srv/proj", guest="/mnt/proj"),
        MountSpec(host="/srv/tree/docs", guest="/srv/tree/docs", mode="ro"),
    )


def test_mounts_seed_block_writes_units_and_prunes() -> None:
    """The seed's mounts half (#620, part #623): a manifest of the
    wanted unit names, one heredoc'd unit per mount parked under
    /etc/msks/mount-units, the prune loop stopping and removing
    the parked units the set no longer names, and the per-boot
    installer that carries the units into /run/systemd/system —
    volatile, so the installer is what survives a reboot. All
    inside the systemctl guard."""
    from msks.spec.mounts import mounts_seed_block

    block = mounts_seed_block(mounts_for_guest_seed_tests())
    assert block.startswith("if command -v systemctl >/dev/null 2>&1; then")
    assert block.rstrip().endswith("fi")
    # The installer names a missing virtiofs driver at start (#674):
    # the units' filesystem, checked only when units exist, the
    # named line on the console and cloud-init's log.
    assert "does not ship virtio_fs" in block
    assert "modprobe virtiofs 2>/dev/null || true" in block
    # The manifest names exactly the wanted units.
    assert "mnt-proj.mount\n" in block
    assert "srv-tree-docs.mount\n" in block
    # One heredoc'd unit per mount, parked by the escaped path —
    # /etc/msks/mount-units, the unit home the two guest images
    # share (/etc/systemd/system is a store symlink on NixOS).
    assert "cat > '/etc/msks/mount-units/mnt-proj.mount' <<'MSEOF'" in block
    assert (
        "cat > '/etc/msks/mount-units/srv-tree-docs.mount' <<'MSEOF'" in block
    )
    # The units carry the ownership marker the prune loop greps and
    # the virtiofs facts — the tag on What is the derived device
    # name, computed from the host path.
    proj, docs = mounts_for_guest_seed_tests()
    assert f"What={mount_tag(proj)}" in block
    assert f"What={mount_tag(docs)}" in block
    assert "Where=/mnt/proj" in block
    assert "Type=virtiofs" in block
    assert "Options=ro" in block  # the ro mount
    # rw is the mount default: no Options line for it. The ro one
    # is the only Options line in the block.
    assert block.count("Options=") == 1
    # The prune loop: manifest membership, stop, remove — the
    # parked file and its runtime copy together.
    assert 'grep -qxF "${u##*/}" /etc/msks/mount.units' in block
    assert 'systemctl stop "${u##*/}"' in block
    assert '"/run/systemd/system/${u##*/}"' in block
    # The per-boot installer: written, made executable, run now —
    # and its body copies each parked unit into the runtime dir
    # and starts it.
    installer = "/var/lib/cloud/scripts/per-boot/50-msks-mounts"
    assert f"cat > '{installer}' <<'MSEOF'" in block
    assert f"chmod 0755 '{installer}'" in block
    assert f"sh '{installer}'" in block
    assert 'cp "$u" /run/systemd/system/' in block
    assert 'systemctl start "${u##*/}"' in block
    assert "systemctl daemon-reload" in block
    # The ordering anchor names each mount's parent chain.
    assert "RequiresMountsFor=/mnt" in block
    assert "RequiresMountsFor=/srv/tree" in block


def test_mounts_seed_block_quotes_hard_paths() -> None:
    """A guest path with a space single-quotes into install -d, and
    one with a quote uses the close-reopen spelling — both mount."""
    from msks.spec.mounts import MountSpec, mounts_seed_block

    spaced = mounts_seed_block(
        (MountSpec(host="/srv/a", guest="/mnt/my dir"),)
    )
    assert "install -d -m 0755 '/mnt/my dir'" in spaced
    quoted = mounts_seed_block(
        (MountSpec(host="/srv/a", guest="/mnt/o'brien"),)
    )
    # The exact spelling: the path opens, closes around a quoted
    # quote, and reopens.
    assert "'/mnt/o'\\''brien'" in quoted


def test_mounts_seed_block_empty_is_the_prune_only_block() -> None:
    """The empty mount set still carries the block: the manifest is
    empty and the prune loop is what removes the last mount's
    units after an all-mounts-removed renewal."""
    from msks.spec.mounts import mounts_seed_block

    block = mounts_seed_block(())
    assert "<<'MSEOF'\nMSEOF" in block  # an empty manifest
    assert 'systemctl stop "${u##*/}"' in block  # the prune half
    assert "sh '/var/lib/cloud/scripts/per-boot/50-msks-mounts'" in block


def test_systemd_escape_matches_the_tool() -> None:
    """The escape is pinned against live systemd-escape output: a
    divergence would make systemd refuse the unit (its file name
    must equal the escaped Where= path). Vectors cover the
    separator, a space, a literal hyphen (which escapes, keeping
    the mapping injective), dots, underscores, and non-ASCII bytes
    (escaped per UTF-8 byte)."""
    import shutil
    import subprocess

    import pytest
    from msks.spec.mounts import systemd_escape_path

    if shutil.which("systemd-escape") is None:
        pytest.skip("systemd-escape is not on this host")
    vectors = [
        "/mnt/proj",
        "/mnt/my share/x",
        "/mnt/a.b-c_d",
        "/mnt/.hidden/dir",
        # the leading-dot rule: a dot that would lead the escaped
        # string escapes too (systemd.unit(5)), so the name never
        # starts with a bare dot — /.config is \x2econfig.mount.
        "/.config",
        "/.a",
        "/srv/ü",
        "/a:b/c+d~e",
    ]
    for path in vectors:
        tool = subprocess.run(
            ["systemd-escape", "-p", path],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert systemd_escape_path(path) == tool, path


def test_mount_unit_grammar() -> None:
    """The unit text: the marker first (the prune grep anchors on
    it), the tag and path on What/Where, virtiofs on Type, Options
    only for ro, and the parent-chain anchor."""
    from msks.spec.mounts import (
        MountSpec,
        mount_parent,
        mount_unit,
        mount_unit_name,
    )

    rw = MountSpec(host="/srv/proj", guest="/mnt/proj")
    ro = MountSpec(host="/srv/docs", guest="/srv/tree/docs", mode="ro")
    assert mount_unit_name(rw) == "mnt-proj.mount"
    assert mount_unit(rw).startswith("# msks mount unit")
    assert "Options=" not in mount_unit(rw)
    assert "Options=ro\n" in mount_unit(ro)
    assert "WantedBy=multi-user.target" in mount_unit(ro)
    # Parent chain: deep paths anchor one level up, a top-level
    # path anchors at the root.
    assert mount_parent("/mnt/proj") == "/mnt"
    assert mount_parent("/proj") == "/"


def test_mounts_fingerprint_keys_on_the_whole_set() -> None:
    """The fingerprint changes with share, guest, or mode, ignores
    order, and differs from the empty set."""
    from msks.spec.mounts import MountSpec, mounts_fingerprint

    a = MountSpec(host="/srv/a", guest="/mnt/a")
    same_a = MountSpec(host="/srv/a", guest="/mnt/a")
    b = MountSpec(host="/srv/a", guest="/mnt/a", mode="ro")
    c = MountSpec(host="/srv/a", guest="/mnt/c")
    assert mounts_fingerprint((a,)) == mounts_fingerprint((same_a,))
    assert mounts_fingerprint((a,)) != mounts_fingerprint((b,))
    assert mounts_fingerprint((a,)) != mounts_fingerprint((c,))
    assert mounts_fingerprint((a, c)) == mounts_fingerprint((c, a))
    assert mounts_fingerprint(()) != mounts_fingerprint((a,))


def test_guest_path_unit_length_refuses_at_the_boundary() -> None:
    """The escaped unit name fits systemd's loader (UNIT_NAME_MAX,
    suffix included): a path whose name is exactly at the ceiling
    passes, one byte more refuses naming the limit — the failure
    it prevents is a unit the loader silently rejects."""
    from msks.spec.mounts import mount_unit_name

    at_edge = "/" + "a" * (255 - len(".mount"))
    assert len(mount_unit_name(mounts_for_edge(at_edge)).encode()) == 255
    validate_mounts(mounts({"host": "/srv/s", "guest": at_edge}))
    # Escape inflation counts: a shorter path with spaces crosses
    # the same ceiling.
    spaced = "/" + "a " * 64
    assert len(spaced) < len(at_edge)
    with pytest.raises(ValueError, match="longer than 255"):
        validate_mounts(mounts({"host": "/srv/s", "guest": spaced}))


def mounts_for_edge(guest: str):
    from msks.spec.mounts import MountSpec

    return MountSpec(host="/srv/s", guest=guest)


@pytest.mark.parametrize(
    ("mounts_body", "why"),
    [
        ([{"host": "/srv/p", "guest": "relative"}], "must be absolute"),
        (
            [
                {"host": "/srv/p", "guest": "/mnt/a"},
                {"host": "/srv/p", "guest": "/mnt/b"},
            ],
            "mounted twice",
        ),
    ],
)
async def test_create_refuses_mounts_the_row_rules_deny(
    client,  # noqa: F811
    tmp_path: Path,
    mounts_body: list,
    why: str,
) -> None:
    """The create's row-rule refusals (#620, part #624): a guest
    path that is not absolute and a host path mounted twice —
    each a named 400 with nothing written."""
    http, app, _stub = client
    root = tmp_path / "code"
    root.mkdir()
    app.state.settings.mounts = MountSettings(host_roots=(str(root),))
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": mounts_body,
        },
        headers=auth(),
    )
    assert created.status_code == 400
    assert why in created.json()["detail"]


async def test_a_set_carrying_the_retired_form_lands_clean(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The removal's migration seam (#685), at the API: a stored
    row's named entries ride a set back — a pre-#685 client's
    read-modify-write, or an old row's first save through a new
    client — and the parser drops them, so the row lands carrying
    only the host form. The reply shows the cleaned list, and a
    row's read serves the cleaned list too: no surface ever sees
    a named entry back."""
    http, app, _stub = client
    proj = tmp_path / "proj"
    proj.mkdir()
    app.state.settings.mounts = MountSettings(host_roots=(str(tmp_path),))
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    # The stored pre-#685 shape, written through the model (the
    # schema cannot spell it — the row predates).
    await app.state.model.set_mounts(
        wid,
        [
            {"share": "proj", "guest": "/mnt/p", "mode": "rw"},
            {"share": "refs", "guest": "/srv/refs", "mode": "ro"},
        ],
    )
    # The read serves the cleaned list.
    row = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert row.json()["mounts"] == []

    # A set carrying the retired entries beside a live host form
    # (the old client's read-modify-write) lands the row clean.
    set_clean = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={
            "mounts": [
                {"share": "proj", "guest": "/mnt/p", "mode": "rw"},
                {"host": str(proj), "guest": "/mnt/proj"},
            ]
        },
        headers=auth(),
    )
    assert set_clean.status_code == 200, set_clean.text
    assert set_clean.json()["mounts"] == [
        {"host": str(proj), "guest": "/mnt/proj", "mode": "rw"}
    ]


async def test_mounts_refuse_while_no_root_is_allowed(
    client,  # noqa: F811
) -> None:
    """Empty allowed roots is the feature off (#672): every mount
    request — create or set — refuses with the config's remedy
    named."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"host": "/srv/proj", "guest": "/mnt/proj"}],
        },
        headers=auth(),
    )
    assert created.status_code == 400
    assert "outside the allowed host-share roots" in created.json()["detail"]


async def test_the_mounts_route_sets_replaces_and_clears(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The mounts route (#620, part #624): the full replacement
    list lands on the row with its derived state, an empty list
    clears, and a running workspace's set succeeds — next-start
    semantics, like resize: the VM keeps its attached devices."""
    http, app, stub = client
    proj = tmp_path / "proj"
    proj.mkdir()
    refs = tmp_path / "refs"
    refs.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]

    set_one = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": [{"host": str(proj), "guest": "/mnt/proj"}]},
        headers=auth(),
    )
    assert set_one.status_code == 200
    assert set_one.json()["mounts"] == [
        {"host": str(proj), "guest": "/mnt/proj", "mode": "rw"}
    ]

    # A running workspace sets fine: nothing launches, the reply's
    # row is the whole contract.
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200
    set_two = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={
            "mounts": [
                {"host": str(proj), "guest": "/mnt/proj", "mode": "ro"},
                {"host": str(refs), "guest": "/srv/refs", "mode": "ro"},
            ]
        },
        headers=auth(),
    )
    assert set_two.status_code == 200
    assert len(set_two.json()["mounts"]) == 2
    assert ("launch", wid) in stub.calls

    cleared = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": []},
        headers=auth(),
    )
    assert cleared.status_code == 200
    assert cleared.json()["mounts"] == []
    assert cleared.json()["mount_issues"] == []


async def test_the_mounts_route_names_its_refusals(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The set route's 400s (#672): a host path outside the
    allowed roots refuses with the config's remedy named, and the
    row keeps the mounts it had."""
    http, app, _stub = client
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    refused = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": [{"host": "/srv/nope", "guest": "/mnt/x"}]},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "outside the allowed host-share roots" in refused.json()["detail"]
    kept = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert kept.json()["mounts"] == []


async def test_clearing_mounts_needs_no_roots_allowed(
    client,  # noqa: F811
) -> None:
    """Clearing is always legal (#620, part #624): the operator
    who removed the last allowed root can still empty a
    workspace's mounts — the empty list passes the gate the
    roots' emptiness would otherwise refuse."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    cleared = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": []},
        headers=auth(),
    )
    assert cleared.status_code == 200
    assert cleared.json()["mounts"] == []


async def test_a_typod_mount_key_refuses_instead_of_dropping(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The wire's unknown keys refuse (#620, part #624): a typo'd
    ``modee`` is a named 422, never a silently dropped mode that
    lands the trust-bearing ``rw`` default."""
    http, app, _stub = client
    proj = tmp_path / "proj"
    proj.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"host": str(proj), "guest": "/mnt/p", "modee": "ro"}],
        },
        headers=auth(),
    )
    assert created.status_code == 422


async def test_a_delete_racing_the_mounts_set_answers_404(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The set route's read-back guard (#624 review): a delete
    landing between the set and the refreshed read answers 404 —
    the row is gone, not a 500 on the missing row."""
    http, app, _stub = client
    proj = tmp_path / "proj"
    proj.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    real_set = app.state.model.set_mounts

    async def set_then_delete(workspace_id, mounts):
        await real_set(workspace_id, mounts)
        await app.state.model.delete_workspace(workspace_id)

    app.state.model.set_mounts = set_then_delete
    gone = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": [{"host": str(proj), "guest": "/mnt/proj"}]},
        headers=auth(),
    )
    assert gone.status_code == 404


# --- the host-share geometry (#667, part #671) ----------------------


def test_prefix_mode_composes_the_reserved_prefix() -> None:
    """The Lima host mode spells a host path as the reserved
    prefix plus the path, and the inverse recovers the operator's
    path; identity leaves both spellings alone."""
    composed = translate_host_path(HOST_MODE_PREFIX, "/Users/chrism/code")
    assert composed == f"{SHARE_ROOT_PREFIX}/Users/chrism/code"
    assert untranslate_vm_path(HOST_MODE_PREFIX, composed) == (
        "/Users/chrism/code"
    )
    assert translate_host_path(HOST_MODE_IDENTITY, "/srv/data") == "/srv/data"
    assert untranslate_vm_path(HOST_MODE_IDENTITY, "/srv/data") == "/srv/data"


def test_share_path_shape_refusals() -> None:
    """Relative paths, traversal, emptiness, and the filesystem
    root are named refusals — spelling cannot dodge the gates."""
    for bad in ("code", "~/code", "/a/../b", "", "/"):
        with pytest.raises(ValueError):
            canonical_host_path(bad)
    assert canonical_host_path("/Users/x/") == "/Users/x"
    assert canonical_host_path("/Users//x/./y") == "/Users/x/y"


def test_prefix_translation_refuses_unprefixed_vm_paths() -> None:
    with pytest.raises(ValueError, match="not under /mnt/msks"):
        untranslate_vm_path(HOST_MODE_PREFIX, "/srv/data")
    with pytest.raises(ValueError, match="prefix itself"):
        untranslate_vm_path(HOST_MODE_PREFIX, SHARE_ROOT_PREFIX)
    with pytest.raises(ValueError, match="host mode"):
        translate_host_path("bridged", "/Users/chrism")
    with pytest.raises(ValueError, match="non-empty string"):
        untranslate_vm_path(HOST_MODE_PREFIX, 5)


def test_untranslate_refuses_paths_that_lexically_escape() -> None:
    """A ``..`` past the prefix names a path outside it even
    though the component prefix matches, and a relative path is
    no daemon-namespace path at all — both are named refusals,
    so the round trip stays total (translate accepts every path
    untranslate returns)."""
    with pytest.raises(ValueError, match="must not contain"):
        untranslate_vm_path(HOST_MODE_PREFIX, f"{SHARE_ROOT_PREFIX}/../etc")
    with pytest.raises(ValueError, match="must not contain"):
        untranslate_vm_path(
            HOST_MODE_PREFIX, f"{SHARE_ROOT_PREFIX}/x/../../etc"
        )
    with pytest.raises(ValueError, match="must be absolute"):
        untranslate_vm_path(HOST_MODE_PREFIX, "mnt/msks/x")
    with pytest.raises(ValueError, match="must be absolute"):
        untranslate_vm_path(HOST_MODE_IDENTITY, "srv/data")
    with pytest.raises(ValueError, match="must not contain"):
        untranslate_vm_path(HOST_MODE_IDENTITY, "/a/../b")
    for mode in HOST_MODES:
        for host in (
            "/Users/chrism/code",
            "/Volumes/data",
            "/srv/a/deep/tree",
        ):
            assert (
                untranslate_vm_path(mode, translate_host_path(mode, host))
                == host
            )


def test_share_root_row_refuses_non_string_mount_point() -> None:
    with pytest.raises(ValueError, match="must be a string, got int"):
        share_root_row("/Users/chrism", 5)
    with pytest.raises(ValueError, match="must be a string, got list"):
        share_roots_from_template(
            [{"location": "/Users/chrism", "mountPoint": ["/mnt/msks/x"]}]
        )


def test_share_root_row_enforces_suffix_preservation() -> None:
    root = share_root_row("/Users/chrism", f"{SHARE_ROOT_PREFIX}/Users/chrism")
    assert root == ShareRoot(
        location="/Users/chrism",
        mount_point=f"{SHARE_ROOT_PREFIX}/Users/chrism",
    )
    with pytest.raises(ValueError, match="reserved-prefix shape"):
        share_root_row("/Users/chrism", f"{SHARE_ROOT_PREFIX}/home/chrism")
    with pytest.raises(ValueError, match="reserved-prefix shape"):
        share_root_row("/Users/chrism", "/Users/chrism")
    with pytest.raises(ValueError, match="no mountPoint"):
        share_root_row("/Users/chrism", None)


def test_share_roots_from_template_set_rules() -> None:
    rows = [
        {
            "location": "/Users/chrism",
            "mountPoint": f"{SHARE_ROOT_PREFIX}/Users/chrism",
        },
        {
            "location": "/Users/chrism/projects",
            "mountPoint": f"{SHARE_ROOT_PREFIX}/Users/chrism/projects",
            "writable": False,
            "sshfs": {"cache": True},
        },
    ]
    roots = share_roots_from_template(rows)
    assert [r.location for r in roots] == [
        "/Users/chrism",
        "/Users/chrism/projects",
    ]
    with pytest.raises(ValueError, match="mounted twice"):
        share_roots_from_template([rows[0], rows[0]])
    with pytest.raises(ValueError, match="mappings"):
        share_roots_from_template(["/Users/chrism"])
    with pytest.raises(ValueError, match="a list"):
        share_roots_from_template(None)


def template_doc() -> dict:
    """The shipped Lima template, parsed. Tests render Lima's two
    expansions themselves — "~" to the user's home path, the
    "{{.User}}" guest template to the user's name — deterministically
    for the probe user, which is what Lima 2.2.1 does at instance
    creation (pkg/limayaml/defaults.go)."""
    from importlib.resources import files

    import yaml

    text = files("msks.server").joinpath("assets/msksd-vm.yaml").read_text()
    doc = yaml.safe_load(text)
    doc["rendered_mounts"] = [
        {
            "location": row["location"].replace("~", "/Users/probe"),
            "mountPoint": row["mountPoint"].replace("{{.User}}", "probe"),
            "writable": row.get("writable"),
        }
        for row in doc["mounts"]
    ]
    return doc


def test_shipped_template_mounts_conform() -> None:
    doc = template_doc()
    roots = share_roots_from_template(doc["rendered_mounts"])
    assert roots == (
        ShareRoot(
            location="/Users/probe",
            mount_point=f"{SHARE_ROOT_PREFIX}/Users/probe",
        ),
    )
    assert doc["rendered_mounts"][0]["writable"] is True


def test_shipped_template_hosts_the_daemon() -> None:
    doc = template_doc()
    assert doc["vmType"] == "vz"
    assert doc["nestedVirtualization"] is True
    assert doc["arch"] == "aarch64"
    assert "9p" in doc["mountTypesUnsupported"]
    assert doc["mountInotify"] is False


# --- the host-form mounts through the API (#672) ---------------------


def host_world(tmp_path: Path) -> MountSettings:
    """A host-share world: identity mode, the throwaway root the
    one allowed root."""
    return MountSettings(host_mode="identity", host_roots=(str(tmp_path),))


async def test_a_host_mount_creates_read_write_by_default(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The #667 client surface behind the API: a host-path mount
    lands on the row read-write unless it says ``ro`` — the same
    default the named form carries — and the row round-trips the
    host path."""
    http, app, _stub = client
    code = tmp_path / "code"
    code.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-h",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"host": str(code), "guest": "/mnt/code"}],
        },
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["mounts"] == [
        {"host": str(code), "guest": "/mnt/code", "mode": "rw"}
    ]


async def test_host_mounts_refuse_with_the_gate_taxonomy(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The two gates refuse at set time with their remedies
    (#667): a path outside the allowed roots names the config
    edit, a path through the gates that does not exist names
    itself — and nothing lands on the row."""
    http, app, _stub = client
    code = tmp_path / "code"
    code.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app.state.settings.mounts = MountSettings(
        host_mode="identity", host_roots=(str(code),)
    )
    for path, why in (
        (str(elsewhere), "outside the allowed host-share roots"),
        (str(code / "nope"), "does not exist or is not a directory"),
    ):
        refused = await http.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-x",
                "kernel": "/k",
                "rootfs": "/r",
                "mounts": [{"host": path, "guest": "/mnt/code"}],
            },
            headers=auth(),
        )
        assert refused.status_code == 400, refused.text
        assert why in refused.json()["detail"]


async def test_prefix_mode_names_vm_recreation(
    client,  # noqa: F811
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The mount gate on a VM-hosted daemon: a path whose root is
    not mounted under the reserved prefix is refused with the
    VM-recreation remedy, and the refusal lands at set time —
    never as a boot-time mount hang (#667)."""
    http, app, _stub = client
    app.state.settings.mounts = MountSettings(
        host_mode="prefix", host_roots=("/Users/probe",)
    )
    monkeypatch.setattr(
        "msks.hostshares.mounted_share_roots", lambda source: ()
    )
    refused = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-v",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"host": "/Users/probe/code", "guest": "/mnt/code"}],
        },
        headers=auth(),
    )
    assert refused.status_code == 400, refused.text
    assert "recreate the VM" in refused.json()["detail"]


async def test_a_narrowed_reload_skips_the_host_mount(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The reload story for host mounts: a share that cleared the
    gates at set time and lost them to a reload reads as a skip
    on the row — the start proceeds without it, the same evidence
    shape a share leaving the config produces."""
    http, app, _stub = client
    code = tmp_path / "code"
    code.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-n",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"host": str(code), "guest": "/mnt/code"}],
        },
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    app.state.settings.mounts = MountSettings(
        host_mode="identity", host_roots=(str(other),)
    )
    row = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert row.json()["mount_issues"] == [
        {
            "kind": "skipped",
            "host": str(code),
            "guest": "/mnt/code",
            "mode": "rw",
            "detail": (
                f"host path {str(code)!r} is outside the allowed "
                "host-share roots; add it (or a parent directory) to "
                "the daemon's config (MSKSD_MOUNT_HOST_ROOTS) and reload"
            ),
        }
    ]


async def test_the_listing_matches_the_row_for_host_skips(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The listing derives the same host-mount skips the row read
    does (#672): both surfaces show the marker, so the CLI's ls
    and the row's show agree."""
    http, app, _stub = client
    code = tmp_path / "code"
    code.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-l",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"host": str(code), "guest": "/mnt/code"}],
        },
        headers=auth(),
    )
    wid = created.json()["id"]
    app.state.settings.mounts = MountSettings(
        host_mode="identity", host_roots=(str(other),)
    )
    row = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    listed = await http.get("/api/v1/workspaces", headers=auth())
    mine = next(r for r in listed.json() if r["id"] == wid)
    assert mine["mount_issues"] == row.json()["mount_issues"]
    assert mine["mount_issues"][0]["kind"] == "skipped"


async def test_host_mounts_hold_no_rw_exclusivity(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Two workspaces mount the same host tree read-write (#672,
    #685): both sets succeed — a host share carries no exclusivity
    anywhere, and the coherence contract is the concurrency
    story."""
    http, app, _stub = client
    tree = tmp_path / "tree"
    tree.mkdir()
    app.state.settings.mounts = host_world(tmp_path)
    for wid in ("ws-a", "ws-b"):
        created = await http.post(
            "/api/v1/workspaces",
            json={"id": wid, "kernel": "/k", "rootfs": "/r"},
            headers=auth(),
        )
        assert created.status_code == 201, created.text
        set_rw = await http.post(
            f"/api/v1/workspaces/{wid}/mounts",
            json={"mounts": [{"host": str(tree), "guest": "/mnt/tree"}]},
            headers=auth(),
        )
        assert set_rw.status_code == 200
        assert set_rw.json()["mount_issues"] == []

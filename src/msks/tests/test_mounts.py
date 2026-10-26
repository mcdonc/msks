"""The host-mount state and policy layer (#620, part #621).

Covers the share config's parse (file list, env JSON, defaults,
named refusals, realpath at load), the mount vocabulary's row
rules and mode bound, the start-time issue derivation (missing
share, rw conflict on an exclusive share, ro beside rw), the
model roundtrip, and the start hook: a start with skipped mounts
succeeds and publishes one event per skip.
"""

import json
from pathlib import Path

import pytest
from msks.app import build_app
from msks.config import LayeredEnv, file_env_overrides
from msks.microvm import VmSpec
from msks.server.api.rows import record_rw_holders, starting_workspaces
from msks.settings import (
    MountSettings,
    ServerSettings,
    Settings,
    mount_shares_from,
)
from msks.spec.mounts import (
    MODE_RW,
    MountShare,
    MountSpec,
    dump_mounts,
    mount_issues,
    parse_mounts,
    validate_against_shares,
    validate_mounts,
)
from test_api import auth, client  # noqa: F401 (fixture)

# --- the share config -------------------------------------------------


def test_shares_default_to_off() -> None:
    """No MSKSD_MOUNT_SHARES anywhere is the feature off: the
    settings carry an empty tuple and every mount request will
    refuse on the missing share."""
    assert mount_shares_from({}) == ()


def test_env_json_array_parses(tmp_path: Path) -> None:
    """The environment's form is one JSON array string; each entry
    lands with the true defaults (writable, multi) and the
    realpath taken at load."""
    root = tmp_path / "projects"
    root.mkdir()
    shares = mount_shares_from(
        {
            "MSKSD_MOUNT_SHARES": json.dumps(
                [{"name": "projects", "path": str(root)}]
            )
        }
    )
    assert shares == (
        MountShare(
            name="projects",
            path=str(root),
            writable=True,
            multi=True,
            resolved=str(root),
        ),
    )


def test_missing_share_path_keeps_unresolved(tmp_path: Path) -> None:
    """A share whose path does not exist yet parses — the doctor
    warns (#622) — with resolved None; no realpath is pinned."""
    missing = tmp_path / "gone"
    (shares,) = mount_shares_from(
        {
            "MSKSD_MOUNT_SHARES": json.dumps(
                [{"name": "gone", "path": str(missing)}]
            )
        }
    )
    assert shares.resolved is None


def test_share_flags_and_refusals(tmp_path: Path) -> None:
    """The narrowings spell out (writable: false, multi: false) and
    every malformed entry is a named error at load."""
    root = tmp_path / "data"
    root.mkdir()
    raw = [
        {"name": "ro", "path": str(root), "writable": False},
        {"name": "one", "path": str(root), "multi": False},
    ]
    shares = mount_shares_from({"MSKSD_MOUNT_SHARES": json.dumps(raw)})
    assert [s.writable for s in shares] == [False, True]
    assert [s.multi for s in shares] == [True, False]
    with pytest.raises(ValueError, match="unknown keys.*bogus"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [{"name": "x", "path": str(root), "bogus": 1}]
                )
            }
        )
    with pytest.raises(ValueError, match="absolute path"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [{"name": "x", "path": "relative"}]
                )
            }
        )
    with pytest.raises(ValueError, match="writable must be true or false"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [{"name": "x", "path": str(root), "writable": "yes"}]
                )
            }
        )
    with pytest.raises(ValueError, match="names share 'x' twice"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [
                        {"name": "x", "path": str(root)},
                        {"name": "x", "path": str(tmp_path)},
                    ]
                )
            }
        )
    with pytest.raises(ValueError, match="JSON array"):
        mount_shares_from({"MSKSD_MOUNT_SHARES": "not-json"})
    with pytest.raises(ValueError, match="JSON array"):
        mount_shares_from({"MSKSD_MOUNT_SHARES": json.dumps("scalar")})
    with pytest.raises(ValueError, match="must be mappings"):
        mount_shares_from({"MSKSD_MOUNT_SHARES": json.dumps(["x"])})
    with pytest.raises(ValueError, match="need a name"):
        mount_shares_from(
            {"MSKSD_MOUNT_SHARES": json.dumps([{"path": str(root)}])}
        )


def test_config_file_list_parses(tmp_path: Path) -> None:
    """The config file's form is the flat ``mount_shares`` key with
    a YAML list of mappings — the same walk as the env's JSON,
    validated through the shared layer."""
    root = tmp_path / "share"
    root.mkdir()
    config = tmp_path / "msksd.yaml"
    config.write_text(
        "mount_shares:\n"
        f"  - name: share\n    path: {root}\n"
        "    writable: false\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(LayeredEnv(file_env_overrides(str(config))))
    assert settings.mounts == MountSettings(
        shares=(
            MountShare(
                name="share",
                path=str(root),
                writable=False,
                multi=True,
                resolved=str(root),
            ),
        )
    )


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
    stores — parse and construct share one path."""
    return parse_mounts(json.dumps(list(entries)))


def test_parse_defaults_mode_to_rw() -> None:
    """A mount that names no mode reads rw — the request names the
    mode, and rw is the default it names."""
    (mount,) = mounts({"share": "projects", "guest": "/mnt/p"})
    assert mount.mode == MODE_RW
    assert parse_mounts(None) == ()
    assert parse_mounts("[]") == ()
    assert dump_mounts(()) is None


def test_parse_refusals() -> None:
    """Every malformed row shape is a named error at parse."""
    with pytest.raises(ValueError, match="unknown keys"):
        mounts({"share": "s", "guest": "/g", "extra": 1})
    with pytest.raises(ValueError, match="needs a share"):
        mounts({"guest": "/g"})
    with pytest.raises(ValueError, match="needs a guest"):
        mounts({"share": "s"})
    with pytest.raises(ValueError, match="mode must be one of"):
        mounts({"share": "s", "guest": "/g", "mode": "rw+"})
    with pytest.raises(ValueError, match="must be mappings"):
        parse_mounts(["not-a-mapping"])
    with pytest.raises(ValueError, match="must be a JSON array"):
        parse_mounts({"share": "s"})
    with pytest.raises(ValueError, match="must be a JSON array"):
        parse_mounts("not-json")


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
        validate_mounts(mounts({"share": "s", "guest": guest}))


def test_guest_path_takes_spaces() -> None:
    """A space prints (#623): the path keeps it, and the unit name
    escapes it — the mount point is the one the operator typed."""
    validate_mounts(mounts({"share": "s", "guest": "/mnt/my share"}))
    parsed = parse_mounts('[{"share": "s", "guest": "/mnt/my share"}]')
    assert parsed[0].guest == "/mnt/my share"


def test_row_disjointness_refusals() -> None:
    """A share appears once, a guest path is mounted once, and no
    mount nests under another — each refusal names the pair."""
    with pytest.raises(ValueError, match="mounted twice"):
        validate_mounts(
            mounts(
                {"share": "a", "guest": "/mnt/a"},
                {"share": "a", "guest": "/mnt/b"},
            )
        )
    with pytest.raises(ValueError, match="guest path '/mnt/a' is mounted"):
        validate_mounts(
            mounts(
                {"share": "a", "guest": "/mnt/a"},
                {"share": "b", "guest": "/mnt/a"},
            )
        )
    with pytest.raises(ValueError, match="shadows"):
        validate_mounts(
            mounts(
                {"share": "a", "guest": "/mnt/a"},
                {"share": "b", "guest": "/mnt/a/deep"},
            )
        )


def test_mode_bound_against_shares() -> None:
    """rw needs a share whose config says writable; ro is a valid
    self-restriction; a share the config never listed refuses."""
    shares = {
        "data": MountShare(name="data", path="/srv/data", writable=False)
    }
    with pytest.raises(ValueError, match="configured read-only"):
        validate_against_shares(
            mounts({"share": "data", "guest": "/mnt/data"}), shares
        )
    validate_against_shares(
        mounts({"share": "data", "guest": "/mnt/data", "mode": "ro"}),
        shares,
    )
    with pytest.raises(ValueError, match="no share named"):
        validate_against_shares(
            mounts({"share": "ghost", "guest": "/mnt/g"}), shares
        )


# --- the start-time derivation ----------------------------------------


def share_map(*shares: MountShare) -> dict[str, MountShare]:
    return {share.name: share for share in shares}


def test_missing_share_issue() -> None:
    """A share the reload deleted skips at start with the
    skipped reason; the healthy mount beside it attaches."""
    issues = mount_issues(
        mounts(
            {"share": "gone", "guest": "/mnt/g"},
            {"share": "live", "guest": "/mnt/l"},
        ),
        share_map(MountShare(name="live", path="/srv/live")),
        {},
    )
    assert [issue.kind for issue in issues] == ["skipped"]
    assert issues[0].event == "mount.skipped"
    assert issues[0].payload("ws1")["share"] == "gone"
    assert dump_mounts((issues[0].mount,)) is not None


def test_rw_conflict_on_exclusive_share() -> None:
    """A multi: false share held rw elsewhere conflicts; the same
    share mounted ro beside the holder never does, and a multi
    share never conflicts at all."""
    exclusive = MountShare(name="db", path="/srv/db", multi=False)
    wide = MountShare(name="scratch", path="/srv/scratch")
    issues = mount_issues(
        mounts({"share": "db", "guest": "/mnt/db"}),
        share_map(exclusive, wide),
        {"db": "holder-1"},
    )
    assert [issue.kind for issue in issues] == ["conflict"]
    assert "holder-1" in issues[0].detail
    assert issues[0].event == "mount.conflict"
    assert (
        mount_issues(
            mounts({"share": "db", "guest": "/mnt/db", "mode": "ro"}),
            share_map(exclusive),
            {"db": "holder-1"},
        )
        == ()
    )
    assert (
        mount_issues(
            mounts({"share": "scratch", "guest": "/mnt/s"}),
            share_map(wide),
            {"scratch": "holder-1"},
        )
        == ()
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
        [{"share": "projects", "guest": "/mnt/p", "mode": "rw"}],
    )
    row = await model.get_workspace("ws-m")
    assert row["mounts"] == [
        {"share": "projects", "guest": "/mnt/p", "mode": "rw"}
    ]
    await model.set_mounts("ws-m", None)
    row = await model.get_workspace("ws-m")
    assert row["mounts"] == []


async def test_start_publishes_skips_and_proceeds(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The resilient start (#620): a mount whose share left the
    config (or whose exclusive rw is held elsewhere) skips — the
    start still launches, and each skip publishes its event with
    the named reason."""
    http, app, stub = client
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
            {"share": "gone", "guest": "/mnt/gone", "mode": "rw"},
            {"share": "fine", "guest": "/mnt/fine", "mode": "ro"},
        ],
    )
    fine = tmp_path / "fine"
    fine.mkdir()
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="fine", path=str(fine)),)
    )
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
                "share": "gone",
                "guest": "/mnt/gone",
                "mode": "rw",
                "detail": "share 'gone' is not in the daemon's config",
            },
        )
    ]


async def test_start_publishes_conflict_and_proceeds(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The exclusive-share conflict (#620): another workspace's
    live VM holds the share rw, so this start skips its rw mount
    with a mount.conflict naming the holder — and still runs."""
    http, app, stub = client
    holder = await http.post(
        "/api/v1/workspaces",
        json={"id": "holder", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    challenger = await http.post(
        "/api/v1/workspaces",
        json={"id": "challenger", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    holder_id = holder.json()["id"]
    challenger_id = challenger.json()["id"]
    db = tmp_path / "db"
    db.mkdir()
    exclusive = MountShare(name="db", path=str(db), multi=False)
    app.state.settings.mounts = MountSettings(shares=(exclusive,))
    for wid in (holder_id, challenger_id):
        await app.state.model.set_mounts(
            wid, [{"share": "db", "guest": "/mnt/db", "mode": "rw"}]
        )
    first = await http.post(
        f"/api/v1/workspaces/{holder_id}/start", headers=auth()
    )
    assert first.status_code == 200
    published: list[tuple[str, dict]] = []
    real_publish = app.state.hub.publish

    async def spy_publish(event: str, data: dict) -> None:
        published.append((event, data))
        await real_publish(event, data)

    app.state.hub.publish = spy_publish
    second = await http.post(
        f"/api/v1/workspaces/{challenger_id}/start", headers=auth()
    )
    assert second.status_code == 200
    assert ("launch", challenger_id) in stub.calls
    assert len(published) == 1
    assert published[0][0] == "mount.conflict"
    assert published[0][1]["detail"] == (
        f"share 'db' is exclusive and already mounted rw by "
        f"workspace {holder_id}"
    )


def test_disjoint_mounts_validate_clean() -> None:
    """The happy path: no mounts, and two mounts on disjoint guest
    paths from two shares, pass the row rules untouched."""
    validate_mounts(())
    validate_mounts(
        mounts(
            {"share": "a", "guest": "/mnt/a"},
            {"share": "b", "guest": "/mnt/b", "mode": "ro"},
        )
    )


def test_non_string_share_layer_value_refuses() -> None:
    """A layer value that is neither list nor string — a config
    grammar no file form can spell — is the same named error as
    malformed JSON."""
    with pytest.raises(ValueError, match="JSON array"):
        mount_shares_from({"MSKSD_MOUNT_SHARES": 5})


def test_ro_only_holder_holds_nothing() -> None:
    """A live row whose mounts are all read-only holds no share:
    the conflict map stays empty, so an rw mount elsewhere never
    conflicts against it."""
    holders: dict[str, str] = {}
    record_rw_holders(
        holders,
        {
            "id": "ro-row",
            "status": "running",
            "mounts": [{"share": "db", "guest": "/mnt/db", "mode": "ro"}],
        },
    )
    assert holders == {}


@pytest.mark.parametrize("guest", ["//", "/./", "/."])
def test_root_equivalent_guest_paths_refuse(guest: str) -> None:
    """Every spelling of the filesystem root refuses: the guest
    path canonicalizes at parse, so `//` and `/./` are `/` by the
    time the rules see them, and a mount may not own the root."""
    with pytest.raises(ValueError, match="filesystem root"):
        validate_mounts(mounts({"share": "s", "guest": guest}))


def test_guest_path_canonicalizes_at_parse() -> None:
    """The stored form is the canonical one: collapsed separators
    and resolved `.` components, trailing slash dropped — `..`
    survives for the row rules to refuse, never silently
    collapsed."""
    (mount,) = mounts({"share": "s", "guest": "/mnt//./a/"})
    assert mount.guest == "/mnt/a"
    with pytest.raises(ValueError, match="must not contain"):
        validate_mounts(mounts({"share": "s", "guest": "/mnt/../etc"}))


def test_proc_named_directory_is_allowed() -> None:
    """The pseudo-filesystem refusal is component-wise, not a
    string prefix: `/mnt/proc` — a directory that merely carries
    the name — passes."""
    validate_mounts(mounts({"share": "s", "guest": "/mnt/proc"}))


def test_shadowing_pair_found_across_a_third_mount() -> None:
    """The shadow check sees every earlier mount, not just the
    adjacent one: the third mount shadows the first."""
    with pytest.raises(ValueError, match="shadows"):
        validate_mounts(
            mounts(
                {"share": "a", "guest": "/mnt/a"},
                {"share": "b", "guest": "/mnt/b"},
                {"share": "c", "guest": "/mnt/a/deep"},
            )
        )


def test_writable_narrowing_denies_at_start() -> None:
    """A reload that narrows a share to read-only under an rw
    mount skips it with the denied reason — honored, never a
    silent read-only downgrade."""
    narrowed = MountShare(name="data", path="/srv/data", writable=False)
    issues = mount_issues(
        mounts({"share": "data", "guest": "/mnt/data"}),
        share_map(narrowed),
        {},
    )
    assert [issue.kind for issue in issues] == ["denied"]
    assert issues[0].event == "mount.denied"
    assert "read-only" in issues[0].detail


def test_file_share_path_refuses(tmp_path: Path) -> None:
    """A share path that exists but is not a directory is a named
    error at load — a share serves a tree."""
    plain = tmp_path / "plain"
    plain.write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        mount_shares_from(
            {
                "MSKSD_MOUNT_SHARES": json.dumps(
                    [{"name": "f", "path": str(plain)}]
                )
            }
        )


def test_double_slash_anchor_canonicalizes() -> None:
    """Pathlib preserves exactly two leading slashes per POSIX —
    a distinct anchor that would dodge the disjoint rules. The
    canonical form pins the anchor to one slash, so `//tmp` and
    `/tmp` collide as one guest path."""
    (mount,) = mounts({"share": "s", "guest": "//tmp"})
    assert mount.guest == "/tmp"
    with pytest.raises(ValueError, match="is mounted twice"):
        validate_mounts(
            mounts(
                {"share": "a", "guest": "/tmp"},
                {"share": "b", "guest": "//tmp"},
            )
        )
    with pytest.raises(ValueError, match="shadows"):
        validate_mounts(
            mounts(
                {"share": "a", "guest": "/mnt"},
                {"share": "b", "guest": "//mnt/a/deep"},
            )
        )


async def test_start_publishes_denied_and_proceeds(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The reload narrowing at start (#620): an rw mount on a
    share the config flipped to read-only skips with mount.denied
    naming both remediations — and the start still runs."""
    http, app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-narrowed", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await app.state.model.set_mounts(
        wid, [{"share": "data", "guest": "/mnt/data", "mode": "rw"}]
    )
    data = tmp_path / "data"
    data.mkdir()
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="data", path=str(data), writable=False),)
    )
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
    assert ("launch", wid) in stub.calls
    assert published == [
        (
            "mount.denied",
            {
                "id": wid,
                "share": "data",
                "guest": "/mnt/data",
                "mode": "rw",
                "detail": (
                    "share 'data' is configured read-only; widen it in "
                    "the daemon's config, or recreate the workspace "
                    "with mode 'ro'"
                ),
            },
        )
    ]


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
    path, one ro under a deeper tree."""
    from msks.spec.mounts import MountSpec

    return (
        MountSpec(share="proj", guest="/mnt/proj"),
        MountSpec(share="docs", guest="/srv/tree/docs", mode="ro"),
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
    # the virtiofs facts.
    assert "What=proj" in block
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

    spaced = mounts_seed_block((MountSpec(share="a", guest="/mnt/my dir"),))
    assert "install -d -m 0755 '/mnt/my dir'" in spaced
    quoted = mounts_seed_block((MountSpec(share="a", guest="/mnt/o'brien"),))
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

    rw = MountSpec(share="proj", guest="/mnt/proj")
    ro = MountSpec(share="docs", guest="/srv/tree/docs", mode="ro")
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

    a = MountSpec(share="a", guest="/mnt/a")
    same_a = MountSpec(share="a", guest="/mnt/a")
    b = MountSpec(share="a", guest="/mnt/a", mode="ro")
    c = MountSpec(share="a", guest="/mnt/c")
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
    validate_mounts(mounts({"share": "s", "guest": at_edge}))
    # Escape inflation counts: a shorter path with spaces crosses
    # the same ceiling.
    spaced = "/" + "a " * 64
    assert len(spaced) < len(at_edge)
    with pytest.raises(ValueError, match="longer than 255"):
        validate_mounts(mounts({"share": "s", "guest": spaced}))


def mounts_for_edge(guest: str):
    from msks.spec.mounts import MountSpec

    return MountSpec(share="s", guest=guest)


def share_dir(tmp_path: Path) -> tuple[Path, MountSettings]:
    """A configured-share world: two shares on disk — a writable
    ``proj`` and a read-only ``refs`` — the pair a two-mount
    workspace names (a share appears once per workspace)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    refs = tmp_path / "refs"
    refs.mkdir()
    return proj, MountSettings(
        shares=(
            MountShare(name="proj", path=str(proj)),
            MountShare(name="refs", path=str(refs), writable=False),
        )
    )


async def test_create_with_mounts_stores_them(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The create's mounts (#620, part #624): validated against
    the config's shares and the row rules, stored on the row, and
    served with the derived state — a create naming a share the
    config carries lands both mounts."""
    http, app, _stub = client
    _root, settings = share_dir(tmp_path)
    app.state.settings.mounts = settings
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [
                {"share": "proj", "guest": "/mnt/proj"},
                {"share": "refs", "guest": "/srv/refs", "mode": "ro"},
            ],
        },
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["mounts"] == [
        {"share": "proj", "guest": "/mnt/proj", "mode": "rw"},
        {"share": "refs", "guest": "/srv/refs", "mode": "ro"},
    ]
    assert row["mount_issues"] == []


@pytest.mark.parametrize(
    ("mounts_body", "why"),
    [
        ([{"share": "nope", "guest": "/mnt/x"}], "no share named 'nope'"),
        ([{"share": "proj", "guest": "/mnt/x", "mode": "rw"}], "read-only"),
        ([{"share": "proj", "guest": "relative"}], "must be absolute"),
        (
            [
                {"share": "proj", "guest": "/mnt/a"},
                {"share": "proj", "guest": "/mnt/b"},
            ],
            "mounted twice",
        ),
    ],
)
async def test_create_refuses_mounts_the_config_or_rules_deny(
    client,  # noqa: F811
    tmp_path: Path,
    mounts_body: list,
    why: str,
) -> None:
    """The create's mount refusals (#620, part #624): a share the
    config does not carry, rw on a share narrowed to read-only,
    and the row rules — each a named 400 with nothing written."""
    http, app, _stub = client
    root = tmp_path / "proj"
    root.mkdir()
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="proj", path=str(root), writable=False),)
    )
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


async def test_mounts_are_refused_while_the_config_lists_no_shares(
    client,  # noqa: F811
) -> None:
    """Empty config is the feature off (#620): every mount request
    — create or set — refuses with the config's remedy named."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"share": "proj", "guest": "/mnt/proj"}],
        },
        headers=auth(),
    )
    assert created.status_code == 400
    assert "lists no shares" in created.json()["detail"]


async def test_the_mounts_route_sets_replaces_and_clears(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The mounts route (#620, part #624): the full replacement
    list lands on the row with its derived state, an empty list
    clears, and a running workspace's set succeeds — next-start
    semantics, like resize: the VM keeps its attached devices."""
    http, app, stub = client
    _root, settings = share_dir(tmp_path)
    app.state.settings.mounts = settings
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]

    set_one = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": [{"share": "proj", "guest": "/mnt/proj"}]},
        headers=auth(),
    )
    assert set_one.status_code == 200
    assert set_one.json()["mounts"] == [
        {"share": "proj", "guest": "/mnt/proj", "mode": "rw"}
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
                {"share": "proj", "guest": "/mnt/proj", "mode": "ro"},
                {"share": "refs", "guest": "/srv/refs", "mode": "ro"},
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


async def test_rows_carry_the_derived_mount_state(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The listing and the row read carry the derived per-mount
    state (#620, part #624): a share gone from the config names
    its skip on both surfaces — the markers the CLI's ls and the
    TUI render."""
    http, app, _stub = client
    _root, settings = share_dir(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    # The share exists at set time — the set-time gate passes —
    # and leaves the config before the read: the skip is derived
    # state, the reload's story.
    app.state.settings.mounts = MountSettings(
        shares=(
            *settings.shares,
            MountShare(name="gone", path=str(gone)),
        )
    )
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [
                {"share": "proj", "guest": "/mnt/proj"},
                {"share": "gone", "guest": "/mnt/gone"},
            ],
        },
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    app.state.settings.mounts = settings

    row = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    issues = row.json()["mount_issues"]
    assert issues == [
        {
            "kind": "skipped",
            "share": "gone",
            "guest": "/mnt/gone",
            "mode": "rw",
            "detail": "share 'gone' is not in the daemon's config",
        }
    ]

    listed = await http.get("/api/v1/workspaces", headers=auth())
    mine = next(r for r in listed.json() if r["id"] == wid)
    assert mine["mount_issues"] == issues


async def test_the_mounts_route_names_its_refusals(
    client,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The set route's 400s (#620, part #624): a share the config
    does not carry refuses with the config's remedy named, and
    the row keeps the mounts it had."""
    http, app, _stub = client
    _root, settings = share_dir(tmp_path)
    app.state.settings.mounts = settings
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-m", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    refused = await http.post(
        f"/api/v1/workspaces/{wid}/mounts",
        json={"mounts": [{"share": "nope", "guest": "/mnt/x"}]},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "no share named 'nope'" in refused.json()["detail"]
    kept = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert kept.json()["mounts"] == []


async def test_clearing_mounts_needs_no_shares_configured(
    client,  # noqa: F811
) -> None:
    """Clearing is always legal (#620, part #624): the operator
    who removed the last share can still empty a workspace's
    mounts — the empty list passes the gate the config's emptiness
    would otherwise refuse."""
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
    _root, settings = share_dir(tmp_path)
    app.state.settings.mounts = settings
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-m",
            "kernel": "/k",
            "rootfs": "/r",
            "mounts": [{"share": "proj", "guest": "/mnt/p", "modee": "ro"}],
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
    _root, settings = share_dir(tmp_path)
    app.state.settings.mounts = settings
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
        json={"mounts": [{"share": "proj", "guest": "/mnt/proj"}]},
        headers=auth(),
    )
    assert gone.status_code == 404

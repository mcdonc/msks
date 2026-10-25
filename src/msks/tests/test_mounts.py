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
from msks.server.api.rows import record_rw_holders
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
        ("/", "cannot be '/'"),
        ("/proc/self", "at or under /proc"),
        ("/sys", "at or under /sys"),
        ("/dev/sda", "at or under /dev"),
        ("/run/sock", "at or under /run"),
        ("/home", "home volume"),
        ("/home/dev/projects", "home volume"),
    ],
)
def test_guest_path_refusals(guest: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        validate_mounts(mounts({"share": "s", "guest": guest}))


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


async def test_start_publishes_skips_and_proceeds(client) -> None:  # noqa: F811
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
    app.state.settings.mounts = MountSettings(
        shares=(MountShare(name="fine", path="/srv/fine"),)
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


async def test_start_publishes_conflict_and_proceeds(client) -> None:  # noqa: F811
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
    exclusive = MountShare(name="db", path="/srv/db", multi=False)
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

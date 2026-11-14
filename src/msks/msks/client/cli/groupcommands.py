"""The sub-app commands: the egress deciders, the image
catalog, the home volume, and the secrets.

Each block registers on the sub-app the root carries
(`egress`, `image`, `home`, `secret`) beside the proxy
sub-app's own module.
"""

import asyncio

import typer

from ...client import egress as egress_mod
from ...conformance_args import (
    CHECK_ARCHIVE_HELP,
    CHECK_BOOT_TIMEOUT_HELP,
    CHECK_EGRESS_HELP,
    CHECK_KEEP_HELP,
    CHECK_SHUTDOWN_TIMEOUT_HELP,
    CHECK_UPLINK_HELP,
    CheckOptions,
)
from ..create import read_pubkey  # noqa: F401 — the tests' seam
from ..images import (  # noqa: F401
    cmd_image_check,
    cmd_image_default,
    cmd_image_import,
    cmd_image_info,
    cmd_image_ls,
    cmd_image_rename,
    cmd_image_rm,
    describe_image,
    designate_image,
    fetch_images,
    import_image,
    imported_cell,
    remove_image,
    unset_default_image,
)
from ..resize import display_name  # noqa: F401 — the tests' seam

# The secrets module's commands (#510) beside the resolver the
# unlock body shares.
from ..secrets import (  # noqa: F401
    cmd_secret_coverage,
    cmd_secret_ls,
    cmd_secret_mint,
    # The lifecycle verbs and the resolver.
    cmd_secret_renew,
    cmd_secret_revoke,
    cmd_secret_unlock,
    resolved_workspace_id,
)

# The volumes module's home and storage commands (#580) beside
# the storage renderers.
from ..volumes import (  # noqa: F401
    cmd_home_export,
    cmd_home_import,
    cmd_storage,
    # The listing's storage strip and its window arithmetic.
    file_windows,
    human_bytes,
    image_cost_table,
    narrowed,
    render_storage,
    run_home_export,
    run_home_import,
    volume_source,
    workspace_table,
)

# The workspaces module's bodies (#874): the commands this group
# wraps, the create-form surface, and the listing row builders.
from ..workspaces import (  # noqa: F401
    CreateFlags,
    cmd_clone,
    cmd_create,
    cmd_key,
    cmd_ls,
    # The lifecycle verbs.
    cmd_mount,
    cmd_resize,
    cmd_rm,
    cmd_start,
    cmd_stop,
    cmd_umount,
    # The create form's helpers and the row renderers.
    create_body,
    create_identity,
    created_date,
    created_line,
    image_column,
    notice_daemon_version_skew,
    read_user_data,
    run_create,
    run_resize,
    stale_image_notice,
    workspace_cells,
)
from .cliapp import (
    CoverageChoice,
    DecisionFilter,
    DurationChoice,
    PostureChoice,
    VerdictChoice,
    egress_app,
    home_app,
    image_app,
    one_line_interrupts,
    secret_app,
    set_json_mode,
)


@egress_app.command("rules")
@one_line_interrupts
def egress_rules(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    as_json: bool = typer.Option(
        False, "--json", help="print the rules document as JSON"
    ),
) -> int:
    """The in-effect verdicts for a workspace."""
    set_json_mode(as_json)
    return asyncio.run(
        egress_mod.run_rules(workspace_id, as_json, transport=ctx.obj)
    )


@egress_app.command("requests")
@one_line_interrupts
def egress_requests(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    decision: DecisionFilter | None = typer.Option(
        None,
        "--decision",
        metavar="DECISION",
        help="filter one lifecycle state (pending, allowed, denied, "
        "expired, or revoked)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the rows as one JSON document"
    ),
) -> int:
    """The consent rows (audit trail)."""
    set_json_mode(as_json)
    return asyncio.run(
        egress_mod.run_requests(
            workspace_id,
            None if decision is None else decision.value,
            as_json,
            transport=ctx.obj,
        )
    )


@egress_app.command("decide")
@one_line_interrupts
def egress_decide(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    request_id: str = typer.Argument(...),
    decision: VerdictChoice = typer.Argument(
        ..., help="the verdict: allow or deny"
    ),
    duration: DurationChoice = typer.Option(
        DurationChoice.tilrestart,
        "--duration",
        metavar="DURATION",
        help="how long enforcement honors the verdict (once, 5m, "
        "15m, tilrestart, or forever; default tilrestart)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the verdict document as JSON"
    ),
) -> int:
    """Give a verdict on a held request."""
    set_json_mode(as_json)
    return asyncio.run(
        egress_mod.run_decide(
            workspace_id,
            request_id,
            decision.value,
            duration.value,
            as_json,
            transport=ctx.obj,
        )
    )


@egress_app.command("revoke")
@one_line_interrupts
def egress_revoke(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    request_id: str = typer.Argument(...),
    as_json: bool = typer.Option(
        False, "--json", help="print the outcome as one JSON line"
    ),
) -> int:
    """Undo an in-effect verdict."""
    set_json_mode(as_json)
    return asyncio.run(
        egress_mod.run_revoke(
            workspace_id, request_id, as_json, transport=ctx.obj
        )
    )


@egress_app.command("mode")
@one_line_interrupts
def egress_mode_command(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    mode: PostureChoice = typer.Argument(
        ...,
        help="the posture to switch to (#280): allow, static, or interactive",
    ),
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        metavar="SPEC",
        help="replace the static allowlist with this entry "
        "(repeatable); omitted, the workspace keeps its list",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="confirm the switch to static even with nothing "
        "effectively allowed (every name NXDOMAINs — an offline "
        "workspace)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the reply as one JSON document"
    ),
) -> int:
    """Switch the egress posture (#280): live for a running
    workspace, at next start for a stopped one."""
    set_json_mode(as_json)
    return asyncio.run(
        egress_mod.run_mode(
            workspace_id,
            mode.value,
            allow,
            offline,
            as_json,
            transport=ctx.obj,
        )
    )


@egress_app.command("watch")
@one_line_interrupts
def egress_watch(
    workspace_id: str | None = typer.Argument(
        None,
        help="decide for this workspace (hold SYNs only while a "
        "decider is connected)",
    ),
    decide: bool = typer.Option(
        False,
        "--decide",
        help="prompt y/n for each pending request",
    ),
    duration: DurationChoice = typer.Option(
        DurationChoice.tilrestart,
        "--duration",
        metavar="DURATION",
        help="the duration a --decide allow applies (once, 5m, 15m, "
        "tilrestart, or forever; default tilrestart)",
    ),
) -> int:
    """Stream egress frames as lines; registers as a decider."""
    return asyncio.run(
        egress_mod.run_watch(workspace_id, decide, duration.value)
    )


@image_app.command("ls")
@one_line_interrupts
def image_ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List catalog images."""
    set_json_mode(as_json)
    return cmd_image_ls(as_json, transport=ctx.obj)


@image_app.command("import")
@one_line_interrupts
def image_import(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help="archive path as the daemon sees it (its own "
        "filesystem; the file is read by the daemon, not uploaded "
        "by this command) or an https:// URL the daemon downloads "
        "itself (#258)",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="register under this name instead of "
        "the archive's own (#340); the manifest pair stays recorded "
        "as the origin",
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        help="register under this version instead of the archive's own (#340)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the imported record as JSON"
    ),
) -> int:
    """Register an image archive from a daemon-side path or an
    https:// URL."""
    set_json_mode(as_json)
    return cmd_image_import(source, name, version, as_json, ctx.obj)


@image_app.command("rename")
@one_line_interrupts
def image_rename(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="name:version, bare name (newest), name@hash, or hash "
        "(a unique hash prefix works too)",
    ),
    name: str | None = typer.Option(
        None, "--name", help="the registered name moves to this"
    ),
    version: str | None = typer.Option(
        None, "--version", help="the registered version moves to this"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the renamed record as JSON"
    ),
) -> int:
    """Change a cataloged image's registered name/version (#340);
    the bytes, the hash, and the manifest origin stay."""
    set_json_mode(as_json)
    return cmd_image_rename(ref, name, version, as_json, ctx.obj)


@image_app.command("check")
@one_line_interrupts
def image_check(
    archive: str = typer.Argument(..., help=CHECK_ARCHIVE_HELP),
    egress: bool = typer.Option(False, "--egress", help=CHECK_EGRESS_HELP),
    uplink: str | None = typer.Option(
        None, "--uplink", help=CHECK_UPLINK_HELP
    ),
    boot_timeout_s: float = typer.Option(
        120.0, "--boot-timeout-s", help=CHECK_BOOT_TIMEOUT_HELP
    ),
    shutdown_timeout_s: float = typer.Option(
        120.0, "--shutdown-timeout-s", help=CHECK_SHUTDOWN_TIMEOUT_HELP
    ),
    keep: bool = typer.Option(False, "--keep", help=CHECK_KEEP_HELP),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="print the contract rows as one JSON document",
    ),
) -> int:
    """Boot an image and verify the guest contract (#258); local —
    needs /dev/kvm, --egress needs root."""
    set_json_mode(as_json)
    return cmd_image_check(
        CheckOptions(
            archive=archive,
            egress=egress,
            uplink=uplink,
            boot_timeout_s=boot_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
            keep=keep,
            as_json=as_json,
        )
    )


@image_app.command("rm")
@one_line_interrupts
def image_rm(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="name:version, bare name (newest), name@hash (full "
        "hash), or hash (a unique hash prefix works too)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the outcome as one JSON line"
    ),
) -> int:
    """Remove an image from the catalog."""
    set_json_mode(as_json)
    return cmd_image_rm(ref, as_json, ctx.obj)


@image_app.command("info")
@one_line_interrupts
def image_info(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="name:version, bare name, name@hash, or hash "
        "(a unique hash prefix works too)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the record as one JSON document"
    ),
) -> int:
    """Show one image's full record."""
    set_json_mode(as_json)
    return cmd_image_info(ref, as_json, ctx.obj)


@image_app.command("default")
@one_line_interrupts
def image_default(
    ctx: typer.Context,
    ref: str | None = typer.Argument(
        None,
        help="name:version, bare name (newest), name@hash, or hash "
        "(a unique hash prefix works too)",
    ),
    unset: bool = typer.Option(
        False,
        "--unset",
        help="clear the designation; a bare create falls back to the "
        "sole catalog entry, or needs --image when several remain",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the reply as one JSON document"
    ),
) -> int:
    """Designate the image a bare create boots (#270), or clear the
    designation with --unset."""
    set_json_mode(as_json)
    return cmd_image_default(ref, unset, as_json, ctx.obj)


@home_app.command("export")
@one_line_interrupts
def home_export(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose volume to download (name or id)"
    ),
    file: str | None = typer.Argument(
        None,
        help="output file (default: <workspace_id>.ext4); - writes stdout",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the outcome as one JSON document"
    ),
) -> int:
    """Download a workspace's /home volume."""
    set_json_mode(as_json)
    return cmd_home_export(workspace_id, file, as_json, transport=ctx.obj)


@home_app.command("import")
@one_line_interrupts
def home_import(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose volume to replace (name or id)"
    ),
    file: str = typer.Argument(
        ..., help="the ext4 volume image to upload; - reads stdin"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the outcome as one JSON document"
    ),
) -> int:
    """Replace a workspace's /home volume from an ext4 image."""
    set_json_mode(as_json)
    return cmd_home_import(workspace_id, file, as_json, transport=ctx.obj)


@secret_app.command("mint")
@one_line_interrupts
def secret_mint(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="the placeholder's label"),
    workspace: list[str] | None = typer.Option(
        None,
        "--workspace",
        metavar="REF",
        help=(
            "a workspace the placeholder binds to, by name or id; "
            "commas split and the flag repeats. Omitted, the mint "
            "covers every workspace on the daemon (#339)"
        ),
    ),
    dests: list[str] = typer.Option(
        ...,
        "--dest",
        metavar="HOST",
        help=(
            "an allowlist destination: an exact host "
            "(api.github.com) or a suffix (.github.com); repeatable"
        ),
    ),
    ttl: int | None = typer.Option(
        None,
        "--ttl",
        metavar="SECONDS",
        help="the placeholder's lifetime (default: unbounded)",
    ),
    secret_file: str = typer.Option(
        ...,
        "--secret-file",
        metavar="PATH",
        help=(
            "the file holding the real secret; - reads stdin "
            "(pipe it from a password manager)"
        ),
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the row (with sentinel) as JSON"
    ),
) -> int:
    """Mint a placeholder: the operator's value rides the request
    and is never echoed; the sentinel prints once. Daemon-wide by
    default, scoped with --workspace."""
    set_json_mode(as_json)
    return cmd_secret_mint(
        workspace, name, dests, ttl, secret_file, as_json, transport=ctx.obj
    )


@secret_app.command("ls")
@one_line_interrupts
def secret_ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List placeholders (sentinels are never listed)."""
    set_json_mode(as_json)
    return cmd_secret_ls(as_json, transport=ctx.obj)


@secret_app.command("revoke")
@one_line_interrupts
def secret_revoke(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="the placeholder's label"),
    workspace: list[str] | None = typer.Option(
        None,
        "--workspace",
        metavar="REF",
        help=(
            "the mint's workspace target (repeatable, commas "
            "split); omitted, the daemon-wide row of this label"
        ),
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the outcome as one JSON line"
    ),
) -> int:
    """Revoke a placeholder (effective next request)."""
    set_json_mode(as_json)
    return cmd_secret_revoke(workspace, name, as_json, transport=ctx.obj)


@secret_app.command("renew")
@one_line_interrupts
def secret_renew(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="the placeholder's label"),
    workspace: list[str] | None = typer.Option(
        None,
        "--workspace",
        metavar="REF",
        help=(
            "the mint's workspace target (repeatable, commas "
            "split); omitted, the daemon-wide row of this label"
        ),
    ),
    ttl: int = typer.Option(
        ...,
        "--ttl",
        metavar="SECONDS",
        help="the new lifetime from now",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the renewed row as JSON"
    ),
) -> int:
    """Extend a placeholder's lifetime in place."""
    set_json_mode(as_json)
    return cmd_secret_renew(workspace, name, ttl, as_json, transport=ctx.obj)


@secret_app.command("coverage")
@one_line_interrupts
def secret_coverage(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose posture flips"
    ),
    coverage: CoverageChoice = typer.Argument(
        ...,
        help="all (default posture) or scoped (daemon-wide "
        "placeholders exempt this workspace)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the reply as one JSON document"
    ),
) -> int:
    """Flip a workspace's daemon-wide placeholder posture (#339)."""
    set_json_mode(as_json)
    return cmd_secret_coverage(
        workspace_id, coverage.value, as_json, transport=ctx.obj
    )


@secret_app.command("unlock")
@one_line_interrupts
def secret_unlock(
    ctx: typer.Context,
    as_json: bool = typer.Option(
        False, "--json", help="print the outcome as one JSON document"
    ),
) -> int:
    """Unlock the store: decrypt the pending ciphertexts the
    configured identity key opens and upload the plaintext
    (rows the key cannot open stay ciphertext)."""
    set_json_mode(as_json)
    return cmd_secret_unlock(as_json, transport=ctx.obj)


#: The root options that take their value as the next token; the
#: card exemption skips that value on its scan for the first
#: positional (#842 review: ``--config broken.yaml agent-card``

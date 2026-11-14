"""The workspace commands: the root callback and the lifecycle
and reach verbs.

`ls`, `agent-card`, `tui`, `storage`, `create`, `start`, `stop`,
`clone`, `resize`, `mount`, `umount`, `rm`, `console`, `forward`,
`key`, `ssh`, `rsync` — thin typer wrappers over the
workspaces/ssh/rsync modules' own commands.
"""

import typer

from ..agentcard import AGENT_CARD_MD
from ..config import bootstrap
from ..console import run_workspace_shell
from ..create import read_pubkey  # noqa: F401 — the tests' seam
from ..forward import run_workspace_forward
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
from ..rsync import run_workspace_rsync
from ..secrets import (  # noqa: F401
    cmd_secret_coverage,
    cmd_secret_ls,
    cmd_secret_mint,
    cmd_secret_renew,
    cmd_secret_revoke,
    cmd_secret_unlock,
    resolved_workspace_id,
)
from ..ssh import run_workspace_ssh
from ..tui.main_app import run_main_tui
from ..volumes import (  # noqa: F401
    cmd_home_export,
    cmd_home_import,
    cmd_storage,
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
from ..workspaces import (  # noqa: F401
    CreateFlags,
    cmd_clone,
    cmd_create,
    cmd_key,
    cmd_ls,
    cmd_mount,
    cmd_resize,
    cmd_rm,
    cmd_start,
    cmd_stop,
    cmd_umount,
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
from . import cliapp
from .cliapp import (
    CoverageChoice,
    PostureChoice,
    app,
    one_line_interrupts,
    passthrough_args,
    set_json_mode,
)
from .invoke import card_requested, help_requested, skew_probe_wanted


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    daemon: str = typer.Option(
        None,
        "--daemon",
        help="the daemon to address: an alias from the config file, "
        "a raw URL, or an absolute socket path (#314, #555)",
    ),
    config: str = typer.Option(
        None,
        "--config",
        help="the client config file to read, or 'none' for "
        "environment only (#314)",
    ),
) -> int:
    """A bare ``msks`` is the workspace tree TUI (#309)."""
    # The config bootstrap runs before every command body (#314):
    # the file's and the flag's winners are in the environment by
    # the time any reader looks — except on a help screen, where
    # the operator is reading, not connecting, and a broken config
    # file must not hide the help; and except on agent-card, whose
    # reference text is read the same way (#842 review).
    if not help_requested(cliapp.invocation_tokens) and not card_requested(
        cliapp.invocation_tokens
    ):
        cliapp.invoked_conf = bootstrap(daemon, config)
        # The version-skew notice (#706): one best-effort /health
        # roundtrip per invocation, on the conditions
        # :func:`skew_probe_wanted` names (an in-process transport
        # is the tests' seam, the image group has an offline
        # member, and --json keeps stderr one object, #841).
        if skew_probe_wanted(cliapp.invocation_tokens, ctx):
            notice_daemon_version_skew()
    if ctx.invoked_subcommand is None:
        # The decorator's edge, same as every command: a Ctrl-C in
        # the tree is one line, not typer's silent 130.
        return one_line_interrupts(run_main_tui)(conf=cliapp.invoked_conf)
    return 0


@app.command("ls")
@one_line_interrupts
def ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List workspaces on the daemon."""
    set_json_mode(as_json)
    return cmd_ls(as_json, transport=ctx.obj)


@app.command("agent-card")
@one_line_interrupts
def agent_card() -> int:
    """Print the agent reference card (#842): the same markdown as
    the docs' agent chapter, for a harness to capture in one call."""
    print(AGENT_CARD_MD, end="")
    return 0


@app.command("tui")
@one_line_interrupts
def tui(
    workspace: str | None = typer.Argument(
        None,
        help="open this workspace's page (name or id) instead of the list",
    ),
) -> int:
    """The full-screen workspace tree (#309): the workspaces list,
    each workspace's page, and the consent decider."""
    return run_main_tui(workspace, conf=cliapp.invoked_conf)


@app.command("storage")
@one_line_interrupts
def storage(
    ctx: typer.Context,
    workspace: str | None = typer.Argument(
        None,
        help="narrow the workspace table to one workspace (name or id)",
    ),
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """Report the state-disk budget and per-workspace cost (#184)."""
    set_json_mode(as_json)
    return cmd_storage(workspace, as_json, transport=ctx.obj)


@app.command("create")
@one_line_interrupts
def create(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ...,
        help="the workspace's name (#246): the label you address it "
        "by (DNS-label charset); the daemon mints the immutable id",
    ),
    image: str | None = typer.Option(
        None, "--image", help="catalog ref: name:version, name, or hash"
    ),
    kernel: str | None = typer.Option(
        None, "--kernel", help="explicit kernel path (skips the catalog)"
    ),
    initrd: str | None = typer.Option(
        None, "--initrd", help="explicit initrd path"
    ),
    rootfs: str | None = typer.Option(
        None, "--rootfs", help="explicit rootfs path (skips the catalog)"
    ),
    cmdline: str | None = typer.Option(
        None, "--cmdline", help="explicit kernel cmdline"
    ),
    cpus: int | None = typer.Option(
        None, "--cpus", help="vcpu count (default 4)"
    ),
    mem_mib: int | None = typer.Option(
        None, "--mem-mib", help="guest memory, MiB (default 8192)"
    ),
    root_mib: int | None = typer.Option(
        None, "--root-mib", help="persistent root size, MiB"
    ),
    home_mib: int | None = typer.Option(
        None, "--home-mib", help="persistent home size, MiB"
    ),
    egress: bool | None = typer.Option(
        None,
        "--egress/--no-egress",
        help="boot with a virtio-net NIC onto a per-VM host tap "
        "(#52; the default is yes — use --no-egress to boot NIC-less)",
    ),
    egress_mode: PostureChoice | None = typer.Option(
        None,
        "--egress-mode",
        metavar="MODE",
        help="the consent posture (#69): allow (the default — new "
        "flows pass, off-list names are recorded), static (the "
        "allowlist only; off-list names never resolve), interactive "
        "(each new flow's first packet holds until a decider allows "
        "or denies it)",
    ),
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        metavar="SPEC",
        help="a static allowlist entry (#69), repeatable: host, "
        "host:port, .host (subdomains included), *.host (subdomains "
        "only), or cidr[:port]. Names gate at the daemon's resolver; "
        "address specs accept in the per-VM chain",
    ),
    secret_coverage: CoverageChoice | None = typer.Option(
        None,
        "--secret-coverage",
        metavar="POSTURE",
        help="the daemon-wide placeholder posture (#339): all "
        "(the default — one sentinel minted for the whole daemon "
        "arms this workspace and swaps on its tap) or scoped "
        "(only placeholders minted directly at it arm it)",
    ),
    user_data: str | None = typer.Option(
        None,
        "--user-data",
        metavar="FILE",
        help="first-boot provisioning payload (a shell script or "
        "cloud-config) delivered on the workspace's cidata seed disk "
        "(#41); - reads stdin. Create-time only",
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        metavar="NAME",
        help="the workspace's login user (#248): seeded into the guest "
        "at first boot (the account, its home, authorized_keys, and "
        "the workspace-user sudo grant) and used as the default login "
        "for msks ssh and rsync — the console session itself is the "
        "guest's root autologin getty (#481) (default: your username)",
    ),
    pubkey: str | None = typer.Option(
        None,
        "--pubkey",
        metavar="FILE",
        help="use a public key you already own as this workspace's ssh "
        "identity (#132), one workspace's worth: the file's one line "
        "travels to the daemon, any well-formed key type, and the "
        "private half stays wherever you keep it (nothing is written "
        "client-side). - reads stdin. Without this flag the create "
        "plants the operator's configured key — the public half "
        "pubkey_file / MSKSC_PUBKEY_FILE names (#495), or the "
        "private key identity_file / MSKSC_IDENTITY_FILE names "
        "(#336) — msks generates no key, and an unconfigured client "
        "refuses the create (#486)",
    ),
    share: list[str] | None = typer.Option(
        None,
        "--share",
        metavar="SPEC",
        help="a host directory mounted into the guest (#667), "
        "repeatable: HOST_PATH:GUEST_PATH:MODE — an absolute host "
        "path (~ expands against your home; the daemon's allowed "
        "roots must cover it), the absolute guest path, and ro or "
        "rw (rw when the mode is omitted). Applies at the next "
        "start",
    ),
    start: bool = typer.Option(
        False, "--start", help="boot the workspace immediately"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the new row as one JSON document"
    ),
) -> int:
    """Create a workspace."""
    set_json_mode(as_json)
    return run_create(
        CreateFlags(
            workspace_id=workspace_id,
            image=image,
            kernel=kernel,
            initrd=initrd,
            rootfs=rootfs,
            cmdline=cmdline,
            cpus=cpus,
            mem_mib=mem_mib,
            root_mib=root_mib,
            home_mib=home_mib,
            egress=(None if egress is None else egress),
            egress_mode=(None if egress_mode is None else egress_mode.value),
            secret_coverage=(
                None if secret_coverage is None else secret_coverage.value
            ),
            allow=allow,
            user_data=user_data,
            user=user,
            pubkey=pubkey,
            share=share,
            start=start,
        ),
        ctx.obj,
        as_json=as_json,
    )


@app.command("start")
@one_line_interrupts
def start(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to boot (name or id)"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the settled row as one JSON document"
    ),
) -> int:
    """Boot a created workspace."""
    set_json_mode(as_json)
    return cmd_start(workspace_id, as_json, transport=ctx.obj)


@app.command("stop")
@one_line_interrupts
def stop(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to stop (name or id)"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the settled row as one JSON document"
    ),
) -> int:
    """Power a workspace off."""
    set_json_mode(as_json)
    return cmd_stop(workspace_id, as_json, transport=ctx.obj)


@app.command("clone")
@one_line_interrupts
def clone(
    ctx: typer.Context,
    source: str = typer.Argument(
        ..., help="the workspace to copy (name or id); it must be stopped"
    ),
    name: str = typer.Argument(
        ...,
        help="the new workspace's name: the label you address the "
        "clone by (DNS-label charset); the daemon mints its own id",
    ),
    copy_allowlist: bool = typer.Option(
        False,
        "--copy-allowlist",
        help="carry the source's egress allowlist into the clone "
        "(#773). Without it the clone starts with an empty list "
        "under the copied mode: an interactive clone earns each "
        "allow anew, a static clone answers every name NXDOMAIN "
        "until its list is set",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the new row as one JSON document"
    ),
) -> int:
    """Copy a workspace into a new one (#773).

    A clone is a full fork of a stopped workspace: both disks copy
    (the root overlay and the /home volume), and the create-time
    config rides the new row (image, sizes, egress mode, mounts,
    login user, the user_data payload — the clone's own factory
    reset re-provisions from it). The new workspace mints its own
    id and starts fresh: the allowlist, in-effect egress verdicts,
    and workspace-scoped secret placeholders stay with the source
    (--copy-allowlist opts into the first). Everything the guest
    generated itself (ssh host keys above all) duplicates — rotate
    what matters before trusting the clone's identity.
    """
    set_json_mode(as_json)
    return cmd_clone(source, name, copy_allowlist, as_json, ctx.obj)


@app.command("resize")
@one_line_interrupts
def resize(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to resize (name or id)"
    ),
    home_mib: int | None = typer.Option(
        None,
        "--home-mib",
        help="new /home volume size, MiB (grows or shrinks)",
    ),
    root_mib: int | None = typer.Option(
        None, "--root-mib", help="new root overlay size, MiB (grows only)"
    ),
    cpus: int | None = typer.Option(
        None, "--cpus", help="new vcpu count (applies at the next boot)"
    ),
    mem_mib: int | None = typer.Option(
        None,
        "--mem-mib",
        help="new guest memory, MiB (applies at the next boot)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the resized row as one JSON document"
    ),
) -> int:
    """Change a stopped workspace's disk sizes and topology (#184,
    #277)."""
    set_json_mode(as_json)
    return run_resize(
        workspace_id, home_mib, root_mib, cpus, mem_mib, ctx.obj, as_json
    )


@app.command("mount")
@one_line_interrupts
def mount(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to mount into (name or id)"
    ),
    spec: str = typer.Argument(
        ...,
        help="HOST_PATH:GUEST_PATH:MODE — an absolute host path "
        "(~ expands; the allowed roots must cover it, #667), the "
        "absolute path it lands at in the guest, and ro or rw "
        "(rw when omitted)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the refreshed row as one JSON document"
    ),
) -> int:
    """Mount a host directory into a workspace (#667).

    The spec names a host directory the daemon's allowed roots
    must cover; the gates bound the attach at set and start.
    Next-start semantics: a running workspace keeps the devices
    it attached, and the next start attaches the new set.
    """
    set_json_mode(as_json)
    return cmd_mount(workspace_id, spec, as_json, ctx.obj)


@app.command("umount")
@one_line_interrupts
def umount(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to unmount from (name or id)"
    ),
    share: str = typer.Argument(
        ...,
        help="the host path (~ expands) whose mounts leave the workspace",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the refreshed row as one JSON document"
    ),
) -> int:
    """Remove a workspace's mounts of one host path (#667).

    The next start attaches the remainder; a running workspace
    keeps its attached devices until then.
    """
    set_json_mode(as_json)
    return cmd_umount(workspace_id, share, as_json, ctx.obj)


@app.command("rm")
@one_line_interrupts
def rm(
    ctx: typer.Context,
    workspace_ids: list[str] = typer.Argument(
        ..., help="the workspaces to delete, in order (name or id)"
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="print one JSON line per removed workspace",
    ),
) -> int:
    """Delete workspaces and their data."""
    set_json_mode(as_json)
    return cmd_rm(workspace_ids, as_json, transport=ctx.obj)


@app.command("console")
@one_line_interrupts
def console(
    workspace_id: str = typer.Argument(
        ..., help="the workspace to attach to (name or id)"
    ),
) -> int:
    """Interactive root shell in a workspace."""
    return run_workspace_shell(workspace_id)


@app.command("forward")
@one_line_interrupts
def forward(
    workspace_id: str = typer.Argument(
        ..., help="the workspace to reach (name or id)"
    ),
    port: int = typer.Argument(..., help="the guest TCP port to reach"),
    local: int | None = typer.Option(
        None,
        "--local",
        metavar="PORT",
        help="bind 127.0.0.1:PORT instead of stdio; every accepted "
        "connection gets its own forward",
    ),
) -> int:
    """Bridge a workspace TCP port to stdio or a local port."""
    return run_workspace_forward(workspace_id, port, local)


@app.command("key")
@one_line_interrupts
def key(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose identity to fetch (name or id)"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="print the identity document as JSON"
    ),
) -> int:
    """Fetch a workspace's ssh identity (#111): the public half.
    The private half is the operator's own key — it never crosses
    the API (#486, #497)."""
    set_json_mode(as_json)
    return cmd_key(workspace_id, as_json, transport=ctx.obj)


@app.command(
    "ssh",
    context_settings={
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)
@one_line_interrupts
def ssh(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to log into (name or id)"
    ),
    passthrough: list[str] | None = typer.Argument(
        None,
        metavar="ARGS",
        help="arguments passed to ssh verbatim ('-l root' is the "
        "recovery login; '-A' forwards your agent, $SSH_AUTH_SOCK)",
    ),
) -> int:
    """Ssh into a workspace over the forward, identity staged in
    memory."""
    return run_workspace_ssh(
        workspace_id,
        passthrough_args(passthrough or []),
        transport=ctx.obj,
    )


@app.command(
    "rsync",
    context_settings={
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)
@one_line_interrupts
def rsync(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to copy against (name or id)"
    ),
    passthrough: list[str] | None = typer.Argument(
        None,
        metavar="ARGS",
        help="arguments passed to rsync verbatim; an empty-host path "
        "(:/remote/path, user@:/remote/path) targets this workspace",
    ),
) -> int:
    """Rsync files to and from a workspace over the forward,
    identity staged in memory."""
    return run_workspace_rsync(
        workspace_id,
        passthrough_args(passthrough or []),
        transport=ctx.obj,
    )

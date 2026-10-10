"""The ``msks`` CLI's typer layer (#315): the parse, the flags,
and the dispatch into the command groups.

The command bodies live in topical modules — workspace lifecycle
(:mod:`msks.client.workspaces`), the disks (:mod:`msks.client.volumes`),
the image catalog (:mod:`msks.client.images`), placeholder secrets
(:mod:`msks.client.secrets`), egress consent (:mod:`msks.client.egress`),
and forwarding/ssh (:mod:`msks.client.forward`, :mod:`msks.client.ssh`,
:mod:`msks.client.rsync`, :mod:`msks.client.console`) — each speaking
the daemon's REST surface through :mod:`msks.client.context`; this
module is the composition shell over them. Every command keeps the
same client conventions (#21): ``MSKSC_URL`` for the daemon,
``MSKSC_TOKEN`` for a bearer token, ``MSKSC_CAFILE`` to pin the
certificate. The interactive console command lives in
:mod:`msks.client.console`; the workspace tree TUI in
:mod:`msks.client.tui.main_app`.
"""

import asyncio
import enum
import functools
import sys

import typer

try:
    # Typer vendors its own click (the 0.16+ line); its exceptions
    # are the ones a parse of this app raises. Older typer rides
    # the installed click instead — the pyproject floor spans both.
    from typer._click.exceptions import UsageError
except ImportError:  # pragma: no cover — the floor spans both eras
    from click.exceptions import UsageError

from ..conformance_args import (
    CHECK_ARCHIVE_HELP,
    CHECK_BOOT_TIMEOUT_HELP,
    CHECK_EGRESS_HELP,
    CHECK_KEEP_HELP,
    CHECK_SHUTDOWN_TIMEOUT_HELP,
    CHECK_UPLINK_HELP,
    CheckOptions,
)
from . import egress as egress_mod
from .config import ClientConfig, bootstrap
from .console import run_workspace_shell

# Re-exported for the tests (they drive cli.write_client_identity
# directly); the create core's own callers live in the groups now.
from .create import write_client_identity  # noqa: F401
from .forward import run_workspace_forward
from .images import (  # noqa: F401
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
from .resize import display_name  # noqa: F401 — the tests' seam
from .rsync import run_workspace_rsync
from .secrets import (  # noqa: F401
    cmd_secret_check,
    cmd_secret_coverage,
    cmd_secret_ls,
    cmd_secret_mint,
    cmd_secret_renew,
    cmd_secret_revoke,
    resolved_workspace_id,
)
from .ssh import run_workspace_ssh
from .tui.main_app import run_main_tui
from .volumes import (  # noqa: F401
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
from .workspaces import (  # noqa: F401
    KEY_TYPE_HELP,
    CreateFlags,
    checked_key_flags,
    checked_key_type,
    cmd_create,
    cmd_key,
    cmd_ls,
    cmd_resize,
    cmd_rm,
    cmd_start,
    cmd_stop,
    create_body,
    create_identity,
    created_date,
    created_line,
    read_pubkey,
    read_user_data,
    run_create,
    run_resize,
    stale_image_notice,
    workspace_cells,
)


class PostureChoice(enum.StrEnum):
    """The egress consent postures (#69)."""

    allow = "allow"
    static = "static"
    interactive = "interactive"


class CoverageChoice(enum.StrEnum):
    """The daemon-wide placeholder postures (#339)."""

    all = "all"
    scoped = "scoped"


class DecisionFilter(enum.StrEnum):
    """The consent row lifecycle states."""

    pending = "pending"
    allowed = "allowed"
    denied = "denied"
    expired = "expired"
    revoked = "revoked"


class VerdictChoice(enum.StrEnum):
    """The decider's two verdicts."""

    allow = "allow"
    deny = "deny"


class DurationChoice(enum.StrEnum):
    """How long enforcement honors a verdict."""

    once = "once"
    five_m = "5m"
    fifteen_m = "15m"
    tilrestart = "tilrestart"
    forever = "forever"


def passthrough_args(argv: list[str]) -> list[str]:
    """The ssh/rsync variadic's verbatim value: click hands the
    ``--`` separator through inside the list where argparse
    swallowed it, so one leading separator drops here — everything
    else, options included, reaches ssh/rsync exactly as typed."""
    return argv[1:] if argv[:1] == ["--"] else argv


#: Whether the current parse reached a command body: the help
# screens and the usage errors never do, and main()'s help gate
# reads the difference (a token spelled like a help flag that a
# command consumed as its value must not masquerade as one).
body_reached = False

#: The current invocation's tokens, recorded by run_parsed for the
#: root callback's help-screen check (see set_invocation_tokens).
invocation_tokens: list[str] = []

#: The invocation's resolved client config (#314), set by the root
#: callback right after the bootstrap and read by the TUI entry
#: points: the tree's new-terminal shell action (#341) spawns its
#: console child with the launcher the resolution carries. None
#: when no bootstrap ran (a help screen — and then no TUI starts
#: either).
invoked_conf: ClientConfig | None = None


def one_line_interrupts(fn):
    """A Ctrl-C during a long boot is one line, not a traceback (a
    raw-mode session never gets here — Ctrl-C reaches the guest):
    caught at the command body's edge, where typer's own
    conversion (a bare exit 130) cannot swallow the line. Marks
    the body-reached flag on the way in."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global body_reached
        body_reached = True
        try:
            return fn(*args, **kwargs)
        except KeyboardInterrupt:
            print("msks: interrupted", file=sys.stderr)
            raise SystemExit(130) from None

    return wrapped


app = typer.Typer(
    name="msks",
    add_completion=False,
    help="msks client: workspace microvms over the daemon API",
    context_settings={"help_option_names": ["-h", "--help"]},
)

egress_app = typer.Typer(
    help="egress consent: decide, watch, and inspect (#69)"
)
image_app = typer.Typer(help="manage the daemon's image catalog (#65)")
home_app = typer.Typer(
    help="move a workspace's /home volume through the daemon (#80)"
)
secret_app = typer.Typer(
    help="placeholder secrets: mint, list, revoke, renew, check"
)
app.add_typer(egress_app, name="egress")
app.add_typer(image_app, name="image")
app.add_typer(home_app, name="home")
app.add_typer(secret_app, name="secret")


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    daemon: str = typer.Option(
        None,
        "--daemon",
        help="the daemon to address: an alias from the config file, "
        "or a raw URL (#314)",
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
    # file must not hide the help.
    global invoked_conf
    if not help_requested(invocation_tokens):
        invoked_conf = bootstrap(daemon, config)
    if ctx.invoked_subcommand is None:
        # The decorator's edge, same as every command: a Ctrl-C in
        # the tree is one line, not typer's silent 130.
        return one_line_interrupts(run_main_tui)(conf=invoked_conf)
    return 0


@app.command("ls")
@one_line_interrupts
def ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List workspaces on the daemon."""
    return cmd_ls(as_json, transport=ctx.obj)


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
    return run_main_tui(workspace, conf=invoked_conf)


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
        None, "--cpus", help="vcpu count (default 2)"
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
    daemon_mint: bool = typer.Option(
        False,
        "--daemon-mint",
        help="let the daemon mint the workspace's ssh identity and "
        "escrow both halves (#111) — an explicit opt-out; the create "
        "default (#336) plants one operator key across workspaces "
        "(identity_file, or the key msks mints under the client data "
        "root — `~/.local/share/msks/identity`, or that root under "
        "MSKSC_DATA_DIR) and the daemon holds public halves only",
    ),
    pubkey: str | None = typer.Option(
        None,
        "--pubkey",
        metavar="FILE",
        help="use a public key you already own as the workspace's ssh "
        "identity (#132), one workspace's worth: the file's one line "
        "travels to the daemon, any well-formed key type, and the "
        "private half stays wherever you keep it (nothing is written "
        "client-side). - reads stdin",
    ),
    key_type: str | None = typer.Option(
        None,
        "--key-type",
        metavar="TYPE",
        help=KEY_TYPE_HELP,
    ),
    start: bool = typer.Option(
        False, "--start", help="boot the workspace immediately"
    ),
) -> int:
    """Create a workspace."""
    checked_key_type(key_type)
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
            daemon_mint=daemon_mint,
            pubkey=pubkey,
            key_type=key_type,
            start=start,
        ),
        ctx.obj,
    )


@app.command("start")
@one_line_interrupts
def start(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to boot (name or id)"
    ),
) -> int:
    """Boot a created workspace."""
    return cmd_start(workspace_id, transport=ctx.obj)


@app.command("stop")
@one_line_interrupts
def stop(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to stop (name or id)"
    ),
) -> int:
    """Power a workspace off."""
    return cmd_stop(workspace_id, transport=ctx.obj)


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
) -> int:
    """Change a stopped workspace's disk sizes and topology (#184,
    #277)."""
    return run_resize(workspace_id, home_mib, root_mib, cpus, mem_mib, ctx.obj)


@app.command("rm")
@one_line_interrupts
def rm(
    ctx: typer.Context,
    workspace_ids: list[str] = typer.Argument(
        ..., help="the workspaces to delete, in order (name or id)"
    ),
) -> int:
    """Delete workspaces and their data."""
    return cmd_rm(workspace_ids, transport=ctx.obj)


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
    as_private: bool = typer.Option(
        False,
        "--private",
        help="print the private half instead of the public line",
    ),
    out: str | None = typer.Option(
        None,
        "--out",
        metavar="FILE",
        help="write the private half to FILE (mode 0600) instead of printing",
    ),
) -> int:
    """Fetch a workspace's ssh identity (#111; the public half alone
    for a client-minted #121 workspace)."""
    checked_key_flags(as_private, out)
    return cmd_key(workspace_id, as_private, out, transport=ctx.obj)


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


@egress_app.command("rules")
@one_line_interrupts
def egress_rules(
    ctx: typer.Context, workspace_id: str = typer.Argument(...)
) -> int:
    """The in-effect verdicts for a workspace."""
    return asyncio.run(egress_mod.run_rules(workspace_id, transport=ctx.obj))


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
) -> int:
    """The consent rows (audit trail)."""
    return asyncio.run(
        egress_mod.run_requests(
            workspace_id,
            None if decision is None else decision.value,
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
) -> int:
    """Give a verdict on a held request."""
    return asyncio.run(
        egress_mod.run_decide(
            workspace_id,
            request_id,
            decision.value,
            duration.value,
            transport=ctx.obj,
        )
    )


@egress_app.command("revoke")
@one_line_interrupts
def egress_revoke(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    request_id: str = typer.Argument(...),
) -> int:
    """Undo an in-effect verdict."""
    return asyncio.run(
        egress_mod.run_revoke(workspace_id, request_id, transport=ctx.obj)
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
) -> int:
    """Switch the egress posture (#280): live for a running
    workspace, at next start for a stopped one."""
    return asyncio.run(
        egress_mod.run_mode(
            workspace_id,
            mode.value,
            allow,
            offline,
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
) -> int:
    """Register an image archive from a daemon-side path or an
    https:// URL."""
    return cmd_image_import(source, name, version, transport=ctx.obj)


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
) -> int:
    """Change a cataloged image's registered name/version (#340);
    the bytes, the hash, and the manifest origin stay."""
    return cmd_image_rename(ref, name, version, transport=ctx.obj)


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
) -> int:
    """Boot an image and verify the guest contract (#258); local —
    needs /dev/kvm, --egress needs root."""
    return cmd_image_check(
        CheckOptions(
            archive=archive,
            egress=egress,
            uplink=uplink,
            boot_timeout_s=boot_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
            keep=keep,
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
) -> int:
    """Remove an image from the catalog."""
    return cmd_image_rm(ref, transport=ctx.obj)


@image_app.command("info")
@one_line_interrupts
def image_info(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="name:version, bare name, name@hash, or hash "
        "(a unique hash prefix works too)",
    ),
) -> int:
    """Show one image's full record."""
    return cmd_image_info(ref, transport=ctx.obj)


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
) -> int:
    """Designate the image a bare create boots (#270), or clear the
    designation with --unset."""
    return cmd_image_default(ref, unset, transport=ctx.obj)


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
) -> int:
    """Download a workspace's /home volume."""
    return cmd_home_export(workspace_id, file, transport=ctx.obj)


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
) -> int:
    """Replace a workspace's /home volume from an ext4 image."""
    return cmd_home_import(workspace_id, file, transport=ctx.obj)


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
) -> int:
    """Mint a placeholder: the operator's value rides the request
    and is never echoed; the sentinel prints once. Daemon-wide by
    default, scoped with --workspace."""
    return cmd_secret_mint(
        workspace, name, dests, ttl, secret_file, transport=ctx.obj
    )


@secret_app.command("ls")
@one_line_interrupts
def secret_ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List placeholders (sentinels are never listed)."""
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
) -> int:
    """Revoke a placeholder (effective next request)."""
    return cmd_secret_revoke(workspace, name, transport=ctx.obj)


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
) -> int:
    """Extend a placeholder's lifetime in place."""
    return cmd_secret_renew(workspace, name, ttl, transport=ctx.obj)


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
) -> int:
    """Flip a workspace's daemon-wide placeholder posture (#339)."""
    return cmd_secret_coverage(workspace_id, coverage.value, transport=ctx.obj)


@secret_app.command("check")
@one_line_interrupts
def secret_check(ctx: typer.Context) -> int:
    """Verify the configured secret store answers writes."""
    return cmd_secret_check(transport=ctx.obj)


def help_requested(argv: list[str]) -> bool:
    """Whether the invocation's parse reaches a help flag — the
    ``--help`` screen exits through SystemExit(0), the shape the
    argparse era pinned; a ``--`` separator hides everything after
    it from the flag scan."""
    for token in argv:
        if token == "--":
            return False
        if token in ("-h", "--help"):
            return True
    return False


def set_invocation_tokens(argv: list[str] | None) -> None:
    """Record the invocation's tokens for the root callback.

    The callback runs before the subcommand parses, so it cannot
    see whether the parse ends at a help screen; the recorded
    tokens let it keep the config bootstrap off the help screens —
    a broken config file must not hide ``msks <cmd> --help``, the
    operator's most discoverable debugging tool.
    """
    global invocation_tokens
    invocation_tokens = sys.argv[1:] if argv is None else argv


def run_parsed(argv: list[str] | None, transport) -> int:
    """One non-standalone pass through the typer app: the command's
    return value is the exit code (typer raises it as an Exit and
    click's non-standalone main hands it back)."""
    # typer.main.get_command is the layer's own bridge (the public
    # Typer.__call__ is standalone-only); the floor rides on it
    # staying the shape every typer release exercises through
    # Typer.__call__ itself.
    global body_reached
    body_reached = False  # per invocation, never across them
    set_invocation_tokens(argv)
    command = typer.main.get_command(app)
    try:
        return command.main(
            argv,
            prog_name="msks",
            obj=transport,
            standalone_mode=False,
        )
    except UsageError as exc:
        # A bad invocation is one line and exit 2 — the argparse-era
        # convention, kept (docs/cli.md documents it).
        print(f"msks: {exc.format_message()}", file=sys.stderr)
        return 2


def help_exit(code: int, tokens: list[str]) -> bool:
    """Whether this run ends as the help screen's exit: a zero
    code, no command body reached (a help-shaped token a command
    swallowed as its value ran one — that is not a help exit), and
    a help flag the scan reaches."""
    return code == 0 and not body_reached and help_requested(tokens)


def main(argv: list[str] | None = None, transport=None) -> int:
    """The ``msks`` entry point: parse with the typer app, run the
    command, return its exit code."""
    tokens = sys.argv[1:] if argv is None else argv
    code = run_parsed(argv, transport)
    if help_exit(code, tokens):
        # SystemExit(0), the shape the argparse era pinned.
        raise SystemExit(0)
    return code or 0


if __name__ == "__main__":
    raise SystemExit(main())

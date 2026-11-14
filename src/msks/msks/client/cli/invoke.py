"""The invocation: argv parsing and ``main``.

The pre-typer argv reads (root positionals, the card and help
and json requests, the skew probe), the parsed run with its
structured-error shapes, and the console entry point.
"""

import sys

import typer

try:
    # Typer vendors its own click (the 0.16+ line); its exceptions
    # are the ones a parse of this app raises. Older typer rides
    # the installed click instead — the pyproject floor spans both.
    from typer._click.exceptions import UsageError
except ImportError:  # pragma: no cover — the floor spans both eras
    from click.exceptions import UsageError

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
from ..jsonio import (
    ApiExit,
    api_failure,
    json_mode,
    plain_failure,
    set_json_mode,
)
from ..resize import display_name  # noqa: F401 — the tests' seam
from ..secrets import (  # noqa: F401
    cmd_secret_coverage,
    cmd_secret_ls,
    cmd_secret_mint,
    cmd_secret_renew,
    cmd_secret_revoke,
    cmd_secret_unlock,
    resolved_workspace_id,
)
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

# The workspaces module's bodies this entry dispatches (#874):
# the command bodies, the create-form surface, and the rows.
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
    app,
)

#: The options whose value the agent-card shortcut reads (the
#: card prints when the argv holds no command; these two options
#: carry values a command-less parse must not swallow).
CARD_VALUE_OPTIONS = frozenset({"--daemon", "--config"})


def root_positionals(argv: list[str]) -> list[str]:
    """The invocation's positional tokens under the root options:
    a ``--`` barrier ends the scan, ``--daemon``/``--config``
    consume the next token, an attached value rides inside its own
    token (``--config=path``), and every other dash token is a
    flag. The root callback's subcommand is the first of these."""
    out: list[str] = []
    tokens = iter(argv)
    for token in tokens:
        if token == "--":
            break
        if token in CARD_VALUE_OPTIONS:
            next(tokens, None)  # the option's value
        elif not token.startswith("-"):
            out.append(token)
    return out


def card_requested(argv: list[str]) -> bool:
    """Whether the invocation prints the agent reference card
    (#842): the first positional token. The card is reference text
    a harness reads offline — a broken client config must not hide
    it, the same exemption the help screens carry."""
    return root_positionals(argv)[:1] == ["agent-card"]


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


def skew_probe_wanted(tokens: list[str], ctx: typer.Context) -> bool:
    """Whether the root callback's version-skew probe runs (#706,
    #841): a real dial (no in-process transport), a non-image
    command, and a human invocation — under ``--json`` the
    notice would land ahead of the structured error document a
    failing run prints."""
    return (
        ctx.obj is None
        and ctx.invoked_subcommand != "image"
        and not json_requested(tokens)
    )


def json_requested(argv: list[str]) -> bool:
    """Whether the invocation carries a ``--json`` flag before any
    ``--`` separator (#841): the root callback's version-skew probe
    runs before a command body parses, and its stderr notice would
    land ahead of the structured error document a failing --json
    run prints — a harness reading stderr as one JSON object would
    break on it. The scan mirrors :func:`help_requested`'s shape;
    a token a passthrough command later swallows only ever
    suppresses the notice."""
    for token in argv:
        if token == "--":
            return False
        if token == "--json":
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
    cliapp.invocation_tokens = sys.argv[1:] if argv is None else argv


def run_parsed(argv: list[str] | None, transport) -> int:
    """One non-standalone pass through the typer app: the command's
    return value is the exit code (typer raises it as an Exit and
    click's non-standalone main hands it back)."""
    # typer.main.get_command is the layer's own bridge (the public
    # Typer.__call__ is standalone-only); the floor rides on it
    # staying the shape every typer release exercises through
    # Typer.__call__ itself.
    cliapp.body_reached = False  # per invocation, never across them
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
    return code == 0 and not cliapp.body_reached and help_requested(tokens)


def structured_error(exc: SystemExit) -> int | None:
    """The JSON-mode exit for a command failure (#841), or None
    when the invocation is human-mode and the one-line exit raises
    through: an API-status failure carries its status and detail,
    every other exit names itself."""
    if not json_mode():
        return None
    if isinstance(exc, ApiExit):
        return api_failure(exc.api_status, exc.api_detail)
    return plain_failure(exc)


def json_or_reraise(exc: SystemExit) -> int:
    """The JSON-mode exit for a command failure (#841); a
    human-mode invocation re-raises its one-line exit."""
    mapped = structured_error(exc)
    if mapped is None:
        raise exc
    return mapped


def main(argv: list[str] | None = None, transport=None) -> int:
    """The ``msks`` entry point: parse with the typer app, run the
    command, return its exit code. A failure in ``--json`` mode is
    the structured error document on stderr with the table's exit
    code (#841); the human modes keep their one-line exits."""
    tokens = sys.argv[1:] if argv is None else argv
    set_json_mode(False)  # per invocation, never across them
    try:
        code = run_parsed(argv, transport)
    except SystemExit as exc:
        return json_or_reraise(exc)
    if help_exit(code, tokens):
        # SystemExit(0), the shape the argparse era pinned.
        raise SystemExit(0)
    return code or 0

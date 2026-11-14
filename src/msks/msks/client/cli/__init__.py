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

from ...client import (
    egress as egress_mod,  # noqa: F401 — the suites patch cli.egress_mod
)
from ..agentcard import AGENT_CARD_MD as AGENT_CARD_MD
from ..config import (
    ClientConfig as ClientConfig,
)
from ..config import (
    bootstrap as bootstrap,
)
from ..console import run_workspace_shell as run_workspace_shell  # noqa: F401
from ..create import read_pubkey  # noqa: F401 — the tests' seam
from ..forward import (
    run_workspace_forward as run_workspace_forward,  # noqa: F401
)
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
    ApiExit as ApiExit,  # noqa: F401
)
from ..jsonio import (
    api_failure as api_failure,  # noqa: F401
)
from ..jsonio import (
    json_mode as json_mode,  # noqa: F401
)
from ..jsonio import (
    plain_failure as plain_failure,  # noqa: F401
)
from ..jsonio import (
    set_json_mode as set_json_mode,  # noqa: F401
)
from ..proxies import (
    app as proxy_app,  # noqa: F401 — the sub-apps register on it
)
from ..resize import display_name  # noqa: F401 — the tests' seam
from ..rsync import run_workspace_rsync as run_workspace_rsync  # noqa: F401
from ..secrets import (  # noqa: F401
    cmd_secret_coverage,
    cmd_secret_ls,
    cmd_secret_mint,
    cmd_secret_renew,
    cmd_secret_revoke,
    cmd_secret_unlock,
    resolved_workspace_id,
)
from ..ssh import run_workspace_ssh as run_workspace_ssh  # noqa: F401
from ..tui.main_app import run_main_tui as run_main_tui  # noqa: F401

# The volumes module's surfaces (#874): the home-volume and
# storage commands beside the row renderers the tests read.
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
    # The home-volume bodies behind the two commands.
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

# The split (#874): the typer apps live in cliapp, the command
# groups register from wscommands and groupcommands, and main
# lives in invoke. Importing them here registers every command
# on the apps before anything resolves `app`.
from . import groupcommands, wscommands  # noqa: F401,E402
from .cliapp import (  # noqa: E402
    DecisionFilter as DecisionFilter,  # noqa: F401
)
from .cliapp import (
    DurationChoice as DurationChoice,  # noqa: F401
)
from .cliapp import (
    PostureChoice as PostureChoice,  # noqa: F401
)
from .cliapp import (
    VerdictChoice as VerdictChoice,  # noqa: F401
)
from .cliapp import (
    app as app,
)
from .cliapp import (
    egress_app as egress_app,
)
from .cliapp import (
    home_app as home_app,
)
from .cliapp import (
    image_app as image_app,
)
from .cliapp import (
    one_line_interrupts as one_line_interrupts,  # noqa: F401
)
from .cliapp import (
    passthrough_args as passthrough_args,  # noqa: F401
)
from .cliapp import (
    secret_app as secret_app,
)
from .invoke import (
    card_requested as card_requested,  # noqa: F401
)
from .invoke import (
    help_exit as help_exit,  # noqa: F401
)
from .invoke import (
    help_requested as help_requested,  # noqa: F401
)
from .invoke import (
    json_or_reraise as json_or_reraise,  # noqa: F401
)
from .invoke import (
    json_requested as json_requested,  # noqa: F401
)
from .invoke import (
    main as main,  # noqa: F401
)
from .invoke import (
    root_positionals as root_positionals,  # noqa: F401
)
from .invoke import (
    run_parsed as run_parsed,  # noqa: F401
)
from .invoke import (
    set_invocation_tokens as set_invocation_tokens,  # noqa: F401
)
from .invoke import (
    skew_probe_wanted as skew_probe_wanted,  # noqa: F401
)
from .invoke import (
    structured_error as structured_error,  # noqa: F401
)
from .wscommands import agent_card as agent_card  # noqa: F401,E402

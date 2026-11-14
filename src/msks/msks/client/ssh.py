"""``msks ssh <workspace-id> [-- ssh args]``: stock ssh into a workspace.

One-off sugar over the pieces that already exist: the workspace is
booted when the daemon reports it as not running (the same pre-flight
as ``msks console``), the workspace identity — the public half
alone (#486, #497: the daemon holds no private half) — is fetched
over the authenticated API, and
``ssh`` runs with the forward
websocket (#109) as its ProxyCommand. When the operator's own
agent — the one ``SSH_AUTH_SOCK`` names — holds the workspace's
key (:func:`agent_held`, #490, matched on key material so the
file the key came from and its name do not matter), the session
authenticates through that agent and the private half never
enters the msks process, encrypted or not. Otherwise the session
stages the private half in a transient in-process ssh-agent
(:mod:`msks.client.agent`) and ssh authenticates through the agent
socket (``-o IdentityAgent=...``) — ssh closes inherited descriptors
at startup, so the socket is the one channel that survives to
authentication — writing no new copy anywhere: the staged half
stays in memory for the session and the file it came from is left
exactly where it is.

The session logs in as the workspace's recorded login user
(#248 — the name ``msks create --user`` planted, the creating
operator's own name by default); ``-l root`` in the passthrough
args is the recovery login. Agent
forwarding asked for on the command line — ``msks ssh <ws> -- -A``
— forwards the operator's agent, the one ``SSH_AUTH_SOCK`` names
(:func:`forward_agent_args` rewrites the request onto that socket
explicitly, because ssh would otherwise forward the transient
session agent ``IdentityAgent`` points at); the workspace identity
stays an authentication credential and reaches the guest as
nothing else. An explicit ``-o ForwardAgent=<path>`` in the
passthrough keeps its own socket, and forwarding set by an ssh
config file keeps the stock meaning (the transient agent). Everything after the
workspace id (or after ``--``) is passed to ssh verbatim; ssh's own
``--`` inside it separates options from a remote command
(``msks ssh <ws> -- -A -- uname -a``).

The config file's ``ssh_options`` key (#385, materialized as
``MSKSC_SSH_OPTIONS`` by the CLI's config bootstrap) remembers a
passthrough for every session :func:`build_args` shapes — the same
tokens, riding behind the command line's own so an explicit
setting still wins (:func:`with_config_options`), with agent
forwarding's precedence decided by the command line alone when it
names any forwarding setting at all. `msks rsync` rides the same
list on its remote shell, so one key covers every ssh invocation
msks runs.

A session whose pre-flight booted the workspace first waits out the
guest's first-boot identity seed (#168): the daemon reports
``running`` while the guest's sshd is up but cloud-init has yet to
write ``authorized_keys``, so a bare dial in that window is refused
with ``Permission denied (publickey)``. A probe login (``true`` as
its remote command) retries behind the boot until the guest accepts
the workspace key, and the real session — interactive or one-shot —
then runs exactly once.

An interactive session self-registers while it runs (#858):
:func:`msks.client.sshsessions.record_session` holds one heartbeat
record under the per-user state directory, so the workspaces list
counts the host's open shells, and the token the record mints
completes the spawned window's title. A passthrough remote command
and ``msks rsync`` register nothing — they are connections, not
shells.
"""

import asyncio
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext

from . import agent
from .env import env_token, env_url, is_unix, ssl_context
from .forwarding import (
    config_quote as config_quote,
)
from .forwarding import (
    effective_forwarding,
    forward_agent_args,
    forward_agent_value,
    option_value,
    without_forwarding,
)

# The identity leaf (#874): re-exported here — the suites and the
# command modules keep the imports they always had.
from .identity import (
    ENCRYPTED_CREATE_RECOVERY as ENCRYPTED_CREATE_RECOVERY,
)
from .identity import (
    ENCRYPTED_SESSION_RECOVERY as ENCRYPTED_SESSION_RECOVERY,
)
from .identity import (
    agent_held as agent_held,
)
from .identity import (
    cache_dir as cache_dir,
)
from .identity import (
    cache_key as cache_key,
)
from .identity import (
    configured_file as configured_file,
)
from .identity import (
    configured_identity_file as configured_identity_file,
)
from .identity import (
    configured_pubkey_file as configured_pubkey_file,
)
from .identity import (
    derived_public as derived_public,
)
from .identity import (
    instance_token as instance_token,
)
from .identity import (
    known_hosts_path as known_hosts_path,
)
from .identity import (
    named_identity_file as named_identity_file,
)
from .identity import (
    operator_identity as operator_identity,
)
from .identity import (
    operator_match as operator_match,
)
from .identity import (
    repin_notice as repin_notice,
)
from .identity import (
    resolve_private as resolve_private,
)
from .identity import (
    rotation_cause as rotation_cause,
)
from .identity import (
    rotation_token as rotation_token,
)
from .identity import (
    session_pin as session_pin,
)
from .identity import (
    unheld_identity_line as unheld_identity_line,
)
from .rest import (
    ensure_running,
    fetch_ssh_key,
)
from .sshsessions import record_session
from .wintitle import title_spawned_window

#: The remembered-passthrough setting's variable (#385): the
#: config file's ``ssh_options`` value, shell-joined by the config
#: bootstrap — the same string form the operator would export
#: directly.
SSH_OPTIONS_ENV = "MSKSC_SSH_OPTIONS"

#: The guest port sshd listens on (#110).
SSH_PORT = 22


#: How long a just-booted workspace's first ssh attempt keeps
#: retrying (#168): the daemon reports ``running`` when the VM
#: process is up, but the guest's sshd answers before cloud-init's
#: identity seed has written ``authorized_keys`` — an ssh dial in
#: that window is refused with ``Permission denied (publickey)``
#: and a bare retry succeeds. The wait applies only when this
#: invocation booted the workspace (a seeded guest authenticates
#: from the first attempt), and it probes with a throwaway ``true``
#: command so a remote command in the passthrough runs exactly
#: once either way.
SSH_SEED_WAIT_S = 30.0

#: The pause between probe attempts inside :data:`SSH_SEED_WAIT_S`.
SSH_RETRY_PAUSE_S = 1.0


async def prepare(
    workspace_id: str,
    url: str,
    token: str,
    ssl_ctx=None,
    transport=None,
) -> tuple[dict, bool]:
    """Boot the workspace if needed, then fetch its identity.

    The second half of the pair is whether this call observed a
    boot (:func:`msks.client.rest.ensure_running`) — a just-booted
    guest may still be running its first-boot identity seed, which
    the run loop waits out before the real session.
    """
    booted = await ensure_running(
        workspace_id, url, token, ssl_ctx=ssl_ctx, transport=transport
    )
    key = await fetch_ssh_key(
        url, token, workspace_id, transport=transport, ssl_ctx=ssl_ctx
    )
    return key, booted


def passthrough_args(args: list[str]) -> list[str]:
    """The ssh arguments to hand through, as a plain list.

    The CLI's parse already dropped one ``--`` separator
    (:func:`msks.client.cli.passthrough_args` — the module shares
    the name and the job), so an ssh-style ``--`` a second time
    reaches ssh untouched: it is the options/command split
    :func:`split_command` reads.
    """
    return list(args)


def split_command(passthrough: list[str]) -> tuple[list[str], list[str]]:
    """(ssh options, remote command) from the passthrough args.

    Two unambiguous shapes: a passthrough that starts with a plain
    word is all command (``msks ssh ws -- uname -a`` — the natural
    form), and ssh's own ``--`` separates options from the command
    (``msks ssh ws -- -A -- uname -a``) when options come first.
    """
    if passthrough and not passthrough[0].startswith("-"):
        return [], passthrough
    if "--" in passthrough:
        split = passthrough.index("--")
        return passthrough[:split], passthrough[split + 1 :]
    return passthrough, []


def wants_user(args: list[str]) -> bool:
    """Whether the passthrough args name a login user themselves.

    ``-l root``, its attached spelling ``-lroot``, and
    ``-o User=root`` — the value separate or inline — all count;
    when ssh is told its user, the default is not injected twice.
    A dangling ``-l`` counts too: ssh's own error is the clear one.
    """
    for index, arg in enumerate(args):
        if names_login(arg):
            return True
        value = option_value(index, arg, args)
        if value is not None and names_user(value):
            return True
    return False


def names_login(arg: str) -> bool:
    """Whether an argument is ssh's ``-l`` — the flag beside its
    value, the attached ``-lroot`` spelling, or dangling. Uppercase
    ``-L`` (a local forward) is a different option."""
    return arg == "-l" or (arg.startswith("-l") and len(arg) > 2)


def names_user(value: str) -> bool:
    """Whether an ssh ``-o`` value sets the login user. ssh_config
    keywords are case-insensitive (``user=`` is ``User=``), so the
    comparison is too."""
    lowered = value.lower()
    return lowered.startswith("user=") or lowered.startswith("user ")


def configured_ssh_options() -> list[str]:
    """The remembered ssh passthrough (#385): the tokens the
    config file's ``ssh_options`` key (or its variable) carries —
    the same tokens an operator would type after ``--`` on the
    ``msks ssh`` command line. The variable holds the string form,
    shell-split here; unset or blank means no options ride.
    """
    value = os.environ.get(SSH_OPTIONS_ENV, "")
    if not value.strip():
        return []
    try:
        words = shlex.split(value)
    except ValueError as exc:
        raise SystemExit(
            f"msks: {SSH_OPTIONS_ENV} is not shell-parseable "
            f"({exc}); quote its tokens the way the shell would "
            "take them"
        ) from None
    if "--" in words:
        raise SystemExit(
            f"msks: {SSH_OPTIONS_ENV} carries the ssh options, not "
            "the -- separator — export the options themselves"
        )
    return words


def with_config_options(options: list[str]) -> list[str]:
    """The command line's options with the config's remembered
    options riding behind them (#385) — the remembered passthrough
    is a default, so it lands after the command line's own the way
    msks's transport options do, and stock ssh's first-obtained
    rule makes an explicit command-line value win over a
    remembered one.

    Agent forwarding is the one setting this order cannot decide:
    flags assign unconditionally, so a remembered ``-A`` behind the
    operator's own ``-a`` would reassign it. When the command line
    names any forwarding setting at all — flag or value — the
    config's forwarding settings are dropped wholesale
    (:func:`without_forwarding`) and the command line decides
    alone; when it names none, the remembered options ride as
    written and :func:`forward_agent_args` resolves the merged
    line with its stock-precedence rules.
    """
    configured = configured_ssh_options()
    if not configured:
        return list(options)
    if effective_forwarding(options) is not None:
        configured = without_forwarding(configured)
    return [*options, *configured]


def forward_agent_option(options: list[str]) -> list[str]:
    """ssh argv tokens for the first ForwardAgent setting among the
    options — the one setting from the passthrough the first-boot
    probe carries, so it dials with the session's forwarding (and
    resolves the operator's agent, or names its absence) exactly as
    the session will."""
    for index, arg in enumerate(options):
        value = option_value(index, arg, options)
        if value is not None and forward_agent_value(value) is not None:
            if arg == "-o":
                return [arg, options[index + 1]]
            return [arg]
    return []


def proxy_command(workspace_id: str) -> str:
    """The ProxyCommand value: THIS client, not whatever ``msks`` the
    ssh child's PATH happens to carry — an absolute interpreter with
    the module form works from any invocation (console script,
    installed venv, ``python -m``). The command runs under a shell,
    so the workspace id takes shell quoting."""
    runner = f"{config_quote(sys.executable)} -m msks.client.cli"
    return (
        f"ProxyCommand={runner} forward {shlex.quote(workspace_id)} {SSH_PORT}"
    )


def session_options(
    workspace_id: str,
    agent_socket: str,
    identity_pub: str,
    known_hosts: str,
) -> list[str]:
    """The transport, host-key, and agent options every msks ssh
    invocation carries — the session and the first-boot probe
    alike: the forward as ProxyCommand, the per-workspace
    known_hosts under ``accept-new``, and the identity named by its
    public half and served from the transient agent under
    ``IdentitiesOnly``."""
    return [
        "-o",
        proxy_command(workspace_id),
        "-o",
        f"UserKnownHostsFile={config_quote(known_hosts)}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"IdentityAgent={config_quote(agent_socket)}",
        "-i",
        identity_pub,
    ]


def workspace_user(key: dict) -> str:
    """The workspace's login user from the identity fetch (#248) —
    the user ``msks ssh`` and ``msks rsync`` log in as when the
    passthrough names none.

    The daemon serves the recorded create-time user with the
    identity (a workspace created by one operator and ssh'd into by
    another answers the same for both). A workspace with no
    recorded user — created before per-workspace users (#248), or
    by an API create that names none — answers ``null``, and root
    is the dial: the account every seed provisions a key for, the
    same failsafe the console logs in as (#481, #500).
    """
    return key.get("user") or "root"


def build_args(
    workspace_id: str,
    agent_socket: str,
    identity_pub: str,
    known_hosts: str,
    passthrough: list[str],
    user: str,
) -> list[str]:
    """ssh's argv: the passthrough's options FIRST, then the
    transport, host-key, and agent options as defaults — ssh takes
    the first obtained value for a repeated option, so an explicit
    passthrough override (``-o UserKnownHostsFile=/dev/null``, a
    different ProxyCommand) wins exactly as it would with stock
    ssh, and the workspace id sits between the options and the
    remote command where ssh parses them.

    ``-i identity.pub`` names the identity (public material only);
    ``IdentityAgent`` points ssh at the transient agent that holds
    the private half, and under ``IdentitiesOnly`` ssh offers that
    one key and nothing else. ``user`` is the workspace's login
    user (:func:`workspace_user`) — injected as ``-l`` only when
    neither the passthrough nor the config's remembered options
    name a user.
    """
    options, command = split_command(passthrough)
    options = forward_agent_args(with_config_options(options))
    argv = [
        "ssh",
        *options,
        *session_options(
            workspace_id, agent_socket, identity_pub, known_hosts
        ),
    ]
    if not wants_user(options):
        argv += ["-l", user]
    return argv + [workspace_id] + command


def identity_comment(key: dict) -> str:
    """The comment on the identity listing, from the public line."""
    fields = key["public_key"].split()
    return fields[2] if len(fields) > 2 else ""


def probe_args(
    workspace_id: str,
    agent_socket: str,
    identity_pub: str,
    known_hosts: str,
    passthrough: list[str],
    user: str,
    command: str = "msks ssh",
) -> list[str] | None:
    """The readiness probe's argv: msks's own transport and agent
    settings, the session's login user and agent-forwarding setting,
    and a throwaway ``true`` as the remote command — the passthrough
    contributes nothing else.

    The probe asks one question — does the guest accept the
    identity yet, as the user the session will log in as — so it
    carries exactly the settings that shape that answer. The
    session's own luggage stays with the session: ``-N``/
    ``SessionType=none`` make ssh ignore a command and hold the
    connection open, ``-W`` and the ``-L``/``-R``/``-D`` forwards
    stretch a probe into a tunnel (bundled short flags like
    ``-fN`` reach the same states spelling-free), and
    ``RemoteCommand`` makes ssh refuse a command-line command
    outright — none of them can stall or distort the probe when
    none of them is in it. Agent forwarding is the one carried
    setting: the probe dials as the session will, so it asks sshd
    to open the agent channel for the probe just as for the session
    (:func:`forward_agent_option` picks that one setting out of the
    rewritten passthrough). ``true`` runs no user command, so a
    retried probe cannot run anything twice, and ``-q`` keeps
    ssh's own per-attempt chatter quiet (the notice and the
    forward's refusal line are what a retry prints).

    None when no probe can represent the session: a user named in
    a shape ssh refuses outright (a dangling ``-l``) answers
    nothing — the session fails with its own immediate usage
    error, so it runs at once.
    """
    options, _ = split_command(passthrough)
    options = with_config_options(options)
    user_fragments = probe_user(options)
    if wants_user(options) and not user_fragments:
        return None
    # After the refusal check: a session ssh rejects outright names
    # no agent to resolve — its own usage error is the answer.
    options = forward_agent_args(options, command)
    forwarded = forward_agent_option(options)
    argv = [
        "ssh",
        "-q",
        *user_fragments,
        *forwarded,
        *session_options(
            workspace_id, agent_socket, identity_pub, known_hosts
        ),
    ]
    if not wants_user(options):
        argv += ["-l", user]
    return argv + [workspace_id, "true"]


def probe_user(options: list[str]) -> list[str]:
    """The passthrough fragments that name the login user: ``-l``
    with its value, and ``-o User=...`` in both spellings.

    A guest can admit some users and refuse others (an operator's
    ``AllowUsers``, a hardened ``PermitRootLogin``), so the probe
    asks as the user the session will log in as — the one session
    setting that changes the answer to its question.
    """
    fragments: list[str] = []
    for index, arg in enumerate(options):
        fragments += user_pair(index, arg, options)
    return fragments


def user_pair(index: int, arg: str, options: list[str]) -> list[str]:
    """The passthrough fragments at ``index`` that name the login
    user: ``-l`` in either spelling, or an ``-o User=...`` in
    either spelling — an empty pair when the argument names no
    user."""
    if names_login(arg):
        return login_pair(index, arg, options)
    value = option_value(index, arg, options)
    if value is not None and names_user(value):
        return inline_user_pair(arg, value)
    return []


def login_pair(index: int, arg: str, options: list[str]) -> list[str]:
    """The ``-l`` fragments: the attached ``-lroot`` spelling is
    one argument, the value split beside the flag is two."""
    if len(arg) > 2:
        return [arg]
    if next_arg_names_user(options, index):
        return [arg, options[index + 1]]
    return []


def inline_user_pair(arg: str, value: str) -> list[str]:
    """The ``-o User=...`` fragments: the split spelling is two
    arguments, the inline ``-oValue`` spelling is one."""
    return [arg, value] if arg == "-o" else [arg]


def next_arg_names_user(options: list[str], index: int) -> bool:
    """Whether the argument after a ``-l`` can be its value — a
    plain word names the user; an option flag means the ``-l`` was
    dangling, and ssh's own usage error for the session is the
    clearer report."""
    return index + 1 < len(options) and not options[index + 1].startswith("-")


def wait_for_identity(
    workspace_id: str,
    probe_argv: list[str],
    deadline: float,
    command: str = "msks ssh",
) -> None:
    """Retry the probe until the guest accepts the login or the
    deadline passes (#168).

    ``command`` names the calling command for its missing-ssh line
    (``msks rsync`` probes through here too, #190).

    Each attempt is bounded by the time the deadline leaves, so a
    stalled connection is one more not-ready answer, not a hang,
    and the whole wait stays inside the deadline plus one pause. A
    deadline that passes leaves the failure to the real session:
    ssh's own message names the refusal, and the probe's notices
    have already said what was being waited for.

    ssh's own stderr rides along (#245): a *permanent* refusal —
    the host-key mismatch of a recreated workspace, a permission
    denial — printed nothing here before, and the retry notice
    read as "the guest is slow" while the session could never
    succeed. Each distinct stderr the probe captures is printed
    once (:func:`report_probe_stderr`); an unchanged answer
    repeats silently, so a slow first boot stays as quiet as it
    ever was.
    """
    last_stderr: str | None = None
    while True:
        now = time.monotonic()
        if now >= deadline:
            return
        failure = probe_attempt(probe_argv, deadline - now, command)
        if failure is None:
            return
        last_stderr = report_probe_stderr(failure, last_stderr, command)
        time.sleep(SSH_RETRY_PAUSE_S)
        if time.monotonic() < deadline:
            print(
                f"msks: {workspace_id} is not accepting the login yet; "
                "retrying",
                file=sys.stderr,
            )


def report_probe_stderr(
    failure: str, last_stderr: str | None, command: str
) -> str:
    """Print a probe failure's ssh stderr when it differs from the
    last one, and return it as the new last-seen answer (#245).

    Deduped on purpose: a slow first boot fails the same quiet way
    for many attempts, and only a *changed* answer carries news.
    """
    if failure and failure != last_stderr:
        for line in failure.splitlines():
            print(f"msks: {command} probe: {line}", file=sys.stderr)
    return failure


def probe_attempt(
    probe_argv: list[str], budget: float, command: str = "msks ssh"
) -> str | None:
    """One probe attempt: None when the guest accepted the login,
    else the probe's captured stderr (possibly empty).

    A stalled connection is one more not-ready answer — bounded by
    the budget the deadline leaves the attempt — and an ssh missing
    from PATH is the session's own named error, raised here so the
    wait reports it before the session ever runs.
    """
    try:
        completed = subprocess.run(
            probe_argv, timeout=budget, capture_output=True, text=True
        )
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        raise SystemExit(f"{command}: ssh not found on PATH") from None
    if completed.returncode == 0:
        return None
    return (completed.stderr or "").strip()


@contextmanager
def session_identity(key: dict, workspace_id: str):
    """The agent the session authenticates through (#490, #497):
    the operator's own when it holds the workspace's key
    (:func:`agent_held` — adopted, not staged, so the private
    material never enters the msks process, and an encrypted
    identity_file stops mattering), else the transient in-process
    agent (:func:`msks.client.agent.serve`) staging the operator
    identity file whose public half matches the row's line
    (:func:`resolve_private` — the daemon serves no private half
    to stage). The holder's two
    attributes — the agent socket and the public half's path —
    are the same pair in both branches, so every session path
    (``msks ssh``'s argv, ``msks rsync``'s per-session config,
    the first-boot probe) treats the two alike."""
    served = key["public_key"].split()[:2]
    held = agent_held(served)
    if held is not None:
        with agent.adopt(held, key["public_key"]) as adopted:
            yield adopted
        return
    private = agent.load_private(resolve_private(key, workspace_id))
    with agent.serve(private, identity_comment(key)) as staged:
        yield staged


@contextmanager
def staged_session(workspace_id: str, transport=None):
    """The pre-flight every ssh-transport command runs: the client
    environment, the boot-if-needed probe wait's inputs, and the
    session's identity — served from the operator's own agent
    when it holds the key (#490) or staged in the transient agent
    (:mod:`msks.client.agent`). Yields ``(booted, served,
    known_hosts, user, pin, label)`` — whether this session booted
    the workspace (the #168 wait runs only then), the agent holder
    carrying the identity (either branch of :func:`session_identity`),
    the per-instance known_hosts path (#246), the workspace's login
    user (#248), the one host alias the session pins under
    (#697), and the workspace's human-facing label (its name,
    else the pin — the half the window title carries, #858).
    ``msks ssh`` (#112) and ``msks rsync`` (#190) share it.
    """
    token = env_token()
    url = env_url()
    ssl_ctx = None if is_unix(url) else ssl_context()
    key, booted = asyncio.run(
        prepare(workspace_id, url, token, ssl_ctx, transport)
    )
    pin = session_pin(key, workspace_id)
    cache_ref, instance = cache_key(key, workspace_id)
    hosts = known_hosts_path(cache_ref, instance=instance)
    repin_notice(key, pin, hosts)
    with session_identity(key, workspace_id) as served:
        yield (
            booted,
            served,
            hosts,
            workspace_user(key),
            pin,
            key.get("name") or pin,
        )


def exec_child(argv: list[str], missing_line: str) -> int:
    """Run a command's child process to its exit code; a binary
    missing from PATH is the calling command's one-line exit
    (``missing_line`` names it), not a traceback."""
    try:
        return subprocess.run(argv).returncode
    except FileNotFoundError:
        raise SystemExit(missing_line) from None


def run_workspace_ssh(
    workspace_id: str, passthrough: list[str], transport=None
) -> int:
    """One ssh session, from boot pre-flight to ssh's own exit
    code. The window the TUI opened for this session is titled at
    once (#445 — a boot or a refusal never leaves it nameless),
    and an interactive session registers itself in the per-user
    session records (#858) once the pre-flight answers: one
    record keyed by the workspace and this process's pid,
    heartbeated for the session's life and unlinked at its end,
    so the workspaces list counts the host's open shells — typed
    sessions and TUI-spawned windows alike. The token the record
    mints completes the window's title (#858): the workspace half
    is the workspace's name (its id when the daemon serves none),
    and a session without a record (a passthrough remote command
    — a connection, not a shell, registering nothing) drops the
    session half of the title."""
    passthrough = passthrough_args(passthrough)
    interactive = not split_command(passthrough)[1]
    title_spawned_window(workspace_id)
    with staged_session(workspace_id, transport) as (
        booted,
        served,
        known_hosts,
        user,
        pin,
        label,
    ):
        guard = record_session(pin) if interactive else nullcontext(None)
        with guard as token:
            title_spawned_window(label, token)
            if booted:
                probe_argv = probe_args(
                    pin,
                    served.server_address,
                    served.identity_path,
                    known_hosts,
                    passthrough,
                    user,
                )
                if probe_argv is not None:
                    wait_for_identity(
                        workspace_id,
                        probe_argv,
                        time.monotonic() + SSH_SEED_WAIT_S,
                    )
            argv = build_args(
                pin,
                served.server_address,
                served.identity_path,
                known_hosts,
                passthrough,
                user,
            )
            return exec_child(argv, "msks ssh: ssh not found on PATH")

"""``msks ssh <workspace-id> [-- ssh args]``: stock ssh into a workspace.

One-off sugar over the pieces that already exist: the workspace is
booted when the daemon reports it as not running (the same pre-flight
as ``msks console``), the workspace identity is fetched over the
authenticated API — the daemon-minted half pair (#111) or the public
half of a client-minted one (#121, whose private half then comes
from the client data root) — and ``ssh`` runs with the forward
websocket (#109) as its ProxyCommand. The session stages the
private half in a transient in-process ssh-agent
(:mod:`msks.client.agent`) and ssh authenticates through the agent
socket (``-o IdentityAgent=...``) — ssh closes inherited descriptors
at startup, so the socket is the one channel that survives to
authentication — writing no new copy anywhere: a daemon-minted
half arrives over the API and stays in memory for the session; a
client-minted half is read from its one file and left exactly
there.

The session logs in as the image's workspace user by default;
``-l root`` in the passthrough args is the recovery login. Agent
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

A session whose pre-flight booted the workspace first waits out the
guest's first-boot identity seed (#168): the daemon reports
``running`` while the guest's sshd is up but cloud-init has yet to
write ``authorized_keys``, so a bare dial in that window is refused
with ``Permission denied (publickey)``. A probe login (``true`` as
its remote command) retries behind the boot until the guest accepts
the workspace key, and the real session — interactive or one-shot —
then runs exactly once.
"""

import asyncio
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from . import agent
from .rest import (
    ensure_running,
    env_token,
    env_url,
    fetch_ssh_key,
    ssl_context,
)

#: The guest port sshd listens on (#110).
SSH_PORT = 22

#: The image's workspace user — the daily identity #111 seeds next
#: to root in ``authorized_keys``.
DEFAULT_USER = "msks"

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


def cache_dir() -> Path:
    """The client cache root: XDG_CACHE_HOME or ~/.cache, under msks."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "msks"


def data_dir() -> Path:
    """The client data root: XDG_DATA_HOME or ~/.local/share, under msks.

    Distinct from :func:`cache_dir` on purpose: the cache is
    disposable by convention (``~/.cache`` may be swept at any
    time), while the client-minted private half (#121) is the
    workspace's only copy — losing it loses ssh — so it lives with
    data that survives cache cleanup.
    """
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share"
    )
    return Path(base) / "msks"


def client_identity_path(workspace_id: str, base: Path | None = None) -> Path:
    """Where a client-minted private half lives (#121): the data
    root's per-workspace directory."""
    root = (base if base is not None else data_dir()) / workspace_id
    return root / "identity"


def resolve_private(key: dict, workspace_id: str) -> str:
    """The private half to serve, by the identity's source.

    A daemon-minted workspace (#111) hands its half over the API; a
    client-minted one (#121) answers ``private_key: null`` — its
    half lives in the client data root, written at create. The stored
    half is checked against the served public line before use: a
    stale cache (the id re-created from another client, a backup
    restored over a re-created workspace) fails as one named line,
    not as ssh's opaque ``Permission denied (publickey)``. Losing
    the file loses ssh and the console alike — a seeded guest
    challenges the console with the same key: the error names the
    path and the recovery, not a traceback.
    """
    if key["private_key"] is not None:
        return key["private_key"]
    path = client_identity_path(workspace_id)
    try:
        pem = path.read_text(encoding="utf-8")
        private = agent.load_private(pem)
    except OSError as exc:
        raise SystemExit(
            f"msks: the workspace's private half is not on this "
            f"client — the daemon holds none, and {path} is not "
            f"readable: {exc}\n"
            "The key was minted on another client (the file lives at "
            "that path on that machine), or it is a key you supplied "
            "at create — log in with it directly (ssh -i, or the "
            "Host msks-* alias), or run the console from the client "
            "that holds the current key: both console and ssh now "
            "need this half."
        ) from exc
    except ValueError as exc:
        raise SystemExit(
            f"msks ssh: the client-minted identity at {path} is not a "
            f"usable private key: {exc}"
        ) from exc
    if derived_public(private) != key["public_key"].split()[:2]:
        raise SystemExit(
            f"msks ssh: the client-minted identity at {path} does not "
            f"match {workspace_id} — the workspace was re-created since "
            "that key was stored. Delete that file and re-create the "
            "workspace (the client that holds the current identity "
            "keeps working for both ssh and the console)"
        )
    return pem


def derived_public(private) -> list[str]:
    """The public line's identifying fields (algorithm, key body) of
    a loaded private half — the comment is provenance, not identity,
    so it stays out of the comparison."""
    line = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return line.split()[:2]


def known_hosts_path(workspace_id: str, base: Path | None = None) -> str:
    """The per-workspace known_hosts file, its directory created.

    Host keys persist across stop/start on the workspace's overlay
    (#110), so one accept-new entry per workspace keeps matching.
    An unusable cache (the path taken by a file, an unwritable
    directory) is operator-shaped: one line, not a traceback.
    """
    root = (base if base is not None else cache_dir()) / workspace_id
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"msks ssh: cannot create {root}: {exc}") from exc
    return str(root / "known_hosts")


def passthrough_args(args: list[str]) -> list[str]:
    """The ssh arguments to pass through, verbatim.

    argparse's REMAINDER keeps everything after the workspace id as
    it was typed — the ``--`` between msks and its passthrough was
    argparse's separator, already eaten — so an ssh-style ``--`` a
    second time reaches ssh untouched (it is the options/command
    split :func:`split_command` reads).
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


def option_value(index: int, arg: str, args: list[str]) -> str | None:
    """The value of the ``-o`` at ``index`` — inline (``-oUser=x``)
    or the next argument — or None when ``arg`` names no option."""
    if arg.startswith("-o") and arg != "-o":
        return arg[2:]
    if arg == "-o" and index + 1 < len(args):
        return args[index + 1]
    return None


def names_user(value: str) -> bool:
    """Whether an ssh ``-o`` value sets the login user. ssh_config
    keywords are case-insensitive (``user=`` is ``User=``), so the
    comparison is too."""
    lowered = value.lower()
    return lowered.startswith("user=") or lowered.startswith("user ")


def operator_agent_socket() -> str:
    """The operator's agent socket, for forwarding into the guest —
    the one the operator's environment names
    (:func:`msks.client.agent.environment_agent`).

    Forwarding was asked for on the command line, so an environment
    that names no live agent is an error here: ssh would otherwise
    forward the session's transient agent, and the operator's keys —
    the whole point of ``-A`` — would quietly not be there.
    """
    socket = agent.environment_agent()
    if socket is None:
        named = os.environ.get("SSH_AUTH_SOCK", "") or "unset"
        raise SystemExit(
            "msks ssh: -A (or ForwardAgent=yes) forwards the "
            "operator's agent, and "
            f"SSH_AUTH_SOCK ({named}) names no agent socket — start "
            "one (ssh-agent, or the desktop agent) or drop the "
            "forwarding option"
        )
    return socket


def forward_agent_value(value: str) -> str | None:
    """The forwarding target an ssh ``-o`` value names, when it is a
    ForwardAgent setting (any whitespace separates keyword and
    value, as ssh's option parser accepts). The target is returned
    as written — normalization is :func:`normalized_target`'s
    job, so a passthrough token survives verbatim."""
    for sep in ("=", " ", "\t"):
        if value.lower().startswith("forwardagent" + sep):
            return value[len("forwardagent") + 1 :]
    return None


def normalized_target(target: str) -> str:
    """A ForwardAgent target as ssh reads it: outer whitespace
    trimmed, surrounding double quotes stripped (ssh's parser drops
    them), a leading ``=`` from the spaced ``key = value`` spelling
    dropped, lowercase — so a comparison here matches ssh's own
    keyword-value parsing."""
    return target.strip().strip('"').lstrip("=").strip().lower()


#: ssh short options that carry a value attached or beside them —
#: scanning a bundled token stops at the first of these (the rest
#: is that option's value, not more flags). The union across
#: supported ssh generations, from ``ssh -h``: B b c D E e F I i
#: J L l m O o p Q R S W w.
VALUE_TAKING_SHORTS = "BDEFIJLOPQRSWbceilmopw"


def flags_until_value(arg: str) -> str:
    """The leading short flags of a bundled token, up to the first
    option that carries its value attached — the rest of the token
    is that value, not more flags (``-JAdmin@h`` names a jump
    host, not a bundle)."""
    for pos, ch in enumerate(arg):
        if ch in VALUE_TAKING_SHORTS:
            return arg[:pos]
    return arg


def is_flag_bundle(arg: str) -> bool:
    """Whether the token is a bundled short-flag cluster this pass
    decomposes: not an ssh long option, not a value-attached option
    form (``-o``/``-l`` own everything after themselves), and at
    least one flag before any attached value."""
    if len(arg) < 3 or not arg.startswith("-"):
        return False
    if arg.startswith(("--", "-o", "-l")):
        return False
    return flags_until_value(arg[1:]) != ""


def bundle_flags(arg: str) -> str:
    """The flag characters of a bundled token (empty when the token
    is no bundle)."""
    return flags_until_value(arg[1:]) if is_flag_bundle(arg) else ""


def bundled_option_value(
    arg: str, index: int, options: list[str]
) -> str | None:
    """The ``-o`` value a bundled token carries, when its flag
    portion ends in ``o``: the attached remainder
    (``-voForwardAgent=yes``) or the next argv token
    (``-vo ForwardAgent=yes``) — ssh accepts both, and the value
    counts as a ForwardAgent setting the same as a plain ``-o``'s
    would. The flag portion is non-empty by precondition (the
    caller checks :func:`is_flag_bundle`)."""
    flags = flags_until_value(arg[1:])
    rest = arg[1 + len(flags) :]
    if not rest.startswith("o"):
        return None
    attached = rest[1:]
    if attached:
        return attached
    if index + 1 < len(options):
        return options[index + 1]
    return None


def option_values(options: list[str]):
    """Every ssh ``-o`` value on the line, in argv order — from
    plain ``-o`` tokens (inline or beside their value) and the
    ``-o`` a flag bundle carries."""
    for index, arg in enumerate(options):
        value = option_value(index, arg, options)
        if value is None and is_flag_bundle(arg):
            value = bundled_option_value(arg, index, options)
        if value is not None:
            yield value


def forward_agent_settings(options: list[str]):
    """Every ForwardAgent value on the line, in argv order, in
    every spelling ssh accepts."""
    for value in option_values(options):
        target = forward_agent_value(value)
        if target is not None:
            yield target


def named_forward_agent_target(options: list[str]) -> str | None:
    """The explicit socket some ForwardAgent setting on the line
    names, when one does: any target that is not yes/no/
    SSH_AUTH_SOCK — a path (absolute or relative), or a value that
    fails at dial time, which is stock's own answer (an empty value
    is stock's usage error). A stated socket is sticky in stock ssh:
    it wins over every flag and value in any order, so its presence
    means the operator already chose the socket and the rewrite
    stands down entirely."""
    for target in forward_agent_settings(options):
        if normalized_target(target) not in ("yes", "no", "ssh_auth_sock"):
            return target
    return None


def assigns_forwarding(flag_chars: str, result: bool | None) -> bool | None:
    """Fold each bundled flag character into the running
    assignment — ``A`` asks, ``a`` disables, later characters win
    as later flags do."""
    for ch in flag_chars:
        if ch == "A":
            result = True
        elif ch == "a":
            result = False
    return result


def last_flag_requests(options: list[str]) -> bool | None:
    """The last ``-A``/``-a`` on the line, bundles included — True
    when the last one asks for forwarding, False when it disables,
    None when no flag appears. ssh's flags assign unconditionally
    in argv order, over every plain value (``-o no -A`` and
    ``-A -o no`` both forward)."""
    result: bool | None = None
    for arg in options:
        if arg == "-A":
            result = True
        elif arg == "-a":
            result = False
        else:
            result = assigns_forwarding(bundle_flags(arg), result)
    return result


def effective_forwarding(options: list[str]) -> bool | None:
    """Whether the line, read as stock ssh reads it, asks for agent
    forwarding: the last ``-A``/``-a`` flag decides over every
    plain value; with no flag, the first ForwardAgent value does
    (plain values are first-obtained — ``-o no -o yes`` stays
    off)."""
    flag = last_flag_requests(options)
    if flag is not None:
        return flag
    for target in forward_agent_settings(options):
        return normalized_target(target) == "yes"
    return None


def forward_agent_args(options: list[str]) -> list[str]:
    """The passthrough's ssh options with command-line agent
    forwarding pointed at the operator's agent socket, mirroring
    stock ssh's own precedence exactly:

    - a ForwardAgent value that names a socket (anything but
      yes/no/SSH_AUTH_SOCK) is sticky — stock ssh resolves it over
      every flag and value in any order — so the rewrite stands
      down entirely and the operator's own spelling rides
      untouched;
    - otherwise the last ``-A``/``-a`` decides (flags assign
      unconditionally in order), and with no flag the first
      ForwardAgent value does (first-obtained) — a line stock
      resolves to off, or that names no forwarding at all, passes
      through untouched;
    - a line that resolves to forwarding gets the operator's
      socket stated at the FRONT of the options: a stated path wins
      over every flag and value behind it, so the user's own
      spellings (a trailing ``-a``, a ``no``, a bundled ``-vA``)
      ride along inert and stock resolves the operator's agent.

    This session authenticates through the transient workspace agent
    (``IdentityAgent``), and ssh forwards *that* socket when asked
    to forward at all — which is why a resolved request is answered
    with the operator's socket explicitly: the workspace identity
    stays an authentication credential; the guest receives the
    operator's real keys and nothing else. #123's own-key sessions
    authenticate through the operator's agent or an identity file;
    the rewrite then states the same socket ssh would forward
    anyway, and this pass reads as a no-op.
    """
    if named_forward_agent_target(options) is not None:
        return list(options)
    if effective_forwarding(options) is not True:
        return list(options)
    return [
        "-o",
        "ForwardAgent=" + config_quote(operator_agent_socket()),
        *options,
    ]


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


def config_quote(value: str) -> str:
    """Double-quote a value for an ``-o`` option when it carries
    whitespace: double quotes are honored everywhere ssh parses
    option values (the parser strips them itself, and the shell
    strips the remainder), keeping the value whole."""
    return f'"{value}"' if any(c.isspace() for c in value) else value


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


def build_args(
    workspace_id: str,
    agent_socket: str,
    identity_pub: str,
    known_hosts: str,
    passthrough: list[str],
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
    one key and nothing else.
    """
    options, command = split_command(passthrough)
    options = forward_agent_args(options)
    argv = [
        "ssh",
        *options,
        *session_options(
            workspace_id, agent_socket, identity_pub, known_hosts
        ),
    ]
    if not wants_user(options):
        argv += ["-l", DEFAULT_USER]
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
    user = probe_user(options)
    if wants_user(options) and not user:
        return None
    # After the refusal check: a session ssh rejects outright names
    # no agent to resolve — its own usage error is the answer.
    options = forward_agent_args(options)
    forwarded = forward_agent_option(options)
    argv = [
        "ssh",
        "-q",
        *user,
        *forwarded,
        *session_options(
            workspace_id, agent_socket, identity_pub, known_hosts
        ),
    ]
    if not wants_user(options):
        argv += ["-l", DEFAULT_USER]
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
    workspace_id: str, probe_argv: list[str], deadline: float
) -> None:
    """Retry the probe until the guest accepts the login or the
    deadline passes (#168).

    Each attempt is bounded by the time the deadline leaves, so a
    stalled connection is one more not-ready answer, not a hang,
    and the whole wait stays inside the deadline plus one pause. A
    deadline that passes leaves the failure to the real session:
    ssh's own message names the refusal, and the probe's notices
    have already said what was being waited for.
    """
    while True:
        now = time.monotonic()
        if now >= deadline:
            return
        if probe_attempt(probe_argv, deadline - now):
            return
        time.sleep(SSH_RETRY_PAUSE_S)
        if time.monotonic() < deadline:
            print(
                f"msks: {workspace_id} is not accepting the login yet; "
                "retrying",
                file=sys.stderr,
            )


def probe_attempt(probe_argv: list[str], budget: float) -> bool:
    """One probe attempt: True when the guest accepted the login.

    A stalled connection is one more not-ready answer — bounded by
    the budget the deadline leaves the attempt — and an ssh missing
    from PATH is the session's own named error, raised here so the
    wait reports it before the session ever runs.
    """
    try:
        completed = subprocess.run(probe_argv, timeout=budget)
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        raise SystemExit("msks ssh: ssh not found on PATH") from None
    return completed.returncode == 0


def run_workspace_ssh(
    workspace_id: str, passthrough: list[str], transport=None
) -> int:
    """One ssh session, from boot pre-flight to ssh's own exit code."""
    passthrough = passthrough_args(passthrough)
    token = env_token()
    url = env_url()
    ssl_ctx = ssl_context()
    key, booted = asyncio.run(
        prepare(workspace_id, url, token, ssl_ctx, transport)
    )
    private = agent.load_private(resolve_private(key, workspace_id))
    with agent.serve(private, identity_comment(key)) as served:
        known_hosts = known_hosts_path(workspace_id)
        if booted:
            probe_argv = probe_args(
                workspace_id,
                served.server_address,
                served.identity_path,
                known_hosts,
                passthrough,
            )
            if probe_argv is not None:
                wait_for_identity(
                    workspace_id,
                    probe_argv,
                    time.monotonic() + SSH_SEED_WAIT_S,
                )
        argv = build_args(
            workspace_id,
            served.server_address,
            served.identity_path,
            known_hosts,
            passthrough,
        )
        try:
            completed = subprocess.run(argv)
        except FileNotFoundError:
            raise SystemExit("msks ssh: ssh not found on PATH") from None
    return completed.returncode

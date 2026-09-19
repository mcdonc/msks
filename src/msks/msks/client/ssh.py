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
forwarding (``msks ssh <ws> -- -A``) forwards the session agent —
the guest can sign as the workspace identity; forwarding the
operator's own agent (git credentials for ``git push`` from inside)
is the alias path's job, where the operator's real ``SSH_AUTH_SOCK``
rides untouched (see ``docs/networking.md``). Everything after the
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
    settings, the session's login user, and a throwaway ``true`` as
    the remote command — the passthrough contributes nothing else.

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
    none of them is in it. ``true`` runs no user command, so a
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
    argv = [
        "ssh",
        "-q",
        *user,
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

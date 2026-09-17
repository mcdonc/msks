"""``msks ssh <workspace-id> [-- ssh args]``: stock ssh into a workspace.

One-off sugar over the pieces that already exist: the workspace is
booted when the daemon reports it as not running (the same pre-flight
as ``msks console``), the workspace identity is fetched over the
authenticated API — the daemon-minted half pair (#111) or the public
half of a client-minted one (#121, whose private half then comes from
the local cache) — and ``ssh`` runs with the forward websocket
(#109) as its ProxyCommand. The private half never becomes a file:
a transient in-process ssh-agent (:mod:`msks.client.agent`) holds it
in memory and ssh authenticates through the agent socket
(``-o IdentityAgent=...``) — ssh closes inherited descriptors at
startup, so the socket is the one channel that survives to
authentication.

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
"""

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import agent
from .rest import ensure_running, env_token, env_url, fetch_ssh_key, ssl_context

#: The guest port sshd listens on (#110).
SSH_PORT = 22

#: The image's workspace user — the daily identity #111 seeds next
#: to root in ``authorized_keys``.
DEFAULT_USER = "msks"


async def prepare(
    workspace_id: str,
    url: str,
    token: str,
    ssl_ctx=None,
    transport=None,
) -> dict:
    """Boot the workspace if needed, then fetch its identity."""
    await ensure_running(workspace_id, url, token, ssl_ctx=ssl_ctx, transport=transport)
    return await fetch_ssh_key(
        url, token, workspace_id, transport=transport, ssl_ctx=ssl_ctx
    )


def cache_dir() -> Path:
    """The client cache root: XDG_CACHE_HOME or ~/.cache, under msks."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "msks"


def client_identity_path(workspace_id: str, base: Path | None = None) -> Path:
    """Where a client-minted private half lives (#121): the cache's
    per-workspace directory, beside ``known_hosts``."""
    root = (base if base is not None else cache_dir()) / workspace_id
    return root / "identity"


def resolve_private(key: dict, workspace_id: str) -> str:
    """The private half to serve, by the identity's source.

    A daemon-minted workspace (#111) hands its half over the API; a
    client-minted one (#121) answers ``private_key: null`` — its
    half lives in the local cache, written at create. Losing that
    file loses ssh (the console still opens): the error names the
    path and the recovery, not a traceback.
    """
    if key["private_key"] is not None:
        return key["private_key"]
    path = client_identity_path(workspace_id)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(
            f"msks ssh: {workspace_id} carries a client-minted identity "
            f"and its private half is not readable at {path}: {exc}\n"
            "The identity was minted on the client that created the "
            "workspace; the console still opens without it."
        ) from exc


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

    ``-l root`` (the recovery login) and ``-o User=root`` — the value
    separate or inline — both count; when ssh is told its user, the
    default is not injected twice.
    """
    for index, arg in enumerate(args):
        if arg == "-l":
            return True  # even dangling: ssh's own error is the clear one
        value = option_value(index, arg, args)
        if value is not None and names_user(value):
            return True
    return False


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
    return f"ProxyCommand={runner} forward {shlex.quote(workspace_id)} {SSH_PORT}"


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
    argv = ["ssh", *options, "-o", proxy_command(workspace_id)]
    argv += [
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
    if not wants_user(options):
        argv += ["-l", DEFAULT_USER]
    return argv + [workspace_id] + command


def identity_comment(key: dict) -> str:
    """The comment on the identity listing, from the public line."""
    fields = key["public_key"].split()
    return fields[2] if len(fields) > 2 else ""


def run_workspace_ssh(workspace_id: str, passthrough: list[str], transport=None) -> int:
    """One ssh session, from boot pre-flight to ssh's own exit code."""
    passthrough = passthrough_args(passthrough)
    token = env_token()
    url = env_url()
    ssl_ctx = ssl_context()
    key = asyncio.run(prepare(workspace_id, url, token, ssl_ctx, transport))
    private = agent.load_private(resolve_private(key, workspace_id))
    with agent.serve(private, identity_comment(key)) as served:
        argv = build_args(
            workspace_id,
            served.server_address,
            served.identity_path,
            known_hosts_path(workspace_id),
            passthrough,
        )
        try:
            completed = subprocess.run(argv)
        except FileNotFoundError:
            raise SystemExit("msks ssh: ssh not found on PATH") from None
    return completed.returncode

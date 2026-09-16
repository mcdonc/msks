"""``msks ssh <workspace-id> [-- ssh args]``: stock ssh into a workspace.

One-off sugar over the pieces that already exist: the workspace is
booted when the daemon reports it as not running (the same pre-flight
as ``msks console``), the minted identity (#111) is fetched over the
authenticated API, and ``ssh`` runs with the forward websocket
(#109) as its ProxyCommand. The private half never becomes a file on
disk: it is written to a sealed memfd (mode 0600) and handed to ssh
as ``-i /proc/self/fd/<n>``, an fd passed to the child — the key
material exists only in process memory and disappears with it.

The session logs in as the image's workspace user by default;
``-l root`` in the passthrough args is the recovery login. Agent
forwarding (``msks ssh <ws> -- -A``) forwards the operator's own
ssh-agent, so ``git push`` from inside the workspace uses the
operator's credentials (#81's credential half). Everything after the
workspace id (or after ``--``) is passed to ssh verbatim; ssh's own
``--`` inside it separates options from a remote command
(``msks ssh <ws> -- -A -- uname -a``).
"""

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

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
    """Boot the workspace if needed, then fetch its minted identity."""
    await ensure_running(workspace_id, url, token, ssl_ctx=ssl_ctx, transport=transport)
    return await fetch_ssh_key(url, token, workspace_id, transport=transport)


def memfd_key(private_pem: str) -> int:
    """The private half in a sealed memfd, mode 0600; its fd.

    The fd is what ssh receives (``-i /proc/self/fd/<fd>`` via
    ``pass_fds``): the key is never a path on any filesystem, and a
    crash leaves nothing behind — the memory goes with the process.
    ssh refuses a group- or world-readable identity file, and a
    fresh memfd carries mode 0777, so the 0600 fchmod is load-bearing.
    """
    create = getattr(os, "memfd_create", None)
    if create is None:
        raise SystemExit(
            "msks ssh: this host has no memfd_create (Linux 3.17+); "
            "materialize the key with 'msks key --out' and run ssh yourself"
        )
    try:
        fd = create("msks-ssh-key", flags=0)
        os.write(fd, private_pem.encode())
        os.fchmod(fd, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise SystemExit(f"msks ssh: cannot stage the identity: {exc}") from exc
    return fd


def identity_path(fd: int) -> str:
    """The ``-i`` argument that reads the sealed memfd in the child."""
    return f"/proc/self/fd/{fd}"


def cache_dir() -> Path:
    """The client cache root: XDG_CACHE_HOME or ~/.cache, under msks."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "msks"


def known_hosts_path(workspace_id: str, base: Path | None = None) -> str:
    """The per-workspace known_hosts file, its directory created.

    Host keys persist across stop/start on the workspace's overlay
    (#110), so one accept-new entry per workspace keeps matching.
    """
    root = (base if base is not None else cache_dir()) / workspace_id
    root.mkdir(parents=True, exist_ok=True)
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

    ssh's own ``--`` separates them (``msks ssh ws -- -A -- top``):
    options go before the destination the way ssh parses them, the
    command follows it — the same split ssh itself would make.
    """
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
            return index + 1 < len(args)
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
    """Whether an ssh ``-o`` value sets the login user."""
    return value.startswith("User=") or value.startswith("User ")


def build_args(
    workspace_id: str,
    identity: str,
    known_hosts: str,
    passthrough: list[str],
) -> list[str]:
    """ssh's argv: transport, host-key, identity — the workspace id
    as the destination between the passthrough's options and its
    remote command — so both ``-A`` (an option) and a command land
    where ssh parses them."""
    proxy = f"ProxyCommand=msks forward {shlex.quote(workspace_id)} {SSH_PORT}"
    options, command = split_command(passthrough)
    argv = [
        "ssh",
        "-o",
        proxy,
        "-o",
        f"UserKnownHostsFile={shlex.quote(known_hosts)}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        identity,
    ]
    if not wants_user(options):
        argv += ["-l", DEFAULT_USER]
    return argv + options + [workspace_id] + command


def run_workspace_ssh(workspace_id: str, passthrough: list[str], transport=None) -> int:
    """One ssh session, from boot pre-flight to ssh's own exit code."""
    passthrough = passthrough_args(passthrough)
    token = env_token()
    url = env_url()
    ssl_ctx = ssl_context()
    key = asyncio.run(prepare(workspace_id, url, token, ssl_ctx, transport))
    fd = memfd_key(key["private_key"])
    try:
        argv = build_args(
            workspace_id,
            identity_path(fd),
            known_hosts_path(workspace_id),
            passthrough,
        )
        completed = subprocess.run(argv, pass_fds=(fd,))
    except FileNotFoundError:
        raise SystemExit("msks ssh: ssh not found on PATH") from None
    finally:
        os.close(fd)
    return completed.returncode

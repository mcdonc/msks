"""``msks rsync <workspace-id> -- <rsync args>``: file copies over the
forward, zero setup.

The same composition ``msks ssh`` (#112) runs, answering the copy
half of "git and rsync over the forward": the workspace is booted
when the daemon reports it as not running (the same notices as
``msks console``), the workspace identity is fetched over the
authenticated API — daemon-minted (#111) or client-minted (#121) —
and the host ``rsync`` runs with the forward websocket (#109) as
its ssh transport. The private half is staged in the transient
in-process ssh-agent (:mod:`msks.client.agent`) and rsync's ssh
children name the identity by its public half and sign through the
agent socket (``IdentityAgent``), so the command writes no key
file: a daemon-minted half arrives over the API and stays in
memory for the session; a client-minted half is read from its one
file and left exactly there. A session whose pre-flight booted the
workspace waits out the first-boot identity seed (#168) with the
same probe login ``msks ssh`` runs.

Everything after the workspace id (or after ``--``) is passed to
rsync verbatim; msks parses no rsync flags. The one shaping pass
is the empty host: rsync's single-colon syntax lets the host name
sit before the colon, and a path that names none (``:src``,
``root@:/root/site``) targets the workspace this command names —
filled in as the host, so the push and pull forms read
``msks rsync my-workspace -- -av ./site/ root@:/root/site/``
(the ``root@`` spelling matters for root-owned paths: the guest's
``/root`` is root's alone, and the default login — the image's
``msks`` workspace user — writes under that user's persistent
``/home``). The direction comes entirely from the rsync
arguments. A path that names a host keeps it (the transport is
the proxy, so the name never resolves); ``::`` (rsync's daemon
protocol) is left as typed, and the workspace image runs no rsync
daemon — sshd stays the guest's one inbound service (#110).

The login user defaults to the image's workspace user the same way
``msks ssh`` injects ``-l msks`` — but rsync itself appends
``-l user`` to the remote shell when a path spells ``user@host:``,
and ssh keeps the first user it obtains, so the default cannot
ride the ``-e`` string. It rides a generated per-session ssh
config (``-F``) instead — the session's transport and identity
settings restated as config directives, with the user as the
default stock ssh semantics make it: overridden by rsync's
``user@`` (``root@:/root/site`` logs in as root) and by anything
else that names a user. Keeping the settings in the config keeps
the ``-e`` value to ``ssh -F <path>`` — one word-splitting level,
with no ssh-option quoting to nest inside it (rsync splits the
``-e`` value on whitespace honoring double quotes, one level
only). An explicit ``-e`` in the passthrough replaces msks's
remote shell entirely (rsync takes the last ``-e``), the same
override shape ssh passthrough options have.
"""

import time
from pathlib import Path

from .ssh import (
    DEFAULT_USER,
    SSH_SEED_WAIT_S,
    config_quote,
    exec_child,
    known_hosts_path,
    passthrough_args,
    probe_args,
    session_options,
    staged_session,
    wait_for_identity,
)


def empty_host(spec: str) -> bool:
    """Whether a host spec names the empty host: a leading single
    colon (``:path``), or a user whose ``@`` is followed directly
    by one (``user@:path``). The double-colon daemon forms name a
    different protocol, not an empty host."""
    return spec.startswith(":") and not spec.startswith("::")


def filled_host(arg: str, workspace_id: str) -> str:
    """One rsync path argument, its empty host filled to the
    workspace this command names.

    Only the first path component can carry a host spec (a colon
    past a slash is a local filename to rsync), and only the empty
    host is filled — a named host rides the same forward untouched.
    A split-form option value that begins with a colon
    (``--filter :rules``) is indistinguishable from a remote path
    without parsing rsync's flags, so that spelling uses the
    attached form (``--filter=:rules``), which msks never touches.
    """
    first = arg.partition("/")[0]
    if empty_host(first):
        return f"{workspace_id}{arg}"  # arg begins with the colon
    at = first.rfind("@")
    if at != -1 and empty_host(first[at + 1 :]):
        return f"{arg[: at + 1]}{workspace_id}{arg[at + 1 :]}"
    return arg


def rsync_paths(args: list[str], workspace_id: str) -> list[str]:
    """The rsync arguments with every empty-host path filled."""
    return [filled_host(arg, workspace_id) for arg in args]


def config_directives(options: list[str], user: str) -> list[str]:
    """ssh config lines from the session's option words, the
    default user first — one source of truth for the session's
    settings (:func:`msks.client.ssh.session_options` builds them
    as ``-o``/``-i`` argv words for ``msks ssh``), restated as the
    directives a per-session ``-F`` file carries.

    The ``-o`` words are keyword=value pairs; an ``-o`` value's
    optional double quotes (:func:`msks.client.ssh.config_quote`)
    ride into the config line, where ssh's config parser strips
    them the same way its option parser would. The session's
    ``-i`` word becomes ``IdentityFile`` — quoted here, because
    the argv word carries its path raw and the config line's
    value must not split on whitespace."""
    lines = [f"User {user}"]
    pending = iter(options)
    for word in pending:
        if word == "-o":
            keyword, _, value = next(pending).partition("=")
            lines.append(f"{keyword} {value}")
        elif word == "-i":
            lines.append(f"IdentityFile {config_quote(next(pending))}")
    return lines


def write_ssh_config(served, options: list[str]) -> str:
    """The per-session ssh config beside the served identity's
    public half, inside the agent's mode-0700 temporary directory —
    it exists exactly as long as the session does. The user lives
    in a config file, not the ``-e`` string, because rsync appends
    its own ``-l user`` (a path's ``user@host:`` spelling) after
    the ``-e`` words and ssh keeps the first user it obtains: in
    a config, the default is what stock ssh makes it — overridden
    from the command line."""
    path = Path(served.identity_path).parent / "ssh_config"
    path.write_text(
        "\n".join(config_directives(options, DEFAULT_USER)) + "\n",
        encoding="utf-8",
    )
    return str(path)


def rsh_word(word: str) -> str:
    """One ssh argv word for the ``-e`` string: double-quoted when
    it carries whitespace, because rsync splits the ``-e`` value on
    whitespace honoring double quotes (one level — which is why the
    ``-e`` value stays at ``ssh -F <path>`` and every other setting
    rides the config file the path names)."""
    return f'"{word}"' if any(c.isspace() for c in word) else word


def rsh_string(config_path: str) -> str:
    """The ``-e`` value: the ssh command rsync runs per connection.
    Every setting rides the per-session config the path names — the
    same transport and host-key posture ``msks ssh`` carries, plus
    the default login user."""
    return " ".join(rsh_word(word) for word in ("ssh", "-F", config_path))


def build_args(
    workspace_id: str,
    config_path: str,
    passthrough: list[str],
) -> list[str]:
    """rsync's argv: msks's remote shell first, then the passthrough
    verbatim (empty hosts filled). rsync takes the last ``-e`` it
    receives, so a passthrough ``-e`` overrides msks's transport —
    the same override shape ssh passthrough options have."""
    return [
        "rsync",
        "-e",
        rsh_string(config_path),
        *rsync_paths(passthrough, workspace_id),
    ]


def require_args(passthrough: list[str]) -> None:
    """Refuse the argumentless form with the usage line — rsync's
    own usage error names no workspace."""
    if not passthrough:
        raise SystemExit(
            "msks rsync: pass the rsync arguments after the "
            "workspace id, e.g. msks rsync alpha -- -av "
            "./site/ root@:/root/site/"
        )


def run_workspace_rsync(
    workspace_id: str, passthrough: list[str], transport=None
) -> int:
    """One rsync run, from boot pre-flight to rsync's own exit code.

    The first-boot probe dials as the DEFAULT user even when the
    copy logs in as another (``root@:`` paths): the guest's seed
    writes both users' ``authorized_keys`` in one cloud-init run,
    so the default user's acceptance is the seed's arrival either
    way."""
    passthrough = passthrough_args(passthrough)
    require_args(passthrough)
    with staged_session(workspace_id, transport) as (booted, served):
        known_hosts = known_hosts_path(workspace_id)
        config_path = write_ssh_config(
            served,
            session_options(
                workspace_id,
                served.server_address,
                served.identity_path,
                known_hosts,
            ),
        )
        if booted:
            wait_for_identity(
                workspace_id,
                probe_args(
                    workspace_id,
                    served.server_address,
                    served.identity_path,
                    known_hosts,
                    [],
                ),
                time.monotonic() + SSH_SEED_WAIT_S,
                "msks rsync",
            )
        return exec_child(
            build_args(workspace_id, config_path, passthrough),
            "msks rsync: rsync not found on PATH",
        )

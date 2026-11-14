"""The operator's ssh identity: cache, keys, pins.

The cache layout under the client cache root, the configured
identity and pubkey files, the operator-key resolution (agent,
file, recovery), the workspace session pins, and the rotation
notices.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from . import agent
from .sshsessions import env_state_dir


def cache_dir() -> Path:
    """The client cache root: `MSKSC_CACHE_DIR` when set, else
    XDG_CACHE_HOME or ~/.cache, under msks.

    The variable names the root itself — the per-workspace
    directories are created below it — so a checkout (the devenv
    shell points it at the worktree's own state, #251) or an
    operator takes the whole cache out of the shared XDG tree.
    """
    override = env_state_dir("MSKSC_CACHE_DIR")
    if override is not None:
        return override
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "msks"


#: The operator identity's setting (#336): the config key
#: ``identity_file`` / the variable ``MSKSC_IDENTITY_FILE`` name a
#: private key file — the operator's own ssh key, the one a bare
#: ``msks create`` plants into every workspace. The config names a
#: path (the ``token_file`` pattern) and the private material
#: itself never moves: msks reads the file where it lives and
#: writes no copy anywhere.
IDENTITY_FILE_ENV = "MSKSC_IDENTITY_FILE"

#: The public-half setting (#495): the config key
#: ``pubkey_file`` / the variable ``MSKSC_PUBKEY_FILE`` name a
#: public key file — the one line a create plants as the identity
#: when the private half is nowhere msks can read it (encrypted at
#: its file, held by the operator's agent): the create reads no
#: private material at all, and sessions resolve through the
#: agent rung (#490). Global-only, like identity_file.
PUBKEY_FILE_ENV = "MSKSC_PUBKEY_FILE"

#: The encrypted-key refusal's recoveries (#490), by calling
#: context: a session consults the operator's agent ahead of the
#: file, so adding the key to the agent is the first recovery,
#: while the create reads the private half itself and offers the
#: decrypt and ``--pubkey`` pair (the create can derive nothing
#: from a file it cannot read — the agent holds no rung there).
ENCRYPTED_SESSION_RECOVERY = (
    "Add the key to your agent (ssh-add), decrypt a copy "
    "(ssh-keygen -p -f <copy> -P <passphrase> -N ''), or pass "
    "--pubkey at create"
)
ENCRYPTED_CREATE_RECOVERY = (
    "Decrypt a copy (ssh-keygen -p -f <copy> -P <passphrase> "
    "-N ''), point pubkey_file at the key's public half, or pass "
    "--pubkey at create"
)


def configured_file(variable: str) -> Path | None:
    """One configured key file's path (``identity_file``,
    ``pubkey_file``), ``~`` expanded, or None when unset — an
    empty value counts as unset, the same rule the shell presets
    apply. A relative value is refused (the same rule the state
    roots carry): a key that resolves per working directory would
    plant a different identity from every directory."""
    value = os.environ.get(variable, "")
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(
            f"msks: {variable} must name an absolute path (got "
            f"'{value}'); unset it, pass an absolute path, or "
            "lead with ~"
        )
    return path


def configured_identity_file() -> Path | None:
    """The operator-named identity file (``identity_file`` /
    ``MSKSC_IDENTITY_FILE``) — the path rules live in
    :func:`configured_file`."""
    return configured_file(IDENTITY_FILE_ENV)


def configured_pubkey_file() -> Path | None:
    """The operator-named public key file (``pubkey_file`` /
    ``MSKSC_PUBKEY_FILE``, #495) — the same path rules
    :func:`configured_identity_file` applies."""
    return configured_file(PUBKEY_FILE_ENV)


def named_identity_file(path: Path, recovery: str) -> str:
    """The strict form for the file the operator named: a missing,
    unreadable, encrypted, or unstagedable key is one line naming
    the setting and the fix — the operator pointed at it, so a
    silent skip would read as the key matching nothing.
    ``recovery`` names the recoveries the calling context offers
    for an encrypted key: the session resolves through the
    operator's agent first (#490), the create reads the file
    itself."""
    try:
        pem = path.read_text(encoding="utf-8")
        private = agent.load_private(pem)
    except OSError as exc:
        raise SystemExit(
            f"msks: the operator identity file {path} is not readable: "
            f"{exc} — point identity_file / {IDENTITY_FILE_ENV} at "
            "your private key file"
        ) from exc
    except TypeError as exc:
        raise SystemExit(
            f"msks: the operator identity file {path} is encrypted "
            f"({exc}); msks never types a passphrase. {recovery}"
        ) from exc
    except ValueError as exc:
        raise SystemExit(
            f"msks: the operator identity file {path} is not a private "
            f"key msks can stage ({exc}). msks stages unencrypted keys "
            "in the OpenSSH format — ed25519, ecdsa (P-256/P-384/"
            "P-521), or rsa; convert a copy with ssh-keygen -p -f "
            "<copy>, or pass --pubkey at create"
        ) from exc
    if not agent.signable(private):
        raise SystemExit(
            f"msks: the operator identity file {path} holds a key type "
            "msks cannot stage (msks stages ed25519, ecdsa "
            "(P-256/P-384/P-521), and rsa keys). Plant its public half "
            "with --pubkey at create, or keep signing with your own ssh"
        )
    return pem


def operator_identity(recovery: str) -> tuple[str, Path] | None:
    """The operator's identity: ``(pem, source path)`` or None
    when no file is configured.

    One rung, deterministic and file-based (#336, #486): the file
    ``identity_file`` / ``MSKSC_IDENTITY_FILE`` names — the
    operator's explicit choice, checked strictly. There is no
    agent discovery and no scanning of the operator's own
    ``~/.ssh`` (which key of yours msks should use is a choice you
    make, not a guess msks makes), and msks never generates a key
    (#486): the create refuses when nothing is configured. ``recovery``
    is the encrypted-key refusal's
    recoveries for the calling context
    (:data:`ENCRYPTED_SESSION_RECOVERY` or
    :data:`ENCRYPTED_CREATE_RECOVERY`).
    """
    configured = configured_identity_file()
    if configured is None:
        return None
    return named_identity_file(configured, recovery), configured


def agent_held(served: list[str]) -> str | None:
    """The operator's agent socket when the agent holds the
    session's key (#490) — the rung ahead of every file: when
    ``SSH_AUTH_SOCK`` names a live agent, the identities it lists
    (:func:`msks.client.agent.listed_lines`) are compared against
    the public half the session needs, on key material and never
    on file paths (the agent may hold the same key under any
    name). A match points the session at the operator's agent —
    the private half, encrypted or not, never enters the msks
    process. No agent, a dead socket, or no match declines to the
    file rungs unchanged."""
    socket_path = agent.environment_agent()
    if socket_path is None:
        return None
    lines = agent.listed_lines(socket_path)
    if lines is None:
        return None
    for line in lines:
        if line.split()[:2] == served:
            return socket_path
    return None


def operator_match(served: list[str]) -> str | None:
    """The operator identity's PEM when its public half matches the
    row's served line — the rung that keeps ``msks ssh``, ``msks
    rsync``, and the console's pubkey auth working on a workspace
    planted with the operator's key (#336), including one
    re-created under the same name (a new id, the same key)."""
    resolved = operator_identity(ENCRYPTED_SESSION_RECOVERY)
    if resolved is None:
        return None
    pem, _source = resolved
    if derived_public(agent.load_private(pem)) == served:
        return pem
    return None


def resolve_private(key: dict, workspace_id: str) -> str:
    """The operator identity's PEM for the row's served line, or
    the one-line refusal — the file rung under the agent (#490,
    #497). The operator's agent answers first
    (:func:`agent_held`); this rung stages the identity file
    whose public half matches the row's line (the #336 operator
    identity — a workspace re-created under the same name keeps
    its access: a new id, the same key). A workspace planted with
    a key this client holds nowhere fails as one named line, not
    ssh's opaque ``Permission denied (publickey)`` — losing the
    half loses ssh and the console alike (a seeded guest
    challenges the console with the same key), so the line names
    the ways back in.
    """
    served = key["public_key"].split()[:2]
    operator = operator_match(served)
    if operator is not None:
        return operator
    raise SystemExit(unheld_identity_line(workspace_id))


def unheld_identity_line(workspace_id: str) -> str:
    """The recovery line for a session whose key this client holds
    nowhere: the rungs it tried and the ways back in."""
    return (
        f"msks: this client holds no private half for {workspace_id} "
        "— the agent lists none (ssh-add the key), and identity_file "
        f"/ {IDENTITY_FILE_ENV} names none or a different key.\n"
        "Add the key to your agent (ssh-add), point identity_file at "
        "it, or log in with it directly (ssh -i, or the Host msks-* "
        "alias). The console keeps working either way: it is the "
        "token-gated root autologin (#481), not a key login."
    )


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


def instance_token(key: dict) -> str | None:
    """The workspace instance's compact stamp (#245), or None.

    The daemon returns ``created_at`` with the identity; hashing it
    keeps the cache directory short while still naming the
    instance — two workspaces created under one name stamp
    differently, so each instance gets its own host-key cache. A
    key without the field (an older daemon, a test fixture) yields
    None and the caller falls back to the name-keyed path.
    """
    created_at = key.get("created_at")
    if not created_at:
        return None
    return hashlib.sha1(created_at.encode()).hexdigest()[:8]


def rotation_token(key: dict) -> str | None:
    """The cache rotation suffix the row's markers compose (#697,
    #803): ``e<N>`` for a recreated disk set, ``r<N>`` for a
    re-baked seed, both when both. None when the row carries
    neither marker — the directory the pre-rotation clients used,
    so an unserved marker (a daemon one version behind) churns
    nothing on upgrade. The seed's marker is a counter, not the
    bake's content hash: a mount set leaving and returning bakes
    the same content twice and still lands a fresh directory,
    because the guest re-keys on the *change*, not the value."""
    parts = []
    if key.get("disk_epoch"):
        parts.append(f"e{key['disk_epoch']}")
    if key.get("seed_epoch"):
        parts.append(f"r{key['seed_epoch']}")
    if not parts:
        return None
    return ".".join(parts)


def cache_key(key: dict, ref: str) -> tuple[str, str | None]:
    """The host-key cache's directory key: the workspace's immutable
    id when the daemon serves it (#246 — the id names the instance,
    so two workspaces created under one name never share a cache),
    else the typed reference with the #245 created_at stamp
    separating instances (a daemon one version behind). A nonzero
    disk epoch (#697) joins the key as a suffix: the daemon
    recreated the workspace's root overlay, the guest's ssh host
    keys regenerated with it, and the rotated directory re-pins
    fresh instead of refusing. A seed epoch (#803) joins the
    same way: a bake or renewal re-provisions the guest under a
    new instance-id and the stock cloud-init ssh module
    regenerates its host keys, so the counter rotates the
    directory; both markers compose (a reset workspace that then
    renews carries both suffixes).
    """
    if key.get("id"):
        return key["id"], rotation_token(key)
    return ref, instance_token(key)


def session_pin(key: dict, ref: str) -> str:
    """The one host alias every ssh invocation pins under (#697):
    the workspace's immutable id when the daemon serves it, else
    the typed reference. ``msks ssh tree`` and the TUI's
    ``msks ssh <id>`` used to pin the same workspace under two
    aliases — one could refuse a regenerated host key while the
    other silently re-pinned — so the session's hostname and
    known_hosts alias come from here, never from what was typed.
    """
    return key.get("id") or ref


def rotation_cause(key: dict) -> str:
    """The re-pin notice's cause half (#697, #803): the markers the
    row carries name why the guest's host keys may differ from any
    earlier pin. Both markers present names both — the client
    cannot know which one moved since its last session, and a
    reset-then-renewed workspace would otherwise read the reset's
    cause on every later renewal."""
    if key.get("disk_epoch") and key.get("seed_epoch"):
        return "disks were recreated or the seed renewed"
    if key.get("disk_epoch"):
        return "disks were recreated"
    return "re-provisioned (a renewed seed)"


def repin_notice(key: dict, pin: str, hosts: str) -> None:
    """The one-line notice a rotated cache directory prints once
    (#697, #803): the rotated directory starts empty and the first
    successful connect writes the entry, so the notice rides the
    one session that re-pins and every later session keeps
    quiet. The cause names itself: a renewed seed re-provisions
    the guest and the stock cloud-init ssh module regenerates its
    host keys there."""
    if not (key.get("disk_epoch") or key.get("seed_epoch")):
        return
    if Path(hosts).is_file():
        return
    label = key.get("name") or pin
    print(
        f"msks: {label}'s {rotation_cause(key)}; pinning fresh host keys",
        file=sys.stderr,
    )


def known_hosts_path(
    workspace_id: str, base: Path | None = None, instance: str | None = None
) -> str:
    """The per-workspace known_hosts file, its directory created.

    Host keys persist across stop/start on the workspace's overlay
    (#110), so one accept-new entry per workspace keeps matching.
    The directory keys on the workspace instance (#246): a current
    daemon serves the workspace's immutable id with the identity
    and the cache lives under that — a recreated workspace is a new
    id and starts with a fresh cache instead of refusing its own
    first-boot host keys. The #245 ``instance`` stamp keeps the
    same separation for daemons that serve no id (the fallback keys
    on the typed reference). An unusable cache (the path taken by
    a file, an unwritable directory) is operator-shaped: one line,
    not a traceback.
    """
    directory = f"{workspace_id}.{instance}" if instance else workspace_id
    root = (base if base is not None else cache_dir()) / directory
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"msks ssh: cannot create {root}: {exc}") from exc
    return str(root / "known_hosts")

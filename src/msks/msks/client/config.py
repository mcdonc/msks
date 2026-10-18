"""The msks client's YAML configuration file (#314), modeled on
msksd's (#46) and klangk's ``klangk.yaml``.

A YAML file is the durable home for the client's settings; the
``MSKSC_*`` environment variables override file values, and the
built-in defaults are the floor — precedence **flag > environment >
per-alias file values > global file values > defaults**. The one
flag is ``--daemon <alias-or-url>``: an alias names an entry in the
file's ``daemons:`` section, a raw URL names the daemon outright,
and either form outranks the environment for the invocation it
appears on (an explicit per-invocation choice beats the ambient
exports — the reverse of the flag-less path, where the environment
wins so the devenv per-worktree presets keep working unchanged
beside a user config file).

The file carries one structural key the environment cannot:
``daemons:`` — named daemon aliases, each with its ``url`` (the
one required key), ``token_file``, ``cafile``, and an optional
``expected_image`` override. ``active_daemon:`` picks the default
alias. A token arrives as a **file reference** (``token_file``):
tokens are minted by the daemon and the dev daemon's token already
lives in a file, so a path keeps each secret's permissions on the
file that holds it and the config file stays free of inline
credentials — ``MSKSC_TOKEN`` still carries an inline token when
the environment is the more convenient place for one.

Key mapping (the daemon's convention, #46): a config key is its
``MSKSC_*`` variable with the prefix stripped and lowercased —
``MSKSC_URL`` → ``url``, ``MSKSC_EXPECTED_IMAGE`` →
``expected_image`` — with the two deliberate exceptions the
sectioned vocabulary adds: ``daemons``/``active_daemon`` (no
variable form) and ``token_file`` (the variable ``MSKSC_TOKEN``
carries the token itself, not a path).

Both spellings of a key are accepted — hyphens fold to
underscores before the walk (``token-file`` and ``token_file``
are one key; klangk's file spelled kebab, msks's variables spell
snake) — and one file cannot carry both spellings of the same
key: the second is a duplicate, refused with the rule named.

The file is located through three ``--config`` modes (msksd's):

- bare ``msks`` → ``$MSKSC_CONFIG_DIR/msks.yaml`` (default
  ``~/.config/msks/msks.yaml``); a missing file is generated as a
  commented template pointing at the docs.
- ``msks --config /path/to/msks.yaml`` → exactly that file; a
  missing file is an error. Explicit paths are never auto-generated.
- ``msks --config=none`` → environment variables and built-in
  defaults only.

``MSKSC_CONFIG_DIR`` is deliberately not a config key: the file
cannot relocate the config tree it lives in (klangkd's bootstrap
rule, shared with :mod:`msks.config`).

The resolved values are **materialized into the environment** by
:func:`bootstrap` — every existing ``os.environ`` reader
(:func:`msks.client.env.env_url`, the ssh state roots, the
expected-image drift check) keeps working unchanged, and only the
values the file or the flag contributed are written (an
environment-provided value is already there; a default is left to
the readers). ``terminal_open_cmd`` resolves the same way — variable,
then file, then the built-in ``xterm -e`` — and lands on the
returned :class:`ClientConfig`, where the workspace TUI's
new-terminal shell action (#341) reads it: the launcher the
action appends its ssh invocation to, falling back to the
same-terminal shell when the launcher cannot start.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..configio import UniqueKeyLoader, write_exclusive

# The ``--config=none`` sentinel: env vars + built-in defaults only.
NO_CONFIG = "none"

# The filename inside the config directory.
CONFIG_FILENAME = "msks.yaml"

#: The file's scalar global keys, each with the ``MSKSC_*``
#: variable that overrides it. ``token_file``'s variable carries
#: the token itself, not a path — the file form points at a file so
#: the config tree stays free of inline credentials (#314).
#: ``identity_file`` names the operator's own private key file
#: (#336) and is global-only (no per-alias form): an identity
#: belongs to the operator, not to a daemon connection.
#: ``pubkey_file`` names the operator's key's public half (#495)
#: and carries the same global-only rule for the same reason.
#: ``terminal_title`` names the title of the workspace-shell
#: windows the workspace page opens (#445) and is global-only:
#: the windows belong to the operator's desktop, not to a daemon
#: connection.
GLOBAL_ENV_VARS: dict[str, str] = {
    "url": "MSKSC_URL",
    "socket": "MSKSC_SOCKET",
    "token_file": "MSKSC_TOKEN",
    "cafile": "MSKSC_CAFILE",
    "expected_image": "MSKSC_EXPECTED_IMAGE",
    "cache_dir": "MSKSC_CACHE_DIR",
    "identity_file": "MSKSC_IDENTITY_FILE",
    "pubkey_file": "MSKSC_PUBKEY_FILE",
    "terminal_title": "MSKSC_TERMINAL_TITLE",
}

#: The keys one ``daemons:`` entry may carry. Exactly one of
#: ``url`` and ``socket`` is required — a remote daemon's address
#: or a local listener's socket path (#555); the rest override
#: their global forms for that alias. A ``socket`` entry needs no
#: ``token_file``: the local listener authenticates by peer
#: credential.
DAEMON_ENTRY_KEYS = ("url", "socket", "token_file", "cafile", "expected_image")

#: The terminal-launch setting's variable (#314): the string form
#: of the file value, overriding it for one shell.
TERMINAL_ENV_VAR = "MSKSC_TERMINAL_OPEN_CMD"

#: The workspace-shell window's title template (#445): the
#: variable the title writers read — the consent launcher inside
#: the window the prefix opened, and the appended ``msks ssh`` in
#: a window the TUI marked at spawn. A file's value reaches them
#: through :func:`apply`'s materialization, the way every
#: file-derived setting rides.
TITLE_ENV_VAR = "MSKSC_TERMINAL_TITLE"

#: The remembered-passthrough setting's variable (#385): the string
#: form of the file value (shell-split), overriding it for one
#: shell — the same shape :data:`TERMINAL_ENV_VAR` carries.
SSH_OPTIONS_ENV = "MSKSC_SSH_OPTIONS"

#: The built-in launcher (#314): ``xterm -e``, the one terminal
#: most Linux distributions carry, with the msks invocation
#: appended after ``-e``. A box without it hits the launch
#: action's unexecutable-launcher rule — an inline error, then the
#: same-terminal path — so the default is safe where xterm is
#: absent.
DEFAULT_TERMINAL_CMD = ("xterm", "-e")


def config_dir() -> str:
    """The config-tree root: ``$MSKSC_CONFIG_DIR``, else
    ``$XDG_CONFIG_HOME/msks`` (XDG fallback ``~/.config/msks``).

    Resolved purely from the environment — ``msks.yaml`` cannot
    relocate the config tree it lives in, so the root must be
    computable before the file is located (msksd's bootstrap rule,
    #46).
    """
    override = os.environ.get("MSKSC_CONFIG_DIR")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return str(Path(xdg).expanduser() / "msks")


def default_config_path() -> str:
    """The path a bare ``msks`` resolves: ``<config_dir>/msks.yaml``."""
    return os.path.join(config_dir(), CONFIG_FILENAME)


def resolve_config_path(config: str | None) -> str:
    """Resolve the ``--config`` value into a path or the sentinel.

    Three modes (msksd's, #46): ``None`` → the default path,
    generated as a near-empty template on first run; ``"none"`` →
    the env-only opt-out; a path → that file, required to exist.
    """
    if config is None:
        return ensure_default_config()
    if config == NO_CONFIG:
        return NO_CONFIG
    if Path(config).is_dir():
        raise ValueError(f"config path is a directory: {config}")
    if not Path(config).is_file():
        raise ValueError(f"config file not found: {config}")
    return config


def ensure_default_config() -> str:
    """The default path: generated on first run, else as-is.

    A concurrent ``msks`` generating the file between the check
    and the open is "the file is there now", not an error. A
    template that cannot be written (a read-only config tree) is
    a one-line refusal, not a traceback — the operator's fix is
    at the filesystem, and the line names the path.
    """
    path = default_config_path()
    if os.path.isfile(path):
        return path
    try:
        generate_template(path)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(
            f"cannot write the first-run config template at {path}: "
            f"{exc.strerror or exc}"
        ) from None
    return path


def generate_template(path: str) -> None:
    """Write the first-run ``msks.yaml`` template at *path*.

    The parent directory is created (0700) when missing and the
    file is written 0600 — the template's examples name token-file
    paths, so the file joins the house pattern of secret-bearing
    artifacts readable only by its owner. An existing file is the
    operator's config: the exclusive create refuses to overwrite.
    """
    write_exclusive(path, render_template())


def parse_config_doc(text: str, path: str) -> dict:
    """Parse config-file text into its validated key walk.

    Unknown keys are errors naming the key and the valid ones; a
    null value (``key:`` with nothing after it) is the unset form —
    the environment (or the default) applies, exactly as an unset
    variable would. Duplicate keys are refused by the loader
    (:class:`msks.configio.UniqueKeyLoader`), shared with msksd so
    the two files carry one duplicate-key story.
    """
    try:
        doc = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        kind = type(doc).__name__
        raise ValueError(
            f"{path}: the config file must be a mapping of keys, got {kind}"
        )
    return validated_doc(doc, path)


def normalized_key(key: str) -> str:
    """One key's canonical spelling: hyphens fold to underscores.

    klangk's file spelled its keys kebab (``terminal-open-cmd``);
    msks's variables spell snake (``terminal_open_cmd``). Both are
    accepted here, and the walk compares normalized spellings so
    either writes the same setting.
    """
    return key.replace("-", "_")


def validated_doc(doc: dict, path: str) -> dict:
    """The validated key walk: each key normalized, then checked."""
    out: dict = {}
    seen: set[str] = set()
    for key, value in doc.items():
        if not isinstance(key, str):
            raise ValueError(
                f"{path}: config keys must be strings, got {key!r}"
            )
        name = normalized_key(key)
        if name in seen:
            raise ValueError(
                f"{path}: duplicate config key {key!r} — hyphens "
                "and underscores spell the same key"
            )
        seen.add(name)
        out.update(validated_pair(name, key, value, path))
    return out


def validated_pair(key: str, spelling: str, value: object, path: str) -> dict:
    """One top-level key's checked {key: value} pair — *key* the
    normalized name, *spelling* the operator's own, echoed by the
    refusal."""
    pair = sectioned_pair(key, value, path)
    if pair is not None:
        return pair
    if key in GLOBAL_ENV_VARS:
        return optional_pair(key, scalar_value(key, value, path))
    valid = ", ".join(valid_top_level_keys())
    raise ValueError(
        f"{path}: unknown config key {spelling!r} (valid keys: "
        f"{valid}; hyphens and underscores spell the same key)"
    )


def sectioned_pair(key: str, value: object, path: str) -> dict | None:
    """The structural and list-valued keys' checked pairs — None
    when the key is none of them, handing the scalar globals and
    the refusal back to :func:`validated_pair`."""
    if key == "daemons":
        return {key: daemons_section(value, path)}
    if key == "active_daemon":
        return {key: active_value(value, path)}
    if key == "terminal_open_cmd":
        return optional_pair(key, terminal_value(value, path))
    if key == "ssh_options":
        return optional_pair(key, ssh_options_value(value, path))
    return None


def optional_pair(key: str, value) -> dict:
    """A pair whose unset form (null, empty) simply drops out."""
    return {key: value} if value is not None else {}


def valid_top_level_keys() -> list[str]:
    """Every accepted top-level key, sorted for the error message."""
    return sorted(
        [*GLOBAL_ENV_VARS, "daemons", "active_daemon", "ssh_options"]
    )


def scalar_value(key: str, value: object, path: str) -> str | None:
    """One scalar global key's value: a string, or the unset form.

    Every scalar key here names a URL, a path, a reference, or a
    window-title template — YAML's native numbers and booleans
    have no meaning for any of them, so a bare one is a typo held
    up at load instead of stringifying into a URL like ``8660``.
    Null and the empty string are the unset form (an explicit empty
    beats a silent default: neither overrides anything).
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        kind = type(value).__name__
        raise ValueError(
            f"{path}: config key {key!r} must be a string, got {kind}"
        )
    return value


def active_value(value: object, path: str) -> str:
    """``active_daemon``: a non-empty alias name."""
    if not isinstance(value, str) or not value:
        kind = type(value).__name__
        raise ValueError(
            f"{path}: active_daemon must be a daemon alias name, got {kind}"
        )
    return value


def daemons_section(value: object, path: str) -> dict[str, dict]:
    """The ``daemons:`` mapping: alias name → validated entry."""
    if not isinstance(value, dict):
        kind = type(value).__name__
        raise ValueError(
            f"{path}: daemons must be a mapping of alias names to "
            f"settings, got {kind}"
        )
    return {
        alias: daemon_entry(alias, entry, path)
        for alias, entry in value.items()
    }


def daemon_entry(alias: object, entry: object, path: str) -> dict:
    """One alias's settings: its keys, with the address rule —
    exactly one of ``url`` and ``socket`` (#555)."""
    if not isinstance(alias, str) or not alias:
        raise ValueError(
            f"{path}: daemon aliases must be names, got {alias!r}"
        )
    if not isinstance(entry, dict):
        kind = type(entry).__name__
        raise ValueError(
            f"{path}: daemon {alias!r} must be a mapping of settings, "
            f"got {kind}"
        )
    out = entry_keys(alias, entry, path)
    refuse_addressless_entry(out, alias, path)
    return out


def refuse_addressless_entry(out: dict, alias: str, path: str) -> None:
    """The one-address rule: an entry names a url or a socket,
    never both, never neither."""
    if "url" not in out and "socket" not in out:
        raise ValueError(f"{path}: daemon {alias!r} needs a url or a socket")
    if "url" in out and "socket" in out:
        raise ValueError(
            f"{path}: daemon {alias!r} carries both a url and a "
            "socket; a daemon is reached one way"
        )


def entry_keys(alias: str, entry: dict, path: str) -> dict:
    """The entry's carried keys, each normalized and checked
    against the four the section defines (a null or empty value is
    simply absent)."""
    out: dict = {}
    seen: set[str] = set()
    for key, value in entry.items():
        name = checked_entry_key(alias, key, seen, path)
        if scalar_value(name, value, path) is not None:
            out[name] = value
    return out


def checked_entry_key(alias: str, key: object, seen: set, path: str) -> str:
    """One entry key's normalized name — refused when it is not a
    string, unknown to the section, or the other spelling of a
    key the entry already carries."""
    if not isinstance(key, str):
        raise ValueError(
            f"{path}: daemon {alias!r} keys must be strings, got {key!r}"
        )
    name = normalized_key(key)
    if name in seen:
        raise ValueError(
            f"{path}: duplicate key {key!r} in daemon {alias!r} — "
            "hyphens and underscores spell the same key"
        )
    if name not in DAEMON_ENTRY_KEYS:
        valid = ", ".join(DAEMON_ENTRY_KEYS)
        raise ValueError(
            f"{path}: unknown key {key!r} in daemon {alias!r} "
            f"(valid keys: {valid})"
        )
    seen.add(name)
    return name


def terminal_value(value: object, path: str) -> list[str] | None:
    """``terminal_open_cmd``: a command prefix, string or list form.

    The string form is shell-split (the operator writes it the way
    the shell would take it); the list form is taken verbatim — no
    quoting to reason about. Either form must name at least one
    word: the msks invocation is appended after it (#314).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return terminal_string(value, path)
    if isinstance(value, list):
        return terminal_list(value, path)
    kind = type(value).__name__
    raise ValueError(
        f"{path}: terminal_open_cmd must be a string or a list of "
        f"strings, got {kind}"
    )


def terminal_string(value: str, path: str) -> list[str] | None:
    """The string form: shell-split, empty meaning unset."""
    if not value.strip():
        return None
    return split_words(value, "terminal_open_cmd", path)


def terminal_list(value: list, path: str) -> list[str]:
    """The list form: at least one non-empty string entry."""
    if not value:
        raise ValueError(
            f"{path}: terminal_open_cmd must name a command, got []"
        )
    return nonempty_words(value, "terminal_open_cmd", path)


def nonempty_words(value: list, key: str, path: str) -> list[str]:
    """A list form's entries: non-empty strings only."""
    words = []
    for word in value:
        if not isinstance(word, str) or not word:
            raise ValueError(
                f"{path}: {key} entries must be non-empty strings"
            )
        words.append(word)
    return words


def ssh_options_value(value: object, path: str) -> list[str] | None:
    """``ssh_options`` (#385): ssh option tokens, string or list
    form — the ssh options an operator would type after ``--`` on
    the ``msks ssh`` command line, remembered.

    The string form is shell-split (the operator writes it the way
    the shell would take it); the list form is taken verbatim —
    flag tokens like ``-A`` stay whole with no quoting to reason
    about. The empty list is the unset form: no options ride.
    """
    if value is None:
        return None
    words = ssh_options_words(value, path)
    if words is not None:
        refuse_separator(words, path)
    return words


def ssh_options_words(value: object, path: str) -> list[str] | None:
    """The value's token list in either form; the caller checks
    the separator."""
    if isinstance(value, str):
        return ssh_options_string(value, path)
    if isinstance(value, list):
        return nonempty_words(value, "ssh_options", path) or None
    kind = type(value).__name__
    raise ValueError(
        f"{path}: ssh_options must be a string or a list of strings, "
        f"got {kind}"
    )


def refuse_separator(words: list[str], path: str) -> None:
    """Refuse the ssh options/command separator among the
    remembered options: msks appends the list to its own options,
    so a remembered ``--`` would hand ssh msks's transport options
    and the workspace id as the remote command."""
    if "--" in words:
        raise ValueError(
            f"{path}: ssh_options carries the ssh options, not the "
            "-- separator — list the options themselves (-A, -o "
            "ServerAliveInterval=30)"
        )


def ssh_options_string(value: str, path: str) -> list[str] | None:
    """The string form: shell-split, empty meaning unset."""
    if not value.strip():
        return None
    return split_words(value, "ssh_options", path)


def split_words(value: str, key: str, path: str) -> list[str]:
    """Shell-split one string value, naming the key on bad quoting."""
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise ValueError(f"{path}: config key {key!r}: {exc}") from None


def load_config(path: str) -> dict:
    """Read and validate the config file at *path* into its keys.

    An unreadable file is a one-line refusal naming the path —
    the bootstrap contract every other config problem carries.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read config file {path}: {exc.strerror or exc}"
        ) from None
    return parse_config_doc(text, path)


@dataclass
class Selection:
    """Which daemon the invocation addresses (#314).

    ``alias`` names a ``daemons:`` entry (``None`` when the flag
    carried a raw URL or nothing selected an alias); ``flagged`` is
    True only for a ``--daemon`` selection — the flag's entry keys
    outrank the environment, an ``active_daemon`` selection's sit
    below it; ``url_override`` is a raw-URL flag's address.
    """

    alias: str | None
    flagged: bool
    url_override: str | None = None
    socket_override: str | None = None


def select_daemon(daemon_arg: str | None, doc: dict, path: str) -> Selection:
    """Resolve the ``--daemon`` flag and ``active_daemon`` into one
    selection, refusing names the file does not define."""
    if daemon_arg is None:
        return ambient_selection(doc, path)
    return flag_selection(daemon_arg, doc, path)


def ambient_selection(doc: dict, path: str) -> Selection:
    """The file's own pick: ``active_daemon``, or nothing."""
    active = doc.get("active_daemon")
    if active is None:
        return Selection(None, False, None, None)
    aliases = doc.get("daemons", {})
    if active not in aliases:
        raise ValueError(
            f"{path}: active_daemon {active!r} is not defined by "
            f"this file's daemons ({alias_list(aliases)})"
        )
    return Selection(active, False, None, None)


def flag_selection(daemon_arg: str, doc: dict, path: str) -> Selection:
    """The flag's pick: an alias the file defines first, else a
    raw URL, else an absolute path naming a local listener's
    socket (#555) (an alias named like a URL stays reachable —
    the table is consulted before the scheme sniff)."""
    aliases = doc.get("daemons", {})
    if daemon_arg in aliases:
        return Selection(daemon_arg, True, None, None)
    if "://" in daemon_arg:
        return Selection(None, True, daemon_arg, None)
    if daemon_arg.startswith("/"):
        return Selection(None, True, None, daemon_arg)
    raise ValueError(
        f"{path}: unknown daemon {daemon_arg!r} "
        f"(defined: {alias_list(aliases)})"
    )


def alias_list(aliases: dict) -> str:
    """The defined alias names, for a refusal's parenthetical."""
    return ", ".join(sorted(aliases)) or "none"


def file_layered(key: str, entry: dict | None, flagged: bool, doc: dict):
    """The file-side winner for one key: ``(value, beats_env)``.

    A flagged alias's entry key outranks the environment (the
    operator named the daemon for this invocation); every other
    file value — an ``active_daemon`` entry's, a global — sits
    below it.
    """
    if entry is not None and key in entry:
        return entry[key], flagged
    return doc.get(key), False


def connect_setting(key: str, entry: dict | None, flagged: bool, doc: dict):
    """One URL-shaped key's winner: the env-form value when the
    environment has one (same semantics both sides), else the
    file-side winner."""
    value, beats_env = file_layered(key, entry, flagged, doc)
    if beats_env:
        return value, True
    env = os.environ.get(GLOBAL_ENV_VARS[key], "")
    if env:
        return env, False
    return value, value is not None


def token_setting(entry: dict | None, flagged: bool, doc: dict, path: str):
    """The bearer token: ``(token, from_file)``.

    ``MSKSC_TOKEN`` carries the token itself, so the environment
    wins without any path reading; ``token_file`` names a file on
    the file side only, read here — before any network activity —
    so an unreadable or empty file is a load-time error naming both
    places a token can come from.
    """
    token_file, beats_env = file_layered("token_file", entry, flagged, doc)
    if beats_env:
        return read_token_file(token_file, path), True
    env = os.environ.get("MSKSC_TOKEN", "")
    if env:
        return env, False
    if token_file is not None:
        return read_token_file(token_file, path), True
    return None, False


def read_token_file(token_file: str, path: str) -> str:
    """The stripped token inside ``token_file`` (#314).

    A missing or unreadable file names both places a token can
    come from — the file key and the variable — the same pair
    :func:`msks.client.env.env_token` names when nothing at all
    provides one.
    """
    target = Path(token_file).expanduser()
    try:
        token = target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(
            f"cannot read token file {target} ({exc.strerror or exc}); "
            f"point token_file in {path} at a readable token file, or "
            "export MSKSC_TOKEN"
        ) from None
    if not token:
        raise ValueError(
            f"token file {target} is empty; point token_file in {path} "
            "at a token file with a token in it, or export MSKSC_TOKEN"
        )
    return token


def global_or_env(key: str, doc: dict):
    """A global-only key's winner (``cache_dir``,
    ``identity_file``): the environment first, then the file's
    global value."""
    env = os.environ.get(GLOBAL_ENV_VARS[key], "")
    if env:
        return env, False
    value = doc.get(key)
    return value, value is not None


def terminal_command(doc: dict) -> list[str]:
    """The terminal launcher (#314): the variable's string form,
    else the file's value, else the built-in ``xterm -e`` — so the
    new-window path works without configuration, and a box
    without xterm falls back to the same-terminal path at launch.
    The workspace TUI's new-terminal shell action (#341) consumes
    the value from the resolved :class:`ClientConfig`."""
    env = os.environ.get(TERMINAL_ENV_VAR, "")
    if env.strip():
        return split_words(env, TERMINAL_ENV_VAR, "environment")
    return doc.get("terminal_open_cmd") or list(DEFAULT_TERMINAL_CMD)


def resolved_ssh_options(doc: dict, layer: dict) -> list[str]:
    """The remembered ssh passthrough (#385): the variable's string
    form, else the file's value, else none. The file's winner is
    materialized into the environment (shell-joined — a faithful
    round trip through the reader's shell split) because the
    readers live deep in the ssh and rsync call paths, the same
    substrate :func:`apply` gives every other file-derived value —
    and the terminal launcher's child and the ProxyCommand child
    inherit it whole."""
    env = os.environ.get(SSH_OPTIONS_ENV, "")
    if env.strip():
        return split_words(env, SSH_OPTIONS_ENV, "environment")
    options = doc.get("ssh_options") or []
    if options:
        layer[SSH_OPTIONS_ENV] = shlex.join(options)
    return list(options)


@dataclass
class ClientConfig:
    """One invocation's resolved client settings (#314).

    ``env_layer`` carries the values the file or the ``--daemon``
    flag contributed, keyed by their ``MSKSC_*`` variables — the
    pairs :func:`apply` materializes into the environment. An
    environment-provided value never appears (it is already there);
    a built-in default never appears (the readers hold the floor).
    """

    url: str
    socket: str
    token: str | None
    cafile: str
    expected_image: str
    cache_dir: str | None
    identity_file: str | None
    pubkey_file: str | None
    terminal_title: str | None
    terminal_open_cmd: list[str]
    ssh_options: list[str]
    daemon: str | None
    env_layer: dict[str, str] = field(default_factory=dict)


def resolve(
    daemon: str | None = None, config: str | None = None
) -> ClientConfig:
    """Resolve the config file, the environment, and the flags
    into one :class:`ClientConfig` (#314).

    Pure apart from the token-file read and the first-run template
    generation — the environment is read, never written; callers
    that want the readers to see the result pass the outcome to
    :func:`apply`.
    """
    path = resolve_config_path(config)
    doc = {} if path == NO_CONFIG else load_config(path)
    selection = select_daemon(daemon, doc, path)
    entry = selection_entry(doc, selection)
    layer: dict[str, str] = {}
    url, socket = resolved_address(entry, selection, doc, layer)
    token = resolved_token(entry, selection.flagged, doc, path, layer)
    cafile = resolved_scalar("cafile", entry, selection, doc, layer)
    image = resolved_scalar("expected_image", entry, selection, doc, layer)
    cache = resolved_global("cache_dir", doc, layer)
    identity = resolved_global("identity_file", doc, layer)
    pubkey = resolved_global("pubkey_file", doc, layer)
    title = resolved_global("terminal_title", doc, layer)
    return ClientConfig(
        url=url,
        socket=socket,
        token=token,
        cafile=cafile or "",
        expected_image=image or "",
        cache_dir=cache,
        identity_file=identity,
        pubkey_file=pubkey,
        terminal_title=title,
        terminal_open_cmd=terminal_command(doc),
        ssh_options=resolved_ssh_options(doc, layer),
        daemon=selection.alias,
        env_layer=layer,
    )


def selection_entry(doc: dict, selection: Selection) -> dict | None:
    """The selected alias's entry, when a selection named one."""
    if selection.alias is None:
        return None
    return doc.get("daemons", {}).get(selection.alias)


def resolved_address(
    entry: dict | None, selection: Selection, doc: dict, layer: dict
) -> tuple[str, str]:
    """The daemon's address as ``(url, socket)`` — exactly one
    non-empty when something named a daemon, both empty when
    nothing did (the readers then look for the default socket,
    #555).

    A raw-URL flag wins outright, and so does an absolute-path
    flag (its socket form). Below the flags, when both a url and
    a socket hold the same tier — both in the environment, or
    both in the file — the url wins; when the socket holds the
    higher tier (a flagged alias carries ``socket`` against an
    environment ``MSKSC_URL``), the socket wins: the flag's
    entry outranks the environment, the same rule every entry
    key carries.
    """
    if selection.url_override is not None:
        layer["MSKSC_URL"] = selection.url_override.rstrip("/")
        return selection.url_override.rstrip("/"), ""
    if selection.socket_override is not None:
        layer["MSKSC_SOCKET"] = selection.socket_override
        return "", selection.socket_override
    url, url_from_file = connect_setting("url", entry, selection.flagged, doc)
    sock, sock_from_file = connect_setting(
        "socket", entry, selection.flagged, doc
    )
    return layered_address(url, url_from_file, sock, sock_from_file, layer)


def layered_address(
    url: str | None,
    url_from_file: bool,
    sock: str | None,
    sock_from_file: bool,
    layer: dict[str, str],
) -> tuple[str, str]:
    """The tiered pick below the flags, filed into *layer* for
    :func:`apply` when the file provided it."""
    url, sock = tiered_pick(url, url_from_file, sock, sock_from_file)
    if url and url_from_file:
        layer["MSKSC_URL"] = url
    if sock and sock_from_file:
        layer["MSKSC_SOCKET"] = sock
    return url, sock


def tiered_pick(
    url: str | None,
    url_from_file: bool,
    sock: str | None,
    sock_from_file: bool,
) -> tuple[str, str]:
    """One address: the higher tier wins, and a tie goes to the
    url — both in the environment, or both in the file, the url
    is the pick (it names a daemon anywhere; a socket only this
    host)."""
    if url is None:
        return "", sock or ""
    if sock is not None and socket_wins(url_from_file, sock_from_file):
        return "", sock
    return url.rstrip("/"), ""


def socket_wins(url_from_file: bool, sock_from_file: bool) -> bool:
    """The socket takes the address when it holds the higher tier:
    the file provided it against an environment-provided url (a
    flagged entry outranks the environment, the rule every entry
    key carries)."""
    return sock_from_file and not url_from_file


def resolved_token(
    entry: dict | None,
    flagged: bool,
    doc: dict,
    path: str,
    layer: dict,
):
    """The bearer token, filed for materialization when the file
    or the flag provided it."""
    token, from_file = token_setting(entry, flagged, doc, path)
    if from_file and token:
        layer["MSKSC_TOKEN"] = token
    return token


def resolved_scalar(
    key: str,
    entry: dict | None,
    selection: Selection,
    doc: dict,
    layer: dict,
):
    """One URL-shaped key's winner (``cafile``,
    ``expected_image``), filed when the file or the flag provided
    it."""
    value, from_file = connect_setting(key, entry, selection.flagged, doc)
    if from_file:
        layer[GLOBAL_ENV_VARS[key]] = value
    return value


def resolved_global(key: str, doc: dict, layer: dict):
    """One global-only key's winner (``cache_dir``,
    ``identity_file``), filed when the file provided it."""
    value, from_file = global_or_env(key, doc)
    if from_file and value is not None:
        layer[GLOBAL_ENV_VARS[key]] = value
    return value


def apply(conf: ClientConfig) -> None:
    """Materialize the file-derived winners into the environment.

    The readers (:func:`msks.client.env.env_url` and its kin) stay
    environment-driven — one materialization point instead of a
    config object threaded through every call, and a value the
    environment already provided is never rewritten (it won its
    layer; writing it back would only blur provenance).
    """
    for name, value in conf.env_layer.items():
        os.environ[name] = value


def render_template() -> str:
    """The generated ``msks.yaml`` body: a commented near-empty file.

    The template's purpose is discoverability — this is where the
    client's config lives — plus a commented example of every key.
    The settings themselves come from the built-in defaults and the
    environment until the operator edits the file.
    """
    return """\
# msks client configuration — generated on first run.
#
# msks looked here because it was started without a --config
# argument and found no file at
# ${{MSKSC_CONFIG_DIR:-$XDG_CONFIG_HOME/msks}}/msks.yaml.
#
# This file is the durable home for the client's settings. Every
# scalar key here also exists as an MSKSC_* environment variable,
# spelled the same with the prefix stripped and lowercased
# (MSKSC_URL -> url, MSKSC_EXPECTED_IMAGE -> expected_image).
# Hyphens and underscores spell the same key (terminal-open-cmd
# and terminal_open_cmd are one key; a file that carries both
# spellings is refused). Precedence, highest first:
#   --daemon flag > MSKSC_* environment > this file's daemons
#   entries > this file's global keys > built-in defaults
#
# A key set to nothing (key: with no value, or "") is the unset
# form: the environment (or the default) applies.
#
# The full reference — every key, alias, and the precedence rule
# with examples — is docs/cli.md in the msks repository:
# https://github.com/mcdonc/msks
#
# --- Example (every line commented; nothing is set) ---
#
# --- Global keys ---
# url: https://127.0.0.1:8660    # the daemon's base URL
# token_file: ~/.config/msks/dev.token  # a file holding one daemon
#                                # token (the config tree stays free
#                                # of inline credentials); the
#                                # MSKSC_TOKEN variable still carries
#                                # an inline token
# cafile: ~/.config/msks/msks-ca.pem  # the CA that verifies the
#                                # daemon's certificate
# expected_image: msks/debian13:1.0  # the image msks ls compares
#                                # against the daemon's /health (#160)
# cache_dir: ~/.cache/msks      # per-workspace host-key caches (#251)
# identity_file: ~/.ssh/id_ed25519  # your own private key — the ssh
#                                # identity a bare msks create plants
#                                # into every workspace (#336); msks
#                                # reads it in place and copies it
#                                # nowhere. Global-only: a per-alias
#                                # identity_file is refused
# pubkey_file: ~/.ssh/id_ed25519.pub  # your key's public half —
#                                # what a bare create plants instead
#                                # of reading identity_file (#495):
#                                # the form for a key you keep
#                                # encrypted (your agent holds it;
#                                # msks never reads the private half).
#                                # --pubkey overrides it for one
#                                # create. Global-only, like
#                                # identity_file
# terminal_open_cmd: konsole -e # the launcher the workspace page's
#                                # new-terminal shell action opens
#                                # workspace shells through (#314,
#                                # #341); string form is shell-split,
#                                # a list form carries its words as
#                                # written; unset -> xterm -e, the
#                                # terminal most Linuxes carry
# terminal_title: msks — {workspace} # the title of the workspace-shell
#                                # windows the workspace page opens
#                                # (#445); {workspace} resolves to the
#                                # workspace's id; unset leaves the
#                                # terminal emulator's own title
# ssh_options:                  # ssh options remembered for every ssh
#                                # session msks runs — msks ssh, the
#                                # new-terminal shell, msks rsync's
#                                # transport (#385). The tokens are
#                                # the ones you would type after --
#                                # on the command line, and the
#                                # command line still overrides them;
#                                # MSKSC_SSH_OPTIONS carries the string
#                                # form for one shell
# ssh_options: -A -o ServerAliveInterval=30   # string (shell-split)
# ssh_options:                  # list form (tokens stay whole)
#   - -A
#   - -o
#   - ServerAliveInterval=30
#
# --- Named daemon aliases ---
# One entry per daemon you talk to; exactly one of url and socket
# is required (#555) — a remote daemon's address, or a local
# listener's socket path (peer-credential auth: no token_file,
# no cafile). The rest override their global keys for that alias.
# daemons:
#   dev:
#     socket: /run/msks/msksd.sock   # the local listener this
#                                    # host's daemon serves
#   lab:
#     url: https://hv-1.lab.example.com:8660
#     token_file: ~/.config/msks/lab.token
#     cafile: ~/.config/msks/lab-ca.pem
#     expected_image: msks/debian13:1.1
#
# active_daemon: lab           # the alias a bare msks addresses;
#                              # unset, a bare msks looks for the
#                              # default local socket first (#555)
"""


def bootstrap(daemon: str | None = None, config: str | None = None):
    """The CLI's config entry: resolve, then materialize (#314).

    A config problem the operator should see as one line — an
    unreadable file, a malformed document, an unknown key or alias,
    an unreadable token file — arrives as ``SystemExit``; the
    callers' conventions make that a clean exit with the line.
    """
    try:
        conf = resolve(daemon, config)
    except ValueError as exc:
        raise SystemExit(f"msks: {exc}") from None
    apply(conf)
    return conf

"""The msks.yaml grammar: parse and validate.

Every key the client's config file carries, validated as the
parser walks it — unique keys, known spellings, scalar shapes,
and the daemons section's entry grammar — so a bad file refuses
with the key and the reason.
"""

from __future__ import annotations

import shlex

import yaml

from ..configio import UniqueKeyLoader
from .config_keys import (
    DAEMON_ENTRY_KEYS,
    GLOBAL_ENV_VARS,
)


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

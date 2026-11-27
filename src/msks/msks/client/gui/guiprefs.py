"""The GUI's preferences file (#1214): ``gui.yaml``, the
user-writable file the settings page saves the dictate pill's
settings into. Qt-free, over the client's own config layer.

The file sits beside the operator's ``msks.yaml`` — under
``MSKSC_CONFIG_DIR`` when set, else ``$XDG_CONFIG_HOME/msks``
(``~/.config/msks`` by default) — and the page never writes the
operator's file: on NixOS that one is generated into
``/etc/msks.yaml``, is read-only, and stays declarative. For the
dictation keys the file stands between the environment and the
operator's file (:mod:`msks.client.gui.dictatekeys`).

The grammar is the operator file's own discipline on a smaller
vocabulary: a flat mapping of the dictation keys, hyphens and
underscores spelling the same key, duplicates and unknown keys
refused at load, every value through the validator's reader. The
write is :func:`msks.configio.write_atomic`: a reader never sees
half a file, and a crash leaves the old one whole.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ...configio import write_atomic
from ..config import config_dir
from ..config_keys import DICTATE_KEYS
from ..validate import load_mapping, normalized_key, validated_pair

#: The file's name under the client config directory (#1214).
PREFS_FILENAME = "gui.yaml"


def prefs_path() -> Path:
    """Where the preferences file lives (#1214): the client config
    directory the operator's ``msks.yaml`` resolves through."""
    return Path(config_dir()) / PREFS_FILENAME


def parse_prefs(text: str, path: str) -> dict:
    """The preferences text into its validated keys (#1214): the
    config file's own walk over the dictation vocabulary — a
    unique-key load, both spellings one key, a null value the
    unset form — refusing an unknown key or a bad value with the
    key and the file named."""
    doc = load_mapping(text, path, "preferences file")
    out: dict = {}
    seen: set = set()
    for spelling, value in doc.items():
        if not isinstance(spelling, str):
            raise ValueError(
                f"{path}: preferences keys must be strings, got {spelling!r}"
            )
        key = normalized_key(spelling)
        if key in seen:
            raise ValueError(
                f"{path}: duplicate preferences key {spelling!r} — "
                "hyphens and underscores spell the same key"
            )
        seen.add(key)
        if key not in DICTATE_KEYS:
            valid = ", ".join(DICTATE_KEYS)
            raise ValueError(
                f"{path}: unknown preferences key {spelling!r} (valid keys: "
                f"{valid}; hyphens and underscores spell the same key)"
            )
        out.update(validated_pair(key, spelling, value, path))
    return out


def load_prefs(path: Path | None = None) -> dict:
    """The standing preferences (#1214) — the validated document,
    or an empty one when no file exists yet. An unreadable file or
    a bad document is a ``ValueError`` naming the path, the
    settings page's to show."""
    target = prefs_path() if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(
            f"cannot read preferences file {target}: {exc.strerror or exc}"
        ) from None
    return parse_prefs(text, str(target))


def dump_prefs(values: dict) -> str:
    """The preferences document's text (#1214): the dictation
    keys that carry a value, in key order, a null or blank one
    dropped — the unset form reads as the layer under it."""
    kept = {
        key: values[key]
        for key in DICTATE_KEYS
        if values.get(key) is not None and values[key] != ""
    }
    return yaml.safe_dump(kept, sort_keys=False, default_flow_style=False)


def save_prefs(values: dict, path: Path | None = None) -> None:
    """Write the preferences (#1214) whole, through the shared
    atomic replace: the file carries exactly ``values``."""
    target = prefs_path() if path is None else path
    write_atomic(str(target), dump_prefs(values))

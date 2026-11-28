"""The dictate pill's settings (#1214): the three keys' resolution
and the record the pill, the settings page, and the transcription
read. Plain data over the client's own config layer — no Qt in
it.

The dictation keys resolve like every other client key (#1224):
the ``MSKSC_DICTATE_*`` variable, then the merged ``msks.yaml``
document (the user's file over the system's, #1224), then the
default. Highest first:

```text
MSKSC_DICTATE_* environment > msks.yaml (user over system) > the defaults
```

The settings page writes the keys into the user's ``msks.yaml``
in place (:func:`msks.client.config.save_keys`) and re-reads the
merged document; the environment still pins a key above the file,
the way it does for every other client setting.

A bad value never reaches this module: the loader refuses the
file at bootstrap (a non-string key value is a one-line refusal
with the path), and a variable is always a string. The record
carries the pin, not a problem.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config_keys import DICTATE_KEYS, GLOBAL_ENV_VARS
from ..dictate import DEFAULT_LANG, DEFAULT_MODEL, model_path

#: The settings tab's label (#1214): the strip's fourth tab, the
#: page that edits these keys. A plain word in this Qt-free leaf,
#: so the window path (``app.py``, Qt-free at parse time) and the
#: strip's module read one definition.
SETTINGS_TAB_LABEL = "Settings"

#: The string keys (#1214): the model (a ggml name or a path),
#: the capture device (a ``pw-record`` target), and the
#: vocabulary prompt. The empty string is their unset form.
STRING_KEYS = ("dictate_model", "dictate_device", "dictate_prompt")


@dataclass(frozen=True)
class DictateSettings:
    """The resolved settings (#1214): the model, the device, and
    the prompt — plus the record the settings page shows beside
    the fields: the keys the environment pinned. The speech gate's
    floor and margin are the leaf's own constants: nobody tunes
    them, and the tracked floor was the spike's fix for a
    speakerphone."""

    model: str = DEFAULT_MODEL
    device: str = ""
    prompt: str = ""
    lang: str = DEFAULT_LANG
    #: The keys a variable pinned (#1214): the page shows their
    #: fields read-only, with the variable named.
    pinned: frozenset = frozenset()

    def model_file(self, cache_root: Path) -> Path:
        """Where the model setting points under the client cache
        root: the cache's whisper directory for a name, the path
        itself for a path."""
        return model_path(self.model, cache_root)

    def value(self, key: str):
        """One key's resolved value, for the page's fields."""
        return getattr(self, key.removeprefix("dictate_"))


def read_value(value) -> str | None:
    """One layer's value for one key: the stripped string, None for
    the unset form (absent, null, blank). Both layers carry
    strings by construction — the loader refuses anything else in
    the file, and a variable is text."""
    if value is None:
        return None
    return value.strip() or None


def environment_layer(key: str, environ):
    """The environment's raw text (#1214): the variable's value."""
    return environ.get(GLOBAL_ENV_VARS[key])


def operator_layer(key: str, operator: dict):
    """The merged config document (#1214, #1224): ``msks.yaml``, the
    user's keys over the system's."""
    return operator.get(key)


def resolve_key(key: str, environ, operator: dict) -> tuple[str | None, bool]:
    """One key's walk down the two layers (#1214, #1224): the first
    layer that carries a value wins; a blank moves on.
    ``(value, pinned)``, the second True only for an environment
    win."""
    value = read_value(environment_layer(key, environ))
    if value is not None:
        return value, True
    return read_value(operator_layer(key, operator)), False


def resolve(environ=None, operator: dict | None = None) -> DictateSettings:
    """The settings under the dictation precedence (#1214, #1224):
    ``environ`` (the process environment by default) and the merged
    config document, each layer's winner read through
    :func:`read_value`."""
    environ = os.environ if environ is None else environ
    operator = {} if operator is None else operator
    values: dict = {}
    pinned: set = set()
    for key in DICTATE_KEYS:
        take_key(key, resolve_key(key, environ, operator), values, pinned)
    return settings_from(values, frozenset(pinned))


def take_key(key: str, resolved: tuple, values: dict, pinned: set) -> None:
    """One key's resolved pair into the collections (#1214): an
    unset key lands nowhere, a value lands in the values and — for
    an environment win — in the pinned set."""
    value, from_env = resolved
    if value is None:
        return
    values[key] = value
    if from_env:
        pinned.add(key)


def settings_from(values: dict, pinned: frozenset) -> DictateSettings:
    """The record from the resolved values (#1214): the strings
    with their defaults for the keys no layer carried."""
    return DictateSettings(
        model=values.get("dictate_model") or DEFAULT_MODEL,
        device=values.get("dictate_device") or "",
        prompt=values.get("dictate_prompt") or "",
        pinned=pinned,
    )

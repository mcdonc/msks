"""The dictate pill's settings (#1214): the five keys' resolution
and the record the pill, the settings page, and the transcription
read. Plain data over the client's own config layer — no Qt in
it.

The dictation keys carry one layer more than every other client
key. Precedence, highest first:

```text
MSKSC_DICTATE_* environment > gui.yaml > the operator's msks.yaml
    > the defaults
```

The settings page writes ``gui.yaml`` and never the operator's
file — on NixOS that file is generated into ``/etc/msks.yaml`` and
stays declarative — so a value the page saves beats the operator's
file, and the environment still pins a key above both, the way it
does for every other client setting.

A value that refuses (a key whose value is the wrong shape) is a
*problem*: the key reads as its default, the refusal names the
variable or file it came from, and the settings page shows it. A
bad pin is never masked by the layer under it.
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
    the prompt — plus the two records the settings page shows
    beside the fields: the keys the environment pinned, and the
    keys whose value refused. The speech gate's floor and margin
    are the leaf's own constants: nobody tunes them, and the
    tracked floor was the spike's fix for a speakerphone."""

    model: str = DEFAULT_MODEL
    device: str = ""
    prompt: str = ""
    lang: str = DEFAULT_LANG
    #: The keys a variable pinned (#1214): the page shows their
    #: fields read-only, with the variable named.
    pinned: frozenset = frozenset()
    #: Each refusal as ``(key, message)`` (#1214), in key order.
    problems: tuple = ()

    def model_file(self, cache_root: Path) -> Path:
        """Where the model setting points under the client cache
        root: the cache's whisper directory for a name, the path
        itself for a path."""
        return model_path(self.model, cache_root)

    def value(self, key: str):
        """One key's resolved value, for the page's fields."""
        return getattr(self, key.removeprefix("dictate_"))


def read_value(key: str, value, source: str):
    """One key's value from one layer, validated: a stripped
    string, None for the unset form (null, blank). The refusal
    names the source."""
    if value is None:
        return None
    if not isinstance(value, str):
        kind = type(value).__name__
        raise ValueError(
            f"{source}: config key {key!r} must be a string, got {kind}"
        )
    text = value.strip()
    return text or None


#: The three layers (#1214), highest first: the environment's
#: variable, the GUI's own file, and the operator's config. A
#: layer's entry names its source for the refusal and the getter
#: that reads it off the layer's own mapping.
def environment_layer(key: str, environ):
    """The environment's raw text (#1214): the variable's value,
    its name the refusal's source."""
    return environ.get(GLOBAL_ENV_VARS[key]), GLOBAL_ENV_VARS[key]


def prefs_layer(key: str, prefs: dict):
    """The GUI's own file (#1214): ``gui.yaml`` the refusal's
    source."""
    return prefs.get(key), "gui.yaml"


def operator_layer(key: str, operator: dict):
    """The operator's config file (#1214): ``msks.yaml`` the
    refusal's source."""
    return operator.get(key), "msks.yaml"


def read_layer(key: str, raw, source: str):
    """One layer's value read through the validator (#1214):
    ``(value, problem)`` — a refusal names the source and the
    value is None; a blank reads as the unset form."""
    if raw is None or raw == "":
        return None, None
    try:
        return read_value(key, raw, source), None
    except ValueError as exc:
        return None, str(exc)


def resolve_key(key: str, environ, prefs: dict, operator: dict):
    """One key's walk down the layers (#1214): the first layer
    that carries a value wins — a refusal there stops the walk
    (the layers under it never mask a bad pin) and reads as a
    problem; a blank moves on. ``(value, pinned, problem)``, the
    middle True only for an environment win."""
    getters = (
        (environment_layer, environ),
        (prefs_layer, prefs),
        (operator_layer, operator),
    )
    for getter, mapping in getters:
        raw, source = getter(key, mapping)
        value, problem = read_layer(key, raw, source)
        if problem is not None:
            return None, False, (key, problem)
        if value is not None:
            return value, getter is environment_layer, None
    return None, False, None


def resolve(
    environ=None, prefs: dict | None = None, operator: dict | None = None
) -> DictateSettings:
    """The settings under the dictation precedence (#1214):
    ``environ`` (the process environment by default), the
    ``gui.yaml`` document, and the operator config's validated
    keys — each layer's winner read through :func:`read_value`.
    A refusal at any layer records a problem and the key takes
    its default."""
    environ = os.environ if environ is None else environ
    prefs = {} if prefs is None else prefs
    operator = {} if operator is None else operator
    values: dict = {}
    pinned: set = set()
    problems: list = []
    for key in DICTATE_KEYS:
        resolved = resolve_key(key, environ, prefs, operator)
        take_key(key, resolved, values, pinned, problems)
    return settings_from(values, frozenset(pinned), tuple(problems))


def take_key(key: str, resolved: tuple, values: dict, pinned: set, problems):
    """One key's resolved triple into the collections (#1214): a
    problem is recorded and nothing else, a value lands in the
    values and — for an environment win — in the pinned set, and
    an unset key lands nowhere."""
    value, from_env, problem = resolved
    if problem is not None:
        problems.append(problem)
        return
    if value is None:
        return
    values[key] = value
    if from_env:
        pinned.add(key)


def settings_from(
    values: dict, pinned: frozenset, problems: tuple
) -> DictateSettings:
    """The record from the resolved values (#1214): the strings
    with their defaults for the keys no layer carried."""
    return DictateSettings(
        model=values.get("dictate_model") or DEFAULT_MODEL,
        device=values.get("dictate_device") or "",
        prompt=values.get("dictate_prompt") or "",
        pinned=pinned,
        problems=problems,
    )

"""Model-list entry parsing (ported from klangk).

Entries arrive as ``provider/model:api_base:api_key`` strings or
LiteLLM-native dicts; both become ``model_list`` rows, with
secret-bearing halves resolved through :mod:`.indirect`.
"""

from __future__ import annotations

import copy
from typing import Any

from .indirect import resolve_indirection

# Provider defaults: well-known providers whose api_base can be
# omitted from an entry (ported from klangk).
PROVIDER_DEFAULTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "cohere": "https://api.cohere.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together_ai": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks_ai": "https://api.fireworks.ai/inference/v1",
}

# Strong references for fire-and-forget client-close tasks (ported
# from klangk #2928): an unreferenced task suspended in an await is
# GC-eligible mid-execution, which would leave the replaced

# The secret-indirection markers: an entry whose key half starts
# with one of these keeps everything from the marker (a path or
# command may itself contain colons).
INDIRECT_MARKERS = ("file:", "cmd:")


def find_marker(rest: str) -> int:
    """The latest indirection marker in *rest*, -1 when none."""
    best = -1
    for marker in INDIRECT_MARKERS:
        idx = rest.rfind(marker)
        if idx > best:
            best = idx
    return best


def split_entry(entry: str) -> tuple[str, str, str]:
    """``(litellm_model, api_base, api_key)``: the **first** colon
    bounds the model, the rest splits three ways — an indirection
    marker (``file:``/``cmd:``) bounds the key so a path or command
    may carry colons; otherwise the colon that bounds the key is
    the last one in the base's **path** portion (a scheme-bearing
    base's authority may carry a port, and an authority with no
    path names no key at all), or the last colon overall when the
    base carries no scheme."""
    first_colon = entry.find(":")
    if first_colon == -1:
        return entry, "", ""
    litellm_model = entry[:first_colon]
    rest = entry[first_colon + 1 :]
    marker = find_marker(rest)
    if marker > 0:
        return litellm_model, rest[: marker - 1], rest[marker:]
    if marker == 0:
        return litellm_model, "", rest
    sep = key_separator(rest)
    if sep == -1:
        return litellm_model, rest, ""
    return litellm_model, rest[:sep], rest[sep + 1 :]


def key_separator(rest: str) -> int:
    """The index of the colon that bounds the key in *rest*, -1
    when the base takes it all: with a scheme, only a colon in the
    path portion (at or after the authority's closing slash)
    separates a key — the authority's own colon is a port; without
    one, the last colon does."""
    last = rest.rfind(":")
    scheme = rest.find("://")
    if scheme == -1:
        return last
    path_start = rest.find("/", scheme + 3)
    if path_start == -1 or last < path_start:
        return -1
    return last


def resolve_api_base(api_base: str, litellm_model: str) -> str:
    """A known provider's default base URL when none was given."""
    if "/" not in litellm_model:
        return api_base
    provider = litellm_model.split("/", 1)[0]
    if api_base or provider not in PROVIDER_DEFAULTS:
        return api_base
    return PROVIDER_DEFAULTS[provider]


def parse_model_entry(entry: str) -> dict:
    """Parse one ``provider/model:api_base:api_key`` string into a
    litellm ``model_list`` entry. Secret-bearing halves resolve
    indirection here, so the built list never carries a ``file:``
    or ``cmd:`` spelling."""
    litellm_model, api_base, api_key = split_entry(entry)
    if api_key:
        api_key = resolve_indirection(api_key, f"model entry {entry!r}")
    api_base = resolve_api_base(api_base, litellm_model)
    model_name = (
        litellm_model.split("/", 1)[1]
        if "/" in litellm_model
        else litellm_model
    )
    params: dict[str, Any] = {"model": litellm_model}
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key
    return {"model_name": model_name, "litellm_params": params}


# litellm_params keys whose values may carry secrets and so
# resolve file:/cmd: indirection inside dict entries too.
INDIRECT_KEYS = frozenset({"api_key", "api_base"})


def normalize_key(key: str) -> str:
    """Kebab-case to snake_case (the file's dict entries accept
    both spellings, klangk's shape)."""
    return key.replace("-", "_")


def normalize_params(params: dict) -> dict:
    """One ``litellm_params`` block: kebab→snake keys, indirection
    resolved on the secret-bearing values."""
    normalized = {}
    for key, value in params.items():
        name = normalize_key(key)
        if name in INDIRECT_KEYS and isinstance(value, str):
            value = resolve_indirection(value, name) or ""
        normalized[name] = value
    return normalized


def normalize_dict_entry(entry: dict) -> dict:
    """One LiteLLM-native dict entry: kebab- or snake-case keys at
    the top level (``model-name``/``model_name``), ``params`` as the
    ``litellm_params`` shorthand, and the params block normalized
    (ported from klangk — the file's list form carries these). The
    copy is deep: the Router owns its model_list outright, and a
    configure retry must see the entry exactly as the file wrote
    it — depth-2 sub-objects included."""
    return _normalized_entry(copy.deepcopy(entry))


def _normalized_entry(entry: dict) -> dict:
    """The normalization walk over a shallow copy of *entry*."""
    normalized = {}
    for key, value in entry.items():
        name = normalize_key(key)
        if name == "params":
            name = "litellm_params"
        if name == "litellm_params" and isinstance(value, dict):
            normalized[name] = normalize_params(value)
        else:
            normalized[name] = value
    return normalized


def build_model_list(
    entries: tuple[str | dict, ...], default_api_key: str
) -> list[dict]:
    """The litellm model_list from the settings' entries — strings
    parsed, dicts normalized; the default key fills every entry that
    named none (``setdefault`` attaches the block, so an entry that
    arrived without one still takes the key — klangk's original
    wrote it into a detached dict and lost it)."""
    items = []
    for entry in entries:
        if isinstance(entry, dict):
            parsed = normalize_dict_entry(entry)
        else:
            parsed = parse_model_entry(entry)
        params = parsed.setdefault("litellm_params", {})
        if default_api_key and "api_key" not in params:
            params["api_key"] = default_api_key
        items.append(parsed)
    return items


def is_passthrough(model_list: list[dict]) -> bool:
    """True when the config is a single wildcard entry: exactly one
    entry whose ``model_name`` is ``*`` — not a name that merely
    contains one."""
    if len(model_list) != 1:
        return False
    return model_list[0].get("model_name", "") == "*"

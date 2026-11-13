"""The workspace LLM proxy (#259): one router, one proxy per tap.

msksd holds the provider credentials and the routing configuration;
every workspace consumes LLMs through an OpenAI-shaped proxy served
on its own tap — the third host-reachable port beside DHCP (67) and
the resolver (53). A workspace holds no provider key and needs no
provider egress: the daemon makes the upstream connection, so a
workspace uses LLMs with its egress consent untouched.

Two routing modes, both ported from klangk's in-process router
(klangk #2070/#2072):

- **multi-provider** — a ``litellm.Router`` over a configured model
  list, entries spelled ``provider/model:api_base:api_key``;
- **passthrough** — one single-upstream entry whose model name is
  ``*``: requests forward verbatim over httpx and ``/models`` is
  discovered dynamically from the upstream.

The app serves the OpenAI pair (``/v1/models`` and
``/v1/chat/completions``) and the Anthropic Messages route
(``/v1/messages``, #765) claude-code and its kin speaks: verbatim
forward in passthrough mode, translation in router mode.

Secret-bearing values (each entry's key, the default key) resolve
``file:`` and ``cmd:`` indirection at configure time, so provider
credentials stay out of the config file and the process
environment. ``litellm`` is imported lazily — its ~5 s import tax
stays off every path that never configures a model list.

Settings are read live: the router rebuilds itself when the settings
object it sees changes identity (the SIGHUP swap), so a model-list
edit applies without a restart wherever a listener already serves.

The package splits along those seams: :mod:`.indirect` resolves
secret indirection, :mod:`.entries` parses model-list entries,
:mod:`.router` holds the routing machinery, :mod:`.anthropic`
translates the Anthropic Messages wire, and :mod:`.proxy` serves
the per-tap OpenAI-shaped API. The names importers use re-export
from here.
"""

from .anthropic import (
    ANTHROPIC_FIELDS,
    AnthropicStream,
    anthropic_api_error,
    anthropic_fields,
    anthropic_to_openai,
    events_from_message,
    openai_to_anthropic,
    render_anthropic_events,
    sse_line,
)
from .entries import (
    build_model_list,
    is_passthrough,
    normalize_dict_entry,
    parse_model_entry,
    split_entry,
)
from .indirect import resolve_indirection
from .proxy import (
    LlmProxy,
    TapListener,
    bind_to_device,
    chat_fields,
    dispatch_completion,
    parse_body,
)
from .router import LlmRouter, finish_aclose_task, spawn_aclose

__all__ = [
    "ANTHROPIC_FIELDS",
    "AnthropicStream",
    "LlmProxy",
    "LlmRouter",
    "TapListener",
    "anthropic_api_error",
    "anthropic_fields",
    "anthropic_to_openai",
    "bind_to_device",
    "build_model_list",
    "chat_fields",
    "dispatch_completion",
    "events_from_message",
    "finish_aclose_task",
    "is_passthrough",
    "normalize_dict_entry",
    "openai_to_anthropic",
    "parse_body",
    "parse_model_entry",
    "render_anthropic_events",
    "resolve_indirection",
    "spawn_aclose",
    "split_entry",
    "sse_line",
]

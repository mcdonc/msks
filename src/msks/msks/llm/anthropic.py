"""The Anthropic Messages translation (#765).

One direction converts a Messages request body to the OpenAI chat
shape the router speaks; the other converts the OpenAI response
back, streaming included — :class:`AnthropicStream` renders the
OpenAI SSE stream as Anthropic events.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

#: The Messages API version the route answers (and forwards in
#: passthrough mode) when the request carries none: 2023-06-01 is
#: the version every Messages client in the wild sends.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

#: The Messages request fields the route reads. The allowlist is
#: the whole defense on this shape too: a guest body naming
#: ``api_key``, ``api_base``, or any other routing parameter never
#: reaches the converter's output, exactly as the OpenAI route's
#: ``CHAT_FIELDS`` gate behaves. ``top_k`` is absent by choice: the
#: OpenAI completion body has no such field, and a forwarded one
#: would fail providers that reject unknown parameters.
ANTHROPIC_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "temperature",
        "top_p",
        "stop_sequences",
        "stream",
        "tools",
        "tool_choice",
        "metadata",
    }
)

#: The finish-reason vocabulary of each shape, one to the other:
#: OpenAI names three ends a Messages client distinguishes, and
#: ``stop_sequence`` has no OpenAI source (the proxy never injects
#: stop sequences), so it never appears as a conversion output.
FINISH_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}


def anthropic_fields(body: dict) -> dict:
    """The request body's translatable subset — the allowlist
    applied."""
    return {k: v for k, v in body.items() if k in ANTHROPIC_FIELDS}


def block_texts(blocks: Any) -> list[str]:
    """A block list's text blocks as strings: the callers join
    them with the separator their shape uses."""
    if not isinstance(blocks, list):
        return []
    return [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def system_text(system: Any) -> str:
    """The system prompt as one string: the Messages API accepts a
    plain string or a list of text blocks, and the OpenAI shape
    wants the string."""
    if isinstance(system, str):
        return system
    return "\n\n".join(part for part in block_texts(system) if part)


def image_part(block: dict) -> dict:
    """One image block to the OpenAI part: a base64 source
    becomes the data-URL spelling every provider accepts, and a
    url source passes its URL through (a url block an upstream
    fetches itself stays a fetch)."""
    source = block.get("source", {})
    if source.get("type") == "url":
        return {
            "type": "image_url",
            "image_url": {"url": source.get("url", "")},
        }
    media_type = source.get("media_type", "application/octet-stream")
    data = source.get("data", "")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{data}"},
    }


def tool_call_part(block: dict) -> dict:
    """One ``tool_use`` block to an OpenAI tool-call item: the
    input dict serializes to the arguments string."""
    return {
        "type": "function",
        "id": block.get("id", ""),
        "function": {
            "name": block.get("name", ""),
            "arguments": json.dumps(block.get("input", {})),
        },
    }


def result_text(content: Any) -> str:
    """A ``tool_result`` block's content as the tool message's
    text: the Messages shape allows a string or a block list, and
    the text blocks join (images inside a result have no OpenAI
    tool-message shape and drop)."""
    if isinstance(content, str):
        return content
    return "\n".join(part for part in block_texts(content) if part)


def split_blocks(
    content: Any,
) -> tuple[list[str], list[dict], list[dict], list[dict]]:
    """A content block list's four bins: the texts, the content
    parts, the tool calls, and the tool results — in walk order."""
    texts: list[str] = []
    parts: list[dict] = []
    tool_calls: list[dict] = []
    results: list[dict] = []
    for block in content:
        if isinstance(block, dict):
            route_block(block, texts, parts, tool_calls, results)
    return texts, parts, tool_calls, results


def route_block(
    block: dict,
    texts: list[str],
    parts: list[dict],
    tool_calls: list[dict],
    results: list[dict],
) -> None:
    """One block into its bin, by kind; a thinking block carries
    no OpenAI shape and drops."""
    kind = block.get("type")
    if kind == "text":
        texts.append(block.get("text", ""))
        parts.append({"type": "text", "text": block.get("text", "")})
    elif kind == "image":
        parts.append(image_part(block))
    elif kind == "tool_use":
        tool_calls.append(tool_call_part(block))
    elif kind == "tool_result":
        results.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": result_text(block.get("content")),
            }
        )


def convert_message(message: dict) -> list[dict]:
    """One Messages turn to its OpenAI messages, in order: a user
    turn's ``tool_result`` blocks become ``tool`` messages that
    come first (the OpenAI shape pins them right after the
    assistant's tool calls), the remaining blocks join as the
    turn's own content, and an assistant turn's ``tool_use``
    blocks become its ``tool_calls``. A turn that is only tool
    results contributes no message of its own."""
    role = message.get("role", "user")
    content = message.get("content", "")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    texts, parts, tool_calls, results = split_blocks(content)
    converted = list(results)
    if role == "assistant":
        converted.extend(assistant_message(texts, parts, tool_calls))
        return converted
    if parts:
        converted.append({"role": "user", "content": parts})
    return converted


def assistant_message(
    texts: list[str], parts: list[dict], tool_calls: list[dict]
) -> list[dict]:
    """An assistant turn's own message: tool calls ride as
    ``tool_calls`` with the text joined beside them, and a turn
    without tool calls keeps its text alone."""
    if tool_calls:
        return [
            {
                "role": "assistant",
                "content": "\n\n".join(texts) if texts else None,
                "tool_calls": tool_calls,
            }
        ]
    if parts or texts:
        return [{"role": "assistant", "content": "\n\n".join(texts)}]
    return []


def openai_tools(tools: Any) -> list[dict]:
    """The Messages tool list to the OpenAI function shape: the
    ``input_schema`` is the ``parameters``."""
    converted = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return converted


#: The ``tool_choice`` kinds that map to an OpenAI keyword:
#: ``any`` is the OpenAI ``required`` (a tool must run).
TOOL_CHOICE_KINDS = {"auto": "auto", "any": "required", "none": "none"}


def openai_tool_choice(choice: Any) -> Any:
    """The Messages ``tool_choice`` to the OpenAI one: ``auto``
    keeps its name, ``any`` becomes ``required`` (the OpenAI
    spelling of "a tool must run"), a named tool wraps in the
    function shape. A shape the map does not know drops — the
    upstream's default (auto) applies."""
    if not isinstance(choice, dict):
        return None
    if choice.get("type") in TOOL_CHOICE_KINDS:
        return TOOL_CHOICE_KINDS[choice["type"]]
    return named_tool_choice(choice)


def named_tool_choice(choice: dict) -> Any:
    """A named-tool choice to the OpenAI function shape; None for
    anything else."""
    if choice.get("type") == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def system_message(src: dict) -> dict | None:
    """The system prompt as an OpenAI system message; None when
    the body carried none (or carried one with no text)."""
    if "system" not in src:
        return None
    text = system_text(src["system"])
    if not text:
        return None
    return {"role": "system", "content": text}


def openai_messages(src: dict) -> list[dict]:
    """The Messages body's system and turns to OpenAI messages:
    the system prompt (a string or blocks) opens the list, and
    each turn converts in order. A malformed entry (a non-dict
    message) skips — a broken guest body costs that entry, never
    the request."""
    messages: list[dict] = []
    system = system_message(src)
    if system is not None:
        messages.append(system)
    for message in src.get("messages") or []:
        if isinstance(message, dict):
            messages.extend(convert_message(message))
    return messages


def metadata_user(src: dict) -> Any:
    """The metadata block's user id, None when absent."""
    return (src.get("metadata") or {}).get("user_id")


def openai_scalars(src: dict) -> dict:
    """The scalar fields that map name for name or to their
    renamed counterpart: the sampling knobs, the stream flag,
    the model, the token ceiling, the stop sequences, and the
    metadata's user id."""
    scalars: dict[str, Any] = {}
    for name in ("model", "max_tokens", "temperature", "top_p", "stream"):
        if name in src:
            scalars[name] = src[name]
    if "stop_sequences" in src:
        scalars["stop"] = src["stop_sequences"]
    user = metadata_user(src)
    if user:
        scalars["user"] = user
    return scalars


def anthropic_to_openai(body: dict) -> dict:
    """One Messages request body to OpenAI completion kwargs. The
    output carries only keys the OpenAI route's own allowlist
    names — the converter is the second gate behind
    :func:`anthropic_fields`, so a guest's routing parameters have
    two fields to survive and neither does."""
    src = anthropic_fields(body)
    kwargs: dict[str, Any] = openai_scalars(src)
    kwargs["messages"] = openai_messages(src)
    tools = openai_tools(src.get("tools"))
    if tools:
        kwargs["tools"] = tools
    choice = openai_tool_choice(src.get("tool_choice"))
    if choice is not None:
        kwargs["tool_choice"] = choice
    return kwargs


def stop_reason(finish: Any) -> str:
    """The OpenAI finish reason to the Messages ``stop_reason``."""
    return FINISH_REASONS.get(finish, "end_turn")


def tool_use_block(call: dict) -> dict:
    """One OpenAI tool-call item to a ``tool_use`` block; the
    arguments string parses back to the input dict, and an
    unparsable one (a provider that truncates mid-JSON) answers
    an empty input rather than an error."""
    function = call.get("function", {})
    try:
        input_obj = json.loads(function.get("arguments") or "{}")
    except ValueError:
        input_obj = {}
    return {
        "type": "tool_use",
        "id": call.get("id", ""),
        "name": function.get("name", ""),
        "input": input_obj,
    }


def is_text_part(part: Any) -> bool:
    """Whether one OpenAI content part is a text part."""
    return isinstance(part, dict) and part.get("type") == "text"


def string_block(content: str) -> list[dict]:
    """A string content as its text blocks: one block, or none
    for the empty string."""
    if content:
        return [{"type": "text", "text": content}]
    return []


def text_blocks(content: Any) -> list[dict]:
    """The text blocks of an OpenAI message's content: a string
    is one block (an empty one carries none), a parts list keeps
    its text parts, anything else carries none."""
    if isinstance(content, str):
        return string_block(content)
    if not isinstance(content, list):
        return []
    return [
        {"type": "text", "text": part.get("text", "")}
        for part in content
        if is_text_part(part)
    ]


def content_blocks(message: dict) -> list[dict]:
    """The OpenAI response message to Messages content blocks: the
    text (string or parts) becomes ``text`` blocks, the tool calls
    become ``tool_use`` blocks, and an empty answer keeps one
    empty ``text`` block — the Messages shape requires content."""
    blocks: list[dict] = list(text_blocks(message.get("content")))
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            blocks.append(tool_use_block(call))
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def openai_to_anthropic(response: dict) -> dict:
    """One OpenAI completion response to the Messages message. The
    first choice is the answer (the proxy serves one completion
    per request); usage maps field for field with zeros when the
    upstream named none."""
    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    usage = response.get("usage") or {}
    return {
        "id": response.get("id", "msg_msks"),
        "type": "message",
        "role": "assistant",
        "model": response.get("model", ""),
        "content": content_blocks(message),
        "stop_reason": stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def sse_line(event: dict) -> str:
    """One event as Anthropic SSE: the ``event`` line the wire
    format carries beside the data, and the JSON body."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def message_start(
    model: str, input_tokens: int, message_id: str = "msg_msks"
) -> dict:
    """The opening event: the message frame with empty content and
    the input usage (the Messages stream bills input here and
    output in ``message_delta``)."""
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }


def usage_input(usage: Any) -> int:
    """A usage block's prompt-token count, zero when the chunk
    carried none or carried a non-mapping."""
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("prompt_tokens") or 0)


class AnthropicStream:
    """One streamed response's conversion state: OpenAI chunks in,
    Anthropic events out.

    A chunk stream interleaves text deltas and tool-call argument
    fragments over unnamed positions, while the Messages event
    stream pins each block to an index the client tracks — so the
    converter owns the index counter, remembers which tool-call
    index maps to which block, and closes a block before the next
    one opens. :meth:`feed` takes each chunk, :meth:`finish`
    closes whatever the stream left open and ends the message.
    """

    def __init__(self) -> None:
        self.id = "msg_msks"
        self.model = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop = "end_turn"
        self.next_index = 0
        self.open_block: int | None = None
        self.text_index: int | None = None
        self.tool_indexes: dict[int, int] = {}
        self.started = False

    def start_events(self) -> list[dict]:
        """The events the first chunk owes: the message frame, then
        the ping a Messages client expects before content."""
        self.started = True
        return [
            message_start(self.model, self.input_tokens, self.id),
            {"type": "ping"},
        ]

    def start_if_first(self, chunk: dict) -> list[dict]:
        """The first chunk's events: the id, model name, and input
        usage land on the converter before the frame renders."""
        if self.started:
            return []
        self.id = chunk.get("id", self.id)
        self.model = chunk.get("model", self.model)
        self.input_tokens = usage_input(chunk.get("usage"))
        return self.start_events()

    def feed(self, chunk: dict) -> list[dict]:
        """One OpenAI chunk to its events; the first chunk also
        carries the message frame, and a chunk with usage records
        it (OpenAI sends usage on the chunk that carries it, which
        is the last when the stream asked for it)."""
        events: list[dict] = self.start_if_first(chunk)
        events.extend(self.choices_events(chunk.get("choices") or []))
        self.record_usage(chunk.get("usage"))
        return events

    def choices_events(self, choices: Any) -> list[dict]:
        """Every choice's events, in order."""
        events: list[dict] = []
        for choice in choices or []:
            events.extend(self.choice_events(choice))
        return events

    def record_usage(self, usage: Any) -> None:
        """A chunk's output-token count onto the converter's
        counter (input billed in the frame already sent; output
        lands in the closing ``message_delta``)."""
        if not isinstance(usage, dict):
            return
        if usage.get("completion_tokens"):
            self.output_tokens = usage["completion_tokens"]

    def choice_events(self, choice: dict) -> list[dict]:
        """One choice's delta to its events: the finish reason
        records for ``message_delta``, then the delta's text and
        tool-call fragments convert."""
        if choice.get("finish_reason"):
            self.stop = stop_reason(choice["finish_reason"])
        return self.delta_events(choice.get("delta") or {})

    def delta_events(self, delta: dict) -> list[dict]:
        """One delta's text and tool calls to their events."""
        events: list[dict] = []
        text = delta.get("content")
        if isinstance(text, str) and text:
            events.extend(self.text_events(text))
        for call in delta.get("tool_calls") or []:
            events.extend(self.tool_events(call))
        return events

    def text_events(self, text: str) -> list[dict]:
        """Open the text block when none is open (closing whatever
        block is), then the delta."""
        if self.text_index is None:
            events = self.close_open()
            self.text_index = self.next_index
            self.next_index += 1
            self.open_block = self.text_index
            events.append(
                {
                    "type": "content_block_start",
                    "index": self.text_index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        else:
            events = []
        events.append(
            {
                "type": "content_block_delta",
                "index": self.text_index,
                "delta": {"type": "text_delta", "text": text},
            }
        )
        return events

    def tool_events(self, call: dict) -> list[dict]:
        """One tool-call fragment: the first for an index opens the
        ``tool_use`` block (closing whatever block is open — the
        Messages event stream stops each block before the next
        opens), and an arguments fragment is an
        ``input_json_delta``. A fragment for an index whose block
        already closed (an interleaved chunk stream) still names
        its block index: OpenAI streams tool calls sequentially,
        so the shape stays a rare upstream's own."""
        index = call.get("index", 0)
        if index not in self.tool_indexes:
            return self.open_tool_block(index, call) + self.fragment_events(
                index, call
            )
        return self.fragment_events(index, call)

    def open_tool_block(self, index: int, call: dict) -> list[dict]:
        """The ``tool_use`` block's opening events at a fresh
        index, after the open block closes."""
        events: list[dict] = self.close_open()
        block_index = self.next_index
        self.next_index += 1
        self.tool_indexes[index] = block_index
        self.open_block = block_index
        function = call.get("function") or {}
        events.append(
            {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": call.get("id") or f"toolu_msks_{index}",
                    "name": function.get("name", ""),
                    "input": {},
                },
            }
        )
        return events

    def fragment_events(self, index: int, call: dict) -> list[dict]:
        """One arguments fragment as an ``input_json_delta``, when
        the fragment carries one."""
        function = call.get("function") or {}
        fragment = function.get("arguments")
        if not (isinstance(fragment, str) and fragment):
            return []
        return [
            {
                "type": "content_block_delta",
                "index": self.tool_indexes[index],
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": fragment,
                },
            }
        ]

    def close_open(self) -> list[dict]:
        """The open block's stop, when one is open: the Messages
        event stream closes each block before the next opens, so
        a block opening and a stream finishing share this."""
        if self.open_block is None:
            return []
        event = {"type": "content_block_stop", "index": self.open_block}
        self.open_block = None
        self.text_index = None
        return [event]

    def finish(self) -> list[dict]:
        """The closing events: the message frame when no chunk ever
        arrived (an empty answer is still a well-formed stream),
        the open block stops, then the usage-bearing
        ``message_delta`` and ``message_stop``."""
        events = self.start_events() if not self.started else []
        events.extend(self.close_open())
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": self.stop, "stop_sequence": None},
                "usage": {"output_tokens": self.output_tokens},
            }
        )
        events.append({"type": "message_stop"})
        return events


def events_from_message(message: dict) -> list[dict]:
    """A complete Messages message (the non-streaming conversion's
    output) to its full event sequence — the stream a client asked
    for and an upstream that answered without one still gets:
    ``message_start``, every block opened, its whole content as
    one delta, stopped, then the closing pair."""
    events = [
        message_start(
            message["model"], message["usage"]["input_tokens"], message["id"]
        ),
        {"type": "ping"},
    ]
    for index, block in enumerate(message["content"]):
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": block,
            }
        )
        if block["type"] == "text":
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"]),
            }
        events.append(
            {"type": "content_block_delta", "index": index, "delta": delta}
        )
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": message["stop_reason"],
                "stop_sequence": None,
            },
            "usage": message["usage"],
        }
    )
    events.append({"type": "message_stop"})
    return events


def anthropic_api_error(message: str) -> dict:
    """The Messages error body (the shape a Messages client
    surfaces): one ``type: error`` frame around the message."""
    return {
        "type": "error",
        "error": {"type": "api_error", "message": message},
    }


async def render_anthropic_events(
    chunks: AsyncIterator[Any], dump
) -> AsyncIterator[str]:
    """One OpenAI chunk iterator to the Anthropic SSE text stream
    (the route's streaming half; *dump* is the chunk-to-dict
    helper, injected so the seam stays here beside the shapes it
    renders)."""
    stream = AnthropicStream()
    async for chunk in chunks:
        for event in stream.feed(await dump(chunk)):
            yield sse_line(event)
    for event in stream.finish():
        yield sse_line(event)

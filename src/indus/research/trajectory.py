"""The one trajectory converter (P22-03): stored messages → training-sample turns.

Every writer of samples calls ``convert`` (batch runs now; the interactive saver and the SWE runner
later), so their formats cannot drift apart (R10.7). The format is documented in
``docs/user/reference/trajectory-format.md``. Pure: no IO, no clock.

A sample is a list of ``{"from": role, "value": text}`` turns, roles ``system``, ``human``, ``gpt``
and ``tool``, using the open function-calling tags ``<tools>``, ``<tool_call>``,
``<tool_response>`` and ``<think>``:

- turn 0 ``system``: our preamble with the run's tool list (compact JSON) inside ``<tools>``;
- turn 1 ``human``: the dataset prompt. Input rows up to and including the first user message are
  not converted again (system rows, the prompt). Prefill messages are added to requests only and
  never stored, so they never appear;
- an assistant message becomes one ``gpt`` turn that always starts with a think block
  (``<think>\\n…\\n</think>\\n``, empty when the model gave no reasoning), then its text, then one
  ``<tool_call>`` block per call with compact ``{"name", "arguments"}`` JSON (arguments parsed; an
  unparseable or non-object value becomes ``{}`` and is counted in ``bad_arguments``);
- the run of tool messages after it becomes one ``tool`` turn of ``<tool_response>`` blocks joined
  by newlines, each compact ``{"tool_call_id", "name", "content"}`` JSON. The name comes from the
  call with the same id; by position only when ids are missing; ``unknown`` otherwise (R10.6).
  Content that reads as a JSON object or array is embedded as JSON;
- image-bearing results keep only text: a parts list becomes its text parts (``[image]`` for each
  other part), a multimodal envelope its ``text_summary``;
- a later user message becomes a ``human`` turn. Trailing whitespace is trimmed from every value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

__all__ = [
    "IMAGE_PLACEHOLDER",
    "UNKNOWN_TOOL",
    "Conversion",
    "call_name",
    "content_text",
    "convert",
    "result_names",
    "system_value",
    "tool_list",
]

UNKNOWN_TOOL = "unknown"
IMAGE_PLACEHOLDER = "[image]"

PREAMBLE = (
    "You are an assistant that can use tools. The tools you may call are described as JSON "
    "inside the <tools></tools> tags below. Call a tool only when it helps with the request; "
    "otherwise answer in plain language. Never guess argument values you were not given.\n"
    "<tools>\n{tools}\n</tools>\n"
    "To call a tool, write one <tool_call></tool_call> block per call holding a JSON object "
    'with the keys "name" (the tool) and "arguments" (an object of its parameters), for example:\n'
    '<tool_call>\n{{"name": "tool_name", "arguments": {{"param": "value"}}}}\n</tool_call>\n'
    "Each result comes back to you inside <tool_response></tool_response> tags. Before you act, "
    "think inside <think></think> tags."
)

_LEADING_THINK = re.compile(r"\A\s*<think>(?P<body>.*?)</think>[ \t]*\n?", re.DOTALL)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Conversion:
    turns: list[dict[str, str]]
    bad_arguments: int = 0


# -- tools --------------------------------------------------------------------------------------


def tool_list(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``{name, description, parameters}`` per tool (the wire's ``function`` wrapper removed)."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        inner = tool.get("function")
        spec = cast(Mapping[str, Any], inner) if isinstance(inner, Mapping) else tool
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append(
            {
                "name": name,
                "description": spec.get("description") or "",
                "parameters": spec.get("parameters") or {},
            }
        )
    return out


def system_value(tools: Sequence[Mapping[str, Any]]) -> str:
    return PREAMBLE.format(tools=_compact(tool_list(tools)))


# -- message pieces -----------------------------------------------------------------------------


def _part_text(part: object) -> str:
    if isinstance(part, Mapping):
        item = cast(Mapping[str, Any], part)
        if item.get("type") == "text":
            return str(item.get("text") or "")
        return IMAGE_PLACEHOLDER
    return str(part)


def content_text(content: object) -> str:
    """Plain text of a message's content: parts joined (images as a placeholder), a multimodal
    envelope as its summary, anything else as text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        env = cast(Mapping[str, Any], content)
        summary = env.get("text_summary")
        if isinstance(summary, str):
            return summary
        parts = env.get("content")
        if isinstance(parts, list):
            return content_text(parts)
        return _compact(dict(env))
    if isinstance(content, list):
        return "\n".join(_part_text(p) for p in cast(list[Any], content))
    return str(content)


def _calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [cast(Mapping[str, Any], c) for c in cast(list[Any], raw) if isinstance(c, Mapping)]


def _function(call: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = call.get("function")
    return cast(Mapping[str, Any], inner) if isinstance(inner, Mapping) else call


def call_name(call: Mapping[str, Any]) -> str:
    name = _function(call).get("name")
    return name if isinstance(name, str) and name else UNKNOWN_TOOL


def _call_id(call: Mapping[str, Any]) -> str:
    value = call.get("id")
    return value if isinstance(value, str) else ""


def _result_id(message: Mapping[str, Any]) -> str:
    value = message.get("tool_call_id")
    return value if isinstance(value, str) else ""


def result_names(
    calls: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The tool name of each result: by call id, by position when ids are missing, else
    ``unknown`` (R10.6)."""
    by_id = {cid: call_name(c) for c in calls if (cid := _call_id(c))}
    calls_have_ids = bool(by_id)
    out: list[str] = []
    for position, result in enumerate(results):
        rid = _result_id(result)
        if rid and rid in by_id:
            out.append(by_id[rid])
        elif (not rid or not calls_have_ids) and position < len(calls):
            out.append(call_name(calls[position]))
        else:
            out.append(UNKNOWN_TOOL)
    return out


def _arguments(call: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Parsed arguments, and whether they had to be replaced by ``{}``."""
    raw = _function(call).get("arguments")
    if raw is None or raw == "":
        return {}, False
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}, True
    if not isinstance(value, dict):
        return {}, True
    return cast(dict[str, Any], value), False


def _split_think(message: Mapping[str, Any]) -> tuple[str, str]:
    """``(reasoning, text)``: the native reasoning field, else a ``<think>`` block that opens the
    text (lifted out), else empty reasoning."""
    text = content_text(message.get("content"))
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip("\n"), text
    found = _LEADING_THINK.match(text)
    if found is not None:
        return found.group("body").strip("\n"), text[found.end() :]
    return "", text


def _think_block(reasoning: str) -> str:
    return f"<think>\n{reasoning}\n</think>\n" if reasoning else "<think>\n</think>\n"


def _gpt_value(message: Mapping[str, Any]) -> tuple[str, int]:
    reasoning, text = _split_think(message)
    value = _think_block(reasoning)
    if text.strip():
        value += text.rstrip() + "\n"
    bad = 0
    for call in _calls(message):
        arguments, replaced = _arguments(call)
        bad += int(replaced)
        payload = _compact({"name": call_name(call), "arguments": arguments})
        value += f"<tool_call>\n{payload}\n</tool_call>\n"
    return value.rstrip(), bad


def _response_content(content: object) -> Any:
    text = content_text(content)
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _tool_value(calls: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]) -> str:
    blocks: list[str] = []
    for result, name in zip(results, result_names(calls, results), strict=True):
        payload = _compact(
            {
                "tool_call_id": _result_id(result),
                "name": name,
                "content": _response_content(result.get("content")),
            }
        )
        blocks.append(f"<tool_response>\n{payload}\n</tool_response>")
    return "\n".join(blocks).rstrip()


def _start(messages: Sequence[Mapping[str, Any]]) -> int:
    """Index of the first row after the prompt (the first user message)."""
    for position, message in enumerate(messages):
        if message.get("role") == "user":
            return position + 1
    return 0


# -- conversion ---------------------------------------------------------------------------------


def convert(
    messages: Sequence[Mapping[str, Any]],
    *,
    prompt: str,
    tools: Sequence[Mapping[str, Any]] = (),
) -> Conversion:
    """The sample for one conversation (see the module notes)."""
    turns: list[dict[str, str]] = [
        {"from": "system", "value": system_value(tools).rstrip()},
        {"from": "human", "value": prompt.rstrip()},
    ]
    bad = 0
    calls: list[Mapping[str, Any]] = []
    position = _start(messages)
    while position < len(messages):
        message = messages[position]
        role = message.get("role")
        if role == "assistant":
            value, wrong = _gpt_value(message)
            bad += wrong
            turns.append({"from": "gpt", "value": value})
            calls = _calls(message)
        elif role == "tool":
            run: list[Mapping[str, Any]] = []
            while position < len(messages) and messages[position].get("role") == "tool":
                run.append(messages[position])
                position += 1
            turns.append({"from": "tool", "value": _tool_value(calls, run)})
            calls = []
            continue
        elif role == "user":
            turns.append({"from": "human", "value": content_text(message.get("content")).rstrip()})
            calls = []
        position += 1
    return Conversion(turns, bad)

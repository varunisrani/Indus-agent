"""A strict reader for the small YAML subset our fake-model scripts use.

Stand-in until ruamel.yaml is in the lock (P01-08). Supported: block mappings and sequences
(including ``- key: value`` items), comments, plain / single- / double-quoted scalars, ``null``,
``true``/``false``, integers, floats, and one-line flow lists and maps (``[a, b]``, ``{k: v}``).
Anything else raises ``YamlSubsetError`` with the line number.

Mappings and lists come back as ``LineMap`` / ``LineList``: a dict / list that also knows the
line it started on, so callers can point errors at the right place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class YamlSubsetError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


class LineMap(dict[str, Any]):
    line: int = 0


class LineList(list[Any]):
    line: int = 0


@dataclass
class _Line:
    number: int
    indent: int
    text: str


_INT = re.compile(r"[-+]?\d+")
_FLOAT = re.compile(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?")
_UNSUPPORTED_START = ("&", "*", "!", "|", ">", "%", "@", "`")


def loads(text: str) -> Any:
    lines = _split(text)
    if not lines:
        return None
    value, index = _block(lines, 0, lines[0].indent)
    if index != len(lines):
        bad = lines[index]
        raise YamlSubsetError(bad.number, "unexpected indentation")
    return value


def _strip_comment(raw: str) -> str:
    quote: str | None = None
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i]
    return raw


def _split(text: str) -> list[_Line]:
    out: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        if body.strip() in ("---", "..."):
            if out:
                raise YamlSubsetError(number, "multiple documents are not supported")
            continue
        stripped = body.lstrip(" ")
        if stripped.startswith("\t") or "\t" in body[: len(body) - len(stripped)]:
            raise YamlSubsetError(number, "tabs are not allowed in indentation")
        out.append(_Line(number, len(body) - len(stripped), stripped))
    return out


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _block(lines: list[_Line], index: int, indent: int) -> tuple[Any, int]:
    if _is_item(lines[index].text):
        return _sequence(lines, index, indent)
    return _mapping(lines, index, indent)


def _sequence(lines: list[_Line], index: int, indent: int) -> tuple[LineList, int]:
    result = LineList()
    result.line = lines[index].number
    while index < len(lines) and lines[index].indent == indent and _is_item(lines[index].text):
        line = lines[index]
        rest = line.text[1:].lstrip(" ")
        if not rest:
            if index + 1 < len(lines) and lines[index + 1].indent > indent:
                value, index = _block(lines, index + 1, lines[index + 1].indent)
            else:
                value, index = None, index + 1
        elif _split_key(rest, line.number) is not None:
            # "- key: value": the mapping starts at the column of "key".
            offset = len(line.text) - len(rest)
            lines[index] = _Line(line.number, indent + offset, rest)
            value, index = _mapping(lines, index, indent + offset)
            value.line = line.number
        else:
            value, index = _scalar_or_flow(rest, line.number), index + 1
        result.append(value)
    if index < len(lines) and lines[index].indent > indent:
        raise YamlSubsetError(lines[index].number, "unexpected indentation")
    return result, index


def _split_key(text: str, number: int) -> tuple[str, str] | None:
    if text[0] in "\"'":
        end = text.find(text[0], 1)
        if end == -1:
            raise YamlSubsetError(number, "unterminated quoted key")
        key, after = text[1:end], text[end + 1 :]
        if after == ":" or after.startswith(": "):
            return key, after[1:].strip()
        return None
    if text[0] in "[{":
        return None
    match = re.match(r"([^:#]+?):(\s+|$)", text)
    if not match:
        return None
    return match.group(1).strip(), text[match.end() :].strip()


def _mapping(lines: list[_Line], index: int, indent: int) -> tuple[LineMap, int]:
    result = LineMap()
    result.line = lines[index].number
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if _is_item(line.text):
            raise YamlSubsetError(line.number, "list item where a mapping key was expected")
        split = _split_key(line.text, line.number)
        if split is None:
            raise YamlSubsetError(line.number, f"expected 'key: value', got {line.text!r}")
        key, rest = split
        if key in result:
            raise YamlSubsetError(line.number, f"duplicate key {key!r}")
        index += 1
        if rest:
            result[key] = _scalar_or_flow(rest, line.number)
        elif index < len(lines) and lines[index].indent > indent:
            result[key], index = _block(lines, index, lines[index].indent)
        elif index < len(lines) and lines[index].indent == indent and _is_item(lines[index].text):
            result[key], index = _sequence(lines, index, indent)
        else:
            result[key] = None
    if index < len(lines) and lines[index].indent > indent:
        raise YamlSubsetError(lines[index].number, "unexpected indentation")
    return result, index


def _scalar_or_flow(text: str, number: int) -> Any:
    if text[0] in "[{":
        parser = _Flow(text, number)
        value = parser.value()
        parser.skip_space()
        if parser.pos != len(text):
            raise YamlSubsetError(number, f"trailing text after flow value: {text[parser.pos :]!r}")
        return value
    return _scalar(text, number)


def _scalar(text: str, number: int) -> Any:
    if text[0] == '"':
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YamlSubsetError(number, f"bad double-quoted string: {exc.msg}") from None
    if text[0] == "'":
        if len(text) < 2 or not text.endswith("'"):
            raise YamlSubsetError(number, "unterminated single-quoted string")
        inner = text[1:-1]
        if re.search(r"(?<!')'(?!')", inner.replace("''", "")):
            raise YamlSubsetError(number, "stray quote in single-quoted string")
        return inner.replace("''", "'")
    if text.startswith(_UNSUPPORTED_START):
        raise YamlSubsetError(number, f"unsupported YAML syntax: {text[0]!r}")
    if text in ("null", "~", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


class _Flow:
    def __init__(self, text: str, number: int) -> None:
        self.text = text
        self.pos = 0
        self.number = number

    def fail(self, message: str) -> YamlSubsetError:
        return YamlSubsetError(self.number, f"{message} (column {self.pos + 1})")

    def skip_space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] == " ":
            self.pos += 1

    def value(self) -> Any:
        self.skip_space()
        if self.pos >= len(self.text):
            raise self.fail("missing value")
        ch = self.text[self.pos]
        if ch == "[":
            return self.items("]")
        if ch == "{":
            return self.items("}")
        if ch in "\"'":
            end = self.pos + 1
            while end < len(self.text):
                if self.text[end] == ch:
                    if ch == "'" and self.text[end + 1 : end + 2] == "'":
                        end += 2
                        continue
                    if ch == '"' and self.text[end - 1] == "\\":
                        end += 1
                        continue
                    break
                end += 1
            else:
                raise self.fail("unterminated string")
            token = self.text[self.pos : end + 1]
            self.pos = end + 1
            return _scalar(token, self.number)
        end = self.pos
        while end < len(self.text) and self.text[end] not in ",]}":
            if self.text[end] == ":" and self.text[end + 1 : end + 2] in ("", " "):
                break  # "key: value" separator inside a flow mapping
            end += 1
        token = self.text[self.pos : end].strip()
        self.pos = end
        if not token:
            raise self.fail("empty value")
        return _scalar(token, self.number)

    def items(self, close: str) -> Any:
        self.pos += 1
        is_map = close == "}"
        result: Any = LineMap() if is_map else LineList()
        result.line = self.number
        self.skip_space()
        if self.text[self.pos : self.pos + 1] == close:
            self.pos += 1
            return result
        while True:
            if is_map:
                key = self.value()
                self.skip_space()
                if self.text[self.pos : self.pos + 1] != ":":
                    raise self.fail("expected ':' in flow mapping")
                self.pos += 1
                result[str(key)] = self.value()
            else:
                result.append(self.value())
            self.skip_space()
            ch = self.text[self.pos : self.pos + 1]
            if ch == ",":
                self.pos += 1
                continue
            if ch == close:
                self.pos += 1
                return result
            raise self.fail(f"expected ',' or {close!r}")

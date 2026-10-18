"""The only module that touches the YAML library.

Reads follow YAML 1.1 (hand-written configs say ``yes``/``no``/``on``/``off`` and mean booleans),
refuse duplicate keys and tolerate a UTF-8 BOM. Every call builds its own parser or emitter: the
library's objects are not safe to share between threads.

Two writing styles exist. The round-trip pair (`load_roundtrip` / `dump_roundtrip`) edits files
people write by hand and keeps comments, key order, quoting and blank lines. `dump_plain` writes
files the product owns wholly. Both use one house style: mappings indented 2, sequences indented 4
with the dash at 2, no line folding, readable Unicode.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.resolver import VersionedResolver
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

__all__ = [
    "CommentedMap",
    "CommentedSeq",
    "YamlError",
    "check_syntax",
    "dump_plain",
    "dump_roundtrip",
    "is_ambiguous_string",
    "load_roundtrip",
    "roundtrip_key",
    "load_safe",
    "to_roundtrip",
]

# Folding a long line can change a double-quoted value on reload, so never fold.
_NO_FOLD_WIDTH = 2**31 - 1
_STR_TAG = "tag:yaml.org,2002:str"
_BOM = "﻿"


class YamlError(ValueError):
    """A YAML document could not be read. ``line`` and ``column`` are 1-based when known."""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None) -> None:
        where = f" (line {line})" if line is not None else ""
        super().__init__(f"{message}{where}")
        self.reason = message
        self.line = line
        self.column = column


class _Yaml11Resolver(VersionedResolver):
    """Resolve plain scalars by YAML 1.1 rules without writing a ``%YAML`` directive."""

    @property
    def processing_version(self) -> Any:
        return (1, 1)


def _to_error(exc: YAMLError) -> YamlError:
    if isinstance(exc, MarkedYAMLError):
        marked: Any = exc
        mark: Any = marked.problem_mark or marked.context_mark
        problem: Any = marked.problem or marked.context or "invalid YAML"
        if mark is not None:
            return YamlError(str(problem), line=mark.line + 1, column=mark.column + 1)
        return YamlError(str(problem))
    return YamlError(str(exc).splitlines()[0] if str(exc) else "invalid YAML")


def _as_text(source: str | bytes | Path) -> str:
    if isinstance(source, Path):
        source = source.read_bytes()
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YamlError(f"not valid UTF-8 at byte {exc.start}") from exc
    return source.removeprefix(_BOM)


def load_safe(source: str | bytes | Path) -> Any:
    """Parse one YAML document into plain Python values (YAML 1.1, duplicate keys refused)."""
    text = _as_text(source)
    yaml: Any = YAML(typ="safe", pure=True)
    yaml.version = (1, 1)
    try:
        return yaml.load(text)
    except YAMLError as exc:
        raise _to_error(exc) from exc


def check_syntax(source: str | bytes | Path) -> None:
    """Raise ``YamlError`` when ``source`` is not well-formed YAML; build no values.

    Only the event stream is parsed, so several documents in one stream and application tags
    (``!Ref``, ``!vault``) pass: this answers "does it parse", not "does it load".
    """
    text = _as_text(source)
    yaml: Any = YAML(typ="safe", pure=True)
    try:
        for _event in yaml.parse(text):
            pass
    except YAMLError as exc:
        raise _to_error(exc) from exc


def _roundtrip_yaml() -> Any:
    yaml: Any = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    yaml.width = _NO_FOLD_WIDTH
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load_roundtrip(source: str | bytes | Path) -> CommentedMap:
    """Parse a document for editing. A missing file or empty document gives an empty map.

    Scalars keep their original spelling (the library's own resolution is used, so ``on`` stays
    the text ``on``); read the same text with `load_safe` for its YAML 1.1 meaning.
    """
    if isinstance(source, Path) and not source.exists():
        return CommentedMap()
    text = _as_text(source)
    try:
        data: Any = _roundtrip_yaml().load(text)
    except YAMLError as exc:
        raise _to_error(exc) from exc
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise YamlError(f"top level must be a mapping, not {type(data).__name__}")
    return data


def dump_roundtrip(doc: Any) -> str:
    """Emit a document loaded by `load_roundtrip` (or built with `to_roundtrip`)."""
    out = io.StringIO()
    _roundtrip_yaml().dump(doc, out)
    return out.getvalue()


def is_ambiguous_string(value: str) -> bool:
    """True when a YAML 1.1 reader would not read ``value`` back as a string if written plain."""
    resolver: Any = _Yaml11Resolver()
    tag: Any = resolver.resolve(ScalarNode, value, (True, False))
    return tag != _STR_TAG


def _needs_quotes(value: Any) -> bool:
    return (
        isinstance(value, str)
        and not isinstance(value, DoubleQuotedScalarString)
        and is_ambiguous_string(value)
    )


def roundtrip_key(key: Any) -> Any:
    """A mapping key for a round-trip document, quoted when YAML 1.1 would misread it."""
    if _needs_quotes(key):
        return DoubleQuotedScalarString(key)
    return key


def to_roundtrip(value: Any) -> Any:
    """Turn a plain value into round-trip nodes, quoting strings that YAML 1.1 would misread."""
    if isinstance(value, dict):
        node = CommentedMap()
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            node[roundtrip_key(key)] = to_roundtrip(item)
        return node
    if isinstance(value, list | tuple):
        return CommentedSeq(to_roundtrip(item) for item in value)  # pyright: ignore[reportUnknownVariableType]
    if _needs_quotes(value):
        return DoubleQuotedScalarString(value)
    return value


def dump_plain(data: Any) -> str:
    """Emit plain data in block style, keys in insertion order, safe for YAML 1.1 readers."""
    yaml: Any = YAML(typ="safe", pure=True)
    yaml.Resolver = _Yaml11Resolver
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = _NO_FOLD_WIDTH
    yaml.sort_base_mapping_type_on_output = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()

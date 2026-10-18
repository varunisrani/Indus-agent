"""Locales: every catalogue has exactly the English keys and placeholders (D-16.51).

``locales/en.yaml`` is the source. Each other ``locales/<lang>.yaml`` must have the same leaf
keys (nested mappings are flattened with dots), the same ``{placeholder}`` names per key, and only
string values. Surfaces whose English lives in code export ``locales/_keys.<surface>.json`` (a
list of keys, or an object of key -> English text); their packs ``locales/<lang>.<surface>.yaml``
are compared to it the same way.

The files are read with a small strict YAML subset reader (block mappings, plain and one-line quoted
scalars, comments) so the guard needs nothing beyond the standard library. Anything outside the
subset is refused with the line number.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tooling.checks.common import Context, Result

NAME = "locales"
SOURCE_LANG = "en"

LANG = re.compile(r"^[a-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
SURFACE = re.compile(r"^[a-z][a-z0-9_-]*$")
KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:[!:][^{}]*)?\}")

# Plain scalars a YAML loader (1.1 or 1.2) would turn into something other than a string.
NON_STRING = re.compile(
    r"""^(?:
        ~|null|Null|NULL
      |y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF
      |[-+]?(?:0|[1-9][0-9_]*)
      |0o?[0-7_]+|0x[0-9a-fA-F_]+|0b[01_]+
      |[-+]?(?:\.[0-9]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9]+)?
      |[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)
      |[-+]?[0-9]+(?::[0-5]?[0-9])+(?:\.[0-9_]*)?
      |\d{4}-\d{1,2}-\d{1,2}(?:[Tt ].*)?
    )$""",
    re.VERBOSE,
)
REFUSED_START = {
    "&": "anchors (&) are not supported",
    "*": "aliases (*) are not supported",
    "!": "tags (!) are not supported",
    "|": "block scalars (|) are not supported; use one quoted line with \\n",
    ">": "folded scalars (>) are not supported; use one quoted line",
    "[": "flow sequences ([) are not supported; quote the value",
    "{": "flow mappings ({) are not supported; quote a value that starts with {placeholder}",
    "@": "a value may not start with @; quote it",
    "`": "a value may not start with `; quote it",
    "%": "a value may not start with %; quote it",
}
ESCAPES = {
    "\\": "\\",
    '"': '"',
    "/": "/",
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    " ": " ",
}


class LocaleSyntaxError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


@dataclass(frozen=True)
class Scalar:
    text: str | None  # None: no value at all
    quoted: bool
    line: int

    @property
    def is_string(self) -> bool:
        if self.text is None:
            return False
        return self.quoted or not NON_STRING.match(self.text)


type Mapping = dict[str, Scalar | Mapping]


# --------------------------------------------------------------------------- YAML subset reader


def _quoted(text: str, line: int) -> tuple[str, str]:
    """Parse a quoted scalar at the start of text; return (value, rest after the closing quote)."""
    quote = text[0]
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                if text[i + 1 : i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), text[i + 1 :]
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            return "".join(out), text[i + 1 :]
        if ch == "\\":
            esc = text[i + 1 : i + 2]
            if esc in ESCAPES:
                out.append(ESCAPES[esc])
                i += 2
                continue
            width = {"x": 2, "u": 4, "U": 8}.get(esc)
            digits = text[i + 2 : i + 2 + width] if width else ""
            if width and re.fullmatch(r"[0-9a-fA-F]+", digits) and len(digits) == width:
                out.append(chr(int(digits, 16)))
                i += 2 + width
                continue
            raise LocaleSyntaxError(line, f"unknown escape \\{esc} in a double-quoted value")
        out.append(ch)
        i += 1
    raise LocaleSyntaxError(line, "unterminated quote (quoted values must fit on one line)")


def _after_quote(rest: str, line: int) -> None:
    rest = rest.rstrip()
    if rest and not (rest[0] in " \t" and rest.lstrip().startswith("#")):
        raise LocaleSyntaxError(line, f"unexpected text after the closing quote: {rest.strip()!r}")


def _value(text: str, line: int) -> Scalar:
    head = text[0]
    if head in "'\"":
        value, rest = _quoted(text, line)
        _after_quote(rest, line)
        return Scalar(value, True, line)
    if head in REFUSED_START:
        raise LocaleSyntaxError(line, REFUSED_START[head])
    if text[:2] in ("- ", "? ", ": ") or text in ("-", "?"):
        raise LocaleSyntaxError(line, f"a value may not start with {text[:2]!r}; quote it")
    cut = text.find(" #")
    plain = (text if cut < 0 else text[:cut]).rstrip()
    if ": " in plain or plain.endswith(":"):
        raise LocaleSyntaxError(line, "a plain value may not contain ': '; quote the value")
    return Scalar(plain, False, line)


def parse(text: str) -> Mapping:
    """Parse the YAML subset into nested dicts of Scalar leaves."""
    text = text.removeprefix("﻿")
    root: Mapping = {}
    stack: list[tuple[int, Mapping]] = [(0, root)]
    pending: tuple[int, Mapping, str, int] | None = None  # indent, parent, key, line
    for number, raw in enumerate(text.splitlines(), start=1):
        body = raw.lstrip(" ")
        indent = len(raw) - len(body)
        if body.startswith("\t") or (not body.strip() and "\t" in raw):
            raise LocaleSyntaxError(number, "tabs are not allowed; indent with spaces")
        if not body.strip() or body.startswith("#"):
            continue
        if indent == 0 and (body.startswith(("---", "...")) or body[0] == "%"):
            raise LocaleSyntaxError(number, "document markers and directives are not supported")
        if body.startswith("- ") or body.rstrip() == "-":
            raise LocaleSyntaxError(number, "sequences (- item) are not supported")
        if body.startswith("? "):
            raise LocaleSyntaxError(number, "complex keys (? key) are not supported")

        if pending is not None:
            p_indent, p_parent, p_key, p_line = pending
            if indent > p_indent:
                child: Mapping = {}
                p_parent[p_key] = child
                stack.append((indent, child))
            else:
                p_parent[p_key] = Scalar(None, False, p_line)
            pending = None
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        if indent != stack[-1][0]:
            raise LocaleSyntaxError(number, "indentation does not match any open mapping")
        mapping = stack[-1][1]

        if body[0] in "'\"":
            key, rest = _quoted(body, number)
        else:
            m = KEY.match(body)
            if not m:
                raise LocaleSyntaxError(number, f"expected 'key: value', got {body.strip()!r}")
            key, rest = m.group(0), body[m.end() :]
        if not rest.startswith(":") or rest[1:2] not in ("", " "):
            raise LocaleSyntaxError(number, f"expected ':' and a space after the key {key!r}")
        if key in mapping:
            raise LocaleSyntaxError(number, f"duplicate key {key!r}")
        value = rest[1:].strip()
        if not value or value.startswith("#"):
            mapping[key] = Scalar(None, False, number)  # replaced if a nested block follows
            pending = (indent, mapping, key, number)
            continue
        mapping[key] = _value(value, number)
    return root


def flatten(tree: Mapping, prefix: str = "") -> dict[str, Scalar]:
    out: dict[str, Scalar] = {}
    for key, value in tree.items():
        full = f"{prefix}{key}"
        items = flatten(value, f"{full}.") if isinstance(value, dict) else {full: value}
        for k, v in items.items():
            if k in out:
                raise LocaleSyntaxError(v.line, f"key {k!r} is defined twice after flattening")
            out[k] = v
    return out


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text.replace("{{", "").replace("}}", "")))


# --------------------------------------------------------------------------- check


def load_catalogue(path: Path, result: Result) -> dict[str, Scalar] | None:
    name = f"locales/{path.name}"
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        result.fail(f"{name}: not valid UTF-8")
        return None
    except OSError as exc:
        result.fail(f"{name}: cannot read: {exc}")
        return None
    try:
        flat = flatten(parse(text))
    except LocaleSyntaxError as exc:
        result.fail(f"{name}:{exc.line}: {exc.message}")
        return None
    for key, scalar in flat.items():
        if not scalar.is_string:
            shown = "nothing" if scalar.text is None else repr(scalar.text)
            result.fail(f"{name}:{scalar.line}: {key}: value {shown} is not a string -> quote it")
    return flat


def load_key_list(path: Path, result: Result) -> dict[str, set[str] | None] | None:
    """Keys of a surface, each with its English placeholders (None when only names are known)."""
    name = f"locales/{path.name}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result.fail(f"{name}: cannot read: {exc}")
        return None
    if isinstance(data, list) and all(isinstance(k, str) for k in data):
        if len(set(data)) != len(data):
            result.fail(f"{name}: lists a key more than once")
        return {k: None for k in data}
    if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
        return {k: placeholders(v) for k, v in data.items()}
    result.fail(f"{name}: must be a JSON list of keys or an object of key -> English string")
    return None


def compare(
    name: str, expected: dict[str, set[str] | None], actual: dict[str, Scalar], result: Result
) -> None:
    for key in sorted(set(expected) - set(actual)):
        result.fail(f"{name}: missing key {key} -> add it (English is the source)")
    for key in sorted(set(actual) - set(expected)):
        result.fail(f"{name}:{actual[key].line}: extra key {key} -> remove it or add it to English")
    for key in sorted(set(expected) & set(actual)):
        want, scalar = expected[key], actual[key]
        if want is None or scalar.text is None:
            continue
        got = placeholders(scalar.text)
        if got != want:
            result.fail(
                f"{name}:{scalar.line}: {key}: placeholders {{{', '.join(sorted(got))}}}"
                f" differ from English {{{', '.join(sorted(want))}}}"
            )


def run(ctx: Context) -> Result:
    result = Result(NAME)
    folder = ctx.root / "locales"
    if not folder.is_dir():
        result.notes.append("no locales/ directory: nothing to check")
        return result
    mains: dict[str, Path] = {}
    packs: dict[str, dict[str, Path]] = {}
    key_lists: dict[str, Path] = {}
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        parts = path.name.split(".")
        lang_ok = bool(LANG.match(parts[0]))
        surface_ok = len(parts) == 3 and bool(SURFACE.match(parts[1]))
        if path.suffix == ".yml":
            result.fail(f"locales/{path.name}: use the .yaml extension")
        elif surface_ok and parts[0] == "_keys" and parts[2] == "json":
            key_lists[parts[1]] = path
        elif len(parts) == 2 and parts[1] == "yaml" and lang_ok:
            mains[parts[0]] = path
        elif surface_ok and parts[2] == "yaml" and lang_ok:
            packs.setdefault(parts[1], {})[parts[0]] = path
        else:
            result.notes.append(f"locales/{path.name}: not a catalogue or key list; ignored")

    if SOURCE_LANG not in mains:
        result.fail(f"locales/{SOURCE_LANG}.yaml: missing (the English source catalogue)")
    else:
        english = load_catalogue(mains[SOURCE_LANG], result)
        if english is not None:
            expected: dict[str, set[str] | None] = {
                k: placeholders(v.text or "") for k, v in english.items()
            }
            for lang, path in sorted(mains.items()):
                if lang == SOURCE_LANG:
                    continue
                flat = load_catalogue(path, result)
                if flat is not None:
                    compare(f"locales/{path.name}", expected, flat, result)
            result.data["keys"] = len(english)

    for surface, by_lang in sorted(packs.items()):
        if surface not in key_lists:
            for path in by_lang.values():
                result.fail(f"locales/{path.name}: no locales/_keys.{surface}.json to compare with")
            continue
        wanted = load_key_list(key_lists[surface], result)
        for _lang, path in sorted(by_lang.items()):
            flat = load_catalogue(path, result)
            if wanted is not None and flat is not None:
                compare(f"locales/{path.name}", wanted, flat, result)

    result.data["languages"] = sorted(mains)
    result.data["surfaces"] = sorted(set(packs) | set(key_lists))
    result.notes.append(
        f"{len(mains)} catalogue(s), {sum(len(v) for v in packs.values())} surface pack(s),"
        f" {len(key_lists)} key list(s)"
    )
    return result


if __name__ == "__main__":
    from tooling.checks.__main__ import main

    raise SystemExit(main([NAME, *sys.argv[1:]]))
